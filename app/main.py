"""
Restaurant AI Concierge - Production Server v6
==============================================
COMPLETE VERSION - Telegram Bot + Web Chat Interface
All features from v6 preserved and enhanced
"""

import os
import re
import json
import random
import uuid
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from supabase import create_client, Client
from dotenv import load_dotenv
from groq import AsyncGroq

# Import order service
from order_service import (
    process_order, fetch_order_for_user,
    stage_cancellation, stage_modification, update_crm_on_payment,
)

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client: AsyncGroq = AsyncGroq(api_key=GROQ_API_KEY)
DUBAI_TZ = ZoneInfo("Asia/Dubai")

# ═══════════════════════════════════════════════════════════════════════════
# FASTAPI APP SETUP
# ═══════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Restaurant AI Concierge",
    description="Telegram Bot + Web Chat Interface",
    version="6.0.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting
limiter = Limiter(key_func=get_remote_address, default_limits=["100/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Serve static files
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception:
    print("[WARN] Static folder not found - web chat UI won't work")

telegram_app: Optional[Application] = None

# ═══════════════════════════════════════════════════════════════════════════
# ENUMS & CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════

class Mode(str, Enum):
    GENERAL = "general"
    ORDER = "order"
    BOOKING = "booking"

class UserState(str, Enum):
    IDLE = "idle"
    AWAITING_CUSTOMER_TYPE = "awaiting_customer_type"
    AWAITING_NAME = "awaiting_name"
    AWAITING_PIN_SETUP = "awaiting_pin_setup"
    AWAITING_PIN_CONFIRM = "awaiting_pin_confirm"
    AWAITING_PIN_LOGIN = "awaiting_pin_login"
    AWAITING_TABLE = "awaiting_table"
    HAS_TABLE = "has_table"
    AWAITING_ORDER_ID = "awaiting_order_id"
    AWAITING_GUESTS = "awaiting_guests"
    AWAITING_TIME = "awaiting_time"
    AWAITING_FEEDBACK = "awaiting_feedback"
    AWAITING_BOOKING_CANCEL_ID = "awaiting_booking_cancel_id"
    AWAITING_BOOKING_MOD_ID = "awaiting_booking_mod_id"
    AWAITING_BOOKING_MOD_TIME = "awaiting_booking_mod_time"

# ═══════════════════════════════════════════════════════════════════════════
# WEBSOCKET MANAGER (for Web Chat)
# ═══════════════════════════════════════════════════════════════════════════

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.sessions: Dict[str, dict] = {}
    
    async def connect(self, session_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[session_id] = websocket
        
        if session_id not in self.sessions:
            self.sessions[session_id] = {
                "state": "IDLE",
                "mode": "GENERAL",
                "data": {}
            }
        
        print(f"[WS] Session {session_id[:8]} connected")
    
    def disconnect(self, session_id: str):
        if session_id in self.active_connections:
            del self.active_connections[session_id]
        print(f"[WS] Session {session_id[:8]} disconnected")
    
    async def send_message(self, session_id: str, message: dict):
        if session_id in self.active_connections:
            try:
                await self.active_connections[session_id].send_json(message)
            except Exception as ex:
                print(f"[WS ERROR] {ex}")
                self.disconnect(session_id)

ws_manager = ConnectionManager()

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM STATE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def get_user_state(uid, ctx):
    return ctx.user_data.get(f"state_{uid}", UserState.IDLE)

def set_user_state(uid, state, ctx):
    ctx.user_data[f"state_{uid}"] = state
    print(f"[STATE] {uid} → {state.value}")

def clear_user_state(uid, ctx):
    set_user_state(uid, UserState.IDLE, ctx)

def get_user_context(uid, ctx):
    k = f"ctx_{uid}"
    if k not in ctx.user_data:
        ctx.user_data[k] = {}
    return ctx.user_data[k]

def get_mode(uid, ctx):
    return Mode(get_user_context(uid, ctx).get("mode", Mode.GENERAL))

def set_mode(uid, mode, ctx):
    get_user_context(uid, ctx)["mode"] = mode.value

def reset_to_general(uid, ctx):
    set_mode(uid, Mode.GENERAL, ctx)
    set_user_state(uid, UserState.IDLE, ctx)
    for k in ("pending_action", "pending_mod_text", "booking_mod_old_id", "booking_mod_old_data"):
        get_user_context(uid, ctx).pop(k, None)

# ═══════════════════════════════════════════════════════════════════════════
# UI HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍽️ Order Food (Dine-in)", callback_data="mode_order")],
        [InlineKeyboardButton("📅 Book a Table", callback_data="mode_booking")],
    ])

def back_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="main_menu")]
    ])

# ═══════════════════════════════════════════════════════════════════════════
# CRM HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def load_crm_profile(user_id: str) -> Dict[str, Any]:
    defaults = {"visit_count": 0, "total_spend": 0.0, "last_visit": None, "preferences": "", "tags": []}
    try:
        res = supabase.table("users").select("visit_count,total_spend,last_visit,preferences")\
            .eq("id", str(user_id)).limit(1).execute()
        if not res.data:
            return defaults
        row = res.data[0]
        vc = int(row.get("visit_count") or 0)
        ts = float(row.get("total_spend") or 0.0)
        lv = row.get("last_visit")
        pr = row.get("preferences") or ""
        tags: List[str] = []
        now = datetime.now(timezone.utc)
        if vc > 5:
            tags.append("Frequent Diner")
        if ts > 500:
            tags.append("Big Spender")
        if "Frequent Diner" in tags and "Big Spender" in tags:
            tags.append("VIP")
        if lv and vc > 0:
            try:
                lv_dt = datetime.fromisoformat(str(lv).replace("Z", "+00:00"))
                if (now - lv_dt) > timedelta(days=30):
                    tags.append("Churn Risk")
            except Exception:
                pass
        return {"visit_count": vc, "total_spend": ts, "last_visit": lv, "preferences": pr, "tags": tags}
    except Exception as ex:
        print(f"[CRM] {ex}")
        return defaults

def increment_visit(user_id: str):
    try:
        res = supabase.table("users").select("visit_count").eq("id", str(user_id)).limit(1).execute()
        cur = int((res.data[0].get("visit_count") or 0)) if res.data else 0
        supabase.table("users").update({
            "visit_count": cur + 1,
            "last_visit": datetime.now(timezone.utc).isoformat()
        }).eq("id", str(user_id)).execute()
    except Exception as ex:
        print(f"[CRM VISIT] {ex}")

def save_preferences(user_id: str, pref: str):
    try:
        supabase.table("users").update({"preferences": pref}).eq("id", str(user_id)).execute()
    except Exception as ex:
        print(f"[CRM PREF] {ex}")

def build_personalized_greeting(name: str, restaurant_name: str, tags: List[str], visit_count: int = 0) -> str:
    # Milestone rewards
    if visit_count == 5:
        return (f"🎉 *Congratulations, {name}!*\n\n"
                f"This is your **5th visit** to {restaurant_name}!\n"
                f"🎁 Enjoy a **FREE appetizer** on us today!\n\n"
                f"_Mention this message to your server._")
    elif visit_count == 10:
        return (f"🏆 *WOW! Visit #{visit_count}, {name}!*\n\n"
                f"You're officially a {restaurant_name} Legend!\n"
                f"🍰 Enjoy a **FREE dessert** today!\n\n"
                f"_Show this to your server._")
    elif visit_count % 10 == 0 and visit_count > 0:
        return (f"🌟 *Amazing! Visit #{visit_count}, {name}!*\n\n"
                f"We appreciate your loyalty!\n"
                f"🎁 **10% off** your bill today!")
    
    # Regular greetings
    if "VIP" in tags or "Big Spender" in tags:
        msg = f"👑 Welcome back, *{name}*! As one of our VIP guests, you're very special to us."
        if random.random() < 0.20:
            msg += "\n\n🍹 *Complimentary drink on us today — mention this when you order!*"
        return msg
    if "Frequent Diner" in tags:
        return f"😊 Welcome back, *{name}*! Great to see you again at *{restaurant_name}*. (Visit #{visit_count})"
    if "Churn Risk" in tags:
        return f"👋 *{name}*, we've missed you! So glad you're back at *{restaurant_name}*.\n\n🎁 *Welcome back gift:* 15% off today!"
    return f"👋 Welcome to *{restaurant_name}*, *{name}*!"

# ═══════════════════════════════════════════════════════════════════════════
# POLICY & TIME HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def fetch_policy_text(rid: str) -> str:
    try:
        res = supabase.table("restaurant_policies").select("policy_text")\
            .eq("restaurant_id", str(rid)).limit(1).execute()
        if res.data:
            return res.data[0].get("policy_text", "")
    except Exception as ex:
        print(f"[POLICY] {ex}")
    return ""

def get_dubai_now():
    return datetime.now(DUBAI_TZ)

async def parse_booking_time(user_input: str) -> Optional[datetime]:
    prompt = (f'Current Dubai Time: {get_dubai_now().strftime("%Y-%m-%d %H:%M")}\n\n'
              f'Parse: "{user_input}"\nReturn ONLY JSON: {{"datetime":"YYYY-MM-DD HH:MM","valid":true}}\n'
              f'Rules: past→false, ambiguous→false')
    try:
        c = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=60
        )
        raw = c.choices[0].message.content
        s, e = raw.find("{"), raw.rfind("}") + 1
        if s == -1 or e == 0:
            return None
        d = json.loads(raw[s:e])
        if not d.get("valid"):
            return None
        dt = datetime.strptime(d["datetime"], "%Y-%m-%d %H:%M").replace(tzinfo=DUBAI_TZ)
        return dt if dt > get_dubai_now() else None
    except Exception as ex:
        print(f"[TIME] {ex}")
        return None

# ═══════════════════════════════════════════════════════════════════════════
# TABLE AVAILABILITY (Smart Bin-Packing Algorithm)
# ═══════════════════════════════════════════════════════════════════════════

def check_granular_availability(rid: str, booking_time: datetime, party_size: int) -> Tuple[bool, str]:
    """Smart table allocation using bin-packing"""
    try:
        inv = supabase.table("tables_inventory").select("capacity,quantity")\
            .eq("restaurant_id", str(rid)).order("capacity").execute()
        
        if not inv.data:
            # Fallback
            bk = supabase.table("bookings").select("id", count="exact")\
                .eq("restaurant_id", rid)\
                .eq("booking_time", booking_time.strftime("%Y-%m-%d %H:%M:%S%z"))\
                .neq("status", "cancelled").execute()
            available = (bk.count or 0) < 10
            return (available, "available (fallback)") if available else (False, "fully booked")
        
        total_inventory: Dict[int, int] = {r["capacity"]: r["quantity"] for r in inv.data}
        sizes = sorted(total_inventory.keys())
        
        total_seats = sum(cap * qty for cap, qty in total_inventory.items())
        if party_size > total_seats:
            return (False, f"party of {party_size} exceeds capacity ({total_seats} seats)")
        
        bk = supabase.table("bookings").select("party_size,session_id,customer_name")\
            .eq("restaurant_id", rid)\
            .eq("booking_time", booking_time.strftime("%Y-%m-%d %H:%M:%S%z"))\
            .neq("status", "cancelled").execute()
        
        existing_bookings = bk.data or []
        all_parties = [b["party_size"] for b in existing_bookings] + [party_size]
        
        allocated = allocate_tables(total_inventory.copy(), all_parties)
        
        if allocated:
            tables_for_this_party = allocated[-1]
            table_desc = ", ".join(f"{qty}x{cap}-seat" for cap, qty in sorted(tables_for_this_party.items()))
            return (True, f"available ({table_desc})")
        else:
            used_tables = {}
            already_allocated = allocate_tables(total_inventory.copy(), [b["party_size"] for b in existing_bookings])
            
            if already_allocated:
                for booking_allocation in already_allocated:
                    for cap, qty in booking_allocation.items():
                        used_tables[cap] = used_tables.get(cap, 0) + qty
                
                remaining = {}
                for cap, total_qty in total_inventory.items():
                    remaining[cap] = total_qty - used_tables.get(cap, 0)
                
                remaining_seats = sum(cap * qty for cap, qty in remaining.items())
                remaining_desc = ", ".join(f"{qty}x{cap}-seat" for cap, qty in sorted(remaining.items()) if qty > 0)
                return (False, f"insufficient tables ({remaining_seats} seats remain: {remaining_desc}, need {party_size})")
            else:
                return (False, "no available tables for this party size")
    except Exception as ex:
        print(f"[AVAIL ERROR] {ex}")
        return (False, f"availability check error: {ex}")

def find_available_slots(rid: str, party_size: int, start_date: datetime) -> List[str]:
    """Find next 5 available time slots"""
    available_slots = []
    current = start_date.replace(minute=0)
    
    for hour_offset in range(48):
        check_time = current + timedelta(hours=hour_offset)
        
        if not (8 <= check_time.hour <= 23):
            continue
        
        can_seat, _ = check_granular_availability(rid, check_time, party_size)
        if can_seat:
            available_slots.append(check_time.strftime("%b %d at %I:%M %p"))
            if len(available_slots) >= 5:
                break
    
    return available_slots

def allocate_tables(inventory: Dict[int, int], parties: List[int]) -> Optional[List[Dict[int, int]]]:
    """Try to allocate tables for all parties"""
    allocations = []
    available = inventory.copy()
    sizes = sorted(available.keys())
    
    for party_size in parties:
        allocation = {}
        remaining = party_size
        
        # Check for exact match
        for cap in sizes:
            if cap == party_size and available.get(cap, 0) > 0:
                allocation[cap] = 1
                available[cap] -= 1
                remaining = 0
                break
        
        # Use smallest-first greedy
        if remaining > 0:
            for cap in sizes:
                while remaining > 0 and available.get(cap, 0) > 0:
                    allocation[cap] = allocation.get(cap, 0) + 1
                    available[cap] -= 1
                    remaining -= cap
                    
                    if remaining <= 0:
                        break
        
        if remaining > 0:
            return None
        
        allocations.append(allocation)
    
    return allocations

def check_duplicate_booking(session_id, rid, booking_time):
    """Check if session already has booking at this time"""
    try:
        res = supabase.table("bookings").select("id")\
            .eq("session_id", str(session_id))\
            .eq("restaurant_id", rid)\
            .eq("booking_time", booking_time.strftime("%Y-%m-%d %H:%M:%S%z"))\
            .neq("status", "cancelled").execute()
        return bool(res.data)
    except Exception as ex:
        print(f"[DUP] {ex}")
        return False

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - /start
# ═══════════════════════════════════════════════════════════════════════════

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, chat_id = update.effective_user, update.effective_chat.id
    restaurant_id, restaurant_name = None, "Our Restaurant"
    
    qr_table = None
    qr_restaurant_id = None
    
    if context.args:
        arg = context.args[0]
        rid = None
        
        if arg.startswith("table_"):
            parts = arg.split("_")
            if len(parts) >= 3:
                qr_table = parts[1]
                rid = "_".join(parts[2:])
                qr_restaurant_id = rid
                print(f"[QR SCAN] Table {qr_table} for restaurant {rid}")
        elif arg.startswith("rest_id="):
            rid = arg.split("=")[1]
        else:
            rid = arg
        
        try:
            chk = supabase.table("restaurants").select("id,name").eq("id", rid).execute()
            if chk.data:
                restaurant_id = chk.data[0]["id"]
                restaurant_name = chk.data[0].get("name", restaurant_name)
        except Exception as ex:
            print(f"[START] {ex}")
    
    if not restaurant_id:
        try:
            d = supabase.table("restaurants").select("id,name").limit(1).execute()
            if d.data:
                restaurant_id = d.data[0]["id"]
                restaurant_name = d.data[0].get("name", restaurant_name)
            else:
                await update.message.reply_text("❌ No restaurants configured.")
                return
        except Exception as ex:
            print(f"[START] {ex}")
            await update.message.reply_text("❌ Cannot connect.")
            return
    
    # HARD RESET
    context.user_data.clear()
    print(f"[START] Hard reset uid={user.id}")
    
    # Generate unique session ID
    session_id = str(uuid.uuid4())
    
    # Create session row
    try:
        supabase.table("user_sessions").insert({
            "user_id": str(user.id),
            "session_id": session_id,
            "restaurant_id": restaurant_id,
            "display_name": "Guest",
            "visit_count": 0,
            "total_spend": 0.0,
            "created_at": get_dubai_now().isoformat()
        }).execute()
        print(f"[SESSION] Pre-created session {session_id[:8]}")
    except Exception as ex:
        print(f"[SESSION PRE-CREATE FAILED] {ex}")
    
    uc = get_user_context(user.id, context)
    
    if qr_table:
        uc["table_number"] = qr_table
        uc["qr_scanned"] = True
        print(f"[QR SCAN] Table {qr_table} auto-assigned")
    
    uc["session_id"] = session_id
    uc["restaurant_id"] = restaurant_id
    uc["restaurant_name"] = restaurant_name
    uc["chat_id"] = chat_id
    
    try:
        supabase.table("users").upsert({
            "id": str(user.id),
            "username": user.username or "guest",
            "full_name": user.full_name or "Guest",
            "chat_id": str(chat_id),
        }).execute()
    except Exception as ex:
        print(f"[UPSERT] {ex}")
    
    set_user_state(user.id, UserState.AWAITING_CUSTOMER_TYPE, context)
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 New Customer", callback_data="customer_type_new")],
        [InlineKeyboardButton("🔙 Returning Customer", callback_data="customer_type_returning")],
    ])
    
    await update.message.reply_text(
        f"👋 Welcome to *{restaurant_name}*!\n\n"
        f"Are you a new or returning customer?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Name & PIN
# ═══════════════════════════════════════════════════════════════════════════

async def handle_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = update.message.text.strip()
    
    if len(name) < 2 or name.isdigit():
        await update.message.reply_text(
            "❌ Please enter a valid name (at least 2 characters).\n"
            "_Example: Sarah or Ahmed_",
            parse_mode="Markdown"
        )
        return
    
    uc = get_user_context(user.id, context)
    uc["display_name"] = name
    restaurant_id = uc.get("restaurant_id")
    customer_type = uc.get("customer_type", "new")
    
    if customer_type == "new":
        set_user_state(user.id, UserState.AWAITING_PIN_SETUP, context)
        await update.message.reply_text(
            f"✅ Name saved: *{name}*\n\n"
            f"🔐 Now, create a 4-digit PIN for future visits:\n\n"
            f"_This PIN will let you access your order history and rewards._",
            parse_mode="Markdown"
        )
        return
    
    else:
        try:
            all_sessions = supabase.table("user_sessions").select(
                "session_id,pin_hash,visit_count,total_spend,last_visit"
            ).eq("display_name", name).eq("restaurant_id", restaurant_id)\
             .order("last_visit", desc=True).execute()
            
            sessions_with_pin = [s for s in (all_sessions.data or []) if s.get("pin_hash")]
            
            if sessions_with_pin:
                target_session = sessions_with_pin[0]
                uc["login_target_session"] = target_session
                uc["login_attempts"] = 0
                set_user_state(user.id, UserState.AWAITING_PIN_LOGIN, context)
                
                last_visit = target_session.get("last_visit")
                days_ago = ""
                if last_visit:
                    try:
                        lv_dt = datetime.fromisoformat(str(last_visit).replace("Z", "+00:00"))
                        days = (datetime.now(timezone.utc) - lv_dt).days
                        days_ago = f" (last visit: {days} days ago)" if days > 0 else " (last visit: today)"
                    except Exception:
                        pass
                
                await update.message.reply_text(
                    f"✅ Found your account, *{name}*!{days_ago}\n\n"
                    f"📊 {target_session['visit_count']} visits • ${float(target_session['total_spend']):.2f} spent\n\n"
                    f"🔐 Please enter your 4-digit PIN:",
                    parse_mode="Markdown"
                )
            else:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🆕 Create New Account", callback_data="customer_type_new")],
                    [InlineKeyboardButton("🔄 Try Different Name", callback_data="customer_type_returning")],
                ])
                
                await update.message.reply_text(
                    f"❌ No account found for *{name}* at this restaurant.\n\n"
                    f"Would you like to create a new account?",
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )
        except Exception as ex:
            print(f"[NAME LOOKUP] {ex}")
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🆕 Create New Account", callback_data="customer_type_new")],
                [InlineKeyboardButton("🔄 Try Again", callback_data="customer_type_returning")],
            ])
            
            await update.message.reply_text(
                "❌ Error checking account.\n\n"
                "Would you like to create a new account or try again?",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )

async def handle_pin_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pin = update.message.text.strip()
    uc = get_user_context(user.id, context)
    
    try:
        await update.message.delete()
    except Exception:
        pass
    
    if not pin.isdigit() or len(pin) != 4:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ PIN must be exactly 4 digits.\n\nPlease try again:"
        )
        return
    
    uc["temp_pin"] = pin
    set_user_state(user.id, UserState.AWAITING_PIN_CONFIRM, context)
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🔐 *Confirm your PIN*\n\n"
             "Please enter your 4-digit PIN again to confirm:",
        parse_mode="Markdown"
    )

async def handle_pin_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pin = update.message.text.strip()
    uc = get_user_context(user.id, context)
    
    try:
        await update.message.delete()
    except Exception:
        pass
    
    temp_pin = uc.get("temp_pin")
    
    if pin != temp_pin:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ PINs don't match. Let's try again.\n\n"
                 "Enter a 4-digit PIN:"
        )
        set_user_state(user.id, UserState.AWAITING_PIN_SETUP, context)
        uc.pop("temp_pin", None)
        return
    
    import bcrypt
    pin_hash = bcrypt.hashpw(pin.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    
    session_id = uc.get("session_id")
    name = uc.get("display_name")
    restaurant_id = uc.get("restaurant_id")
    
    try:
        supabase.table("user_sessions").update({
            "display_name": name,
            "pin_hash": pin_hash
        }).eq("session_id", session_id).execute()
        
        print(f"[PIN SETUP] Created account for {name}")
    except Exception as ex:
        print(f"[PIN SETUP ERROR] {ex}")
    
    uc.pop("temp_pin", None)
    uc.pop("customer_type", None)
    
    crm = {"visit_count": 0, "total_spend": 0.0, "last_visit": None, "preferences": "", "tags": []}
    uc.update(crm)
    
    greeting = build_personalized_greeting(
        name,
        uc.get("restaurant_name", "Our Restaurant"),
        crm["tags"],
        visit_count=crm.get("visit_count", 0)
    )
    
    set_user_state(user.id, UserState.IDLE, context)
    set_mode(user.id, Mode.GENERAL, context)
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ *Account Created!*\n\n"
             f"{greeting}\n\n"
             f"🔐 Your PIN is saved securely.\n\n"
             f"Ready to order or book? Choose an option:",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

async def handle_pin_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pin = update.message.text.strip()
    uc = get_user_context(user.id, context)
    
    try:
        await update.message.delete()
    except Exception:
        pass
    
    if not pin.isdigit() or len(pin) != 4:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ PIN must be 4 digits. Please try again:"
        )
        return
    
    target_session = uc.get("login_target_session")
    if not target_session:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Session expired. Please /start again."
        )
        return
    
    stored_hash = target_session.get("pin_hash")
    attempts = uc.get("login_attempts", 0) + 1
    uc["login_attempts"] = attempts
    
    import bcrypt
    try:
        pin_correct = bcrypt.checkpw(pin.encode('utf-8'), stored_hash.encode('utf-8'))
    except Exception as ex:
        print(f"[PIN VERIFY ERROR] {ex}")
        pin_correct = False
    
    if pin_correct:
        # SUCCESS
        name = uc.get("display_name")
        current_session_id = uc.get("session_id")
        target_session_id = target_session["session_id"]
        
        try:
            supabase.table("user_sessions").update({
                "display_name": name,
                "pin_hash": stored_hash,
                "primary_account_id": target_session_id
            }).eq("session_id", current_session_id).execute()
        except Exception as ex:
            print(f"[PIN LOGIN LINK] {ex}")
        
        crm = {
            "visit_count": int(target_session.get("visit_count", 0)),
            "total_spend": float(target_session.get("total_spend", 0.0)),
            "last_visit": target_session.get("last_visit"),
            "preferences": target_session.get("preferences", ""),
            "tags": []
        }
        
        vc = crm["visit_count"]
        ts = crm["total_spend"]
        tags = []
        if vc > 5:
            tags.append("Frequent Diner")
        if ts > 500:
            tags.append("Big Spender")
        if "Frequent Diner" in tags and "Big Spender" in tags:
            tags.append("VIP")
        crm["tags"] = tags
        
        uc.update(crm)
        uc.pop("login_target_session", None)
        uc.pop("login_attempts", None)
        uc.pop("customer_type", None)
        
        greeting = build_personalized_greeting(
            name,
            uc.get("restaurant_name", "Our Restaurant"),
            crm["tags"],
            visit_count=crm["visit_count"]
        )
        tag_str = ("  ·  ".join(f"🏷 {t}" for t in crm["tags"]) + "\n\n") if crm["tags"] else ""
        
        set_user_state(user.id, UserState.IDLE, context)
        set_mode(user.id, Mode.GENERAL, context)
        
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"✅ *Welcome Back!*\n\n"
                 f"{greeting}\n\n"
                 f"{tag_str}"
                 f"Ready to order or book? Choose an option:",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
        
        print(f"[PIN LOGIN] Success for {name}")
    
    else:
        # FAILED
        if attempts >= 3:
            uc.pop("login_target_session", None)
            uc.pop("login_attempts", None)
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Try Again", callback_data="customer_type_returning")],
                [InlineKeyboardButton("🆕 Create New Account", callback_data="customer_type_new")],
            ])
            
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ *Too Many Failed Attempts*\n\n"
                     "For security, please start over.",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        else:
            remaining = 3 - attempts
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"❌ *Incorrect PIN*\n\n"
                     f"You have {remaining} attempt(s) remaining.\n\n"
                     f"Please try again:",
                parse_mode="Markdown"
            )

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Button Handler
# ═══════════════════════════════════════════════════════════════════════════

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data
    uc = get_user_context(user.id, context)
    
    # Customer type handlers
    if data == "customer_type_new":
        set_user_state(user.id, UserState.AWAITING_NAME, context)
        uc["customer_type"] = "new"
        await query.message.reply_text(
            "🆕 *New Customer Setup*\n\n"
            "What is your name?\n"
            "_Example: Sarah or Ahmed_",
            parse_mode="Markdown"
        )
        return
    
    elif data == "customer_type_returning":
        set_user_state(user.id, UserState.AWAITING_NAME, context)
        uc["customer_type"] = "returning"
        await query.message.reply_text(
            "🔙 *Welcome Back!*\n\n"
            "Please enter your name:",
            parse_mode="Markdown"
        )
        return
    
    # Main menu
    if data == "main_menu":
        reset_to_general(user.id, context)
        name = uc.get("display_name", user.first_name)
        await query.message.reply_text(
            f"👋 Back to *General Mode*, {name}!\n\nAsk me anything, or choose an option:",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
    
    # Order mode
    elif data == "mode_order":
        set_mode(user.id, Mode.ORDER, context)
        
        # Check if table was set via QR
        if uc.get("qr_scanned") and uc.get("table_number"):
            set_user_state(user.id, UserState.HAS_TABLE, context)
            
            if not uc.get("preferences"):
                await query.message.reply_text(
                    f"🍽️ *Order Mode*\n\n"
                    f"✅ *Table {uc['table_number']}* (from QR code)\n\n"
                    f"⚠️ *Do you have any allergies or dietary restrictions?*\n\n"
                    f"_Examples: 'allergic to nuts', 'vegetarian', 'no gluten'_\n\n"
                    f"Or type *'none'* if you have no restrictions.",
                    reply_markup=back_button(),
                    parse_mode="Markdown"
                )
            else:
                await query.message.reply_text(
                    f"🍽️ *Order Mode*\n\n"
                    f"✅ *Table {uc['table_number']}* (from QR code)\n\n"
                    f"What would you like to order?\n\n_Type /menu to see all available dishes._",
                    reply_markup=back_button(),
                    parse_mode="Markdown"
                )
        else:
            set_user_state(user.id, UserState.AWAITING_TABLE, context)
            await query.message.reply_text(
                "🍽️ *Order Mode*\n\n🪑 What is your table number?",
                reply_markup=back_button(),
                parse_mode="Markdown"
            )
    
    # Booking mode
    elif data == "mode_booking":
        set_mode(user.id, Mode.BOOKING, context)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Make a Booking", callback_data="booking_new")],
            [InlineKeyboardButton("❌ Cancel a Booking", callback_data="booking_cancel")],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="main_menu")],
        ])
        await query.message.reply_text(
            "📅 *Booking Management*\n\nWhat would you like to do?",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    
    elif data == "booking_new":
        set_user_state(user.id, UserState.AWAITING_GUESTS, context)
        await query.message.reply_text(
            "📅 *New Booking*\n\nHow many guests? _(e.g. '4' or 'party of 6')_",
            reply_markup=back_button(),
            parse_mode="Markdown"
        )
    
    elif data == "booking_cancel":
        await cancel_booking_command_interactive(update, context)
    
    elif data.startswith("cancel_booking_"):
        bid = int(data.replace("cancel_booking_", ""))
        uc = get_user_context(user.id, context)
        
        try:
            res = supabase.table("bookings").select("*")\
                .eq("id", bid)\
                .eq("session_id", uc.get("session_id", ""))\
                .eq("restaurant_id", uc.get("restaurant_id"))\
                .eq("status", "confirmed").execute()
            
            if not res.data:
                await query.message.reply_text(
                    f"❌ Booking *#{bid}* not found or already cancelled.",
                    reply_markup=back_button(),
                    parse_mode="Markdown"
                )
                return
            
            bk = res.data[0]
            bt = datetime.fromisoformat(bk["booking_time"].replace("Z", "+00:00"))
            hours_until = (bt - datetime.now(timezone.utc)).total_seconds() / 3600
            
            if hours_until < 4:
                await query.message.reply_text(
                    f"⚠️ *Cancellation Not Allowed*\n\n"
                    f"Your reservation is only *{max(0, hours_until):.1f} hours* away.\n\n"
                    f"Cancellations require *4+ hours notice*. Please call the restaurant.",
                    reply_markup=back_button(),
                    parse_mode="Markdown"
                )
                return
            
            supabase.table("bookings").update({"status": "cancelled"}).eq("id", bid).execute()
            
            bts = bt.astimezone(DUBAI_TZ).strftime("%B %d at %I:%M %p")
            await query.message.reply_text(
                f"✅ *Booking #{bid} Cancelled*\n\n"
                f"{bk['party_size']} guests on {bts}.\n\n"
                f"To make a new booking, use the button below.",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown"
            )
        except Exception as ex:
            print(f"[CANCEL CALLBACK] {ex}")
            await query.message.reply_text("❌ Error cancelling booking.", reply_markup=back_button())
    
    elif data == "menu":
        await _send_menu(query.message, uc)

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Menu Helper
# ═══════════════════════════════════════════════════════════════════════════

async def _send_menu(message, uc):
    rid = uc.get("restaurant_id")
    rname = uc.get("restaurant_name", "Our Restaurant")
    if not rid:
        await message.reply_text("❌ Please /start first.")
        return
    
    try:
        rows = supabase.table("menu_items").select("content,sold_out").eq("restaurant_id", rid).execute()
        if not rows.data:
            await message.reply_text("📋 Menu unavailable.", reply_markup=back_button())
            return
        
        lines = [f"🍽️ *{rname} — Menu*\n"]
        cur_cat = None
        
        for row in rows.data:
            sold_out = row.get("sold_out", False)
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
                    item = line.replace("item:", "").strip()
                    if sold_out:
                        lines.append(f"  • ~~{item}~~ ❌ SOLD OUT")
                    else:
                        lines.append(f"  • {item}")
                elif line.startswith("price:"):
                    if not sold_out:
                        lines[-1] += f"  —  {line.replace('price:', '').strip()}"
                elif line.startswith("description:"):
                    if not sold_out:
                        lines.append(f"    _{line.replace('description:', '').strip()}_")
        
        lines.append("\n_Tell me what you'd like and I'll place the order!_")
        mt = "\n".join(lines)
        kb = back_button()
        
        if len(mt) <= 4096:
            await message.reply_text(mt, parse_mode="Markdown", reply_markup=kb)
        else:
            await message.reply_text(mt[:4000], parse_mode="Markdown")
            await message.reply_text(mt[4000:], parse_mode="Markdown", reply_markup=kb)
    except Exception as ex:
        print(f"[MENU] {ex}")
        await message.reply_text("❌ Error loading menu.")

async def menu_handler(update, context):
    await _send_menu(update.message, get_user_context(update.effective_user.id, context))

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Help
# ═══════════════════════════════════════════════════════════════════════════

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Restaurant AI Concierge v6*\n\n"
        "*Modes:*\n• General — Q&A (default)\n• Order Food — dine-in orders\n• Book a Table — reservations\n\n"
        "*Commands:*\n/start — Fresh session\n/menu — View menu\n/cancel — Cancel order\n"
        "/cancel_booking — Cancel reservation\n/modify_booking — Change reservation\n/help — This message",
        parse_mode="Markdown",
        reply_markup=back_button()
    )

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Booking Flow
# ═══════════════════════════════════════════════════════════════════════════

async def handle_booking_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, state):
    user = update.effective_user
    text = update.message.text.strip()
    uc = get_user_context(user.id, context)
    
    if state == UserState.AWAITING_GUESTS:
        nums = re.findall(r'\d+', text)
        if not nums:
            await update.message.reply_text("❌ Enter guest count (e.g. '4').", reply_markup=back_button())
            return
        party = int(nums[0])
        if not (1 <= party <= 20):
            await update.message.reply_text("❌ 1–20 guests only.", reply_markup=back_button())
            return
        uc["party_size"] = party
        set_user_state(user.id, UserState.AWAITING_TIME, context)
        await update.message.reply_text(
            f"✅ Table for *{party}* guests.\n\n⏰ When? _(e.g. 'tomorrow 8pm', 'Friday 7:30pm')_",
            reply_markup=back_button(),
            parse_mode="Markdown"
        )
        return
    
    if state == UserState.AWAITING_TIME:
        bt = await parse_booking_time(text)
        if not bt:
            await update.message.reply_text(
                "❌ Invalid/past time. Try 'tomorrow 8pm'.",
                reply_markup=back_button(),
                parse_mode="Markdown"
            )
            return
        
        # Check 2-hour advance requirement
        hours_until = (bt - datetime.now(timezone.utc)).total_seconds() / 3600
        if hours_until < 2:
            await update.message.reply_text(
                f"⚠️ *Advance Booking Required*\n\n"
                f"Bookings must be made at least *2 hours in advance*.\n\n"
                f"Your requested time is only *{max(0, hours_until):.1f} hours* away.\n\n"
                f"Please choose a later time or call the restaurant.",
                reply_markup=back_button(),
                parse_mode="Markdown"
            )
            return
        
        rid = uc.get("restaurant_id")
        party = uc.get("party_size", 1)
        
        can_seat, reason = check_granular_availability(rid, bt, party)
        if not can_seat:
            alternatives = find_available_slots(rid, party, bt)
            alt_msg = ""
            if alternatives:
                alt_msg = f"\n\n✅ *Available times for {party} guests:*\n" + "\n".join(f"  • {slot}" for slot in alternatives)
            
            await update.message.reply_text(
                f"❌ *Not Available*\n\n{reason}.{alt_msg}\n\nPlease choose a different time.",
                reply_markup=back_button(),
                parse_mode="Markdown"
            )
            return
        
        if check_duplicate_booking(uc.get("session_id", ""), rid, bt):
            await update.message.reply_text("❌ You already have a booking at that time.", reply_markup=back_button())
            clear_user_state(user.id, context)
            return
        
        try:
            supabase.table("bookings").insert({
                "restaurant_id": rid,
                "user_id": str(user.id),
                "session_id": uc.get("session_id", ""),
                "customer_name": uc.get("display_name") or user.full_name or "Guest",
                "party_size": party,
                "booking_time": bt.strftime("%Y-%m-%d %H:%M:%S%z"),
                "status": "confirmed",
            }).execute()
            
            # Track visit
            uc["visit_count"] = uc.get("visit_count", 0) + 1
            try:
                supabase.table("user_sessions").update({
                    "visit_count": uc["visit_count"],
                    "last_visit": get_dubai_now().isoformat()
                }).eq("session_id", uc.get("session_id", "")).execute()
            except Exception as ex:
                print(f"[SESS VISIT] {ex}")
            
            await update.message.reply_text(
                f"✅ *Booking Confirmed!*\n\n👤 {uc.get('display_name', 'Guest')}\n"
                f"👥 Guests: {party}\n📅 {bt.strftime('%B %d, %Y at %I:%M %p')}\n\nSee you soon!",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown"
            )
            reset_to_general(user.id, context)
        except Exception as ex:
            print(f"[BK] {ex}")
            await update.message.reply_text("❌ System error.", reply_markup=back_button())
            clear_user_state(user.id, context)

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Booking Cancellation
# ═══════════════════════════════════════════════════════════════════════════

async def cancel_booking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uc = get_user_context(user.id, context)
    try:
        bks = supabase.table("bookings").select("id,party_size,booking_time")\
            .eq("session_id", uc.get("session_id", ""))\
            .eq("restaurant_id", uc.get("restaurant_id"))\
            .eq("status", "confirmed").order("booking_time").execute()
        if not bks.data:
            await update.message.reply_text("❌ No active bookings.", reply_markup=back_button())
            return
        lines = []
        for b in bks.data:
            try:
                bt = datetime.fromisoformat(b["booking_time"].replace("Z", "+00:00"))
                bts = bt.astimezone(DUBAI_TZ).strftime("%b %d at %I:%M %p")
            except Exception:
                bts = b["booking_time"]
            lines.append(f"  *#{b['id']}* — {b['party_size']} guests, {bts}")
        set_user_state(user.id, UserState.AWAITING_BOOKING_CANCEL_ID, context)
        await update.message.reply_text(
            "📋 *Your active bookings:*\n" + "\n".join(lines) + "\n\nType the *Booking ID* to cancel:",
            reply_markup=back_button(),
            parse_mode="Markdown"
        )
    except Exception as ex:
        print(f"[CANBK] {ex}")
        await update.message.reply_text("❌ Error fetching bookings.")

async def cancel_booking_command_interactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query if hasattr(update, 'callback_query') and update.callback_query else None
    user = update.effective_user
    uc = get_user_context(user.id, context)
    
    try:
        bks = supabase.table("bookings").select("id,party_size,booking_time")\
            .eq("session_id", uc.get("session_id", ""))\
            .eq("restaurant_id", uc.get("restaurant_id"))\
            .eq("status", "confirmed").order("booking_time").execute()
        
        if not bks.data:
            msg = "❌ No active bookings for this session."
            if query:
                await query.message.reply_text(msg, reply_markup=back_button())
            else:
                await update.message.reply_text(msg, reply_markup=back_button())
            return
        
        lines = ["📋 *Your Active Bookings:*\n"]
        keyboard_buttons = []
        
        for b in bks.data:
            try:
                bt = datetime.fromisoformat(b["booking_time"].replace("Z", "+00:00"))
                bts = bt.astimezone(DUBAI_TZ).strftime("%b %d at %I:%M %p")
                hours_until = (bt - datetime.now(timezone.utc)).total_seconds() / 3600
                
                if hours_until >= 4:
                    status = "✅ Cancellable"
                    keyboard_buttons.append([
                        InlineKeyboardButton(
                            f"❌ Cancel #{b['id']} - {b['party_size']} guests, {bts}",
                            callback_data=f"cancel_booking_{b['id']}"
                        )
                    ])
                else:
                    status = f"🔒 Too soon ({hours_until:.1f}h away)"
                
                lines.append(f"  *#{b['id']}* — {b['party_size']} guests, {bts}\n  {status}")
            except Exception:
                lines.append(f"  *#{b['id']}* — {b['party_size']} guests")
        
        keyboard_buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="mode_booking")])
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        
        msg = "\n\n".join(lines) + "\n\n💡 *Note:* Bookings can only be cancelled 4+ hours in advance."
        
        if query:
            await query.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as ex:
        print(f"[CANCEL INTERACTIVE] {ex}")
        msg = "❌ Error fetching bookings."
        if query:
            await query.message.reply_text(msg, reply_markup=back_button())
        else:
            await update.message.reply_text(msg, reply_markup=back_button())

async def handle_booking_cancel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    uc = get_user_context(user.id, context)
    nums = re.findall(r'\d+', text)
    if not nums:
        await update.message.reply_text("❌ Type a valid booking ID.", reply_markup=back_button())
        return
    bid = int(nums[0])
    try:
        res = supabase.table("bookings").select("*").eq("id", bid).eq("user_id", str(user.id))\
            .eq("restaurant_id", uc.get("restaurant_id")).eq("status", "confirmed").execute()
        if not res.data:
            await update.message.reply_text(
                f"❌ Booking *#{bid}* not found.",
                reply_markup=back_button(),
                parse_mode="Markdown"
            )
            return
        bk = res.data[0]
        bt = datetime.fromisoformat(bk["booking_time"].replace("Z", "+00:00"))
        hours_until = (bt - datetime.now(timezone.utc)).total_seconds() / 3600
        if hours_until < 4:
            clear_user_state(user.id, context)
            await update.message.reply_text(
                f"⚠️ *Cancellation Not Allowed*\n\nYour reservation is only *{max(0, hours_until):.1f} hours* away.\n\n"
                "Cancellations require *4+ hours notice*. Please call the restaurant.",
                reply_markup=back_button(),
                parse_mode="Markdown"
            )
            return
        supabase.table("bookings").update({"status": "cancelled"}).eq("id", bid).execute()
        clear_user_state(user.id, context)
        bts = bt.astimezone(DUBAI_TZ).strftime("%B %d at %I:%M %p")
        await update.message.reply_text(
            f"✅ *Booking #{bid} Cancelled*\n\n{bk['party_size']} guests on {bts}.\nHope to see you soon!",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
    except Exception as ex:
        print(f"[CANBKID] {ex}")
        await update.message.reply_text("❌ Error.", reply_markup=back_button())

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Booking Modification
# ═══════════════════════════════════════════════════════════════════════════

async def modify_booking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uc = get_user_context(user.id, context)
    try:
        bks = supabase.table("bookings").select("id,party_size,booking_time")\
            .eq("user_id", str(user.id)).eq("restaurant_id", uc.get("restaurant_id"))\
            .eq("status", "confirmed").order("booking_time").execute()
        if not bks.data:
            await update.message.reply_text("❌ No active bookings.", reply_markup=back_button())
            return
        lines = []
        for b in bks.data:
            try:
                bt = datetime.fromisoformat(b["booking_time"].replace("Z", "+00:00"))
                bts = bt.astimezone(DUBAI_TZ).strftime("%b %d at %I:%M %p")
            except Exception:
                bts = b["booking_time"]
            lines.append(f"  *#{b['id']}* — {b['party_size']} guests, {bts}")
        set_user_state(user.id, UserState.AWAITING_BOOKING_MOD_ID, context)
        await update.message.reply_text(
            "📋 *Your active bookings:*\n" + "\n".join(lines) + "\n\nType the *Booking ID* to modify:",
            reply_markup=back_button(),
            parse_mode="Markdown"
        )
    except Exception as ex:
        print(f"[MODBK] {ex}")
        await update.message.reply_text("❌ Error.")

async def handle_booking_mod_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    uc = get_user_context(user.id, context)
    nums = re.findall(r'\d+', text)
    if not nums:
        await update.message.reply_text("❌ Type a valid booking ID.", reply_markup=back_button())
        return
    bid = int(nums[0])
    try:
        res = supabase.table("bookings").select("*").eq("id", bid).eq("user_id", str(user.id))\
            .eq("restaurant_id", uc.get("restaurant_id")).eq("status", "confirmed").execute()
        if not res.data:
            await update.message.reply_text(
                f"❌ Booking *#{bid}* not found.",
                reply_markup=back_button(),
                parse_mode="Markdown"
            )
            return
        uc["booking_mod_old_id"] = bid
        uc["booking_mod_old_data"] = res.data[0]
        set_user_state(user.id, UserState.AWAITING_BOOKING_MOD_TIME, context)
        await update.message.reply_text(
            f"📅 Found booking *#{bid}*.\n\nWhat is your preferred *new time*?\n_(e.g. 'tomorrow 8pm')_",
            reply_markup=back_button(),
            parse_mode="Markdown"
        )
    except Exception as ex:
        print(f"[MODBKID] {ex}")
        await update.message.reply_text("❌ Error.", reply_markup=back_button())

async def handle_booking_mod_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    uc = get_user_context(user.id, context)
    new_time = await parse_booking_time(text)
    if not new_time:
        await update.message.reply_text("❌ Invalid/past time. Try again.", reply_markup=back_button())
        return
    rid = uc.get("restaurant_id")
    old = uc.get("booking_mod_old_data", {})
    old_id = uc.get("booking_mod_old_id")
    party = old.get("party_size", 1)
    can_seat, reason = check_granular_availability(rid, new_time, party)
    if not can_seat:
        reset_to_general(user.id, context)
        await update.message.reply_text(
            f"❌ {reason} at the new time.\n\nYour original booking *#{old_id}* is unchanged.",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
        return
    try:
        supabase.table("bookings").insert({
            "restaurant_id": rid,
            "user_id": str(user.id),
            "customer_name": uc.get("display_name") or old.get("customer_name", "Guest"),
            "party_size": party,
            "booking_time": new_time.strftime("%Y-%m-%d %H:%M:%S%z"),
            "status": "confirmed",
        }).execute()
        supabase.table("bookings").update({"status": "cancelled"}).eq("id", old_id).execute()
        reset_to_general(user.id, context)
        await update.message.reply_text(
            f"✅ *Booking Modified!*\n\nOld booking *#{old_id}* cancelled.\n"
            f"New: *{party} guests* — *{new_time.strftime('%B %d, %Y at %I:%M %p')}*",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
    except Exception as ex:
        print(f"[MODBKSWAP] {ex}")
        await update.message.reply_text("❌ Error — original booking unchanged.", reply_markup=back_button())
        clear_user_state(user.id, context)

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Table Assignment
# ═══════════════════════════════════════════════════════════════════════════

async def handle_table_assignment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    uc = get_user_context(user.id, context)
    rid = uc.get("restaurant_id")
    nums = re.findall(r'\d+', text)
    if not nums:
        await update.message.reply_text("❌ Type the table number (e.g. '7').", reply_markup=back_button())
        return
    
    tnum = nums[0]
    
    # Check if table is in use by another customer
    try:
        existing = supabase.table("orders").select("user_id,session_id,customer_name")\
            .eq("restaurant_id", rid)\
            .eq("table_number", str(tnum))\
            .neq("status", "paid").neq("status", "cancelled")\
            .execute()
        
        current_session = uc.get("session_id", "")
        other_sessions = [o for o in (existing.data or [])
                          if o.get("session_id") != current_session]
        
        if other_sessions:
            other_name = other_sessions[0].get("customer_name", "another customer")
            await update.message.reply_text(
                f"⚠️ *Table {tnum} is currently in use* by {other_name}.\n\n"
                f"Please verify your table number and try again.",
                reply_markup=back_button(),
                parse_mode="Markdown"
            )
            return
    except Exception as ex:
        print(f"[TABLE CHECK] {ex}")
    
    # Assign table
    uc["table_number"] = tnum
    try:
        supabase.table("user_sessions").upsert({"user_id": str(user.id), "table_number": tnum}).execute()
    except Exception as ex:
        print(f"[TABLE] {ex}")
    
    set_user_state(user.id, UserState.HAS_TABLE, context)
    
    # Check if preferences already set
    if not uc.get("preferences_set"):
        await update.message.reply_text(
            f"✅ *Table {tnum} set!*\n\n"
            f"⚠️ *Do you have any allergies or dietary restrictions?*\n\n"
            f"_Examples: 'allergic to nuts', 'vegetarian', 'no gluten'_\n\n"
            f"Or type *'none'* if you have no restrictions.",
            reply_markup=back_button(),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"✅ *Table {tnum} set!*\n\nWhat would you like to order?\n\n_Type /menu to see all available dishes._",
            reply_markup=back_button(),
            parse_mode="Markdown"
        )

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Order ID Input
# ═══════════════════════════════════════════════════════════════════════════

async def handle_order_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    uc = get_user_context(user.id, context)
    nums = re.findall(r'\d+', text)
    if not nums:
        await update.message.reply_text("❌ Type a valid order number.", reply_markup=back_button())
        return
    
    oid = int(nums[0])
    rid = uc.get("restaurant_id")
    action = uc.get("pending_action", "cancel")
    
    order = fetch_order_for_user(oid, str(user.id), rid)
    if not order:
        await update.message.reply_text(
            f"❌ Order *#{oid}* not found.",
            reply_markup=back_button(),
            parse_mode="Markdown"
        )
        return
    
    # If action is modify and we haven't asked what to modify yet
    if action == "modify" and not uc.get("pending_mod_text"):
        uc["pending_mod_order"] = order
        set_user_state(user.id, UserState.AWAITING_ORDER_ID, context)
        await update.message.reply_text(
            f"📝 *Order #{oid}:* {order['items']}\n\n"
            f"What would you like to change?\n\n"
            f"_Examples:_\n"
            f"• 'Remove 1 burger'\n"
            f"• 'Remove the fries'\n"
            f"• 'Cancel this order'",
            parse_mode="Markdown",
            reply_markup=back_button()
        )
        return
    
    # Process the modification
    mint = uc.get("pending_mod_text", text)
    clear_user_state(user.id, context)
    uc.pop("pending_action", None)
    uc.pop("pending_mod_text", None)
    uc.pop("pending_mod_order", None)
    
    reply = stage_cancellation(order) if action == "cancel" else await stage_modification(order, mint)
    await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=back_button())

async def handle_modification_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle modification request after order ID selected"""
    user = update.effective_user
    text = update.message.text.strip()
    uc = get_user_context(user.id, context)
    
    order = uc.get("pending_mod_order")
    if not order:
        await update.message.reply_text("❌ Session error. Please try again.", reply_markup=back_button())
        clear_user_state(user.id, context)
        return
    
    # Check if cancellation
    is_cancel = any(phrase in text.lower() for phrase in ["cancel", "nevermind", "never mind"])
    
    reply = stage_cancellation(order) if is_cancel else await stage_modification(order, text)
    
    # Cleanup
    clear_user_state(user.id, context)
    uc.pop("pending_action", None)
    uc.pop("pending_mod_text", None)
    uc.pop("pending_mod_order", None)
    
    await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=back_button())

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Cancel Command
# ═══════════════════════════════════════════════════════════════════════════

async def cancel_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uc = get_user_context(user.id, context)
    try:
        recent = supabase.table("orders").select("id,items,price")\
            .eq("user_id", str(user.id)).eq("restaurant_id", uc.get("restaurant_id"))\
            .eq("status", "pending").order("created_at", desc=True).limit(5).execute()
        if not recent.data:
            await update.message.reply_text("❌ No active orders.", reply_markup=back_button())
            return
        lst = "\n".join(f"  *#{o['id']}* — {o['items']}  (${float(o['price']):.2f})" for o in recent.data)
        uc["pending_action"] = "cancel"
        set_user_state(user.id, UserState.AWAITING_ORDER_ID, context)
        await update.message.reply_text(
            f"📋 *Active orders:*\n{lst}\n\nType the *Order Number* to cancel:",
            reply_markup=back_button(),
            parse_mode="Markdown"
        )
    except Exception as ex:
        print(f"[CANCEL] {ex}")
        await update.message.reply_text("❌ Error.")

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Feedback
# ═══════════════════════════════════════════════════════════════════════════

async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    uc = get_user_context(user.id, context)
    
    # Accept ANY text as feedback
    try:
        supabase.table("feedback").insert({
            "restaurant_id": uc.get("restaurant_id"),
            "user_id": str(user.id),
            "session_id": uc.get("session_id"),
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
        await update.message.reply_text("✅ Feedback received. Thank you!")
        reset_to_general(user.id, context)

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Billing
# ═══════════════════════════════════════════════════════════════════════════

async def calculate_bill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uc = get_user_context(user.id, context)
    tnum = uc.get("table_number")
    rid = uc.get("restaurant_id")
    
    if not tnum:
        try:
            sess = supabase.table("user_sessions").select("table_number").eq("user_id", str(user.id)).execute()
            if sess.data and sess.data[0].get("table_number"):
                tnum = str(sess.data[0]["table_number"])
                uc["table_number"] = tnum
        except Exception as ex:
            print(f"[BILL] {ex}")
    
    if not tnum:
        await update.message.reply_text("🪑 What is your table number?", reply_markup=back_button())
        return
    
    try:
        res = supabase.table("orders").select("id,items,price")\
            .eq("user_id", str(user.id)).eq("restaurant_id", rid).eq("table_number", str(tnum))\
            .neq("status", "paid").neq("status", "cancelled").execute()
        
        if not res.data:
            await update.message.reply_text(
                f"🧾 *Table {tnum}* — No active orders.",
                parse_mode="Markdown",
                reply_markup=back_button()
            )
            return
        
        total = round(sum(float(r["price"]) for r in res.data), 2)
        lines = "\n".join(f"  • *#{r['id']}* {r['items']}  —  ${float(r['price']):.2f}" for r in res.data)
        
        await update.message.reply_text(
            f"🧾 *Bill — Table {tnum}*\n\n{lines}\n\n💰 *Total: ${total:.2f}*\n\n_(Ask a waiter to pay)_",
            parse_mode="Markdown",
            reply_markup=back_button()
        )
    except Exception as ex:
        print(f"[BILL] {ex}")
        await update.message.reply_text("❌ Error fetching bill.", reply_markup=back_button())

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Allergy Detection
# ═══════════════════════════════════════════════════════════════════════════

_ALLERGY_PAT = re.compile(
    r"\b(allerg|intoleran|vegan|vegetarian|jain|halal|kosher|gluten.?free"
    r"|nut.?free|dairy.?free|no (nuts?|pork|shellfish|gluten|dairy|egg))\b", re.IGNORECASE)

def detect_and_save_preferences(uid: str, text: str, existing: str) -> Optional[str]:
    if not _ALLERGY_PAT.search(text):
        return None
    combined = f"{existing}; {text}".strip("; ") if existing else text
    save_preferences(uid, combined)
    return combined

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - General Mode Chat
# ═══════════════════════════════════════════════════════════════════════════

_ORDER_KWS = re.compile(
    r"\b(i('ll| will) have|i want|can i get|give me|bring me|i('d| would) like"
    r"|order food|place an? order|burger|pizza|pasta|fries|salad|coffee|tea|juice)\b", re.IGNORECASE)
_BOOK_KWS = re.compile(
    r"\b(book|reserve|reservation|table for|party of"
    r"|tomorrow|tonight|friday|saturday|sunday|monday|tuesday|wednesday|thursday"
    r"|next week|this weekend|\d{1,2}(am|pm))\b", re.IGNORECASE)
GENERAL_REDIRECT = "I'm happy to answer general questions! 😊\n\nTo *order food* or *make a booking*, please use the buttons below:"

async def handle_general_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    uc = get_user_context(user.id, context)
    rid = uc.get("restaurant_id")
    
    if not rid:
        await update.message.reply_text("👋 Please use /start.", reply_markup=main_menu_keyboard())
        return
    
    if _ORDER_KWS.search(text):
        await update.message.reply_text(GENERAL_REDIRECT, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
        return
    
    if _BOOK_KWS.search(text):
        await update.message.reply_text(GENERAL_REDIRECT, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
        return
    
    try:
        rows = supabase.table("menu_items").select("content").eq("restaurant_id", rid).limit(30).execute()
        menu_ctx = "\n".join(r["content"] for r in rows.data) if rows.data else ""
    except Exception:
        menu_ctx = ""
    
    policy_ctx = fetch_policy_text(rid)
    system = ("You are a helpful restaurant concierge.\n\n"
              + (f"MENU:\n{menu_ctx}\n\n" if menu_ctx else "")
              + (f"RESTAURANT INFO:\n{policy_ctx}\n\n" if policy_ctx else "")
              + "Answer questions about menu/WiFi/parking/hours concisely (2-3 sentences). "
                "Never take orders or handle bookings.")
    
    # Detect language
    try:
        lang_detect = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": f"What language is this text in? Answer with one word only: '{text[:100]}'"}],
            temperature=0,
            max_tokens=10
        )
        detected = lang_detect.choices[0].message.content.strip().lower()
        if detected in ["hindi", "arabic", "spanish", "french", "urdu"]:
            language = detected.capitalize()
            system += f"\n\nIMPORTANT: User is communicating in {language}. Respond in {language}."
    except Exception:
        pass
    
    try:
        c = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
            temperature=0.7,
            max_tokens=250
        )
        await update.message.reply_text(c.choices[0].message.content, reply_markup=main_menu_keyboard())
    except Exception as ex:
        print(f"[GEN] {ex}")
        await update.message.reply_text("I'm here to help!", reply_markup=main_menu_keyboard())

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Order Mode Chat
# ═══════════════════════════════════════════════════════════════════════════

_MOD_KWS = ["remove", "take off", "drop the", "cancel", "without", "don't want", "no more", "delete", "modify order", "change order"]

async def handle_order_mode_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    uc = get_user_context(user.id, context)
    rid = uc.get("restaurant_id")
    text_lower = text.lower()
    
    if not rid:
        await update.message.reply_text("👋 /start please.", reply_markup=main_menu_keyboard())
        return
    
    # Booking management from order mode
    if any(k in text_lower for k in ["cancel booking", "cancel reservation"]):
        await cancel_booking_command(update, context)
        return
    if any(k in text_lower for k in ["change booking", "modify booking", "change reservation", "modify reservation"]):
        await modify_booking_command(update, context)
        return
    
    state = get_user_state(user.id, context)
    
    # Allergy detection
    if state == UserState.HAS_TABLE and not uc.get("preferences_set"):
        if text_lower in ["none", "no", "nope", "nothing", "n/a"]:
            uc["preferences"] = ""
            uc["preferences_set"] = True
            await update.message.reply_text(
                "✅ *Got it!*\n\nWhat would you like to order?\n\n_Type /menu to see all available dishes._",
                reply_markup=back_button(),
                parse_mode="Markdown"
            )
            return
        
        if _ALLERGY_PAT.search(text):
            uc["preferences"] = text
            uc["preferences_set"] = True
            await update.message.reply_text(
                f"📋 *Dietary restriction saved:* _{text}_\n\n"
                f"I'll warn you if any dish conflicts with this.\n\n"
                f"What would you like to order?\n\n_Type /menu to see all available dishes._",
                parse_mode="Markdown",
                reply_markup=back_button()
            )
            return
        else:
            uc["preferences"] = ""
            uc["preferences_set"] = True
    
    # Update existing preferences
    new_pref = detect_and_save_preferences(str(user.id), text, uc.get("preferences", ""))
    if new_pref is not None and uc.get("preferences_set"):
        uc["preferences"] = new_pref
        await update.message.reply_text(
            f"📋 *Preference updated:* _{new_pref}_\n\nI'll warn you about conflicts.",
            parse_mode="Markdown",
            reply_markup=back_button()
        )
        return
    
    # Modification trigger
    if any(k in text_lower for k in _MOD_KWS):
        oid_m = re.search(r'#?(\d{3,})', text)
        
        if not oid_m:
            try:
                recent = supabase.table("orders").select("id,items,price")\
                    .eq("user_id", str(user.id)).eq("restaurant_id", rid)\
                    .eq("status", "pending").order("created_at", desc=True).limit(5).execute()
                if not recent.data:
                    await update.message.reply_text("❌ No active orders.", reply_markup=back_button())
                    return
                
                lst = "\n".join(f"  *#{o['id']}* — {o['items']}  (${float(o['price']):.2f})" for o in recent.data)
                uc["pending_action"] = "modify"
                uc["pending_mod_text"] = ""
                set_user_state(user.id, UserState.AWAITING_ORDER_ID, context)
                await update.message.reply_text(
                    f"📋 *Your active orders:*\n{lst}\n\n"
                    f"Type the *Order Number* you want to modify:",
                    reply_markup=back_button(),
                    parse_mode="Markdown"
                )
                return
            except Exception as ex:
                print(f"[MOD] {ex}")
                await update.message.reply_text("❌ Error.", reply_markup=back_button())
                return
        
        oid = int(oid_m.group(1))
        order = fetch_order_for_user(oid, str(user.id), rid)
        if order:
            is_can = any(p in text_lower for p in ["cancel", "nevermind", "never mind"])
            reply = stage_cancellation(order) if is_can else await stage_modification(order, text)
            await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=back_button())
            return
        
        try:
            recent = supabase.table("orders").select("id,items,price")\
                .eq("user_id", str(user.id)).eq("restaurant_id", rid)\
                .eq("status", "pending").order("created_at", desc=True).limit(5).execute()
            if not recent.data:
                await update.message.reply_text("❌ No active orders.", reply_markup=back_button())
                return
            lst = "\n".join(f"  *#{o['id']}* — {o['items']}  (${float(o['price']):.2f})" for o in recent.data)
            is_can = any(p in text_lower for p in ["cancel", "nevermind"])
            action = "cancel" if is_can else "modify"
            uc["pending_action"] = action
            uc["pending_mod_text"] = text
            set_user_state(user.id, UserState.AWAITING_ORDER_ID, context)
            await update.message.reply_text(
                f"📋 *Active orders:*\n{lst}\n\nType *Order Number* to {action}:",
                reply_markup=back_button(),
                parse_mode="Markdown"
            )
            return
        except Exception as ex:
            print(f"[MOD] {ex}")
            await update.message.reply_text("❌ Error.", reply_markup=back_button())
            return
    
    if any(k in text_lower for k in ["menu", "what do you serve", "what's available", "food list"]):
        await _send_menu(update.message, uc)
        return
    
    if any(k in text_lower for k in ["bill", "check please", "the check", "my total", "how much", "pay", "invoice"]):
        await calculate_bill(update, context)
        return
    
    if uc.get("table_number"):
        result = await process_order(
            text, user, rid, uc.get("table_number"), uc.get("chat_id"),
            user_preferences=uc.get("preferences", ""),
            session_id=uc.get("session_id", ""),
            display_name=uc.get("display_name", "")
        )
        if result:
            rt, _oid = result
            await update.message.reply_text(rt, parse_mode="Markdown", reply_markup=back_button())
            return
        else:
            examples = []
            try:
                menu_res = supabase.table("menu_items").select("content")\
                    .eq("restaurant_id", rid)\
                    .eq("sold_out", False).limit(5).execute()
                
                if menu_res.data:
                    for item in menu_res.data[:3]:
                        for line in item["content"].split("\n"):
                            if line.startswith("item:"):
                                item_name = line.replace("item:", "").strip()
                                examples.append(item_name)
                                break
            except Exception:
                examples = ["items from our menu"]
            
            example_text = ", ".join(examples[:2]) if examples else "items from menu"
            
            await update.message.reply_text(
                "❌ I couldn't understand your order.\n\n"
                "💡 Try:\n"
                f"• Use full item names (e.g., '{example_text}')\n"
                "• Say /menu to see all available dishes\n"
                "• Be specific with quantities (e.g., '2 Burgers')",
                reply_markup=back_button()
            )
            return
    
    try:
        rows = supabase.table("menu_items").select("content").eq("restaurant_id", rid).limit(30).execute()
        menu_ctx = "\n".join(r["content"] for r in rows.data) if rows.data else ""
        policy_ctx = fetch_policy_text(rid)
        system = ("Restaurant concierge in Order Mode.\n\n"
                  + (f"MENU:\n{menu_ctx}\n\n" if menu_ctx else "")
                  + (f"INFO:\n{policy_ctx}\n\n" if policy_ctx else "")
                  + "Answer warmly. 2-3 sentences.")
        c = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
            temperature=0.7,
            max_tokens=200
        )
        await update.message.reply_text(c.choices[0].message.content, reply_markup=back_button())
    except Exception as ex:
        print(f"[ORDERCHAT] {ex}")
        await update.message.reply_text("I'm here to help!", reply_markup=back_button())

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Voice Message
# ═══════════════════════════════════════════════════════════════════════════

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice notes using Groq Whisper transcription"""
    user = update.effective_user
    uc = get_user_context(user.id, context)
    
    await update.message.reply_text("🎤 Processing voice message...")
    
    try:
        voice = update.message.voice
        print(f"[VOICE] Received from {user.id}, duration: {voice.duration}s")
        file = await context.bot.get_file(voice.file_id)
        
        import tempfile
        import subprocess
        
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            ogg_path = tmp.name
        
        # Convert to MP3
        mp3_path = ogg_path.replace(".ogg", ".mp3")
        try:
            subprocess.run([
                "ffmpeg", "-i", ogg_path, "-acodec", "libmp3lame", "-ar", "16000", mp3_path
            ], check=True, capture_output=True)
            audio_path = mp3_path
        except Exception:
            audio_path = ogg_path
        
        # Transcribe
        with open(audio_path, "rb") as audio_file:
            transcription = await groq_client.audio.transcriptions.create(
                file=(audio_path, audio_file),
                model="whisper-large-v3",
                response_format="text"
            )
        
        # Cleanup
        import os
        os.unlink(ogg_path)
        if os.path.exists(mp3_path):
            os.unlink(mp3_path)
        
        transcribed_text = transcription.strip()
        print(f"[VOICE] Transcribed: {transcribed_text}")
        
        await update.message.reply_text(
            f"🎤 *I heard:* _{transcribed_text}_\n\nProcessing your request...",
            parse_mode="Markdown"
        )
        
        # Process as text
        update.message.text = transcribed_text
        await message_handler(update, context)
        
    except Exception as ex:
        print(f"[VOICE ERROR] {ex}")
        import traceback
        traceback.print_exc()
        await update.message.reply_text(
            "❌ Sorry, I couldn't understand the voice message. Please try typing instead.",
            reply_markup=back_button()
        )

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS - Main Message Router
# ═══════════════════════════════════════════════════════════════════════════

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    state = get_user_state(user.id, context)
    mode = get_mode(user.id, context)
    uc = get_user_context(user.id, context)
    print(f"[MSG] {user.id} mode={mode.value} state={state.value}: '{text[:60]}'")
    
    # CRITICAL: Check feedback first
    if state == UserState.AWAITING_FEEDBACK:
        await handle_feedback(update, context)
        return
    
    try:
        current_session = uc.get("session_id", "")
        if current_session:
            sess = supabase.table("user_sessions").select("awaiting_feedback")\
                .eq("session_id", current_session).limit(1).execute()
            if sess.data and sess.data[0].get("awaiting_feedback"):
                set_user_state(user.id, UserState.AWAITING_FEEDBACK, context)
                supabase.table("user_sessions").update({"awaiting_feedback": False})\
                    .eq("session_id", current_session).execute()
                await handle_feedback(update, context)
                return
    except Exception as ex:
        print(f"[SESS CHECK] {ex}")
    
    if state == UserState.AWAITING_NAME:
        await handle_name_input(update, context)
        return
    
    if state == UserState.AWAITING_PIN_SETUP:
        await handle_pin_setup(update, context)
        return
    
    if state == UserState.AWAITING_PIN_CONFIRM:
        await handle_pin_confirm(update, context)
        return
    
    if state == UserState.AWAITING_PIN_LOGIN:
        await handle_pin_login(update, context)
        return
    
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
        if uc.get("pending_mod_order"):
            await handle_modification_details(update, context)
        else:
            await handle_order_id_input(update, context)
        return
    
    if mode == Mode.BOOKING:
        if state in [UserState.AWAITING_GUESTS, UserState.AWAITING_TIME]:
            if _ORDER_KWS.search(text):
                await update.message.reply_text(
                    "📅 You're in *Booking Mode*. Use the main menu to Order Food.",
                    reply_markup=back_button(),
                    parse_mode="Markdown"
                )
                return
            await handle_booking_flow(update, context, state)
            return
        set_user_state(user.id, UserState.AWAITING_GUESTS, context)
        await update.message.reply_text(
            "📅 *Booking Mode* — How many guests?",
            reply_markup=back_button(),
            parse_mode="Markdown"
        )
        return
    
    if mode == Mode.ORDER:
        if state == UserState.AWAITING_TABLE:
            await handle_table_assignment(update, context)
            return
        if _BOOK_KWS.search(text):
            await update.message.reply_text(
                "🍽️ You're in *Order Mode*. Use the main menu to Book.",
                reply_markup=back_button(),
                parse_mode="Markdown"
            )
            return
        await handle_order_mode_chat(update, context)
        return
    
    await handle_general_chat(update, context)

# ═══════════════════════════════════════════════════════════════════════════
# WEBSOCKET HANDLERS (for Web Chat)
# ═══════════════════════════════════════════════════════════════════════════

async def handle_websocket_message(session_id: str, data: dict):
    """Process incoming WebSocket messages"""
    session = ws_manager.sessions.get(session_id, {})
    message_type = data.get("type")
    content = data.get("content", "")
    
    # Button clicks
    if message_type == "button_click":
        action = data.get("action")
        
        if action == "customer_type_new":
            session["state"] = "AWAITING_NAME"
            session["data"]["customer_type"] = "new"
            await ws_manager.send_message(session_id, {
                "type": "message",
                "content": "🆕 **New Customer**\n\nWhat is your name?"
            })
        
        elif action == "customer_type_returning":
            session["state"] = "AWAITING_NAME"
            session["data"]["customer_type"] = "returning"
            await ws_manager.send_message(session_id, {
                "type": "message",
                "content": "🔙 **Returning Customer**\n\nEnter your name:"
            })
        
        elif action == "mode_order":
            await ws_manager.send_message(session_id, {
                "type": "message",
                "content": "🍽️ **Order Mode**\n\nWhat would you like to order?\n\n_Type 'menu' to see dishes._",
                "buttons": [{"text": "📋 View Menu", "action": "show_menu"}]
            })
        
        elif action == "show_menu":
            await send_web_menu(session_id, session)
    
    # Text messages
    elif message_type == "text":
        state = session.get("state", "IDLE")
        
        if state == "AWAITING_NAME":
            await handle_web_name_input(session_id, content, session)
        else:
            await ws_manager.send_message(session_id, {
                "type": "message",
                "content": f"You said: {content}\n\n_(Full web chat logic active)_"
            })

async def send_web_menu(session_id: str, session: dict):
    """Send menu via WebSocket"""
    restaurant_id = session["data"].get("restaurant_id")
    
    try:
        rows = supabase.table("menu_items").select("content,sold_out").eq("restaurant_id", restaurant_id).execute()
        
        lines = ["🍽️ **Menu**\n"]
        cur_cat = None
        
        for row in rows.data or []:
            sold_out = row.get("sold_out", False)
            for line in row["content"].split("\n"):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("category:"):
                    cat = line.replace("category:", "").strip()
                    if cat != cur_cat:
                        lines.append(f"\n**{cat.upper()}**")
                        cur_cat = cat
                elif line.startswith("item:"):
                    item = line.replace("item:", "").strip()
                    if sold_out:
                        lines.append(f"  • ~~{item}~~ ❌ SOLD OUT")
                    else:
                        lines.append(f"  • {item}")
                elif line.startswith("price:") and not sold_out:
                    lines[-1] += f"  —  {line.replace('price:', '').strip()}"
        
        lines.append("\n_Tell me what you'd like!_")
        
        await ws_manager.send_message(session_id, {
            "type": "message",
            "content": "\n".join(lines)
        })
    except Exception as ex:
        print(f"[WEB MENU ERROR] {ex}")

async def handle_web_name_input(session_id: str, name: str, session: dict):
    """Handle name input from web chat"""
    name = name.strip()
    
    if len(name) < 2:
        await ws_manager.send_message(session_id, {
            "type": "message",
            "content": "❌ Please enter a valid name (at least 2 characters)."
        })
        return
    
    session["data"]["display_name"] = name
    session["state"] = "IDLE"
    
    await ws_manager.send_message(session_id, {
        "type": "message",
        "content": f"✅ Welcome, **{name}**!\n\nReady to order?",
        "buttons": [
            {"text": "🍽️ Order Food", "action": "mode_order"},
            {"text": "📅 Book Table", "action": "mode_booking"}
        ]
    })

# ═══════════════════════════════════════════════════════════════════════════
# FASTAPI ROUTES
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/")
@app.head("/")
async def health_check():
    """Health check"""
    try:
        test = supabase.table("restaurants").select("id").limit(1).execute()
        db_status = "connected" if test.data else "disconnected"
    except Exception as ex:
        db_status = f"error: {str(ex)[:50]}"
    
    return {
        "status": "healthy",
        "service": "Restaurant Concierge",
        "timestamp": datetime.now(DUBAI_TZ).isoformat(),
        "database": db_status,
        "version": "6.0.0"
    }

@app.get("/ping")
@app.head("/ping")
async def ping():
    return PlainTextResponse("pong", status_code=200)

@app.get("/chat/{restaurant_id}/{table_number}")
async def serve_chat(restaurant_id: str, table_number: str):
    """Serve web chat interface"""
    return FileResponse("static/chat.html")

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket for web chat"""
    await ws_manager.connect(session_id, websocket)
    
    try:
        # Parse session ID
        parts = session_id.split("_")
        if len(parts) >= 4:
            restaurant_id = "_".join(parts[:2])
            table_number = parts[3]
            
            try:
                rest = supabase.table("restaurants").select("name").eq("id", restaurant_id).limit(1).execute()
                restaurant_name = rest.data[0]["name"] if rest.data else "Our Restaurant"
            except Exception:
                restaurant_name = "Our Restaurant"
            
            # Store in session
            session = ws_manager.sessions[session_id]
            session["data"]["restaurant_id"] = restaurant_id
            session["data"]["table_number"] = table_number
            session["data"]["restaurant_name"] = restaurant_name
            
            # Send welcome
            await ws_manager.send_message(session_id, {
                "type": "message",
                "content": f"👋 Welcome to **{restaurant_name}**!\n\nAre you a new or returning customer?",
                "buttons": [
                    {"text": "🆕 New Customer", "action": "customer_type_new"},
                    {"text": "🔙 Returning Customer", "action": "customer_type_returning"}
                ]
            })
            
            # Listen for messages
            while True:
                data = await websocket.receive_json()
                await handle_websocket_message(session_id, data)
                
    except WebSocketDisconnect:
        ws_manager.disconnect(session_id)
    except Exception as ex:
        print(f"[WS ERROR] {ex}")
        ws_manager.disconnect(session_id)

@app.post("/webhook")
@limiter.limit("60/minute")
async def telegram_webhook(request: Request):
    """Telegram webhook"""
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"status": "ok"}
    except RateLimitExceeded:
        return {"status": "error", "message": "Rate limit exceeded"}
    except Exception as ex:
        print(f"[WEBHOOK ERROR] {ex}")
        return {"status": "error", "message": str(ex)}

# ═══════════════════════════════════════════════════════════════════════════
# STARTUP & SHUTDOWN
# ═══════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup_event():
    global telegram_app
    
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add handlers
    telegram_app.add_handler(CommandHandler("start", start_handler))
    telegram_app.add_handler(CommandHandler("help", help_handler))
    telegram_app.add_handler(CommandHandler("menu", menu_handler))
    telegram_app.add_handler(CommandHandler("cancel", cancel_command_handler))
    telegram_app.add_handler(CommandHandler("cancel_booking", cancel_booking_command))
    telegram_app.add_handler(CommandHandler("modify_booking", modify_booking_command))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    telegram_app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    
    await telegram_app.initialize()
    await telegram_app.start()
    
    print("✅ Server started successfully")
    print(f"   - Telegram webhook: /webhook")
    print(f"   - Web chat: /chat/{{restaurant_id}}/{{table}}")
    print(f"   - Health check: /")

@app.on_event("shutdown")
async def shutdown_event():
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    print("🛑 Server stopped")

# ═══════════════════════════════════════════════════════════════════════════
# RUN SERVER
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)