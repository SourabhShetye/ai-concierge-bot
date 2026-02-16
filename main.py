import os
import json
import asyncio
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from supabase import create_client
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from groq import AsyncGroq
from order_service import process_order

# --- CONFIGURATION ---
load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# --- HELPER: TIMEZONE (DUBAI UTC+4) ---
def get_dubai_time():
    return datetime.now(timezone.utc) + timedelta(hours=4)

def get_user_session(user_id):
    try:
        # Force string conversion to ensure match
        res = supabase.table("user_sessions").select("*").eq("user_id", str(user_id)).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        print(f"DB Error: {e}")
        return None

# --- AI WRAPPER ---
async def call_groq(prompt, system_role="You are a helpful assistant."):
    try:
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_role}, {"role": "user", "content": prompt}],
            temperature=0, max_tokens=400
        )
        return completion.choices[0].message.content, None
    except Exception as e:
        return None, str(e)

# --- BOOKING LOGIC ---
async def process_booking_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    user_id = update.effective_user.id
    
    # 1. VALIDATE SESSION & NAME
    session = get_user_session(user_id)
    if not session:
        await update.message.reply_text("⚠️ Connection lost. Please type /start to reconnect.")
        return

    if not session.get('customer_name'):
        context.user_data['state'] = 'AWAITING_NAME'
        await update.message.reply_text("👋 Before we book, **what is your name?**")
        return

    # 2. EXTRACT DETAILS
    now_dubai = get_dubai_time()
    prompt = f"""
    Extract booking details from: "{user_text}"
    CONTEXT: Current Time (Dubai): {now_dubai.strftime('%Y-%m-%d %H:%M')}, Today is {now_dubai.strftime('%A')}
    RULES:
    1. Calculate YYYY-MM-DD from words like "tomorrow".
    2. Convert time to 24-hour HH:MM.
    3. If guests/party size is NOT mentioned, set "guests": null.
    
    Return JSON ONLY: {{ "valid": true, "date": "YYYY-MM-DD", "time": "HH:MM", "guests": null or int }}
    """
    response, error = await call_groq(prompt, "You are a JSON extractor. Output ONLY raw JSON.")

    if error or not response:
        await update.message.reply_text("📉 System busy. Please try again.")
        return

    try:
        clean_json = response[response.find("{"):response.rfind("}")+1]
        data = json.loads(clean_json)
        
        if not data.get("valid"):
            context.user_data['state'] = 'AWAITING_BOOKING_DETAILS'
            await update.message.reply_text("🤔 I didn't catch the date or time. Please say it clearly (e.g., 'Tomorrow at 7pm').")
            return

        if data.get('guests') is None:
            context.user_data['partial_booking'] = data
            context.user_data['state'] = 'AWAITING_GUESTS'
            await update.message.reply_text("🗓️ Date & Time look good! **How many people** will be joining?")
            return

        await finalize_booking(update, context, data['date'], data['time'], data['guests'], session['current_restaurant_id'])
        
    except Exception as e:
        print(f"Booking Parse Error: {e}")
        context.user_data['state'] = 'AWAITING_BOOKING_DETAILS'
        await update.message.reply_text("❌ I didn't understand that. Please try 'Book for 2 people tomorrow at 8pm'.")

async def finalize_booking(update, context, date, time, guests, rest_id):
    user = update.effective_user
    session = get_user_session(user.id)
    customer_name = session.get('customer_name', 'Guest')
    
    booking_time = f"{date} {time}:00"

    # Check Availability (10 table rule)
    slot_bookings = supabase.table("bookings").select("*", count="exact")\
        .eq("restaurant_id", rest_id)\
        .eq("booking_time", booking_time)\
        .neq("status", "cancelled")\
        .execute()
        
    if slot_bookings.count >= 10:
        await update.message.reply_text(f"❌ {time} is fully booked. Please try another time.")
        return

    booking = {
        "restaurant_id": str(rest_id),
        "user_id": str(user.id),
        "customer_name": customer_name,
        "party_size": int(guests),
        "booking_time": booking_time,
        "status": "confirmed"
    }
    supabase.table("bookings").insert(booking).execute()
    context.user_data.clear()
    await update.message.reply_text(f"✅ **Booking Confirmed!**\n👤 {customer_name}\n📅 {date} at {time}\n👥 {guests} Guests")

# --- MAIN HANDLER ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    text_lower = text.lower()
    state = context.user_data.get('state')
    
    # 0. GLOBAL CANCEL
    if text_lower in ["cancel", "stop", "reset"]:
        context.user_data.clear()
        await update.message.reply_text("🔄 Action cancelled. How can I help?")
        return

    # 1. STATE: AWAITING NAME (Bug Fix Applied Here)
    if state == 'AWAITING_NAME':
        # GUARD: Don't accept commands as names
        forbidden_names = ["book", "order", "food", "table", "menu", "hi", "hello"]
        if any(w in text_lower for w in forbidden_names) and len(text.split()) < 3:
            await update.message.reply_text("⚠️ That sounds like a command. Please enter your **Name** to continue (e.g., 'John').")
            return

        # FORCE UPDATE
        try:
            supabase.table("user_sessions").update({"customer_name": text}).eq("user_id", str(user_id)).execute()
            context.user_data['state'] = None
            await update.message.reply_text(f"Nice to meet you, {text}! \n\nWhat would you like to do?\n🔹 **Book a table**\n🔹 **Order food**")
        except Exception as e:
            await update.message.reply_text("❌ Error saving name. Please try again.")
            print(f"Name Save Error: {e}")
        return

    # 2. STATE: AWAITING GUESTS
    if state == 'AWAITING_GUESTS':
        try:
            guests = int(''.join(filter(str.isdigit, text)))
            partial = context.user_data.get('partial_booking')
            session = get_user_session(user_id)
            await finalize_booking(update, context, partial['date'], partial['time'], guests, session['current_restaurant_id'])
            return
        except:
            await update.message.reply_text("🔢 Please enter a number (e.g. '4').")
            return

    # 3. STATE: AWAITING BOOKING DETAILS
    if state == 'AWAITING_BOOKING_DETAILS':
        await process_booking_request(update, context)
        return

    # 4. STATE: AWAITING TABLE
    if state == 'AWAITING_TABLE':
        supabase.table("user_sessions").update({"table_number": text}).eq("user_id", str(user_id)).execute()
        del context.user_data['state']
        await update.message.reply_text(f"✅ Table {text} set! You can now order food.")
        return

    # --- INTENT DETECTION ---

    # A. MENU
    if "menu" in text_lower and "order" not in text_lower:
        session = get_user_session(user_id)
        if session:
            menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", session['current_restaurant_id']).limit(40).execute()
            if menu_res.data:
                items = "\n".join([f"• {m['content'].splitlines()[1].replace('item: ', '')}" for m in menu_res.data if 'item:' in m['content']])
                await update.message.reply_text(f"📜 **Menu:**\n\n{items}\n\nSay 'I want the [Item]' to order.")
            else:
                await update.message.reply_text("🚫 No menu found.")
        return

    # B. BOOKING
    if any(k in text_lower for k in ["book", "reserve", "reservation"]):
        await process_booking_request(update, context)
        return

    # C. ORDER
    if any(k in text_lower for k in ["order", "have", "eat", "drink", "want"]):
        session = get_user_session(user_id)
        if not session:
            await update.message.reply_text("⚠️ Please type /start first.")
            return

        if not session.get('table_number'):
            context.user_data['state'] = 'AWAITING_TABLE'
            await update.message.reply_text("🍽️ **What is your Table Number?**")
            return
            
        reply = await process_order(text, update.effective_user, session['current_restaurant_id'], session['table_number'], update.message.chat_id)
        await update.message.reply_text(reply)
        return

    # D. FALLBACK
    session = get_user_session(user_id)
    if session:
        menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", session['current_restaurant_id']).limit(10).execute()
        menu = "\n".join([m['content'] for m in menu_res.data]) if menu_res.data else ""
        reply, _ = await call_groq(f"Menu: {menu}\nUser: {text}", "Restaurant Concierge. Be brief.")
        if reply: await update.message.reply_text(reply)

# --- STARTUP COMMAND ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    
    # 1. CLEAN PARSING of rest_id
    # Handles: /start, /start 123, /start rest_id=123
    raw_arg = args[0] if args else "1"
    if "=" in raw_arg:
        rest_id = raw_arg.split("=")[1]
    else:
        rest_id = raw_arg

    # 2. SESSION HANDLING
    try:
        existing = get_user_session(user_id)
        
        # Data payload
        data = {
            "user_id": str(user_id),
            "current_restaurant_id": str(rest_id)
        }

        # Logic: If name exists, keep it. If not, reset to trigger prompt.
        if existing and existing.get('customer_name'):
             # User returning: Just update restaurant ID
             supabase.table("user_sessions").update(data).eq("user_id", str(user_id)).execute()
             msg = f"👋 Welcome back, {existing['customer_name']}! (Location: {rest_id})"
             context.user_data['state'] = None
        else:
            # New user: Create row, Name is NULL
            data["customer_name"] = None 
            data["table_number"] = None
            supabase.table("user_sessions").upsert(data).execute()
            msg = "👋 Welcome! I am your AI Concierge."
            context.user_data['state'] = 'AWAITING_NAME'

        await update.message.reply_text(msg)
        if context.user_data.get('state') == 'AWAITING_NAME':
            await update.message.reply_text("To get started, **what is your name?**")
            
    except Exception as e:
        print(f"Start Error: {e}")
        await update.message.reply_text("⚠️ System Error. Please try again.")

# --- RESET COMMAND ---
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        supabase.table("user_sessions").delete().eq("user_id", str(user_id)).execute()
        context.user_data.clear()
        await update.message.reply_text("🔄 **System Reset.**\nYou are now a new customer.\nType /start to begin.")
    except Exception as e:
        await update.message.reply_text(f"Reset failed: {e}")

# --- SERVER ---
app = FastAPI()
ptb_app = Application.builder().token(TELEGRAM_TOKEN).build()
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("reset", reset))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

@app.post("/webhook")
async def webhook(request: Request):
    try:
        if not ptb_app._initialized:
            await ptb_app.initialize()
            await ptb_app.start()
        data = await request.json()
        await ptb_app.process_update(Update.de_json(data, ptb_app.bot))
    except Exception as e:
        print(e)
    return {"status": "ok"}

@app.api_route("/", methods=["GET", "HEAD"])
async def root(): return {"status": "Online"}