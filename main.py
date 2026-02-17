"""
Restaurant AI Concierge - Main FastAPI/Telegram Bot
Production-Ready Version with State Machine
"""

import os
import re
import asyncio
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

from order_service import process_order

# ============================================================================
# CONFIGURATION & INITIALIZATION
# ============================================================================

load_dotenv()

# Environment Variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Clients
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# FastAPI App
app = FastAPI(title="Restaurant Concierge API")

# Telegram Application (Global)
telegram_app: Optional[Application] = None

# Dubai Timezone
DUBAI_TZ = ZoneInfo("Asia/Dubai")

# ============================================================================
# STATE MACHINE ENUMS
# ============================================================================

class UserState(str, Enum):
    """
    Explicit states to prevent feedback loop bugs.
    Users can ONLY be in ONE state at a time.
    """
    IDLE = "idle"                          # Default state, no active flow
    AWAITING_GUESTS = "awaiting_guests"    # Booking: waiting for party size
    AWAITING_TIME = "awaiting_time"        # Booking: waiting for time input
    AWAITING_TABLE = "awaiting_table"      # Ordering: waiting for table number
    AWAITING_FEEDBACK = "awaiting_feedback" # Payment complete, waiting for ratings
    HAS_TABLE = "has_table"                # User has table, can order freely

# ============================================================================
# STATE MANAGEMENT HELPERS
# ============================================================================

def get_user_state(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> UserState:
    """
    Retrieve current user state from context.
    Default is IDLE if not set.
    """
    return context.user_data.get(f"state_{user_id}", UserState.IDLE)


def set_user_state(user_id: int, state: UserState, context: ContextTypes.DEFAULT_TYPE):
    """
    Set user state in context storage.
    """
    context.user_data[f"state_{user_id}"] = state
    print(f"[STATE] User {user_id} -> {state.value}")


def clear_user_state(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    Reset user to IDLE state.
    """
    set_user_state(user_id, UserState.IDLE, context)


def get_user_context(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    """
    Get user's temporary context data (restaurant_id, table_number, etc.)
    """
    key = f"context_{user_id}"
    if key not in context.user_data:
        context.user_data[key] = {}
    return context.user_data[key]


# ============================================================================
# TIMEZONE & VALIDATION HELPERS
# ============================================================================

def get_dubai_now() -> datetime:
    """Get current time in Dubai timezone"""
    return datetime.now(DUBAI_TZ)


async def parse_booking_time(user_input: str) -> Optional[datetime]:
    """
    Parse natural language time input into datetime using Groq AI.
    Returns None if invalid or in the past.

    IMPORTANT: This is async because FastAPI/python-telegram-bot run on an
    already-running asyncio event loop. Calling loop.run_until_complete() from
    within a running loop raises RuntimeError and silently kills the booking flow.
    """
    import json

    prompt = f"""
    Current Dubai Time: {get_dubai_now().strftime('%Y-%m-%d %H:%M')}

    Parse this booking request into a datetime: "{user_input}"

    Return ONLY a JSON object (no extra text):
    {{"datetime": "YYYY-MM-DD HH:MM", "valid": true}}

    Rules:
    - "tomorrow 8pm" = tomorrow at 20:00
    - "12 july 4pm" = July 12 of the current or next year at 16:00
    - "friday 7:30pm" = next Friday at 19:30
    - Past times are invalid → return {{"valid": false}}
    - If ambiguous, return {{"valid": false}}
    """

    try:
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=60
        )
        response = completion.choices[0].message.content

        start = response.find("{")
        end   = response.rfind("}") + 1
        if start == -1 or end == 0:
            print(f"[TIME PARSE] No JSON in response: {response!r}")
            return None

        data = json.loads(response[start:end])

        if not data.get("valid"):
            return None

        parsed_dt = datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M")
        parsed_dt = parsed_dt.replace(tzinfo=DUBAI_TZ)

        if parsed_dt <= get_dubai_now():
            return None

        return parsed_dt

    except Exception as e:
        print(f"[TIME PARSE ERROR] {e}")
        return None


def check_availability(restaurant_id: str, booking_time: datetime) -> bool:
    """
    Check if restaurant has capacity at requested time.
    Returns True if available, False if fully booked.
    """
    try:
        # Format time for query
        time_str = booking_time.strftime("%Y-%m-%d %H:%M:%S%z")
        
        # Count existing bookings at this time (not cancelled)
        response = supabase.table("bookings")\
            .select("id", count="exact")\
            .eq("restaurant_id", restaurant_id)\
            .eq("booking_time", time_str)\
            .neq("status", "cancelled")\
            .execute()
        
        count = response.count or 0
        
        # Hard limit: 10 tables
        return count < 10
        
    except Exception as e:
        print(f"[AVAILABILITY ERROR] {e}")
        return False  # Fail safe


def check_duplicate_booking(user_id: int, restaurant_id: str, booking_time: datetime) -> bool:
    """
    Check if user already has a booking at this time.
    Returns True if duplicate exists.
    """
    try:
        time_str = booking_time.strftime("%Y-%m-%d %H:%M:%S%z")
        
        response = supabase.table("bookings")\
            .select("id")\
            .eq("user_id", str(user_id))\
            .eq("restaurant_id", restaurant_id)\
            .eq("booking_time", time_str)\
            .neq("status", "cancelled")\
            .execute()
        
        return len(response.data) > 0
        
    except Exception as e:
        print(f"[DUPLICATE CHECK ERROR] {e}")
        return False


# ============================================================================
# COMMAND HANDLERS
# ============================================================================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start command - Initialize user and handle restaurant selection.
    Supports: /start rest_id=ABC123

    Captures and stores restaurant_name alongside restaurant_id so the
    welcome message can greet "Welcome to [Name]!" and downstream handlers
    never need an extra DB round-trip just to display the restaurant name.
    """
    user = update.effective_user
    chat_id = update.effective_chat.id

    restaurant_id   = None
    restaurant_name = "Our Restaurant"   # safe fallback if DB has no name column

    # --- Parse restaurant ID from command args ---
    if context.args:
        arg = context.args[0]
        if arg.startswith("rest_id="):
            restaurant_id = arg.split("=")[1]
        else:
            restaurant_id = arg           # support bare IDs as well

    # --- Validate the provided ID (or fall back to first restaurant) ---
    if restaurant_id:
        try:
            rest_check = supabase.table("restaurants")\
                .select("id, name")\
                .eq("id", restaurant_id)\
                .execute()

            if rest_check.data:
                restaurant_name = rest_check.data[0].get("name", restaurant_name)
            else:
                restaurant_id = None      # invalid ID → trigger fallback below
        except Exception as e:
            print(f"[START] Restaurant lookup error: {e}")
            restaurant_id = None

    if not restaurant_id:
        try:
            default = supabase.table("restaurants")\
                .select("id, name")\
                .limit(1)\
                .execute()

            if default.data:
                restaurant_id   = default.data[0]["id"]
                restaurant_name = default.data[0].get("name", restaurant_name)
            else:
                await update.message.reply_text("❌ System Error: No restaurants configured.")
                return
        except Exception as e:
            print(f"[START] Default restaurant error: {e}")
            await update.message.reply_text("❌ System Error: Unable to connect to restaurant.")
            return

    # --- Persist to user context (in-memory + DB) ---
    user_ctx = get_user_context(user.id, context)
    user_ctx["restaurant_id"]   = restaurant_id
    user_ctx["restaurant_name"] = restaurant_name
    user_ctx["chat_id"]         = chat_id

    # Restore table_number from DB if the user already had one
    # (survives bot restarts — context.user_data is cleared on redeploy)
    try:
        session = supabase.table("user_sessions")\
            .select("table_number")\
            .eq("user_id", str(user.id))\
            .execute()
        if session.data and session.data[0].get("table_number"):
            user_ctx["table_number"] = str(session.data[0]["table_number"])
    except Exception as e:
        print(f"[START] Session restore error: {e}")

    # Upsert user to database
    try:
        supabase.table("users").upsert({
            "id":        str(user.id),
            "username":  user.username or "guest",
            "full_name": user.full_name or "Guest",
            "chat_id":   str(chat_id)
        }).execute()
    except Exception as e:
        print(f"[USER UPSERT ERROR] {e}")

    # --- Welcome message with real restaurant name ---
    keyboard = [
        [InlineKeyboardButton("🍽️ View Menu",        callback_data="menu")],
        [InlineKeyboardButton("📅 Book a Table",     callback_data="book")],
        [InlineKeyboardButton("🪑 Set Table Number", callback_data="get_table")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_text = (
        f"👋 Welcome to *{restaurant_name}*, {user.first_name}!\n\n"
        f"I'm your AI Concierge. How can I assist you today?"
    )

    await update.message.reply_text(welcome_text, reply_markup=reply_markup,
                                    parse_mode="Markdown")
    clear_user_state(user.id, context)


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /help command - Show available commands and features.
    """
    help_text = """
🤖 **Restaurant AI Concierge**

**Commands:**
/start - Begin conversation
/menu - View full menu
/book - Make a reservation
/table - Set your table number for ordering
/cancel - Cancel your last order
/help - Show this message

**Features:**
✅ Natural language ordering ("I'll have 2 burgers and a coffee")
✅ Modify orders ("Remove the fries")
✅ Real-time kitchen updates
✅ Instant feedback system

Just chat naturally - I understand you! 🧠
    """
    
    await update.message.reply_text(help_text)


async def _send_menu(message, user_ctx: dict) -> None:
    """
    Shared menu-rendering helper used by:
      - /menu command handler
      - 🍽️ View Menu inline button
      - "menu" keyword intercept in message_handler

    Always fetches real rows from menu_items filtered by restaurant_id.
    Never calls the LLM — we want the exact list, not a creative summary.
    """
    restaurant_id   = user_ctx.get("restaurant_id")
    restaurant_name = user_ctx.get("restaurant_name", "Our Restaurant")

    if not restaurant_id:
        await message.reply_text("❌ Please use /start first to select a restaurant.")
        return

    try:
        rows = supabase.table("menu_items")\
            .select("content")\
            .eq("restaurant_id", restaurant_id)\
            .execute()

        if not rows.data:
            await message.reply_text("📋 Menu is currently unavailable. Please ask staff for assistance.")
            return

        # Build structured plain-text menu — no LLM involved
        menu_lines = [f"🍽️ *{restaurant_name} — Menu*\n"]
        current_category = None

        for row in rows.data:
            for line in row["content"].split("\n"):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("category:"):
                    cat = line.replace("category:", "").strip()
                    if cat != current_category:
                        menu_lines.append(f"\n*{cat.upper()}*")
                        current_category = cat
                elif line.startswith("item:"):
                    menu_lines.append(f"  • {line.replace('item:', '').strip()}")
                elif line.startswith("price:"):
                    menu_lines[-1] += f"  —  {line.replace('price:', '').strip()}"
                elif line.startswith("description:"):
                    menu_lines.append(f"    _{line.replace('description:', '').strip()}_")

        menu_lines.append("\n_Say the item name to order, e.g. 'I\'ll have the Full Stack Burger'_")

        menu_text = "\n".join(menu_lines)

        # Telegram message limit is 4096 chars; split if needed
        if len(menu_text) <= 4096:
            await message.reply_text(menu_text, parse_mode="Markdown")
        else:
            # Split at the 4000-char mark on a newline boundary
            chunk, rest = menu_text[:4000], menu_text[4000:]
            await message.reply_text(chunk, parse_mode="Markdown")
            await message.reply_text(rest,  parse_mode="Markdown")

    except Exception as e:
        print(f"[MENU ERROR] {e}")
        await message.reply_text("❌ Error loading menu. Please try again.")


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /menu command — delegates to _send_menu() for consistent rendering
    across the command, inline button, and keyword intercept paths.
    """
    user_ctx = get_user_context(update.effective_user.id, context)
    await _send_menu(update.message, user_ctx)


async def cancel_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /cancel command - Request cancellation of last order.
    """
    user = update.effective_user
    user_ctx = get_user_context(user.id, context)
    restaurant_id = user_ctx.get("restaurant_id")
    
    try:
        # Find most recent pending order
        response = supabase.table("orders")\
            .select("*")\
            .eq("user_id", str(user.id))\
            .eq("restaurant_id", restaurant_id)\
            .eq("status", "pending")\
            .neq("cancellation_status", "requested")\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()
        
        if not response.data:
            await update.message.reply_text("❌ No active orders to cancel.")
            return
        
        order = response.data[0]
        
        # Request cancellation
        supabase.table("orders")\
            .update({"cancellation_status": "requested"})\
            .eq("id", order["id"])\
            .execute()
        
        await update.message.reply_text(
            f"📩 **Cancellation Requested**\n"
            f"Order ID: #{order['id']}\n"
            f"Items: {order['items']}\n\n"
            f"Kitchen will review your request shortly."
        )
        
    except Exception as e:
        print(f"[CANCEL ERROR] {e}")
        await update.message.reply_text("❌ Error processing cancellation.")


# ============================================================================
# /table COMMAND HANDLER
# ============================================================================

async def table_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /table command — ask the user for their table number and enter AWAITING_TABLE.
    This is the clean entry point; users can also be routed here via the inline
    button or by saying "table 7" from any state (handled in message_handler).
    """
    user = update.effective_user
    set_user_state(user.id, UserState.AWAITING_TABLE, context)
    await update.message.reply_text(
        "🪑 *What is your table number?*\n"
        "_(Just type the number, e.g. '7' or 'Table 12')_",
        parse_mode="Markdown"
    )


# ============================================================================
# CALLBACK QUERY HANDLERS (Inline Buttons)
# ============================================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle all inline button callbacks.

    CRITICAL: When a CallbackQuery fires, update.message is ALWAYS None.
    The message lives on update.callback_query.message (aliased as query.message).
    Never pass `update` into a handler that calls update.message.reply_text()
    from a callback context — always reply via query.message directly.
    """
    query = update.callback_query
    await query.answer()  # Removes the "loading" spinner on the button

    user = update.effective_user
    data = query.data

    if data == "menu":
        # Delegate to shared helper — query.message is the correct reply target
        # for CallbackQuery updates (update.message is None in this context).
        user_ctx = get_user_context(user.id, context)
        await _send_menu(query.message, user_ctx)

    elif data == "book":
        # Start booking flow
        set_user_state(user.id, UserState.AWAITING_GUESTS, context)
        await query.message.reply_text("📅 Great! How many guests? (e.g., '4' or 'party of 6')")

    elif data == "get_table":
        # Start table assignment flow
        set_user_state(user.id, UserState.AWAITING_TABLE, context)
        await query.message.reply_text("🪑 What's your table number? (e.g., '5' or 'Table 12')")


# ============================================================================
# BOOKING FLOW HANDLERS
# ============================================================================

async def handle_booking_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState):
    """
    Handle booking state machine logic.
    """
    user = update.effective_user
    text = update.message.text.strip()
    user_ctx = get_user_context(user.id, context)
    
    # STATE: AWAITING_GUESTS
    if user_state == UserState.AWAITING_GUESTS:
        # Extract party size
        try:
            # Try to extract number from text
            numbers = re.findall(r'\d+', text)
            if not numbers:
                await update.message.reply_text("❌ Please provide a number (e.g., '4' or 'party of 6')")
                return
            
            party_size = int(numbers[0])
            
            if party_size < 1 or party_size > 20:
                await update.message.reply_text("❌ Party size must be between 1 and 20 guests.")
                return
            
            # Store party size
            user_ctx["party_size"] = party_size
            
            # Move to next state
            set_user_state(user.id, UserState.AWAITING_TIME, context)
            await update.message.reply_text(
                f"✅ Table for {party_size} guests.\n\n"
                "⏰ When would you like to dine?\n"
                "(e.g., 'tomorrow 8pm', 'Friday 7:30pm', 'Jan 25 at 6pm')"
            )
            
        except Exception as e:
            print(f"[PARTY SIZE ERROR] {e}")
            await update.message.reply_text("❌ Invalid input. Please enter a number.")
    
    # STATE: AWAITING_TIME
    elif user_state == UserState.AWAITING_TIME:
        # Parse booking time
        booking_time = await parse_booking_time(text)
        
        if not booking_time:
            await update.message.reply_text(
                "❌ Invalid time or past date.\n\n"
                "Please try again with:\n"
                "• 'tomorrow 8pm'\n"
                "• 'Friday 7:30pm'\n"
                "• 'January 25 at 6pm'"
            )
            return
        
        restaurant_id = user_ctx.get("restaurant_id")
        party_size = user_ctx.get("party_size")
        
        # Check for duplicate booking
        if check_duplicate_booking(user.id, restaurant_id, booking_time):
            await update.message.reply_text("❌ You already have a booking at this time!")
            clear_user_state(user.id, context)
            return
        
        # Check availability
        if not check_availability(restaurant_id, booking_time):
            await update.message.reply_text(
                "❌ Sorry, we're fully booked at that time.\n\n"
                "Please try a different time."
            )
            return
        
        # Create booking
        # NOTE: chat_id is NOT inserted into bookings — that column does not
        # exist on the bookings table (it lives on orders only). Inserting it
        # causes a PGRST204 schema-cache error and kills every booking attempt.
        try:
            booking_data = {
                "restaurant_id": restaurant_id,
                "user_id": str(user.id),
                "customer_name": user.full_name or "Guest",
                "party_size": party_size,
                "booking_time": booking_time.strftime("%Y-%m-%d %H:%M:%S%z"),
                "status": "confirmed"
            }
            
            supabase.table("bookings").insert(booking_data).execute()
            
            # Success message
            await update.message.reply_text(
                f"✅ **Booking Confirmed!**\n\n"
                f"👥 Guests: {party_size}\n"
                f"📅 Date: {booking_time.strftime('%B %d, %Y')}\n"
                f"⏰ Time: {booking_time.strftime('%I:%M %p')}\n\n"
                f"We look forward to serving you!"
            )
            
            # Reset state
            clear_user_state(user.id, context)
            
        except Exception as e:
            print(f"[BOOKING ERROR] {e}")
            await update.message.reply_text("❌ System error. Please try again later.")
            clear_user_state(user.id, context)


# ============================================================================
# TABLE ASSIGNMENT HANDLER
# ============================================================================

async def handle_table_assignment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle table number assignment flow.

    Saves to TWO places so it is never forgotten:
      1. user_ctx["table_number"]      — fast in-memory access for this session
      2. supabase user_sessions table  — persists across bot restarts / redeployments

    Without the DB write, a Render redeploy wipes context.user_data and the bot
    loops asking for the table number on every restart.
    """
    user = update.effective_user
    text = update.message.text.strip()
    user_ctx = get_user_context(user.id, context)

    # Extract digits — accepts "table 7", "7", "Table 12", "I'm at table 3", etc.
    numbers = re.findall(r'\d+', text)
    if not numbers:
        await update.message.reply_text(
            "❌ Please provide a table number (e.g., '5' or 'Table 12')"
        )
        return

    table_number = numbers[0]

    # 1. Store in memory context — used for all order checks this session
    user_ctx["table_number"] = table_number

    # 2. Persist to database — survives restarts
    try:
        supabase.table("user_sessions").upsert({
            "user_id":      str(user.id),
            "table_number": table_number
        }).execute()
        print(f"[TABLE] User {user.id} assigned to table {table_number} (saved to DB)")
    except Exception as e:
        # Non-fatal: in-memory copy still works for this session
        print(f"[TABLE] DB persist warning: {e}")

    # 3. Advance state machine
    set_user_state(user.id, UserState.HAS_TABLE, context)

    await update.message.reply_text(
        f"✅ *Table {table_number} confirmed!*\n\n"
        f"You can now order — just tell me what you'd like!\n"
        f"_Example: 'I'll have 2 Binary Bites and a Java Jolt'_",
        parse_mode="Markdown"
    )


# ============================================================================
# FEEDBACK HANDLER
# ============================================================================

async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle feedback/ratings from users.
    CRITICAL: Only processes if user is in AWAITING_FEEDBACK state.
    """
    user = update.effective_user
    text = update.message.text.strip()
    
    # Extract ratings (numbers 1-5)
    ratings = re.findall(r'\b[1-5]\b', text)
    
    if not ratings:
        await update.message.reply_text(
            "Please provide ratings (1-5 stars) for each dish and overall experience."
        )
        return
    
    # Store feedback in database
    try:
        user_ctx = get_user_context(user.id, context)
        restaurant_id = user_ctx.get("restaurant_id")
        
        feedback_data = {
            "restaurant_id": restaurant_id,
            "user_id": str(user.id),
            "ratings": text,
            "created_at": get_dubai_now().isoformat()
        }
        
        supabase.table("feedback").insert(feedback_data).execute()
        
        await update.message.reply_text(
            "⭐ Thank you for your feedback!\n\n"
            "We appreciate your time and hope to see you again soon! 😊"
        )
        
        # Reset state
        clear_user_state(user.id, context)
        
    except Exception as e:
        print(f"[FEEDBACK ERROR] {e}")
        await update.message.reply_text("✅ Feedback received. Thank you!")
        clear_user_state(user.id, context)



# ============================================================================
# BILLING HANDLER
# ============================================================================

async def calculate_bill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Display the running bill for the user's current table.

    Context resolution order (never asks if table already known):
      1. user_ctx["table_number"]  — in-memory, set this session
      2. user_sessions DB row      — persists across restarts/redeploys
      3. Only if genuinely absent  — ask the user for table number

    Math: Python sum() over price column floats — never an LLM.
    The $8 + $4 = $13 hallucination bug is impossible here.
    """
    user      = update.effective_user
    user_ctx  = get_user_context(user.id, context)

    table_number  = user_ctx.get("table_number")
    restaurant_id = user_ctx.get("restaurant_id")

    # ── 1. Try DB session if not in memory ───────────────────────────────────
    if not table_number:
        try:
            sess = supabase.table("user_sessions") \
                .select("table_number") \
                .eq("user_id", str(user.id)) \
                .execute()
            if sess.data and sess.data[0].get("table_number"):
                table_number = str(sess.data[0]["table_number"])
                user_ctx["table_number"] = table_number    # cache in memory
        except Exception as e:
            print(f"[BILL] Session lookup error: {e}")

    # ── 2. Still unknown — ask once ───────────────────────────────────────────
    if not table_number:
        set_user_state(user.id, UserState.AWAITING_TABLE, context)
        await update.message.reply_text(
            "🪑 *What is your table number?*\n"
            "_(I will pull up your bill right after!)_",
            parse_mode="Markdown"
        )
        return

    # ── 3. Fetch unpaid orders for this table ────────────────────────────────
    try:
        res = supabase.table("orders") \
            .select("items, price") \
            .eq("user_id", str(user.id)) \
            .eq("restaurant_id", restaurant_id) \
            .eq("table_number", str(table_number)) \
            .neq("status", "paid") \
            .neq("status", "cancelled") \
            .execute()

        if not res.data:
            await update.message.reply_text(
                f"🧾 *Table {table_number} — No active orders found.*",
                parse_mode="Markdown"
            )
            return

        # ── Python sum — deterministic, never an LLM ─────────────────────
        total = round(sum(float(row["price"]) for row in res.data), 2)

        lines = ["\n".join(
            f"  • {row['items']}  —  ${float(row['price']):.2f}"
            for row in res.data
        )]
        line_block = "\n".join(lines)

        await update.message.reply_text(
            f"🧾 *Your Bill — Table {table_number}*\n\n"
            f"{line_block}\n\n"
            f"💰 *Total: ${total:.2f}*\n\n"
            f"_(Ask a waiter to process payment)_",
            parse_mode="Markdown"
        )

    except Exception as e:
        print(f"[BILL ERROR] {e}")
        await update.message.reply_text(
            "❌ Error fetching bill. Please ask staff for assistance."
        )


# ============================================================================
# MAIN MESSAGE HANDLER (State Router)
# ============================================================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Main message router based on user state.

    Priority order (top = highest):
      1. Active booking flow  (AWAITING_GUESTS / AWAITING_TIME)
      2. Active table entry   (AWAITING_TABLE)
      3. Feedback state       (AWAITING_FEEDBACK) — checked before numbers cause issues
      4. Passive table-number detection — catches "table 7" typed from any non-flow state
      5. Menu keyword intercept — "show menu", "menu please" → structured list, not AI summary
      6. Order processing — if table is known, attempt order parse first
      7. General AI chat — fallback for everything else
    """
    user = update.effective_user
    text = update.message.text.strip()
    text_lower = text.lower()

    # Get current state and context
    user_state = get_user_state(user.id, context)
    user_ctx   = get_user_context(user.id, context)

    print(f"[MSG] User {user.id} state={user_state.value} table={user_ctx.get('table_number','—')}: '{text[:60]}'")

    # ── 1. BOOKING FLOW STATES ───────────────────────────────────────────────
    if user_state in [UserState.AWAITING_GUESTS, UserState.AWAITING_TIME]:
        await handle_booking_flow(update, context, user_state)
        return

    # ── 2. TABLE ASSIGNMENT STATE ────────────────────────────────────────────
    if user_state == UserState.AWAITING_TABLE:
        await handle_table_assignment(update, context)
        return

    # ── 3. FEEDBACK STATE ────────────────────────────────────────────────────
    # Must run before any numeric/order logic to avoid star ratings being
    # misread as table numbers or order quantities.
    if user_state == UserState.AWAITING_FEEDBACK:
        await handle_feedback(update, context)
        return

    # ── 4. PASSIVE TABLE-NUMBER DETECTION ───────────────────────────────────
    # Catches users who type "table 7" or "I'm at table 3" from IDLE state
    # without going through the button / /table command flow.
    # Pattern: message contains the word "table" followed (anywhere) by digits.
    table_mention = re.search(r'\btable\s*(\d+)\b', text_lower)
    if table_mention and not user_ctx.get("table_number"):
        # Temporarily set state so handle_table_assignment runs correctly
        set_user_state(user.id, UserState.AWAITING_TABLE, context)
        await handle_table_assignment(update, context)
        return

    # ── 5. MENU KEYWORD INTERCEPT ────────────────────────────────────────────
    # Catches "show menu", "view menu", "what's on the menu?", "menu please"
    # Bypasses the AI fallback so users always get the real structured list.
    menu_keywords = ["menu", "what do you serve", "what's available",
                     "what do you have", "show me food", "food list"]
    if any(kw in text_lower for kw in menu_keywords):
        await _send_menu(update.message, user_ctx)
        return

    # ── 5b. BILLING KEYWORD INTERCEPT ────────────────────────────────────────
    # Catches "bill please", "check please", "what's my total", "how much do I owe"
    # Routed here BEFORE order processing so billing keywords don't accidentally
    # trigger a food-order attempt.
    bill_keywords = ["bill", "check please", "the check", "my total",
                     "how much", "pay", "invoice", "receipt"]
    if any(kw in text_lower for kw in bill_keywords):
        await calculate_bill(update, context)
        return

    # ── 6. ORDER PROCESSING ──────────────────────────────────────────────────
    # Only attempt if table number is known. process_order returns None when
    # the message is not a food order, falling through to general chat below.
    if user_ctx.get("table_number"):
        order_result = await process_order(
            text,
            user,
            user_ctx.get("restaurant_id"),
            user_ctx.get("table_number"),
            user_ctx.get("chat_id")
        )
        if order_result:
            await update.message.reply_text(order_result)
            return
    else:
        # No table set yet — if message looks order-like, prompt for table first
        order_keywords = ["order", "i'll have", "i want", "can i get",
                          "give me", "bring me", "i'd like"]
        if any(kw in text_lower for kw in order_keywords):
            set_user_state(user.id, UserState.AWAITING_TABLE, context)
            await update.message.reply_text(
                "🪑 *What's your table number?*\n"
                "_(I'll place the order right after!)_",
                parse_mode="Markdown"
            )
            return

    # ── 7. GENERAL AI CHAT ───────────────────────────────────────────────────
    await handle_general_chat(update, context)


async def handle_general_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle general queries using AI when not in specific flow.
    Always scoped to the user's current restaurant_id so that menu items
    from other restaurants are never exposed.
    """
    user = update.effective_user
    text = update.message.text.strip()
    user_ctx = get_user_context(user.id, context)
    restaurant_id = user_ctx.get("restaurant_id")

    # Guard: restaurant_id must be set. Without this, the Supabase query has
    # no restaurant filter and would return items from every restaurant.
    if not restaurant_id:
        await update.message.reply_text(
            "👋 Please use /start to begin.\n"
            "That will connect you to the right restaurant."
        )
        return

    try:
        # Get menu context — always filtered to THIS restaurant only
        menu_items = supabase.table("menu_items")\
            .select("content")\
            .eq("restaurant_id", restaurant_id)\
            .limit(20)\
            .execute()
        
        menu_context = "\n".join([m["content"] for m in menu_items.data]) if menu_items.data else "No menu available"
        
        # AI Response
        prompt = f"""
        You are a friendly restaurant AI assistant.
        
        Menu:
        {menu_context}
        
        User Question: "{text}"
        
        Instructions:
        - If asking about menu items, describe them warmly
        - If asking about policies (parking, hours, etc.), be helpful
        - If asking to order, politely say they need to provide table number first
        - Keep responses concise (2-3 sentences max)
        - Be friendly and professional
        
        Response:
        """
        
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=200
        )
        
        response = completion.choices[0].message.content
        await update.message.reply_text(response)
        
    except Exception as e:
        print(f"[CHAT ERROR] {e}")
        await update.message.reply_text(
            "I'm here to help! Try:\n"
            "• /menu - View our menu\n"
            "• /book - Make a reservation\n"
            "• /table - Set your table number to order"
        )


# ============================================================================
# FASTAPI WEBHOOK ENDPOINT
# ============================================================================

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """
    FastAPI endpoint to receive Telegram updates.
    """
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")
        return {"status": "error", "message": str(e)}


@app.get("/")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "running",
        "service": "Restaurant AI Concierge",
        "timestamp": get_dubai_now().isoformat()
    }


# ============================================================================
# APPLICATION STARTUP
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """
    Initialize Telegram bot on FastAPI startup.
    """
    global telegram_app
    
    # Build application
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Register handlers
    telegram_app.add_handler(CommandHandler("start",  start_handler))
    telegram_app.add_handler(CommandHandler("help",   help_handler))
    telegram_app.add_handler(CommandHandler("menu",   menu_handler))
    telegram_app.add_handler(CommandHandler("table",  table_command_handler))  # /table → AWAITING_TABLE
    telegram_app.add_handler(CommandHandler("cancel", cancel_order_handler))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    # Initialize bot
    await telegram_app.initialize()
    await telegram_app.start()
    
    print("✅ Telegram Bot Started Successfully")


@app.on_event("shutdown")
async def shutdown_event():
    """
    Clean shutdown of Telegram bot.
    """
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    
    print("🛑 Telegram Bot Stopped")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)