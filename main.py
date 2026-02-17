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
    Parse natural language time input into datetime.
    Returns None if invalid or in the past.
    """
    try:
        # Use Groq to parse natural language
        prompt = f"""
        Current Dubai Time: {get_dubai_now().strftime('%Y-%m-%d %H:%M')}
        
        Parse this booking request into a datetime: "{user_input}"
        
        Return ONLY a JSON object:
        {{"datetime": "YYYY-MM-DD HH:MM", "valid": true}}
        
        Rules:
        - "tomorrow 8pm" = tomorrow at 20:00
        - "friday 7:30pm" = next Friday at 19:30
        - Past times are invalid (valid: false)
        - If ambiguous, return valid: false
        """
        
        # FIX: Directly await the async call
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        
        response = completion.choices[0].message.content
        
        # Extract JSON
        import json
        start = response.find("{")
        end = response.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        
        data = json.loads(response[start:end])
        
        if not data.get("valid"):
            return None
        
        # Parse datetime string
        dt_str = data["datetime"]
        parsed_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        
        # Add Dubai timezone
        parsed_dt = parsed_dt.replace(tzinfo=DUBAI_TZ)
        
        # Validate not in past
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
    """
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Parse restaurant ID from command args
    restaurant_id = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("rest_id="):
            restaurant_id = arg.split("=")[1]
    
    # Validate restaurant ID
    if restaurant_id:
        try:
            rest_check = supabase.table("restaurants")\
                .select("id, name")\
                .eq("id", restaurant_id)\
                .execute()
            
            if not rest_check.data:
                restaurant_id = None  # Invalid, fallback to default
        except:
            restaurant_id = None
    
    # Default restaurant if none specified
    if not restaurant_id:
        try:
            default = supabase.table("restaurants")\
                .select("id, name")\
                .limit(1)\
                .execute()
            
            if default.data:
                restaurant_id = default.data[0]["id"]
        except Exception as e:
            await update.message.reply_text("❌ System Error: Unable to connect to restaurant.")
            return
    
    # Store in user context
    user_ctx = get_user_context(user.id, context)
    user_ctx["restaurant_id"] = restaurant_id
    user_ctx["chat_id"] = chat_id
    
    # Upsert user to database
    try:
        supabase.table("users").upsert({
            "id": str(user.id),
            "username": user.username or "guest",
            "full_name": user.full_name or "Guest",
            "chat_id": str(chat_id)
        }).execute()
    except Exception as e:
        print(f"[USER UPSERT ERROR] {e}")
    
    # Welcome message
    keyboard = [
        [InlineKeyboardButton("🍽️ View Menu", callback_data="menu")],
        [InlineKeyboardButton("📅 Book a Table", callback_data="book")],
        [InlineKeyboardButton("🪑 Get Table Number", callback_data="get_table")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        f"👋 Welcome {user.first_name}!\n\n"
        "I'm your AI Concierge. How can I assist you today?"
    )
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)
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


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /menu command - Display restaurant menu.
    """
    user_ctx = get_user_context(update.effective_user.id, context)
    restaurant_id = user_ctx.get("restaurant_id")
    
    if not restaurant_id:
        await update.message.reply_text("❌ Please use /start first to select a restaurant.")
        return
    
    try:
        menu_items = supabase.table("menu_items")\
            .select("content")\
            .eq("restaurant_id", restaurant_id)\
            .execute()
        
        if not menu_items.data:
            await update.message.reply_text("📋 Menu is currently unavailable.")
            return
        
        # Format menu nicely
        menu_text = "🍽️ **OUR MENU**\n\n"
        
        current_category = None
        for item in menu_items.data:
            content = item["content"]
            lines = content.split("\n")
            
            for line in lines:
                if line.startswith("category:"):
                    category = line.replace("category:", "").strip()
                    if category != current_category:
                        menu_text += f"\n**{category.upper()}**\n"
                        current_category = category
                elif line.startswith("item:"):
                    menu_text += f"• {line.replace('item:', '').strip()}\n"
                elif line.startswith("price:"):
                    menu_text += f"  {line.replace('price:', '').strip()}\n"
                elif line.startswith("description:"):
                    menu_text += f"  _{line.replace('description:', '').strip()}_\n"
        
        await update.message.reply_text(menu_text, parse_mode="Markdown")
        
    except Exception as e:
        print(f"[MENU ERROR] {e}")
        await update.message.reply_text("❌ Error loading menu.")


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
# CALLBACK QUERY HANDLERS (Inline Buttons)
# ============================================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle all inline button callbacks.
    """
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    data = query.data
    
    if data == "menu":
        # Show menu via callback
        await menu_handler(update, context)
    
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
        try:
            booking_data = {
                "restaurant_id": restaurant_id,
                "user_id": str(user.id),
                "customer_name": user.full_name or "Guest",
                "party_size": party_size,
                "booking_time": booking_time.strftime("%Y-%m-%d %H:%M:%S%z"),
                "status": "confirmed",
                "chat_id": str(update.effective_chat.id)
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
    """
    user = update.effective_user
    text = update.message.text.strip()
    user_ctx = get_user_context(user.id, context)
    
    # Extract table number
    numbers = re.findall(r'\d+', text)
    if not numbers:
        await update.message.reply_text("❌ Please provide a table number (e.g., '5' or 'Table 12')")
        return
    
    table_number = numbers[0]
    
    # Store table number
    user_ctx["table_number"] = table_number
    
    # Change state to HAS_TABLE
    set_user_state(user.id, UserState.HAS_TABLE, context)
    
    await update.message.reply_text(
        f"✅ **Table {table_number} Assigned**\n\n"
        f"You can now order by simply telling me what you'd like!\n\n"
        f"Example: 'I'll have 2 burgers and a coffee'"
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
# MAIN MESSAGE HANDLER (State Router)
# ============================================================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Main message router based on user state.
    CRITICAL: Prevents feedback loop by checking state FIRST.
    """
    user = update.effective_user
    text = update.message.text.strip()
    
    # Get current state
    user_state = get_user_state(user.id, context)
    user_ctx = get_user_context(user.id, context)
    
    print(f"[MSG] User {user.id} in state {user_state.value}: '{text[:50]}'")
    
    # ========================================================================
    # STATE-BASED ROUTING (Priority Order Matters!)
    # ========================================================================
    
    # 1. BOOKING FLOW STATES (Highest Priority)
    if user_state in [UserState.AWAITING_GUESTS, UserState.AWAITING_TIME]:
        await handle_booking_flow(update, context, user_state)
        return
    
    # 2. TABLE ASSIGNMENT STATE
    if user_state == UserState.AWAITING_TABLE:
        await handle_table_assignment(update, context)
        return
    
    # 3. FEEDBACK STATE (CRITICAL: Must be checked BEFORE order processing)
    if user_state == UserState.AWAITING_FEEDBACK:
        await handle_feedback(update, context)
        return
    
    # 4. ORDER PROCESSING (User has table and is in HAS_TABLE or IDLE state)
    # CRITICAL: Check if user has table number assigned
    if user_ctx.get("table_number"):
        # Try to process as order first
        restaurant_id = user_ctx.get("restaurant_id")
        table_number = user_ctx.get("table_number")
        chat_id = user_ctx.get("chat_id")
        
        order_result = await process_order(text, user, restaurant_id, table_number, chat_id)
        
        if order_result:  # Successfully processed as order
            await update.message.reply_text(order_result)
            return
    
    # 5. FALLBACK: AI Chat (General Queries)
    # User is in IDLE state or query didn't match any order
    await handle_general_chat(update, context)


async def handle_general_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle general queries using AI when not in specific flow.
    """
    user = update.effective_user
    text = update.message.text.strip()
    user_ctx = get_user_context(user.id, context)
    restaurant_id = user_ctx.get("restaurant_id")
    
    try:
        # Get menu context
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


# Change @app.get("/") to this:
@app.api_route("/", methods=["GET", "HEAD"])
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
    telegram_app.add_handler(CommandHandler("start", start_handler))
    telegram_app.add_handler(CommandHandler("help", help_handler))
    telegram_app.add_handler(CommandHandler("menu", menu_handler))
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
