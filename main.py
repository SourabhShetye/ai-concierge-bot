"""
Restaurant AI Concierge — main.py  v5
======================================
Three-mode architecture:

  GENERAL (default)
    AI answers any question (menu, WiFi, parking, policies, hours).
    Uses restaurant_policies text injected into the system prompt.
    Booking and ordering attempts are BLOCKED with a redirect message.

  ORDER  (🍽️ Order Food button)
    Immediately asks for table number.
    Only in this mode can orders be placed.
    Every order receives a visible Order #ID.
    Modification/cancellation requires quoting the Order ID.

  BOOKING  (📅 Book a Table button)
    Only date/time/party-size inputs accepted.
    Food ordering attempts are blocked with a redirect.
"""

import os
import re
import json
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, Tuple
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from supabase import create_client, Client
from dotenv import load_dotenv
from groq import AsyncGroq

from order_service import (
    process_order,
    fetch_order_for_user,
    stage_cancellation,
    stage_modification,
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY")

supabase:     Client             = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client:  AsyncGroq          = AsyncGroq(api_key=GROQ_API_KEY)
app:          FastAPI             = FastAPI(title="Restaurant Concierge API v5")
telegram_app: Optional[Application] = None

DUBAI_TZ = ZoneInfo("Asia/Dubai")


# ============================================================================
# MODE & STATE ENUMS
# ============================================================================

class Mode(str, Enum):
    GENERAL = "general"   # Default — AI Q&A only, no orders/bookings
    ORDER   = "order"     # 🍽️ Order Food — table → order flow
    BOOKING = "booking"   # 📅 Book a Table — date/party flow


class UserState(str, Enum):
    IDLE              = "idle"
    AWAITING_TABLE    = "awaiting_table"    # ORDER mode: waiting for table number
    HAS_TABLE         = "has_table"         # ORDER mode: table set, can order
    AWAITING_ORDER_ID = "awaiting_order_id" # ORDER mode: waiting for order ID for mod/cancel
    AWAITING_GUESTS   = "awaiting_guests"   # BOOKING mode
    AWAITING_TIME     = "awaiting_time"     # BOOKING mode
    AWAITING_FEEDBACK = "awaiting_feedback" # Post-payment rating


# ============================================================================
# STATE / MODE HELPERS
# ============================================================================

def get_user_state(uid: int, ctx: ContextTypes.DEFAULT_TYPE) -> UserState:
    return ctx.user_data.get(f"state_{uid}", UserState.IDLE)

def set_user_state(uid: int, state: UserState, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data[f"state_{uid}"] = state
    print(f"[STATE] {uid} → {state.value}")

def clear_user_state(uid: int, ctx: ContextTypes.DEFAULT_TYPE):
    set_user_state(uid, UserState.IDLE, ctx)

def get_user_context(uid: int, ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    key = f"ctx_{uid}"
    if key not in ctx.user_data:
        ctx.user_data[key] = {}
    return ctx.user_data[key]

def get_mode(uid: int, ctx: ContextTypes.DEFAULT_TYPE) -> Mode:
    return Mode(get_user_context(uid, ctx).get("mode", Mode.GENERAL))

def set_mode(uid: int, mode: Mode, ctx: ContextTypes.DEFAULT_TYPE):
    get_user_context(uid, ctx)["mode"] = mode.value
    print(f"[MODE]  {uid} → {mode.value}")

def reset_to_general(uid: int, ctx: ContextTypes.DEFAULT_TYPE):
    """Hard reset: GENERAL mode, IDLE state, clear in-flight data."""
    set_mode(uid, Mode.GENERAL, ctx)
    set_user_state(uid, UserState.IDLE, ctx)
    uc = get_user_context(uid, ctx)
    uc.pop("pending_action", None)
    uc.pop("pending_mod_text", None)


# ============================================================================
# UI HELPERS
# ============================================================================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍽️ Order Food (Dine-in)", callback_data="mode_order")],
        [InlineKeyboardButton("📅 Book a Table",          callback_data="mode_booking")],
    ])

def back_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="main_menu")]
    ])

async def send_main_menu(message, restaurant_name: str, first_name: str):
    await message.reply_text(
        f"👋 Welcome to *{restaurant_name}*, {first_name}!\n\n"
        f"You're in *General Mode* — ask me anything about our menu, "
        f"WiFi, parking, or policies.\n\n"
        f"Ready to order or book? Choose an option:",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )


# ============================================================================
# POLICY FETCHER  (injects restaurant context into AI prompts)
# ============================================================================

def fetch_policy_text(restaurant_id: str) -> str:
    """
    Fetch the restaurant's policy/info text from restaurant_policies table.
    Returns an empty string if not found — the AI still works, just has less context.
    """
    try:
        res = supabase.table("restaurant_policies") \
            .select("policy_text") \
            .eq("restaurant_id", str(restaurant_id)) \
            .limit(1) \
            .execute()
        if res.data:
            return res.data[0].get("policy_text", "")
    except Exception as e:
        print(f"[POLICY] fetch error: {e}")
    return ""


# ============================================================================
# TIMEZONE & BOOKING HELPERS
# ============================================================================

def get_dubai_now() -> datetime:
    return datetime.now(DUBAI_TZ)

async def parse_booking_time(user_input: str) -> Optional[datetime]:
    prompt = (
        f'Current Dubai Time: {get_dubai_now().strftime("%Y-%m-%d %H:%M")}\n\n'
        f'Parse this booking request: "{user_input}"\n\n'
        f'Return ONLY JSON: {{"datetime": "YYYY-MM-DD HH:MM", "valid": true}}\n\n'
        f'Rules:\n'
        f'- "tomorrow 8pm" = tomorrow 20:00\n'
        f'- Past times → {{"valid": false}}\n'
        f'- Ambiguous  → {{"valid": false}}'
    )
    try:
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0, max_tokens=60,
        )
        raw = completion.choices[0].message.content
        s, e = raw.find("{"), raw.rfind("}") + 1
        if s == -1 or e == 0:
            return None
        data = json.loads(raw[s:e])
        if not data.get("valid"):
            return None
        dt = datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M").replace(tzinfo=DUBAI_TZ)
        return dt if dt > get_dubai_now() else None
    except Exception as ex:
        print(f"[TIME PARSE] {ex}")
        return None

def check_availability(restaurant_id: str, booking_time: datetime) -> bool:
    try:
        res = supabase.table("bookings") \
            .select("id", count="exact") \
            .eq("restaurant_id", restaurant_id) \
            .eq("booking_time", booking_time.strftime("%Y-%m-%d %H:%M:%S%z")) \
            .neq("status", "cancelled") \
            .execute()
        return (res.count or 0) < 10
    except Exception as ex:
        print(f"[AVAIL] {ex}")
        return False

def check_duplicate_booking(uid: int, restaurant_id: str, booking_time: datetime) -> bool:
    try:
        res = supabase.table("bookings") \
            .select("id") \
            .eq("user_id", str(uid)) \
            .eq("restaurant_id", restaurant_id) \
            .eq("booking_time", booking_time.strftime("%Y-%m-%d %H:%M:%S%z")) \
            .neq("status", "cancelled") \
            .execute()
        return bool(res.data)
    except Exception as ex:
        print(f"[DUP] {ex}")
        return False


# ============================================================================
# /start
# ============================================================================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id

    restaurant_id   = None
    restaurant_name = "Our Restaurant"

    if context.args:
        arg = context.args[0]
        rid = arg.split("=")[1] if arg.startswith("rest_id=") else arg
        try:
            chk = supabase.table("restaurants").select("id,name").eq("id", rid).execute()
            if chk.data:
                restaurant_id   = chk.data[0]["id"]
                restaurant_name = chk.data[0].get("name", restaurant_name)
        except Exception as ex:
            print(f"[START] lookup: {ex}")

    if not restaurant_id:
        try:
            dflt = supabase.table("restaurants").select("id,name").limit(1).execute()
            if dflt.data:
                restaurant_id   = dflt.data[0]["id"]
                restaurant_name = dflt.data[0].get("name", restaurant_name)
            else:
                await update.message.reply_text("❌ No restaurants configured.")
                return
        except Exception as ex:
            print(f"[START] default: {ex}")
            await update.message.reply_text("❌ Cannot connect.")
            return

    uc = get_user_context(user.id, context)
    uc["restaurant_id"]   = restaurant_id
    uc["restaurant_name"] = restaurant_name
    uc["chat_id"]         = chat_id

    # Restore persisted table number
    try:
        sess = supabase.table("user_sessions") \
            .select("table_number").eq("user_id", str(user.id)).execute()
        if sess.data and sess.data[0].get("table_number"):
            uc["table_number"] = str(sess.data[0]["table_number"])
    except Exception as ex:
        print(f"[START] session: {ex}")

    try:
        supabase.table("users").upsert({
            "id": str(user.id), "username": user.username or "guest",
            "full_name": user.full_name or "Guest", "chat_id": str(chat_id),
        }).execute()
    except Exception as ex:
        print(f"[UPSERT] {ex}")

    reset_to_general(user.id, context)
    await send_main_menu(update.message, restaurant_name, user.first_name)


# ============================================================================
# BUTTON HANDLER
# ============================================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """CRITICAL: update.message is None for CallbackQuery — always use query.message."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data
    uc   = get_user_context(user.id, context)

    if data == "main_menu":
        reset_to_general(user.id, context)
        await send_main_menu(
            query.message,
            uc.get("restaurant_name", "Our Restaurant"),
            user.first_name,
        )
        return

    if data == "mode_order":
        set_mode(user.id, Mode.ORDER, context)
        set_user_state(user.id, UserState.AWAITING_TABLE, context)
        await query.message.reply_text(
            "🍽️ *Order Mode*\n\n"
            "🪑 What is your table number?",
            reply_markup=back_button(),
            parse_mode="Markdown",
        )
        return

    if data == "mode_booking":
        set_mode(user.id, Mode.BOOKING, context)
        set_user_state(user.id, UserState.AWAITING_GUESTS, context)
        await query.message.reply_text(
            "📅 *Booking Mode*\n\n"
            "How many guests will be dining? _(e.g. '4' or 'party of 6')_",
            reply_markup=back_button(),
            parse_mode="Markdown",
        )
        return

    # Legacy callbacks from old sessions
    if data == "menu":
        await _send_menu(query.message, uc)


# ============================================================================
# MENU HELPER
# ============================================================================

async def _send_menu(message, uc: dict) -> None:
    """Always fetches live DB rows — never an LLM summary."""
    restaurant_id   = uc.get("restaurant_id")
    restaurant_name = uc.get("restaurant_name", "Our Restaurant")
    if not restaurant_id:
        await message.reply_text("❌ Please /start first.")
        return
    try:
        rows = supabase.table("menu_items") \
            .select("content").eq("restaurant_id", restaurant_id).execute()
        if not rows.data:
            await message.reply_text("📋 Menu unavailable. Please ask staff.", reply_markup=back_button())
            return
        lines = [f"🍽️ *{restaurant_name} — Menu*\n"]
        cur_cat = None
        for row in rows.data:
            for line in row["content"].split("\n"):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("category:"):
                    cat = line.replace("category:", "").strip()
                    if cat != cur_cat:
                        lines.append(f"\n*{cat.upper()}*")
                        cur_cat = cat
                elif line.startswith("item:"):
                    lines.append(f"  • {line.replace('item:', '').strip()}")
                elif line.startswith("price:"):
                    lines[-1] += f"  —  {line.replace('price:', '').strip()}"
                elif line.startswith("description:"):
                    lines.append(f"    _{line.replace('description:', '').strip()}_")
        lines.append("\n_Tell me what you'd like and I'll place the order!_")
        menu_text = "\n".join(lines)
        kb = back_button()
        if len(menu_text) <= 4096:
            await message.reply_text(menu_text, parse_mode="Markdown", reply_markup=kb)
        else:
            await message.reply_text(menu_text[:4000], parse_mode="Markdown")
            await message.reply_text(menu_text[4000:], parse_mode="Markdown", reply_markup=kb)
    except Exception as ex:
        print(f"[MENU] {ex}")
        await message.reply_text("❌ Error loading menu.")

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_menu(update.message, get_user_context(update.effective_user.id, context))


# ============================================================================
# /help
# ============================================================================

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Restaurant AI Concierge*\n\n"
        "*Modes:*\n"
        "• *General* (default) — Ask about menu, WiFi, parking, hours\n"
        "• *Order Food* — Place dine-in orders by table number\n"
        "• *Book a Table* — Reserve a table with date and party size\n\n"
        "*Commands:*\n"
        "/start — Main menu\n"
        "/menu  — View full menu\n"
        "/cancel — Cancel an order by ID\n"
        "/help  — This message\n\n"
        "To modify: say _'remove fries from order #42'_ or use /cancel",
        parse_mode="Markdown",
        reply_markup=back_button(),
    )


# ============================================================================
# /cancel
# ============================================================================

async def cancel_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uc   = get_user_context(user.id, context)
    rid  = uc.get("restaurant_id")
    try:
        recent = supabase.table("orders") \
            .select("id, items, price") \
            .eq("user_id", str(user.id)) \
            .eq("restaurant_id", rid) \
            .eq("status", "pending") \
            .order("created_at", desc=True) \
            .limit(5).execute()
        if not recent.data:
            await update.message.reply_text("❌ No active orders to cancel.", reply_markup=back_button())
            return
        order_list = "\n".join(
            f"  *#{o['id']}* — {o['items']}  (${float(o['price']):.2f})"
            for o in recent.data
        )
        uc["pending_action"] = "cancel"
        set_user_state(user.id, UserState.AWAITING_ORDER_ID, context)
        await update.message.reply_text(
            f"📋 *Your active orders:*\n{order_list}\n\n"
            f"Please type the *Order Number* you wish to cancel:",
            reply_markup=back_button(), parse_mode="Markdown",
        )
    except Exception as ex:
        print(f"[CANCEL CMD] {ex}")
        await update.message.reply_text("❌ Error fetching orders.")


# ============================================================================
# BOOKING FLOW
# ============================================================================

async def handle_booking_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, state: UserState):
    user = update.effective_user
    text = update.message.text.strip()
    uc   = get_user_context(user.id, context)

    if state == UserState.AWAITING_GUESTS:
        nums = re.findall(r'\d+', text)
        if not nums:
            await update.message.reply_text("❌ Please enter the number of guests (e.g. '4').", reply_markup=back_button())
            return
        party = int(nums[0])
        if not (1 <= party <= 20):
            await update.message.reply_text("❌ Party size must be between 1 and 20.", reply_markup=back_button())
            return
        uc["party_size"] = party
        set_user_state(user.id, UserState.AWAITING_TIME, context)
        await update.message.reply_text(
            f"✅ Table for *{party}* guests.\n\n"
            f"⏰ When? _(e.g. 'tomorrow 8pm', 'Friday 7:30pm', 'Jan 25 at 6pm')_",
            reply_markup=back_button(), parse_mode="Markdown",
        )
        return

    if state == UserState.AWAITING_TIME:
        booking_time = await parse_booking_time(text)
        if not booking_time:
            await update.message.reply_text(
                "❌ Invalid or past time.\n\nTry: _'tomorrow 8pm'_, _'Friday 7:30pm'_",
                reply_markup=back_button(), parse_mode="Markdown",
            )
            return
        rid   = uc.get("restaurant_id")
        party = uc.get("party_size")
        if check_duplicate_booking(user.id, rid, booking_time):
            await update.message.reply_text("❌ You already have a booking at that time.", reply_markup=back_button())
            clear_user_state(user.id, context)
            return
        if not check_availability(rid, booking_time):
            await update.message.reply_text("❌ Fully booked at that time. Please try another slot.", reply_markup=back_button())
            return
        try:
            supabase.table("bookings").insert({
                "restaurant_id": rid, "user_id": str(user.id),
                "customer_name": user.full_name or "Guest",
                "party_size": party,
                "booking_time": booking_time.strftime("%Y-%m-%d %H:%M:%S%z"),
                "status": "confirmed",
            }).execute()
            await update.message.reply_text(
                f"✅ *Booking Confirmed!*\n\n"
                f"👥 Guests: {party}\n"
                f"📅 {booking_time.strftime('%B %d, %Y at %I:%M %p')}\n\n"
                f"We look forward to serving you!",
                reply_markup=main_menu_keyboard(), parse_mode="Markdown",
            )
            reset_to_general(user.id, context)
        except Exception as ex:
            print(f"[BOOKING] {ex}")
            await update.message.reply_text("❌ System error. Please try again.", reply_markup=back_button())
            clear_user_state(user.id, context)


# ============================================================================
# TABLE ASSIGNMENT  (ORDER mode)
# ============================================================================

async def handle_table_assignment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    uc   = get_user_context(user.id, context)
    nums = re.findall(r'\d+', text)
    if not nums:
        await update.message.reply_text("❌ Please type just the table number (e.g. '7').", reply_markup=back_button())
        return
    table_number = nums[0]
    uc["table_number"] = table_number
    try:
        supabase.table("user_sessions").upsert({"user_id": str(user.id), "table_number": table_number}).execute()
    except Exception as ex:
        print(f"[TABLE] DB: {ex}")
    set_user_state(user.id, UserState.HAS_TABLE, context)
    await update.message.reply_text(
        f"✅ *Table {table_number} set!*\n\n"
        f"What would you like to order?\n"
        f"_Example: '2 Binary Bites and a Java Jolt'_",
        reply_markup=back_button(), parse_mode="Markdown",
    )


# ============================================================================
# ORDER ID HANDLER
# ============================================================================

async def handle_order_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    uc   = get_user_context(user.id, context)
    nums = re.findall(r'\d+', text)
    if not nums:
        await update.message.reply_text("❌ Please type a valid order number.", reply_markup=back_button())
        return
    order_id   = int(nums[0])
    rid        = uc.get("restaurant_id")
    action     = uc.get("pending_action", "cancel")
    mod_intent = uc.get("pending_mod_text", "")
    order = fetch_order_for_user(order_id, str(user.id), rid)
    if not order:
        await update.message.reply_text(
            f"❌ Order *#{order_id}* not found or doesn't belong to you.",
            reply_markup=back_button(), parse_mode="Markdown",
        )
        return
    clear_user_state(user.id, context)
    uc.pop("pending_action", None)
    uc.pop("pending_mod_text", None)
    reply = stage_cancellation(order) if action == "cancel" else await stage_modification(order, mod_intent)
    await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=back_button())


# ============================================================================
# FEEDBACK
# ============================================================================

async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    uc   = get_user_context(user.id, context)
    ratings = re.findall(r'\b[1-5]\b', text)
    if not ratings:
        await update.message.reply_text("Please provide ratings (1-5) for each dish and overall experience.")
        return
    try:
        supabase.table("feedback").insert({
            "restaurant_id": uc.get("restaurant_id"), "user_id": str(user.id),
            "ratings": text, "created_at": get_dubai_now().isoformat(),
        }).execute()
        await update.message.reply_text("⭐ Thank you for your feedback! See you again! 😊", reply_markup=main_menu_keyboard())
        reset_to_general(user.id, context)
    except Exception as ex:
        print(f"[FEEDBACK] {ex}")
        await update.message.reply_text("✅ Feedback received. Thank you!")
        reset_to_general(user.id, context)


# ============================================================================
# BILLING
# ============================================================================

async def calculate_bill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user      = update.effective_user
    uc        = get_user_context(user.id, context)
    table_num = uc.get("table_number")
    rid       = uc.get("restaurant_id")
    if not table_num:
        try:
            sess = supabase.table("user_sessions").select("table_number").eq("user_id", str(user.id)).execute()
            if sess.data and sess.data[0].get("table_number"):
                table_num = str(sess.data[0]["table_number"])
                uc["table_number"] = table_num
        except Exception as ex:
            print(f"[BILL] session: {ex}")
    if not table_num:
        await update.message.reply_text("🪑 What is your table number?", reply_markup=back_button())
        return
    try:
        res = supabase.table("orders") \
            .select("id, items, price") \
            .eq("user_id", str(user.id)).eq("restaurant_id", rid) \
            .eq("table_number", str(table_num)) \
            .neq("status", "paid").neq("status", "cancelled").execute()
        if not res.data:
            await update.message.reply_text(f"🧾 *Table {table_num}* — No active orders.", parse_mode="Markdown", reply_markup=back_button())
            return
        total = round(sum(float(r["price"]) for r in res.data), 2)
        lines = "\n".join(f"  • *#{r['id']}* {r['items']}  —  ${float(r['price']):.2f}" for r in res.data)
        await update.message.reply_text(
            f"🧾 *Bill — Table {table_num}*\n\n{lines}\n\n💰 *Total: ${total:.2f}*\n\n_(Ask a waiter to pay)_",
            parse_mode="Markdown", reply_markup=back_button(),
        )
    except Exception as ex:
        print(f"[BILL] {ex}")
        await update.message.reply_text("❌ Error fetching bill.", reply_markup=back_button())


# ============================================================================
# GENERAL MODE AI CHAT  (answers everything, no ordering/booking)
# ============================================================================

_ORDER_KWS = re.compile(
    r"\b(i('ll| will) have|i want|can i get|give me|bring me|i('d| would) like"
    r"|order food|place an? order|burger|pizza|pasta|fries|salad|coffee|tea|juice)\b",
    re.IGNORECASE,
)
_BOOK_KWS = re.compile(
    r"\b(book|reserve|reservation|table for|party of"
    r"|tomorrow|tonight|friday|saturday|sunday|monday|tuesday|wednesday|thursday"
    r"|next week|this weekend|\d{1,2}(am|pm))\b",
    re.IGNORECASE,
)

GENERAL_REDIRECT = (
    "I'm happy to answer general questions here! 😊\n\n"
    "To *order food* or *make a booking*, please use the buttons below:"
)

async def handle_general_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    GENERAL MODE handler.
    - Blocks ordering and booking attempts with a redirect.
    - Answers all other questions using AI + policy context.
    """
    user = update.effective_user
    text = update.message.text.strip()
    uc   = get_user_context(user.id, context)
    rid  = uc.get("restaurant_id")

    if not rid:
        await update.message.reply_text("👋 Please use /start to begin.", reply_markup=main_menu_keyboard())
        return

    text_lower = text.lower()

    # Block ordering attempts
    if _ORDER_KWS.search(text):
        await update.message.reply_text(GENERAL_REDIRECT, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
        return

    # Block booking attempts  
    if _BOOK_KWS.search(text):
        await update.message.reply_text(GENERAL_REDIRECT, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
        return

    # Fetch context: menu + policy
    try:
        menu_rows = supabase.table("menu_items") \
            .select("content").eq("restaurant_id", rid).limit(30).execute()
        menu_ctx = "\n".join(r["content"] for r in menu_rows.data) if menu_rows.data else "No menu available"
    except Exception:
        menu_ctx = "No menu available"

    policy_ctx = fetch_policy_text(rid)

    system_prompt = (
        "You are a helpful restaurant concierge assistant.\n\n"
        f"MENU:\n{menu_ctx}\n\n"
        + (f"RESTAURANT INFO (WiFi, parking, policies, hours):\n{policy_ctx}\n\n" if policy_ctx else "")
        + "INSTRUCTIONS:\n"
        "- Answer questions about the menu, ingredients, WiFi, parking, hours, policies warmly and accurately.\n"
        "- If asked about specific info (WiFi password, parking), check the Restaurant Info section.\n"
        "- Keep responses to 2-3 sentences — concise and friendly.\n"
        "- NEVER discuss bookings or take orders — those happen in dedicated modes.\n"
        "- If asked to order or book, say: 'Please use the menu buttons below to order or book.'"
    )

    try:
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": text},
            ],
            temperature=0.7, max_tokens=250,
        )
        await update.message.reply_text(
            completion.choices[0].message.content,
            reply_markup=main_menu_keyboard(),
        )
    except Exception as ex:
        print(f"[GENERAL CHAT] {ex}")
        await update.message.reply_text("I'm here to help! Ask me about our menu, WiFi, or parking.", reply_markup=main_menu_keyboard())


# ============================================================================
# ORDER MODE AI CHAT  (answers questions, processes orders, handles mods)
# ============================================================================

_MOD_KEYWORDS = ["remove", "take off", "drop the", "cancel", "without",
                 "don't want", "no more", "delete", "modify order", "change order"]

async def handle_order_mode_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    General Q&A within ORDER mode — answers menu questions,
    processes orders, handles modification triggers.
    """
    user = update.effective_user
    text = update.message.text.strip()
    uc   = get_user_context(user.id, context)
    rid  = uc.get("restaurant_id")

    if not rid:
        await update.message.reply_text("👋 Please use /start.", reply_markup=main_menu_keyboard())
        return

    text_lower = text.lower()

    # Modification / cancel trigger
    mod_trigger      = any(kw in text_lower for kw in _MOD_KEYWORDS)
    order_id_in_text = re.search(r'#?(\d{3,})', text)

    if mod_trigger:
        if order_id_in_text:
            oid   = int(order_id_in_text.group(1))
            order = fetch_order_for_user(oid, str(user.id), rid)
            if order:
                is_cancel = any(p in text_lower for p in ["cancel", "nevermind", "never mind"])
                reply = stage_cancellation(order) if is_cancel else await stage_modification(order, text)
                await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=back_button())
                return
        # No ID — ask for it
        try:
            recent = supabase.table("orders") \
                .select("id, items, price") \
                .eq("user_id", str(user.id)).eq("restaurant_id", rid) \
                .eq("status", "pending").order("created_at", desc=True).limit(5).execute()
            if not recent.data:
                await update.message.reply_text("❌ No active orders to modify.", reply_markup=back_button())
                return
            order_list = "\n".join(f"  *#{o['id']}* — {o['items']}  (${float(o['price']):.2f})" for o in recent.data)
            is_cancel_intent = any(p in text_lower for p in ["cancel", "nevermind", "never mind"])
            action = "cancel" if is_cancel_intent else "modify"
            uc["pending_action"]   = action
            uc["pending_mod_text"] = text
            set_user_state(user.id, UserState.AWAITING_ORDER_ID, context)
            await update.message.reply_text(
                f"📋 *Your active orders:*\n{order_list}\n\n"
                f"Type the *Order Number* you wish to {action}:",
                reply_markup=back_button(), parse_mode="Markdown",
            )
            return
        except Exception as ex:
            print(f"[MOD TRIGGER] {ex}")
            await update.message.reply_text("❌ Error. Please try again.", reply_markup=back_button())
            return

    # Menu keyword
    menu_kws = ["menu", "what do you serve", "what's available", "what do you have", "food list"]
    if any(kw in text_lower for kw in menu_kws):
        await _send_menu(update.message, uc)
        return

    # Bill keyword
    bill_kws = ["bill", "check please", "the check", "my total", "how much", "pay", "invoice", "receipt"]
    if any(kw in text_lower for kw in bill_kws):
        await calculate_bill(update, context)
        return

    # Try to process as a food order
    if uc.get("table_number"):
        result = await process_order(text, user, rid, uc.get("table_number"), uc.get("chat_id"))
        if result:
            reply_text, _oid = result
            await update.message.reply_text(reply_text, parse_mode="Markdown", reply_markup=back_button())
            return

    # General Q&A within order mode (allowed — e.g. "is the soup vegan?")
    try:
        menu_rows = supabase.table("menu_items").select("content").eq("restaurant_id", rid).limit(30).execute()
        menu_ctx  = "\n".join(r["content"] for r in menu_rows.data) if menu_rows.data else "No menu available"
        policy_ctx = fetch_policy_text(rid)
        system = (
            "You are a restaurant concierge in Order Mode.\n\n"
            f"MENU:\n{menu_ctx}\n\n"
            + (f"RESTAURANT INFO:\n{policy_ctx}\n\n" if policy_ctx else "")
            + "Answer menu questions warmly. Keep responses to 2-3 sentences."
        )
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
            temperature=0.7, max_tokens=200,
        )
        await update.message.reply_text(completion.choices[0].message.content, reply_markup=back_button())
    except Exception as ex:
        print(f"[ORDER CHAT] {ex}")
        await update.message.reply_text("I'm here to help! Try /menu to see today's offerings.", reply_markup=back_button())


# ============================================================================
# MAIN MESSAGE HANDLER
# ============================================================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Mode-isolated router.

    Priority:
      1. AWAITING_ORDER_ID  — collect order number for mod/cancel
      2. AWAITING_FEEDBACK  — collect ratings
      3. BOOKING mode       — handle_booking_flow (order attempts → redirect)
      4. ORDER mode
         a. AWAITING_TABLE  — collect table number
         b. HAS_TABLE/IDLE  — handle_order_mode_chat
      5. GENERAL mode       — handle_general_chat (AI Q&A, orders/bookings → redirect)
    """
    user       = update.effective_user
    text       = update.message.text.strip()
    text_lower = text.lower()
    state      = get_user_state(user.id, context)
    mode       = get_mode(user.id, context)
    uc         = get_user_context(user.id, context)

    print(f"[MSG] {user.id} mode={mode.value} state={state.value} "
          f"table={uc.get('table_number','—')}: '{text[:60]}'")

    # 1. Order ID collection
    if state == UserState.AWAITING_ORDER_ID:
        await handle_order_id_input(update, context)
        return

    # 2. Feedback
    if state == UserState.AWAITING_FEEDBACK:
        await handle_feedback(update, context)
        return

    # 3. BOOKING mode
    if mode == Mode.BOOKING:
        if state in [UserState.AWAITING_GUESTS, UserState.AWAITING_TIME]:
            # Block order attempts inside booking flow
            if _ORDER_KWS.search(text):
                await update.message.reply_text(
                    "📅 You're in *Booking Mode*.\n\n"
                    "Please use the main menu buttons to Order Food.",
                    reply_markup=back_button(), parse_mode="Markdown",
                )
                return
            await handle_booking_flow(update, context, state)
            return
        # Unexpected state in booking — re-prompt
        set_user_state(user.id, UserState.AWAITING_GUESTS, context)
        await update.message.reply_text("📅 *Booking Mode* — How many guests?", reply_markup=back_button(), parse_mode="Markdown")
        return

    # 4. ORDER mode
    if mode == Mode.ORDER:
        if state == UserState.AWAITING_TABLE:
            await handle_table_assignment(update, context)
            return
        # Block booking attempts inside order mode
        if _BOOK_KWS.search(text_lower):
            await update.message.reply_text(
                "🍽️ You're in *Order Mode*.\n\n"
                "To make a reservation, please use the main menu buttons.",
                reply_markup=back_button(), parse_mode="Markdown",
            )
            return
        await handle_order_mode_chat(update, context)
        return

    # 5. GENERAL mode (default)
    await handle_general_chat(update, context)


# ============================================================================
# FASTAPI ENDPOINTS
# ============================================================================

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data   = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"status": "ok"}
    except Exception as ex:
        print(f"[WEBHOOK] {ex}")
        return {"status": "error", "message": str(ex)}

@app.get("/")
async def health_check():
    return {"status": "running", "service": "Restaurant Concierge v5", "timestamp": get_dubai_now().isoformat()}


# ============================================================================
# STARTUP / SHUTDOWN
# ============================================================================

@app.on_event("startup")
async def startup_event():
    global telegram_app
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start",  start_handler))
    telegram_app.add_handler(CommandHandler("help",   help_handler))
    telegram_app.add_handler(CommandHandler("menu",   menu_handler))
    telegram_app.add_handler(CommandHandler("cancel", cancel_command_handler))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    await telegram_app.initialize()
    await telegram_app.start()
    print("✅ Bot v5 started")

@app.on_event("shutdown")
async def shutdown_event():
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    print("🛑 Bot stopped")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)