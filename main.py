"""
Restaurant AI Concierge — main.py  v4
======================================
Changes from v3:
  MODE ISOLATION
    Three strict context modes entered via /start inline buttons:
      MODE_BOOKING  — only booking inputs accepted
      MODE_DINING   — ordering, menu, bill, general chat
      MODE_TABLE    — only digit input accepted
    Cross-mode attempts get a clear redirect message + "⬅️ Main Menu" button.

  ORDER IDs
    process_order() now returns (reply, order_id).
    Confirmation message always shows "Order #123".

  EXPLICIT MODIFICATION FLOW
    /cancel and natural-language mod requests enter AWAITING_ORDER_ID state.
    User must type the order number.  main.py validates ownership via
    order_service.fetch_order_for_user() before calling stage_modification()
    or stage_cancellation().

  BILLING
    calculate_bill() unchanged — Python sum, no LLM, session-aware.
"""

import os
import re
import json
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict, Any
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

supabase:     Client      = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client:  AsyncGroq   = AsyncGroq(api_key=GROQ_API_KEY)
app:          FastAPI      = FastAPI(title="Restaurant Concierge API")
telegram_app: Optional[Application] = None

DUBAI_TZ = ZoneInfo("Asia/Dubai")


# ============================================================================
# STATE & MODE ENUMS
# ============================================================================

class UserState(str, Enum):
    """
    Fine-grained state within the current mode.
    NEVER mix states from different modes — the mode layer enforces isolation.
    """
    IDLE                = "idle"
    # Booking mode states
    AWAITING_GUESTS     = "awaiting_guests"
    AWAITING_TIME       = "awaiting_time"
    # Dining mode states
    AWAITING_TABLE      = "awaiting_table"
    HAS_TABLE           = "has_table"
    AWAITING_ORDER_ID   = "awaiting_order_id"   # NEW: user must type order number
    # Cross-mode
    AWAITING_FEEDBACK   = "awaiting_feedback"


class Mode(str, Enum):
    """
    Top-level context mode.  Set once when the user presses a main-menu button.
    Cleared by /start or pressing ⬅️ Main Menu.
    """
    NONE    = "none"     # At /start, no mode selected
    BOOKING = "booking"  # 📅 Book a Table
    DINING  = "dining"   # 🍽️ View Menu & Order
    TABLE   = "table"    # 🪑 Set Table Number


# ============================================================================
# STATE HELPERS
# ============================================================================

def get_user_state(uid: int, ctx: ContextTypes.DEFAULT_TYPE) -> UserState:
    return ctx.user_data.get(f"state_{uid}", UserState.IDLE)

def set_user_state(uid: int, state: UserState, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data[f"state_{uid}"] = state
    print(f"[STATE] {uid} → {state.value}")

def clear_user_state(uid: int, ctx: ContextTypes.DEFAULT_TYPE):
    set_user_state(uid, UserState.IDLE, ctx)

def get_user_context(uid: int, ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    key = f"context_{uid}"
    if key not in ctx.user_data:
        ctx.user_data[key] = {}
    return ctx.user_data[key]

def get_mode(uid: int, ctx: ContextTypes.DEFAULT_TYPE) -> Mode:
    user_ctx = get_user_context(uid, ctx)
    return Mode(user_ctx.get("mode", Mode.NONE))

def set_mode(uid: int, mode: Mode, ctx: ContextTypes.DEFAULT_TYPE):
    user_ctx = get_user_context(uid, ctx)
    user_ctx["mode"] = mode.value
    print(f"[MODE]  {uid} → {mode.value}")

def clear_mode(uid: int, ctx: ContextTypes.DEFAULT_TYPE):
    set_mode(uid, Mode.NONE, ctx)


# ============================================================================
# SHARED UI HELPERS
# ============================================================================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    """The /start main menu — three clearly separated mode buttons."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍽️ View Menu & Order",  callback_data="mode_dining")],
        [InlineKeyboardButton("📅 Book a Table",        callback_data="mode_booking")],
        [InlineKeyboardButton("🪑 Set Table Number",    callback_data="mode_table")],
    ])

def back_button() -> InlineKeyboardMarkup:
    """Single ⬅️ Main Menu button attached to every mode-entry message."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="main_menu")]
    ])

async def send_main_menu(message, restaurant_name: str, first_name: str):
    """Send or re-send the /start welcome screen."""
    await message.reply_text(
        f"👋 Welcome to *{restaurant_name}*, {first_name}!\n\n"
        f"Please choose an option to get started:",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )


# ============================================================================
# TIMEZONE & VALIDATION HELPERS
# ============================================================================

def get_dubai_now() -> datetime:
    return datetime.now(DUBAI_TZ)

async def parse_booking_time(user_input: str) -> Optional[datetime]:
    """
    Parse natural-language booking time via Groq.
    Returns None if invalid or in the past.
    """
    prompt = f"""
Current Dubai Time: {get_dubai_now().strftime('%Y-%m-%d %H:%M')}

Parse this booking request: "{user_input}"

Return ONLY JSON (no extra text):
{{"datetime": "YYYY-MM-DD HH:MM", "valid": true}}

Rules:
- "tomorrow 8pm" = tomorrow at 20:00
- Past times → {{"valid": false}}
- Ambiguous  → {{"valid": false}}
"""
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
        dt = datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M")
        dt = dt.replace(tzinfo=DUBAI_TZ)
        return dt if dt > get_dubai_now() else None
    except Exception as e:
        print(f"[TIME PARSE ERROR] {e}")
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
    except Exception as e:
        print(f"[AVAIL ERROR] {e}")
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
    except Exception as e:
        print(f"[DUP CHECK ERROR] {e}")
        return False


# ============================================================================
# /start HANDLER
# ============================================================================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start — initialise session, clear all modes and states, show main menu.
    """
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
        except Exception as e:
            print(f"[START] lookup error: {e}")

    if not restaurant_id:
        try:
            dflt = supabase.table("restaurants").select("id,name").limit(1).execute()
            if dflt.data:
                restaurant_id   = dflt.data[0]["id"]
                restaurant_name = dflt.data[0].get("name", restaurant_name)
            else:
                await update.message.reply_text("❌ No restaurants configured.")
                return
        except Exception as e:
            print(f"[START] default error: {e}")
            await update.message.reply_text("❌ Cannot connect to restaurant.")
            return

    user_ctx = get_user_context(user.id, context)
    user_ctx["restaurant_id"]   = restaurant_id
    user_ctx["restaurant_name"] = restaurant_name
    user_ctx["chat_id"]         = chat_id

    # Restore persisted table_number
    try:
        sess = supabase.table("user_sessions") \
            .select("table_number").eq("user_id", str(user.id)).execute()
        if sess.data and sess.data[0].get("table_number"):
            user_ctx["table_number"] = str(sess.data[0]["table_number"])
    except Exception as e:
        print(f"[START] session restore: {e}")

    try:
        supabase.table("users").upsert({
            "id": str(user.id), "username": user.username or "guest",
            "full_name": user.full_name or "Guest", "chat_id": str(chat_id),
        }).execute()
    except Exception as e:
        print(f"[USER UPSERT] {e}")

    # Clear all state/mode — /start is always a hard reset
    clear_user_state(user.id, context)
    clear_mode(user.id, context)
    user_ctx.pop("pending_action", None)   # clear any in-flight mod intent

    await send_main_menu(update.message, restaurant_name, user.first_name)


# ============================================================================
# INLINE KEYBOARD BUTTON HANDLER
# ============================================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    All inline button presses.
    CRITICAL: update.message is None for CallbackQuery — always use query.message.
    """
    query = update.callback_query
    await query.answer()
    user     = update.effective_user
    data     = query.data
    user_ctx = get_user_context(user.id, context)

    # ── Main Menu (mode exit) ────────────────────────────────────────────────
    if data == "main_menu":
        clear_user_state(user.id, context)
        clear_mode(user.id, context)
        user_ctx.pop("pending_action", None)
        await send_main_menu(
            query.message,
            user_ctx.get("restaurant_name", "Our Restaurant"),
            user.first_name,
        )
        return

    # ── Mode: DINING ─────────────────────────────────────────────────────────
    if data == "mode_dining":
        set_mode(user.id, Mode.DINING, context)
        clear_user_state(user.id, context)
        table = user_ctx.get("table_number")
        if table:
            set_user_state(user.id, UserState.HAS_TABLE, context)
            await query.message.reply_text(
                f"🍽️ *Dining Mode* — Table {table} active.\n\n"
                f"What would you like to order? You can also ask about the menu or request your bill.",
                reply_markup=back_button(),
                parse_mode="Markdown",
            )
        else:
            set_user_state(user.id, UserState.AWAITING_TABLE, context)
            await query.message.reply_text(
                "🍽️ *Dining Mode*\n\n"
                "🪑 First, what is your table number?",
                reply_markup=back_button(),
                parse_mode="Markdown",
            )
        return

    # ── Mode: BOOKING ────────────────────────────────────────────────────────
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

    # ── Mode: TABLE ──────────────────────────────────────────────────────────
    if data == "mode_table":
        set_mode(user.id, Mode.TABLE, context)
        set_user_state(user.id, UserState.AWAITING_TABLE, context)
        await query.message.reply_text(
            "🪑 *Set Table Number*\n\n"
            "Please type your table number _(digits only)_:",
            reply_markup=back_button(),
            parse_mode="Markdown",
        )
        return

    # ── Legacy menu button (from old sessions) ───────────────────────────────
    if data == "menu":
        await _send_menu(query.message, user_ctx)
        return


# ============================================================================
# MENU HELPERS
# ============================================================================

async def _send_menu(message, user_ctx: dict) -> None:
    """
    Fetch live menu_items and send as a structured text list.
    Never calls the LLM — always DB rows.
    """
    restaurant_id   = user_ctx.get("restaurant_id")
    restaurant_name = user_ctx.get("restaurant_name", "Our Restaurant")

    if not restaurant_id:
        await message.reply_text("❌ Please use /start first.")
        return

    try:
        rows = supabase.table("menu_items") \
            .select("content").eq("restaurant_id", restaurant_id).execute()

        if not rows.data:
            await message.reply_text(
                "📋 Menu unavailable. Please ask staff.",
                reply_markup=back_button(),
            )
            return

        lines = [f"🍽️ *{restaurant_name} — Menu*\n"]
        current_cat = None

        for row in rows.data:
            for line in row["content"].split("\n"):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("category:"):
                    cat = line.replace("category:", "").strip()
                    if cat != current_cat:
                        lines.append(f"\n*{cat.upper()}*")
                        current_cat = cat
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

    except Exception as e:
        print(f"[MENU ERROR] {e}")
        await message.reply_text("❌ Error loading menu.")


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_ctx = get_user_context(update.effective_user.id, context)
    await _send_menu(update.message, user_ctx)


# ============================================================================
# /help HANDLER
# ============================================================================

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Restaurant AI Concierge*\n\n"
        "*Commands:*\n"
        "/start — Main menu\n"
        "/menu  — View full menu\n"
        "/cancel — Cancel an order by ID\n"
        "/help  — This message\n\n"
        "*How it works:*\n"
        "1. Press *View Menu & Order* to enter Dining Mode\n"
        "2. Set your table number once\n"
        "3. Just type what you want — I'll handle the rest!\n\n"
        "To modify an order, say: _\"modify order #123\"_ or use /cancel",
        parse_mode="Markdown",
        reply_markup=back_button(),
    )


# ============================================================================
# /cancel COMMAND — enters AWAITING_ORDER_ID state
# ============================================================================

async def cancel_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /cancel — ask for the Order ID explicitly.
    The blind "latest order" lookup is intentionally removed.
    """
    user     = update.effective_user
    user_ctx = get_user_context(user.id, context)

    # Show their recent orders as a reference
    restaurant_id = user_ctx.get("restaurant_id")
    try:
        recent = supabase.table("orders") \
            .select("id, items, price") \
            .eq("user_id", str(user.id)) \
            .eq("restaurant_id", restaurant_id) \
            .eq("status", "pending") \
            .order("created_at", desc=True) \
            .limit(5) \
            .execute()

        if not recent.data:
            await update.message.reply_text(
                "❌ You have no active orders to cancel.",
                reply_markup=back_button(),
            )
            return

        order_list = "\n".join(
            f"  *#{o['id']}* — {o['items']}  (${float(o['price']):.2f})"
            for o in recent.data
        )
        user_ctx["pending_action"] = "cancel"
        set_user_state(user.id, UserState.AWAITING_ORDER_ID, context)

        await update.message.reply_text(
            f"📋 *Your active orders:*\n{order_list}\n\n"
            f"Please type the *Order Number* you wish to cancel:",
            reply_markup=back_button(),
            parse_mode="Markdown",
        )

    except Exception as e:
        print(f"[CANCEL CMD ERROR] {e}")
        await update.message.reply_text("❌ Error fetching orders. Please try again.")


# ============================================================================
# /table COMMAND
# ============================================================================

async def table_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    set_mode(user.id, Mode.TABLE, context)
    set_user_state(user.id, UserState.AWAITING_TABLE, context)
    await update.message.reply_text(
        "🪑 *Set Table Number*\n"
        "Please type your table number _(digits only)_:",
        reply_markup=back_button(),
        parse_mode="Markdown",
    )


# ============================================================================
# BOOKING FLOW
# ============================================================================

async def handle_booking_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: UserState
):
    user     = update.effective_user
    text     = update.message.text.strip()
    user_ctx = get_user_context(user.id, context)

    if state == UserState.AWAITING_GUESTS:
        nums = re.findall(r'\d+', text)
        if not nums:
            await update.message.reply_text(
                "❌ Please enter the number of guests (e.g. '4').",
                reply_markup=back_button(),
            )
            return
        party = int(nums[0])
        if not (1 <= party <= 20):
            await update.message.reply_text(
                "❌ Party size must be between 1 and 20.",
                reply_markup=back_button(),
            )
            return
        user_ctx["party_size"] = party
        set_user_state(user.id, UserState.AWAITING_TIME, context)
        await update.message.reply_text(
            f"✅ Table for *{party}* guests.\n\n"
            f"⏰ When would you like to dine?\n"
            f"_(e.g. 'tomorrow 8pm', 'Friday 7:30pm', 'Jan 25 at 6pm')_",
            reply_markup=back_button(),
            parse_mode="Markdown",
        )
        return

    if state == UserState.AWAITING_TIME:
        booking_time = await parse_booking_time(text)
        if not booking_time:
            await update.message.reply_text(
                "❌ Invalid or past time.\n\n"
                "Try: _'tomorrow 8pm'_, _'Friday 7:30pm'_, _'Jan 25 at 6pm'_",
                reply_markup=back_button(),
                parse_mode="Markdown",
            )
            return

        restaurant_id = user_ctx.get("restaurant_id")
        party         = user_ctx.get("party_size")

        if check_duplicate_booking(user.id, restaurant_id, booking_time):
            await update.message.reply_text(
                "❌ You already have a booking at that time.",
                reply_markup=back_button(),
            )
            clear_user_state(user.id, context)
            return

        if not check_availability(restaurant_id, booking_time):
            await update.message.reply_text(
                "❌ Fully booked at that time. Please try another slot.",
                reply_markup=back_button(),
            )
            return

        try:
            supabase.table("bookings").insert({
                "restaurant_id": restaurant_id,
                "user_id":       str(user.id),
                "customer_name": user.full_name or "Guest",
                "party_size":    party,
                "booking_time":  booking_time.strftime("%Y-%m-%d %H:%M:%S%z"),
                "status":        "confirmed",
            }).execute()

            await update.message.reply_text(
                f"✅ *Booking Confirmed!*\n\n"
                f"👥 Guests: {party}\n"
                f"📅 {booking_time.strftime('%B %d, %Y at %I:%M %p')}\n\n"
                f"We look forward to serving you!",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )
            clear_user_state(user.id, context)
            clear_mode(user.id, context)

        except Exception as e:
            print(f"[BOOKING ERROR] {e}")
            await update.message.reply_text(
                "❌ System error. Please try again.",
                reply_markup=back_button(),
            )
            clear_user_state(user.id, context)


# ============================================================================
# TABLE ASSIGNMENT
# ============================================================================

async def handle_table_assignment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user     = update.effective_user
    text     = update.message.text.strip()
    user_ctx = get_user_context(user.id, context)
    mode     = get_mode(user.id, context)

    nums = re.findall(r'\d+', text)
    if not nums:
        await update.message.reply_text(
            "❌ Please type just the table number (e.g. '7' or '12').",
            reply_markup=back_button(),
        )
        return

    table_number = nums[0]
    user_ctx["table_number"] = table_number

    try:
        supabase.table("user_sessions").upsert({
            "user_id":      str(user.id),
            "table_number": table_number,
        }).execute()
        print(f"[TABLE] {user.id} → table {table_number} (saved)")
    except Exception as e:
        print(f"[TABLE] DB warning: {e}")

    # Auto-transition: if in TABLE mode, move to DINING mode after assignment
    if mode == Mode.TABLE:
        set_mode(user.id, Mode.DINING, context)

    set_user_state(user.id, UserState.HAS_TABLE, context)

    await update.message.reply_text(
        f"✅ *Table {table_number} confirmed!*\n\n"
        f"You're in Dining Mode. Just tell me what you'd like to order!\n"
        f"_Example: 'I'll have 2 Binary Bites and a Java Jolt'_",
        reply_markup=back_button(),
        parse_mode="Markdown",
    )


# ============================================================================
# ORDER ID HANDLER  (AWAITING_ORDER_ID state)
# ============================================================================

async def handle_order_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    The user has typed an order number in response to a /cancel or
    "modify order #X" prompt.  Validate ownership then execute the action.
    """
    user     = update.effective_user
    text     = update.message.text.strip()
    user_ctx = get_user_context(user.id, context)

    nums = re.findall(r'\d+', text)
    if not nums:
        await update.message.reply_text(
            "❌ Please type a valid order number (digits only).",
            reply_markup=back_button(),
        )
        return

    order_id      = int(nums[0])
    restaurant_id = user_ctx.get("restaurant_id")
    action        = user_ctx.get("pending_action", "cancel")   # "cancel" or "modify"
    mod_intent    = user_ctx.get("pending_mod_text", "")

    order = fetch_order_for_user(order_id, str(user.id), restaurant_id)

    if not order:
        await update.message.reply_text(
            f"❌ Order *#{order_id}* not found or doesn't belong to you.\n"
            f"Please check the number and try again.",
            reply_markup=back_button(),
            parse_mode="Markdown",
        )
        return

    # Clean up pending state
    clear_user_state(user.id, context)
    user_ctx.pop("pending_action", None)
    user_ctx.pop("pending_mod_text", None)

    if action == "cancel":
        reply = stage_cancellation(order)
    else:
        reply = await stage_modification(order, mod_intent)

    await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=back_button())


# ============================================================================
# FEEDBACK HANDLER
# ============================================================================

async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user     = update.effective_user
    text     = update.message.text.strip()
    user_ctx = get_user_context(user.id, context)

    ratings = re.findall(r'\b[1-5]\b', text)
    if not ratings:
        await update.message.reply_text(
            "Please provide ratings (1-5) for each dish and overall experience."
        )
        return

    try:
        supabase.table("feedback").insert({
            "restaurant_id": user_ctx.get("restaurant_id"),
            "user_id":       str(user.id),
            "ratings":       text,
            "created_at":    get_dubai_now().isoformat(),
        }).execute()
        await update.message.reply_text(
            "⭐ Thank you for your feedback!\n\nWe hope to see you again soon! 😊",
            reply_markup=main_menu_keyboard(),
        )
        clear_user_state(user.id, context)
    except Exception as e:
        print(f"[FEEDBACK ERROR] {e}")
        await update.message.reply_text("✅ Feedback received. Thank you!")
        clear_user_state(user.id, context)


# ============================================================================
# BILLING
# ============================================================================

async def calculate_bill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Python sum over price column — never an LLM.
    Resolves table from memory → DB → asks user only if truly absent.
    """
    user      = update.effective_user
    user_ctx  = get_user_context(user.id, context)
    table_num = user_ctx.get("table_number")
    rest_id   = user_ctx.get("restaurant_id")

    if not table_num:
        try:
            sess = supabase.table("user_sessions") \
                .select("table_number").eq("user_id", str(user.id)).execute()
            if sess.data and sess.data[0].get("table_number"):
                table_num = str(sess.data[0]["table_number"])
                user_ctx["table_number"] = table_num
        except Exception as e:
            print(f"[BILL] session error: {e}")

    if not table_num:
        set_user_state(user.id, UserState.AWAITING_TABLE, context)
        await update.message.reply_text(
            "🪑 What is your table number? _(I'll pull your bill right after!)_",
            reply_markup=back_button(),
            parse_mode="Markdown",
        )
        return

    try:
        res = supabase.table("orders") \
            .select("id, items, price") \
            .eq("user_id", str(user.id)) \
            .eq("restaurant_id", rest_id) \
            .eq("table_number", str(table_num)) \
            .neq("status", "paid") \
            .neq("status", "cancelled") \
            .execute()

        if not res.data:
            await update.message.reply_text(
                f"🧾 *Table {table_num}* — No active orders found.",
                parse_mode="Markdown",
                reply_markup=back_button(),
            )
            return

        total = round(sum(float(r["price"]) for r in res.data), 2)
        lines = "\n".join(
            f"  • *#{r['id']}* {r['items']}  —  ${float(r['price']):.2f}"
            for r in res.data
        )

        await update.message.reply_text(
            f"🧾 *Your Bill — Table {table_num}*\n\n"
            f"{lines}\n\n"
            f"💰 *Total: ${total:.2f}*\n\n"
            f"_(Ask a waiter to process payment)_",
            parse_mode="Markdown",
            reply_markup=back_button(),
        )

    except Exception as e:
        print(f"[BILL ERROR] {e}")
        await update.message.reply_text(
            "❌ Error fetching bill. Please ask staff.",
            reply_markup=back_button(),
        )


# ============================================================================
# GENERAL AI CHAT  (Dining mode only)
# ============================================================================

async def handle_general_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user     = update.effective_user
    text     = update.message.text.strip()
    user_ctx = get_user_context(user.id, context)
    rest_id  = user_ctx.get("restaurant_id")

    if not rest_id:
        await update.message.reply_text(
            "👋 Please use /start to begin.",
            reply_markup=main_menu_keyboard(),
        )
        return

    try:
        rows = supabase.table("menu_items") \
            .select("content").eq("restaurant_id", rest_id).limit(20).execute()
        menu_ctx = "\n".join(r["content"] for r in rows.data) if rows.data else "No menu available"

        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": (
                f"You are a friendly restaurant assistant.\n\n"
                f"Menu:\n{menu_ctx}\n\n"
                f"Customer: \"{text}\"\n\n"
                f"Instructions:\n"
                f"- Describe menu items warmly if asked\n"
                f"- Answer restaurant policy questions helpfully\n"
                f"- Keep response to 2-3 sentences\n"
                f"- Be warm and professional\n\n"
                f"Response:"
            )}],
            temperature=0.7,
            max_tokens=200,
        )
        await update.message.reply_text(
            completion.choices[0].message.content,
            reply_markup=back_button(),
        )
    except Exception as e:
        print(f"[CHAT ERROR] {e}")
        await update.message.reply_text(
            "I'm here to help! Try /menu to see what we're serving today.",
            reply_markup=back_button(),
        )


# ============================================================================
# MAIN MESSAGE HANDLER  (Mode-isolated router)
# ============================================================================

# Patterns used by the booking-attempt detector in Dining mode
_DATE_PATTERN = re.compile(
    r'\b(tomorrow|today|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday'
    r'|january|february|march|april|may|june|july|august|september|october|november|december'
    r'|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec'
    r'|next\s+week|this\s+weekend|\d{1,2}[/-]\d{1,2})\b',
    re.IGNORECASE,
)

# Patterns used by the food-order detector in Booking mode
_ORDER_PATTERN = re.compile(
    r"\b(i('ll| will) have|i want|can i get|give me|bring me|i('d| would) like"
    r"|order|burger|pizza|pasta|coffee|tea|juice|water|fries|salad|soup)\b",
    re.IGNORECASE,
)

# Modification trigger keywords
_MOD_KEYWORDS = ["remove", "take off", "drop the", "cancel", "without",
                 "don't want", "no more", "delete", "modify order", "change order"]


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Mode-isolated message router.

    Priority (top = highest):
      1. AWAITING_ORDER_ID         — collect order number for mod/cancel
      2. AWAITING_FEEDBACK         — collect ratings
      3. Booking mode states       — AWAITING_GUESTS / AWAITING_TIME
         └ cross-mode guard: food order attempt → redirect
      4. AWAITING_TABLE            — table number input (digits only)
      5. Dining mode (HAS_TABLE / IDLE with mode=DINING)
         ├ cross-mode guard: date-like input → redirect
         ├ modification/cancel trigger → ask for order ID
         ├ menu keywords → structured menu
         ├ bill keywords → bill
         ├ order processing → process_order()
         └ general chat → AI
      6. No mode (IDLE at /start) → prompt to choose a mode
    """
    user       = update.effective_user
    text       = update.message.text.strip()
    text_lower = text.lower()

    state    = get_user_state(user.id, context)
    mode     = get_mode(user.id, context)
    user_ctx = get_user_context(user.id, context)

    print(f"[MSG] {user.id} mode={mode.value} state={state.value} "
          f"table={user_ctx.get('table_number', '—')}: '{text[:60]}'")

    # ── 1. ORDER ID COLLECTION ────────────────────────────────────────────────
    if state == UserState.AWAITING_ORDER_ID:
        await handle_order_id_input(update, context)
        return

    # ── 2. FEEDBACK ───────────────────────────────────────────────────────────
    if state == UserState.AWAITING_FEEDBACK:
        await handle_feedback(update, context)
        return

    # ── 3. BOOKING MODE ───────────────────────────────────────────────────────
    if mode == Mode.BOOKING:
        if state in [UserState.AWAITING_GUESTS, UserState.AWAITING_TIME]:
            # Cross-mode guard: reject food orders inside booking flow
            if _ORDER_PATTERN.search(text):
                await update.message.reply_text(
                    "📅 You're in *Booking Mode*.\n\n"
                    "Please finish your reservation first, "
                    "or press ⬅️ Main Menu to switch modes.",
                    reply_markup=back_button(),
                    parse_mode="Markdown",
                )
                return
            await handle_booking_flow(update, context, state)
            return
        # Booking mode but unexpected state — re-prompt
        set_user_state(user.id, UserState.AWAITING_GUESTS, context)
        await update.message.reply_text(
            "📅 *Booking Mode* — How many guests?",
            reply_markup=back_button(),
            parse_mode="Markdown",
        )
        return

    # ── 4. TABLE NUMBER MODE (only digits) ────────────────────────────────────
    if mode == Mode.TABLE or state == UserState.AWAITING_TABLE:
        # Cross-mode guard inside TABLE mode: reject booking-date input
        if mode == Mode.TABLE and _DATE_PATTERN.search(text) and not text.isdigit():
            await update.message.reply_text(
                "🪑 *Set Table Number* — please type only digits (e.g. '7').",
                reply_markup=back_button(),
                parse_mode="Markdown",
            )
            return
        await handle_table_assignment(update, context)
        return

    # ── 5. DINING MODE (or passive table detection) ───────────────────────────
    if mode == Mode.DINING or user_ctx.get("table_number"):

        # Cross-mode guard: reject booking-date input in dining mode
        if mode == Mode.DINING and _DATE_PATTERN.search(text):
            # But allow "today's special" style — only block if also has a time hint
            time_hint = re.search(r'\b(\d{1,2}(am|pm|:\d{2})|tonight|morning|evening|noon)\b',
                                  text_lower)
            if time_hint:
                await update.message.reply_text(
                    "🍽️ You're in *Dining Mode*.\n\n"
                    "To make a reservation, press ⬅️ Main Menu and choose *Book a Table*.",
                    reply_markup=back_button(),
                    parse_mode="Markdown",
                )
                return

        # Passive table detection (user typed "table 7" without pressing a button)
        table_match = re.search(r'\btable\s*(\d+)\b', text_lower)
        if table_match and not user_ctx.get("table_number"):
            set_mode(user.id, Mode.DINING, context)
            set_user_state(user.id, UserState.AWAITING_TABLE, context)
            await handle_table_assignment(update, context)
            return

        # Modification / cancel trigger
        mod_trigger = any(kw in text_lower for kw in _MOD_KEYWORDS)
        order_id_in_text = re.search(r'#?(\d{3,})', text)   # order IDs tend to be ≥3 digits

        if mod_trigger:
            rest_id = user_ctx.get("restaurant_id")
            # If they included an order ID in the same message, validate immediately
            if order_id_in_text:
                oid   = int(order_id_in_text.group(1))
                order = fetch_order_for_user(oid, str(user.id), rest_id)
                if order:
                    is_cancel = any(p in text_lower for p in
                                    ["cancel", "cancel order", "cancel everything",
                                     "nevermind", "never mind"])
                    if is_cancel:
                        reply = stage_cancellation(order)
                    else:
                        reply = await stage_modification(order, text)
                    await update.message.reply_text(
                        reply, parse_mode="Markdown", reply_markup=back_button()
                    )
                    return
                # ID given but doesn't match — fall through to ask
            # No valid ID in message — ask for it
            try:
                recent = supabase.table("orders") \
                    .select("id, items, price") \
                    .eq("user_id", str(user.id)) \
                    .eq("restaurant_id", rest_id) \
                    .eq("status", "pending") \
                    .order("created_at", desc=True) \
                    .limit(5).execute()

                if not recent.data:
                    await update.message.reply_text(
                        "❌ You have no active orders to modify.",
                        reply_markup=back_button(),
                    )
                    return

                order_list = "\n".join(
                    f"  *#{o['id']}* — {o['items']}  (${float(o['price']):.2f})"
                    for o in recent.data
                )

                is_cancel_intent = any(p in text_lower for p in
                                       ["cancel", "nevermind", "never mind"])
                action = "cancel" if is_cancel_intent else "modify"
                user_ctx["pending_action"]   = action
                user_ctx["pending_mod_text"] = text
                set_user_state(user.id, UserState.AWAITING_ORDER_ID, context)

                await update.message.reply_text(
                    f"📋 *Your active orders:*\n{order_list}\n\n"
                    f"Please type the *Order Number* you wish to {action}:",
                    reply_markup=back_button(),
                    parse_mode="Markdown",
                )
                return
            except Exception as e:
                print(f"[MOD TRIGGER ERROR] {e}")
                await update.message.reply_text(
                    "❌ Error fetching orders. Please try again.",
                    reply_markup=back_button(),
                )
                return

        # Menu keyword intercept
        menu_kws = ["menu", "what do you serve", "what's available",
                    "what do you have", "show me food", "food list"]
        if any(kw in text_lower for kw in menu_kws):
            await _send_menu(update.message, user_ctx)
            return

        # Bill keyword intercept
        bill_kws = ["bill", "check please", "the check", "my total",
                    "how much", "pay", "invoice", "receipt"]
        if any(kw in text_lower for kw in bill_kws):
            await calculate_bill(update, context)
            return

        # Order processing
        if user_ctx.get("table_number"):
            result = await process_order(
                text, user,
                user_ctx.get("restaurant_id"),
                user_ctx.get("table_number"),
                user_ctx.get("chat_id"),
            )
            if result:
                reply_text, _order_id = result
                await update.message.reply_text(
                    reply_text,
                    parse_mode="Markdown",
                    reply_markup=back_button(),
                )
                return
        else:
            # Table not known yet and user looks like they want to order
            order_kws = ["order", "i'll have", "i want", "can i get",
                         "give me", "bring me", "i'd like"]
            if any(kw in text_lower for kw in order_kws):
                set_mode(user.id, Mode.DINING, context)
                set_user_state(user.id, UserState.AWAITING_TABLE, context)
                await update.message.reply_text(
                    "🪑 *What's your table number?*\n"
                    "_(I'll place the order right after!)_",
                    reply_markup=back_button(),
                    parse_mode="Markdown",
                )
                return

        # General chat within dining mode
        await handle_general_chat(update, context)
        return

    # ── 6. NO MODE — prompt to pick one ──────────────────────────────────────
    user_ctx_name = user_ctx.get("restaurant_name", "Our Restaurant")
    await update.message.reply_text(
        f"👋 Welcome to *{user_ctx_name}*!\n\n"
        f"Please choose an option from the menu to get started:",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )


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
    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")
        return {"status": "error", "message": str(e)}


@app.get("/")
async def health_check():
    return {
        "status":    "running",
        "service":   "Restaurant AI Concierge v4",
        "timestamp": get_dubai_now().isoformat(),
    }


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
    telegram_app.add_handler(CommandHandler("table",  table_command_handler))
    telegram_app.add_handler(CommandHandler("cancel", cancel_command_handler))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler)
    )

    await telegram_app.initialize()
    await telegram_app.start()
    print("✅ Telegram Bot v4 Started")


@app.on_event("shutdown")
async def shutdown_event():
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    print("🛑 Bot stopped")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)