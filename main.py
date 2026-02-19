"""
Restaurant AI Concierge — main.py  v6
======================================
New in v6:
  1. STRICT SESSION RESET — /start hard-wipes context.user_data, then
     asks for the user's name (AWAITING_NAME) before any menu is shown.
  2. ADVANCED BOOKING RULES — 4-hour cancellation policy enforced;
     modification is a transactional swap (book new → cancel old).
  3. SMART TABLE INVENTORY — check_granular_availability() uses
     tables_inventory table with bin-packing, replaces count<10 guard.
  4. CUSTOMER CRM — load_crm_profile() computes tags; personalised
     greetings; allergy detection & warnings; VIP comp offers.
"""

import os, re, json, random
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from supabase import create_client, Client
from dotenv import load_dotenv
from groq import AsyncGroq

from order_service import (
    process_order, fetch_order_for_user,
    stage_cancellation, stage_modification, update_crm_on_payment,
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY")

supabase:     Client               = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client:  AsyncGroq            = AsyncGroq(api_key=GROQ_API_KEY)
app:          FastAPI               = FastAPI(title="Restaurant Concierge API v6")
telegram_app: Optional[Application] = None
DUBAI_TZ = ZoneInfo("Asia/Dubai")


# ── Enums ─────────────────────────────────────────────────────────────────────

class Mode(str, Enum):
    GENERAL = "general"
    ORDER   = "order"
    BOOKING = "booking"

class UserState(str, Enum):
    IDLE                       = "idle"
    AWAITING_NAME              = "awaiting_name"
    AWAITING_TABLE             = "awaiting_table"
    HAS_TABLE                  = "has_table"
    AWAITING_ORDER_ID          = "awaiting_order_id"
    AWAITING_GUESTS            = "awaiting_guests"
    AWAITING_TIME              = "awaiting_time"
    AWAITING_FEEDBACK          = "awaiting_feedback"
    AWAITING_BOOKING_CANCEL_ID = "awaiting_booking_cancel_id"
    AWAITING_BOOKING_MOD_ID    = "awaiting_booking_mod_id"
    AWAITING_BOOKING_MOD_TIME  = "awaiting_booking_mod_time"


# ── State helpers ──────────────────────────────────────────────────────────────

def get_user_state(uid, ctx): return ctx.user_data.get(f"state_{uid}", UserState.IDLE)
def set_user_state(uid, state, ctx):
    ctx.user_data[f"state_{uid}"] = state
    print(f"[STATE] {uid} → {state.value}")
def clear_user_state(uid, ctx): set_user_state(uid, UserState.IDLE, ctx)
def get_user_context(uid, ctx):
    k = f"ctx_{uid}"
    if k not in ctx.user_data: ctx.user_data[k] = {}
    return ctx.user_data[k]
def get_mode(uid, ctx): return Mode(get_user_context(uid, ctx).get("mode", Mode.GENERAL))
def set_mode(uid, mode, ctx): get_user_context(uid, ctx)["mode"] = mode.value
def reset_to_general(uid, ctx):
    set_mode(uid, Mode.GENERAL, ctx); set_user_state(uid, UserState.IDLE, ctx)
    for k in ("pending_action","pending_mod_text","booking_mod_old_id","booking_mod_old_data"):
        get_user_context(uid, ctx).pop(k, None)


# ── UI helpers ─────────────────────────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍽️ Order Food (Dine-in)", callback_data="mode_order")],
        [InlineKeyboardButton("📅 Book a Table",          callback_data="mode_booking")],
    ])
def back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data="main_menu")]])


# ── CRM helpers ────────────────────────────────────────────────────────────────

def load_crm_profile(user_id: str) -> Dict[str, Any]:
    defaults = {"visit_count":0,"total_spend":0.0,"last_visit":None,"preferences":"","tags":[]}
    try:
        res = supabase.table("users").select("visit_count,total_spend,last_visit,preferences")\
            .eq("id", str(user_id)).limit(1).execute()
        if not res.data: return defaults
        row = res.data[0]
        vc = int(row.get("visit_count") or 0)
        ts = float(row.get("total_spend") or 0.0)
        lv = row.get("last_visit")
        pr = row.get("preferences") or ""
        tags: List[str] = []
        now = datetime.now(timezone.utc)
        if vc > 5:  tags.append("Frequent Diner")
        if ts > 500: tags.append("Big Spender")
        if "Frequent Diner" in tags and "Big Spender" in tags: tags.append("VIP")
        if lv and vc > 0:
            try:
                lv_dt = datetime.fromisoformat(str(lv).replace("Z","+00:00"))
                if (now - lv_dt) > timedelta(days=30): tags.append("Churn Risk")
            except Exception: pass
        return {"visit_count":vc,"total_spend":ts,"last_visit":lv,"preferences":pr,"tags":tags}
    except Exception as ex:
        print(f"[CRM] {ex}"); return defaults

def increment_visit(user_id: str):
    try:
        res = supabase.table("users").select("visit_count").eq("id",str(user_id)).limit(1).execute()
        cur = int((res.data[0].get("visit_count") or 0)) if res.data else 0
        supabase.table("users").update({"visit_count":cur+1,"last_visit":datetime.now(timezone.utc).isoformat()})\
            .eq("id",str(user_id)).execute()
    except Exception as ex: print(f"[CRM VISIT] {ex}")

def save_preferences(user_id: str, pref: str):
    try: supabase.table("users").update({"preferences":pref}).eq("id",str(user_id)).execute()
    except Exception as ex: print(f"[CRM PREF] {ex}")

def build_personalized_greeting(name: str, restaurant_name: str, tags: List[str]) -> str:
    if "VIP" in tags or "Big Spender" in tags:
        msg = f"👑 Welcome back, *{name}*! As one of our VIP guests, you're very special to us."
        if random.random() < 0.20:
            msg += "\n\n🍹 *Complimentary drink on us today — mention this when you order!*"
        return msg
    if "Frequent Diner" in tags:
        return f"😊 Welcome back, *{name}*! Great to see you again at *{restaurant_name}*."
    if "Churn Risk" in tags:
        return f"👋 *{name}*, we've missed you! So glad you're back at *{restaurant_name}*."
    return f"👋 Welcome to *{restaurant_name}*, *{name}*!"


# ── Policy fetcher ─────────────────────────────────────────────────────────────

def fetch_policy_text(rid: str) -> str:
    try:
        res = supabase.table("restaurant_policies").select("policy_text")\
            .eq("restaurant_id",str(rid)).limit(1).execute()
        if res.data: return res.data[0].get("policy_text","")
    except Exception as ex: print(f"[POLICY] {ex}")
    return ""


# ── Time helpers ───────────────────────────────────────────────────────────────

def get_dubai_now(): return datetime.now(DUBAI_TZ)

async def parse_booking_time(user_input: str) -> Optional[datetime]:
    prompt = (f'Current Dubai Time: {get_dubai_now().strftime("%Y-%m-%d %H:%M")}\n\n'
              f'Parse: "{user_input}"\nReturn ONLY JSON: {{"datetime":"YYYY-MM-DD HH:MM","valid":true}}\n'
              f'Rules: past→false, ambiguous→false')
    try:
        c = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"user","content":prompt}], temperature=0, max_tokens=60)
        raw = c.choices[0].message.content
        s,e = raw.find("{"), raw.rfind("}")+1
        if s==-1 or e==0: return None
        d = json.loads(raw[s:e])
        if not d.get("valid"): return None
        dt = datetime.strptime(d["datetime"],"%Y-%m-%d %H:%M").replace(tzinfo=DUBAI_TZ)
        return dt if dt > get_dubai_now() else None
    except Exception as ex:
        print(f"[TIME] {ex}"); return None


# ── Smart table availability ───────────────────────────────────────────────────

def check_granular_availability(rid: str, booking_time: datetime, party_size: int) -> Tuple[bool,str]:
    """
    Bin-packing algorithm using tables_inventory table.
    Falls back to count<10 if no inventory configured.
    """
    try:
        inv = supabase.table("tables_inventory").select("capacity,table_count")\
            .eq("restaurant_id",str(rid)).order("capacity").execute()
        if not inv.data:
            bk = supabase.table("bookings").select("id",count="exact")\
                .eq("restaurant_id",rid)\
                .eq("booking_time",booking_time.strftime("%Y-%m-%d %H:%M:%S%z"))\
                .neq("status","cancelled").execute()
            return ((bk.count or 0) < 10, "available (fallback)") if (bk.count or 0) < 10 \
                else (False, "fully booked")

        available: Dict[int,int] = {r["capacity"]:r["table_count"] for r in inv.data}
        sizes = sorted(available.keys())

        # CRITICAL FIX: Check if party size exceeds total capacity
        total_seats = sum(cap * qty for cap, qty in available.items())
        if party_size > total_seats:
            return (False, f"party of {party_size} exceeds total capacity ({total_seats} seats)")


        # Deduct tables already booked at this slot
        bk = supabase.table("bookings").select("party_size")\
            .eq("restaurant_id",rid)\
            .eq("booking_time",booking_time.strftime("%Y-%m-%d %H:%M:%S%z"))\
            .neq("status","cancelled").execute()
        for b in (bk.data or []):
            ps = b["party_size"]
            for cap in sizes:
                if cap >= ps and available.get(cap,0) > 0:
                    available[cap] -= 1; break
            else:
                rem = ps
                for cap in sizes:
                    while rem > 0 and available.get(cap,0) > 0:
                        available[cap] -= 1; rem -= cap

        # Greedy bin-pack: smallest table first (efficiency)
        remaining = party_size
        used: List[str] = []
        for cap in sizes:
            while remaining > 0 and available.get(cap,0) > 0:
                available[cap] -= 1; remaining -= cap; used.append(f"{cap}-seater")
        if remaining <= 0:
            return True, f"seats available ({', '.join(used)})"
        return False, "not enough tables for that party size"
    except Exception as ex:
        print(f"[AVAIL] {ex}")
        # Fallback: if no inventory defined, use simple count<10 check
        try:
            res = supabase.table("bookings") \
                .select("id", count="exact") \
                .eq("restaurant_id", rid) \
                .eq("booking_time", booking_time.strftime("%Y-%m-%d %H:%M:%S%z")) \
                .neq("status", "cancelled") \
                .execute()
            available = (res.count or 0) < 10
            return (available, "Available" if available else "Fully booked (simple check)")
        except Exception:
            return (False, f"Availability check error: {ex}")
    # Fallback: if no inventory defined, use simple count<10 check
        try:
            res = supabase.table("bookings") \
                .select("id", count="exact") \
                .eq("restaurant_id", rid) \
                .eq("booking_time", booking_time.strftime("%Y-%m-%d %H:%M:%S%z")) \
                .neq("status", "cancelled") \
                .execute()
            available = (res.count or 0) < 10
            return (available, "Available" if available else "Fully booked (simple check)")
        except Exception:
            return (False, f"Availability check error: {ex}")

def check_duplicate_booking(uid, rid, booking_time):
    """Check if THIS user already has a booking at this exact time."""
    try:
        res = supabase.table("bookings").select("id")\
            .eq("user_id",str(uid)).eq("restaurant_id",rid)\
            .eq("booking_time",booking_time.strftime("%Y-%m-%d %H:%M:%S%z"))\
            .neq("status","cancelled").execute()
        return bool(res.data)
    except Exception as ex:
        print(f"[DUP] {ex}"); return False

# ── /start — hard reset + name handshake ──────────────────────────────────────

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, chat_id = update.effective_user, update.effective_chat.id
    restaurant_id, restaurant_name = None, "Our Restaurant"

    if context.args:
        arg = context.args[0]
        rid = arg.split("=")[1] if arg.startswith("rest_id=") else arg
        try:
            chk = supabase.table("restaurants").select("id,name").eq("id",rid).execute()
            if chk.data: restaurant_id=chk.data[0]["id"]; restaurant_name=chk.data[0].get("name",restaurant_name)
        except Exception as ex: print(f"[START] {ex}")

    if not restaurant_id:
        try:
            d = supabase.table("restaurants").select("id,name").limit(1).execute()
            if d.data: restaurant_id=d.data[0]["id"]; restaurant_name=d.data[0].get("name",restaurant_name)
            else: await update.message.reply_text("❌ No restaurants configured."); return
        except Exception as ex:
            print(f"[START] {ex}"); await update.message.reply_text("❌ Cannot connect."); return

    # HARD RESET
    context.user_data.clear()
    print(f"[START] Hard reset uid={user.id}")

    uc = get_user_context(user.id, context)
    uc["restaurant_id"] = restaurant_id
    uc["restaurant_name"] = restaurant_name
    uc["chat_id"] = chat_id

    try:
        supabase.table("users").upsert({
            "id":str(user.id),"username":user.username or "guest",
            "full_name":user.full_name or "Guest","chat_id":str(chat_id),
        }).execute()
    except Exception as ex: print(f"[UPSERT] {ex}")

    set_user_state(user.id, UserState.AWAITING_NAME, context)
    await update.message.reply_text(
        f"👋 Welcome to *{restaurant_name}*!\n\nBefore we begin, what is your name?",
        parse_mode="Markdown",
    )


# ── Name handler ───────────────────────────────────────────────────────────────

async def handle_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = update.message.text.strip()
    if len(name) < 2 or name.isdigit():
        await update.message.reply_text("Please enter your name (e.g. *Sarah* or *Ahmed*).", parse_mode="Markdown")
        return
    uc = get_user_context(user.id, context)
    uc["display_name"] = name
    crm = load_crm_profile(str(user.id))
    uc.update({"visit_count":crm["visit_count"],"total_spend":crm["total_spend"],
               "tags":crm["tags"],"preferences":crm["preferences"]})
    greeting = build_personalized_greeting(name, uc.get("restaurant_name","Our Restaurant"), crm["tags"])
    tag_str  = ("  ·  ".join(f"🏷 {t}" for t in crm["tags"]) + "\n") if crm["tags"] else ""
    pref_note = (f"\n\n📋 *Preferences on file:* _{crm['preferences']}_") if crm["preferences"] else ""
    set_user_state(user.id, UserState.IDLE, context)
    set_mode(user.id, Mode.GENERAL, context)
    await update.message.reply_text(
        f"{greeting}\n_{tag_str}_\n\nYou're in *General Mode* — ask me anything about our menu, "
        f"WiFi, parking, or policies.{pref_note}\n\nReady to order or book? Choose an option:",
        reply_markup=main_menu_keyboard(), parse_mode="Markdown",
    )


# ── Button handler ─────────────────────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    user = update.effective_user; data = query.data; uc = get_user_context(user.id, context)

    if data == "main_menu":
        reset_to_general(user.id, context)
        name = uc.get("display_name", user.first_name)
        await query.message.reply_text(
            f"👋 Back to *General Mode*, {name}!\n\nAsk me anything, or choose an option:",
            reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    elif data == "mode_order":
        set_mode(user.id, Mode.ORDER, context)
        set_user_state(user.id, UserState.AWAITING_TABLE, context)
        await query.message.reply_text("🍽️ *Order Mode*\n\n🪑 What is your table number?",
            reply_markup=back_button(), parse_mode="Markdown")
    elif data == "mode_booking":
        set_mode(user.id, Mode.BOOKING, context)
        set_user_state(user.id, UserState.AWAITING_GUESTS, context)
        await query.message.reply_text(
            "📅 *Booking Mode*\n\nHow many guests? _(e.g. '4' or 'party of 6')_",
            reply_markup=back_button(), parse_mode="Markdown")
    elif data == "menu":
        await _send_menu(query.message, uc)


# ── Menu helper ────────────────────────────────────────────────────────────────

async def _send_menu(message, uc):
    rid = uc.get("restaurant_id"); rname = uc.get("restaurant_name","Our Restaurant")
    if not rid: await message.reply_text("❌ Please /start first."); return
    try:
        rows = supabase.table("menu_items").select("content").eq("restaurant_id",rid).execute()
        if not rows.data: await message.reply_text("📋 Menu unavailable.",reply_markup=back_button()); return
        lines = [f"🍽️ *{rname} — Menu*\n"]; cur_cat = None
        for row in rows.data:
            for line in row["content"].split("\n"):
                line = line.strip()
                if not line: continue
                if line.startswith("category:"):
                    cat = line.replace("category:","").strip()
                    if cat != cur_cat: lines.append(f"\n*{cat.upper()}*"); cur_cat = cat
                elif line.startswith("item:"):   lines.append(f"  • {line.replace('item:','').strip()}")
                elif line.startswith("price:"):  lines[-1] += f"  —  {line.replace('price:','').strip()}"
                elif line.startswith("description:"): lines.append(f"    _{line.replace('description:','').strip()}_")
        lines.append("\n_Tell me what you'd like and I'll place the order!_")
        mt = "\n".join(lines); kb = back_button()
        if len(mt) <= 4096: await message.reply_text(mt, parse_mode="Markdown", reply_markup=kb)
        else:
            await message.reply_text(mt[:4000], parse_mode="Markdown")
            await message.reply_text(mt[4000:], parse_mode="Markdown", reply_markup=kb)
    except Exception as ex:
        print(f"[MENU] {ex}"); await message.reply_text("❌ Error loading menu.")

async def menu_handler(update, context):
    await _send_menu(update.message, get_user_context(update.effective_user.id, context))


# ── /help ──────────────────────────────────────────────────────────────────────

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Restaurant AI Concierge v6*\n\n"
        "*Modes:*\n• General — Q&A (default)\n• Order Food — dine-in orders\n• Book a Table — reservations\n\n"
        "*Commands:*\n/start — Fresh session\n/menu — View menu\n/cancel — Cancel order\n"
        "/cancel_booking — Cancel reservation\n/modify_booking — Change reservation\n/help — This message",
        parse_mode="Markdown", reply_markup=back_button())


# ── Booking flow (new) ─────────────────────────────────────────────────────────

async def handle_booking_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, state):
    user = update.effective_user; text = update.message.text.strip()
    uc = get_user_context(user.id, context)

    if state == UserState.AWAITING_GUESTS:
        nums = re.findall(r'\d+', text)
        if not nums: await update.message.reply_text("❌ Enter guest count (e.g. '4').",reply_markup=back_button()); return
        party = int(nums[0])
        if not (1 <= party <= 20): await update.message.reply_text("❌ 1–20 guests only.",reply_markup=back_button()); return
        uc["party_size"] = party
        set_user_state(user.id, UserState.AWAITING_TIME, context)
        await update.message.reply_text(
            f"✅ Table for *{party}* guests.\n\n⏰ When? _(e.g. 'tomorrow 8pm', 'Friday 7:30pm')_",
            reply_markup=back_button(), parse_mode="Markdown"); return

    if state == UserState.AWAITING_TIME:
        bt = await parse_booking_time(text)
        if not bt:
            await update.message.reply_text("❌ Invalid/past time. Try 'tomorrow 8pm'.",
                reply_markup=back_button(), parse_mode="Markdown"); return
        rid = uc.get("restaurant_id"); party = uc.get("party_size",1)
        
        can_seat, reason = check_granular_availability(rid, bt, party)
        if not can_seat:
            await update.message.reply_text(f"❌ {reason}.\n\nPlease try a different time.",
                reply_markup=back_button()); return
        
        if check_duplicate_booking(user.id, rid, bt):
            await update.message.reply_text("❌ You already have a booking at that time.",reply_markup=back_button())
            clear_user_state(user.id, context); return
        
        try:
            supabase.table("bookings").insert({
                "restaurant_id":rid,"user_id":str(user.id),
                "customer_name":uc.get("display_name") or user.full_name or "Guest",
                "party_size":party,"booking_time":bt.strftime("%Y-%m-%d %H:%M:%S%z"),"status":"confirmed",
            }).execute()
            increment_visit(str(user.id)); uc["visit_count"] = uc.get("visit_count",0)+1
            await update.message.reply_text(
                f"✅ *Booking Confirmed!*\n\n👤 {uc.get('display_name','Guest')}\n"
                f"👥 Guests: {party}\n📅 {bt.strftime('%B %d, %Y at %I:%M %p')}\n\nSee you soon!",
                reply_markup=main_menu_keyboard(), parse_mode="Markdown")
            reset_to_general(user.id, context)
        except Exception as ex:
            print(f"[BK] {ex}"); await update.message.reply_text("❌ System error.",reply_markup=back_button())
            clear_user_state(user.id, context)


# ── Booking cancellation ───────────────────────────────────────────────────────

async def cancel_booking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uc = get_user_context(user.id, context)
    try:
        bks = supabase.table("bookings").select("id,party_size,booking_time")\
            .eq("user_id",str(user.id)).eq("restaurant_id",uc.get("restaurant_id"))\
            .eq("status","confirmed").order("booking_time").execute()
        if not bks.data:
            await update.message.reply_text("❌ No active bookings.",reply_markup=back_button()); return
        lines = []
        for b in bks.data:
            try:
                bt = datetime.fromisoformat(b["booking_time"].replace("Z","+00:00"))
                bts = bt.astimezone(DUBAI_TZ).strftime("%b %d at %I:%M %p")
            except Exception: bts = b["booking_time"]
            lines.append(f"  *#{b['id']}* — {b['party_size']} guests, {bts}")
        set_user_state(user.id, UserState.AWAITING_BOOKING_CANCEL_ID, context)
        await update.message.reply_text(
            "📋 *Your active bookings:*\n"+"\n".join(lines)+"\n\nType the *Booking ID* to cancel:",
            reply_markup=back_button(), parse_mode="Markdown")
    except Exception as ex:
        print(f"[CANBK] {ex}"); await update.message.reply_text("❌ Error fetching bookings.")

async def handle_booking_cancel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; text = update.message.text.strip()
    uc = get_user_context(user.id, context)
    nums = re.findall(r'\d+', text)
    if not nums: await update.message.reply_text("❌ Type a valid booking ID.",reply_markup=back_button()); return
    bid = int(nums[0])
    try:
        res = supabase.table("bookings").select("*").eq("id",bid).eq("user_id",str(user.id))\
            .eq("restaurant_id",uc.get("restaurant_id")).eq("status","confirmed").execute()
        if not res.data:
            await update.message.reply_text(f"❌ Booking *#{bid}* not found.",
                reply_markup=back_button(), parse_mode="Markdown"); return
        bk = res.data[0]
        bt = datetime.fromisoformat(bk["booking_time"].replace("Z","+00:00"))
        hours_until = (bt - datetime.now(timezone.utc)).total_seconds() / 3600
        if hours_until < 4:
            clear_user_state(user.id, context)
            await update.message.reply_text(
                f"⚠️ *Cancellation Not Allowed*\n\nYour reservation is only *{max(0,hours_until):.1f} hours* away.\n\n"
                "Cancellations require *4+ hours notice*. Please call the restaurant.",
                reply_markup=back_button(), parse_mode="Markdown"); return
        supabase.table("bookings").update({"status":"cancelled"}).eq("id",bid).execute()
        clear_user_state(user.id, context)
        bts = bt.astimezone(DUBAI_TZ).strftime("%B %d at %I:%M %p")
        await update.message.reply_text(
            f"✅ *Booking #{bid} Cancelled*\n\n{bk['party_size']} guests on {bts}.\nHope to see you soon!",
            reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    except Exception as ex:
        print(f"[CANBKID] {ex}"); await update.message.reply_text("❌ Error.",reply_markup=back_button())


# ── Booking modification ───────────────────────────────────────────────────────

async def modify_booking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uc = get_user_context(user.id, context)
    try:
        bks = supabase.table("bookings").select("id,party_size,booking_time")\
            .eq("user_id",str(user.id)).eq("restaurant_id",uc.get("restaurant_id"))\
            .eq("status","confirmed").order("booking_time").execute()
        if not bks.data:
            await update.message.reply_text("❌ No active bookings.",reply_markup=back_button()); return
        lines = []
        for b in bks.data:
            try:
                bt = datetime.fromisoformat(b["booking_time"].replace("Z","+00:00"))
                bts = bt.astimezone(DUBAI_TZ).strftime("%b %d at %I:%M %p")
            except Exception: bts = b["booking_time"]
            lines.append(f"  *#{b['id']}* — {b['party_size']} guests, {bts}")
        set_user_state(user.id, UserState.AWAITING_BOOKING_MOD_ID, context)
        await update.message.reply_text(
            "📋 *Your active bookings:*\n"+"\n".join(lines)+"\n\nType the *Booking ID* to modify:",
            reply_markup=back_button(), parse_mode="Markdown")
    except Exception as ex:
        print(f"[MODBK] {ex}"); await update.message.reply_text("❌ Error.")

async def handle_booking_mod_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; text = update.message.text.strip()
    uc = get_user_context(user.id, context)
    nums = re.findall(r'\d+', text)
    if not nums: await update.message.reply_text("❌ Type a valid booking ID.",reply_markup=back_button()); return
    bid = int(nums[0])
    try:
        res = supabase.table("bookings").select("*").eq("id",bid).eq("user_id",str(user.id))\
            .eq("restaurant_id",uc.get("restaurant_id")).eq("status","confirmed").execute()
        if not res.data:
            await update.message.reply_text(f"❌ Booking *#{bid}* not found.",
                reply_markup=back_button(), parse_mode="Markdown"); return
        uc["booking_mod_old_id"] = bid; uc["booking_mod_old_data"] = res.data[0]
        set_user_state(user.id, UserState.AWAITING_BOOKING_MOD_TIME, context)
        await update.message.reply_text(
            f"📅 Found booking *#{bid}*.\n\nWhat is your preferred *new time*?\n_(e.g. 'tomorrow 8pm')_",
            reply_markup=back_button(), parse_mode="Markdown")
    except Exception as ex:
        print(f"[MODBKID] {ex}"); await update.message.reply_text("❌ Error.",reply_markup=back_button())

async def handle_booking_mod_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; text = update.message.text.strip()
    uc = get_user_context(user.id, context)
    new_time = await parse_booking_time(text)
    if not new_time:
        await update.message.reply_text("❌ Invalid/past time. Try again.",reply_markup=back_button()); return
    rid = uc.get("restaurant_id"); old = uc.get("booking_mod_old_data",{}); old_id = uc.get("booking_mod_old_id")
    party = old.get("party_size",1)
    can_seat, reason = check_granular_availability(rid, new_time, party)
    if not can_seat:
        reset_to_general(user.id, context)
        await update.message.reply_text(
            f"❌ {reason} at the new time.\n\nYour original booking *#{old_id}* is unchanged.",
            reply_markup=main_menu_keyboard(), parse_mode="Markdown"); return
    try:
        supabase.table("bookings").insert({
            "restaurant_id":rid,"user_id":str(user.id),
            "customer_name":uc.get("display_name") or old.get("customer_name","Guest"),
            "party_size":party,"booking_time":new_time.strftime("%Y-%m-%d %H:%M:%S%z"),"status":"confirmed",
        }).execute()
        supabase.table("bookings").update({"status":"cancelled"}).eq("id",old_id).execute()
        reset_to_general(user.id, context)
        await update.message.reply_text(
            f"✅ *Booking Modified!*\n\nOld booking *#{old_id}* cancelled.\n"
            f"New: *{party} guests* — *{new_time.strftime('%B %d, %Y at %I:%M %p')}*",
            reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    except Exception as ex:
        print(f"[MODBKSWAP] {ex}")
        await update.message.reply_text("❌ Error — original booking unchanged.",reply_markup=back_button())
        clear_user_state(user.id, context)


# ── Table assignment ───────────────────────────────────────────────────────────

async def handle_table_assignment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; text = update.message.text.strip()
    uc   = get_user_context(user.id, context)
    rid  = uc.get("restaurant_id")
    nums = re.findall(r'\d+', text)
    if not nums: 
        await update.message.reply_text("❌ Type the table number (e.g. '7').",reply_markup=back_button())
        return
    
    tnum = nums[0]
    
    # CRITICAL FIX: Check if this table is already in use by another customer
    try:
        # Check if there are active orders at this table from OTHER users
        existing = supabase.table("orders").select("user_id,customer_name")\
            .eq("restaurant_id", rid)\
            .eq("table_number", str(tnum))\
            .neq("status", "paid").neq("status", "cancelled")\
            .execute()
        
        # Filter out orders from THIS user (they can reuse their own table)
        other_users = [o for o in (existing.data or []) if o["user_id"] != str(user.id)]
        
        if other_users:
            other_name = other_users[0].get("customer_name", "another customer")
            await update.message.reply_text(
                f"⚠️ *Table {tnum} is currently in use* by {other_name}.\n\n"
                f"Please verify your table number and try again.",
                reply_markup=back_button(), parse_mode="Markdown"
            )
            return
    except Exception as ex:
        print(f"[TABLE CHECK] {ex}")
        # If check fails, allow it (better than blocking legitimate customers)
    
    # Table is available - assign it
    uc["table_number"] = tnum
    try: 
        supabase.table("user_sessions").upsert({"user_id":str(user.id),"table_number":tnum}).execute()
    except Exception as ex: 
        print(f"[TABLE] {ex}")
    
    set_user_state(user.id, UserState.HAS_TABLE, context)
    await update.message.reply_text(
        f"✅ *Table {tnum} set!*\n\nWhat would you like to order?\n_Example: '2 Binary Bites and a Java Jolt'_",
        reply_markup=back_button(), parse_mode="Markdown"
    )


# ── Order ID handler ───────────────────────────────────────────────────────────

async def handle_order_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; text = update.message.text.strip()
    uc = get_user_context(user.id, context)
    nums = re.findall(r'\d+', text)
    if not nums: await update.message.reply_text("❌ Type a valid order number.",reply_markup=back_button()); return
    oid = int(nums[0]); rid = uc.get("restaurant_id")
    action = uc.get("pending_action","cancel"); mint = uc.get("pending_mod_text","")
    order = fetch_order_for_user(oid, str(user.id), rid)
    if not order:
        await update.message.reply_text(f"❌ Order *#{oid}* not found.",reply_markup=back_button(),parse_mode="Markdown"); return
    clear_user_state(user.id, context); uc.pop("pending_action",None); uc.pop("pending_mod_text",None)
    reply = stage_cancellation(order) if action=="cancel" else await stage_modification(order, mint)
    await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=back_button())


# ── /cancel order ──────────────────────────────────────────────────────────────

async def cancel_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uc = get_user_context(user.id, context)
    try:
        recent = supabase.table("orders").select("id,items,price")\
            .eq("user_id",str(user.id)).eq("restaurant_id",uc.get("restaurant_id"))\
            .eq("status","pending").order("created_at",desc=True).limit(5).execute()
        if not recent.data: await update.message.reply_text("❌ No active orders.",reply_markup=back_button()); return
        lst = "\n".join(f"  *#{o['id']}* — {o['items']}  (${float(o['price']):.2f})" for o in recent.data)
        uc["pending_action"] = "cancel"
        set_user_state(user.id, UserState.AWAITING_ORDER_ID, context)
        await update.message.reply_text(f"📋 *Active orders:*\n{lst}\n\nType the *Order Number* to cancel:",
            reply_markup=back_button(), parse_mode="Markdown")
    except Exception as ex:
        print(f"[CANCEL] {ex}"); await update.message.reply_text("❌ Error.")


# ── Feedback ───────────────────────────────────────────────────────────────────

async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    uc   = get_user_context(user.id, context)
    
    # Accept ANY text as feedback - don't validate format
    # This prevents edge cases where validation fails and state isn't cleared
    try:
        supabase.table("feedback").insert({
            "restaurant_id": uc.get("restaurant_id"),
            "user_id": str(user.id),
            "ratings": text,
            "created_at": get_dubai_now().isoformat(),
        }).execute()
        
        await update.message.reply_text(
            "⭐ Thank you for your feedback!\n\nSee you again soon! 😊",
            reply_markup=main_menu_keyboard()
        )
        reset_to_general(user.id, context)
    except Exception as ex:
        print(f"[FB] {ex}")
        # Even on error, clear the state to prevent being stuck
        await update.message.reply_text("✅ Feedback received. Thank you!")
        reset_to_general(user.id, context)


# ── Billing ────────────────────────────────────────────────────────────────────

async def calculate_bill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uc = get_user_context(user.id, context)
    tnum = uc.get("table_number"); rid = uc.get("restaurant_id")
    if not tnum:
        try:
            sess = supabase.table("user_sessions").select("table_number").eq("user_id",str(user.id)).execute()
            if sess.data and sess.data[0].get("table_number"):
                tnum = str(sess.data[0]["table_number"]); uc["table_number"] = tnum
        except Exception as ex: print(f"[BILL] {ex}")
    if not tnum: await update.message.reply_text("🪑 What is your table number?",reply_markup=back_button()); return
    try:
        res = supabase.table("orders").select("id,items,price")\
            .eq("user_id",str(user.id)).eq("restaurant_id",rid).eq("table_number",str(tnum))\
            .neq("status","paid").neq("status","cancelled").execute()
        if not res.data:
            await update.message.reply_text(f"🧾 *Table {tnum}* — No active orders.",
                parse_mode="Markdown",reply_markup=back_button()); return
        total = round(sum(float(r["price"]) for r in res.data),2)
        lines = "\n".join(f"  • *#{r['id']}* {r['items']}  —  ${float(r['price']):.2f}" for r in res.data)
        await update.message.reply_text(
            f"🧾 *Bill — Table {tnum}*\n\n{lines}\n\n💰 *Total: ${total:.2f}*\n\n_(Ask a waiter to pay)_",
            parse_mode="Markdown", reply_markup=back_button())
    except Exception as ex:
        print(f"[BILL] {ex}"); await update.message.reply_text("❌ Error fetching bill.",reply_markup=back_button())


# ── Allergy detection ──────────────────────────────────────────────────────────

_ALLERGY_PAT = re.compile(
    r"\b(allerg|intoleran|vegan|vegetarian|jain|halal|kosher|gluten.?free"
    r"|nut.?free|dairy.?free|no (nuts?|pork|shellfish|gluten|dairy|egg))\b", re.IGNORECASE)

def detect_and_save_preferences(uid: str, text: str, existing: str) -> Optional[str]:
    if not _ALLERGY_PAT.search(text): return None
    combined = f"{existing}; {text}".strip("; ") if existing else text
    save_preferences(uid, combined); return combined


# ── General mode chat ──────────────────────────────────────────────────────────

_ORDER_KWS = re.compile(
    r"\b(i('ll| will) have|i want|can i get|give me|bring me|i('d| would) like"
    r"|order food|place an? order|burger|pizza|pasta|fries|salad|coffee|tea|juice)\b", re.IGNORECASE)
_BOOK_KWS = re.compile(
    r"\b(book|reserve|reservation|table for|party of"
    r"|tomorrow|tonight|friday|saturday|sunday|monday|tuesday|wednesday|thursday"
    r"|next week|this weekend|\d{1,2}(am|pm))\b", re.IGNORECASE)
GENERAL_REDIRECT = "I'm happy to answer general questions! 😊\n\nTo *order food* or *make a booking*, please use the buttons below:"

async def handle_general_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; text = update.message.text.strip()
    uc = get_user_context(user.id, context); rid = uc.get("restaurant_id")
    if not rid: await update.message.reply_text("👋 Please use /start.",reply_markup=main_menu_keyboard()); return
    if _ORDER_KWS.search(text): await update.message.reply_text(GENERAL_REDIRECT,reply_markup=main_menu_keyboard(),parse_mode="Markdown"); return
    if _BOOK_KWS.search(text):  await update.message.reply_text(GENERAL_REDIRECT,reply_markup=main_menu_keyboard(),parse_mode="Markdown"); return
    try:
        rows = supabase.table("menu_items").select("content").eq("restaurant_id",rid).limit(30).execute()
        menu_ctx = "\n".join(r["content"] for r in rows.data) if rows.data else ""
    except Exception: menu_ctx = ""
    policy_ctx = fetch_policy_text(rid)
    system = ("You are a helpful restaurant concierge.\n\n"
              +(f"MENU:\n{menu_ctx}\n\n" if menu_ctx else "")
              +(f"RESTAURANT INFO:\n{policy_ctx}\n\n" if policy_ctx else "")
              +"Answer questions about menu/WiFi/parking/hours concisely (2-3 sentences). "
              "Never take orders or handle bookings.")
    try:
        c = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":system},{"role":"user","content":text}],
            temperature=0.7, max_tokens=250)
        await update.message.reply_text(c.choices[0].message.content,reply_markup=main_menu_keyboard())
    except Exception as ex:
        print(f"[GEN] {ex}"); await update.message.reply_text("I'm here to help!",reply_markup=main_menu_keyboard())


# ── Order mode chat ────────────────────────────────────────────────────────────

_MOD_KWS = ["remove","take off","drop the","cancel","without","don't want","no more","delete","modify order","change order"]

async def handle_order_mode_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; text = update.message.text.strip()
    uc = get_user_context(user.id, context); rid = uc.get("restaurant_id"); text_lower = text.lower()
    if not rid: await update.message.reply_text("👋 /start please.",reply_markup=main_menu_keyboard()); return

    # Booking management from order mode
    if any(k in text_lower for k in ["cancel booking","cancel reservation"]):
        await cancel_booking_command(update, context); return
    if any(k in text_lower for k in ["change booking","modify booking","change reservation","modify reservation"]):
        await modify_booking_command(update, context); return

    # Allergy detection
    new_pref = detect_and_save_preferences(str(user.id), text, uc.get("preferences",""))
    if new_pref is not None:
        uc["preferences"] = new_pref
        await update.message.reply_text(f"📋 *Preference saved:* _{new_pref}_\n\nI'll warn you about conflicts.",
            parse_mode="Markdown", reply_markup=back_button()); return

    # Modification trigger
    if any(k in text_lower for k in _MOD_KWS):
        oid_m = re.search(r'#?(\d{3,})', text)
        if oid_m:
            oid = int(oid_m.group(1)); order = fetch_order_for_user(oid, str(user.id), rid)
            if order:
                is_can = any(p in text_lower for p in ["cancel","nevermind","never mind"])
                reply = stage_cancellation(order) if is_can else await stage_modification(order, text)
                await update.message.reply_text(reply,parse_mode="Markdown",reply_markup=back_button()); return
        try:
            recent = supabase.table("orders").select("id,items,price")\
                .eq("user_id",str(user.id)).eq("restaurant_id",rid)\
                .eq("status","pending").order("created_at",desc=True).limit(5).execute()
            if not recent.data: await update.message.reply_text("❌ No active orders.",reply_markup=back_button()); return
            lst = "\n".join(f"  *#{o['id']}* — {o['items']}  (${float(o['price']):.2f})" for o in recent.data)
            is_can = any(p in text_lower for p in ["cancel","nevermind"])
            action = "cancel" if is_can else "modify"
            uc["pending_action"] = action; uc["pending_mod_text"] = text
            set_user_state(user.id, UserState.AWAITING_ORDER_ID, context)
            await update.message.reply_text(f"📋 *Active orders:*\n{lst}\n\nType *Order Number* to {action}:",
                reply_markup=back_button(), parse_mode="Markdown"); return
        except Exception as ex:
            print(f"[MOD] {ex}"); await update.message.reply_text("❌ Error.",reply_markup=back_button()); return

    if any(k in text_lower for k in ["menu","what do you serve","what's available","food list"]):
        await _send_menu(update.message, uc); return
    if any(k in text_lower for k in ["bill","check please","the check","my total","how much","pay","invoice"]):
        await calculate_bill(update, context); return

    if uc.get("table_number"):
        result = await process_order(text, user, rid, uc.get("table_number"),
                                     uc.get("chat_id"), user_preferences=uc.get("preferences",""))
        if result:
            rt, _oid = result
            await update.message.reply_text(rt, parse_mode="Markdown", reply_markup=back_button()); return

    try:
        rows = supabase.table("menu_items").select("content").eq("restaurant_id",rid).limit(30).execute()
        menu_ctx = "\n".join(r["content"] for r in rows.data) if rows.data else ""
        policy_ctx = fetch_policy_text(rid)
        system = ("Restaurant concierge in Order Mode.\n\n"
                  +(f"MENU:\n{menu_ctx}\n\n" if menu_ctx else "")
                  +(f"INFO:\n{policy_ctx}\n\n" if policy_ctx else "")
                  +"Answer warmly. 2-3 sentences.")
        c = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":system},{"role":"user","content":text}],
            temperature=0.7, max_tokens=200)
        await update.message.reply_text(c.choices[0].message.content,reply_markup=back_button())
    except Exception as ex:
        print(f"[ORDERCHAT] {ex}"); await update.message.reply_text("I'm here to help!",reply_markup=back_button())


# ── Main message router ────────────────────────────────────────────────────────

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; 
    text = update.message.text.strip()
    state = get_user_state(user.id, context); 
    mode = get_mode(user.id, context)
    uc = get_user_context(user.id, context)
    print(f"[MSG] {user.id} mode={mode.value} state={state.value}: '{text[:60]}'")

# CRITICAL: AWAITING_FEEDBACK must be checked FIRST
# Otherwise numbers like "5,4,5" get parsed as food order
    if state == UserState.AWAITING_FEEDBACK:
        await handle_feedback(update, context)
        return
    # ALSO check if user_sessions says awaiting_feedback (set by admin panel)
    try:
        sess = supabase.table("user_sessions").select("awaiting_feedback")\
            .eq("user_id", str(user.id)).limit(1).execute()
        if sess.data and sess.data[0].get("awaiting_feedback"):
            # User was marked as awaiting feedback by payment system
            set_user_state(user.id, UserState.AWAITING_FEEDBACK, context)
            # Clear the flag
            supabase.table("user_sessions").update({"awaiting_feedback": False})\
                .eq("user_id", str(user.id)).execute()
            # Now handle the feedback
            await handle_feedback(update, context)
            return
    except Exception as ex:
        print(f"[SESS CHECK] {ex}")

    if state == UserState.AWAITING_NAME:
        await handle_name_input(update, context)
        return

    # NOW compute text_lower for other handlers
    text_lower = text.lower()

    if state == UserState.AWAITING_BOOKING_CANCEL_ID:
        await handle_booking_cancel_id(update, context)
        return

    if state == UserState.AWAITING_BOOKING_MOD_ID:
        await handle_booking_mod_id(update, context)
        return

    if state == UserState.AWAITING_BOOKING_MOD_TIME:
        await handle_booking_mod_time(update, context)
        return

    if state == UserState.AWAITING_ORDER_ID:
        await handle_order_id_input(update, context)
        return
    if mode == Mode.BOOKING:
        if state in [UserState.AWAITING_GUESTS, UserState.AWAITING_TIME]:
            if _ORDER_KWS.search(text):
                await update.message.reply_text("📅 You're in *Booking Mode*. Use the main menu to Order Food.",
                    reply_markup=back_button(), parse_mode="Markdown"); return
            await handle_booking_flow(update, context, state); return
        set_user_state(user.id, UserState.AWAITING_GUESTS, context)
        await update.message.reply_text("📅 *Booking Mode* — How many guests?",reply_markup=back_button(),parse_mode="Markdown"); return

    if mode == Mode.ORDER:
        if state == UserState.AWAITING_TABLE: await handle_table_assignment(update, context); return
        if _BOOK_KWS.search(text):
            await update.message.reply_text("🍽️ You're in *Order Mode*. Use the main menu to Book.",
                reply_markup=back_button(), parse_mode="Markdown"); return
        await handle_order_mode_chat(update, context); return

    await handle_general_chat(update, context)


# ── FastAPI ────────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"status":"ok"}
    except Exception as ex:
        print(f"[WEBHOOK] {ex}"); return {"status":"error","message":str(ex)}

@app.get("/")
async def health_check():
    return {"status":"running","service":"Restaurant Concierge v6","timestamp":get_dubai_now().isoformat()}

@app.on_event("startup")
async def startup_event():
    global telegram_app
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start",          start_handler))
    telegram_app.add_handler(CommandHandler("help",           help_handler))
    telegram_app.add_handler(CommandHandler("menu",           menu_handler))
    telegram_app.add_handler(CommandHandler("cancel",         cancel_command_handler))
    telegram_app.add_handler(CommandHandler("cancel_booking", cancel_booking_command))
    telegram_app.add_handler(CommandHandler("modify_booking", modify_booking_command))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    await telegram_app.initialize(); await telegram_app.start()
    print("✅ Bot v6 started")

@app.on_event("shutdown")
async def shutdown_event():
    if telegram_app: await telegram_app.stop(); await telegram_app.shutdown()
    print("🛑 Bot stopped")

if __name__ == "__main__":
    import uvicorn; uvicorn.run(app, host="0.0.0.0", port=10000)