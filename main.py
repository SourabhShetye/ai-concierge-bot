import os
import json
import asyncio
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks
from supabase import create_client
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from groq import AsyncGroq
from order_service import process_order

# --- CONFIGURATION ---
load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# --- HELPERS ---
def get_dubai_time():
    return datetime.now(timezone.utc) + timedelta(hours=4)

def get_user_session(user_id):
    try:
        res = supabase.table("user_sessions").select("*").eq("user_id", str(user_id)).execute()
        return res.data[0] if res.data else None
    except:
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
    
    # 1. Check Session/Name first
    session = get_user_session(user_id)
    if not session or not session.get('customer_name'):
        context.user_data['state'] = 'AWAITING_NAME'
        await update.message.reply_text("👋 Before we book, **what is your name?**")
        return

    # 2. Extract Details
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
            await update.message.reply_text("🤔 I didn't catch the date or time. Could you say it again? (e.g., 'Tomorrow at 7pm')")
            return

        if data.get('guests') is None:
            context.user_data['partial_booking'] = data
            context.user_data['state'] = 'AWAITING_GUESTS'
            await update.message.reply_text("🗓️ Date & Time look good! **How many people** will be joining?")
            return

        await finalize_booking(update, context, data['date'], data['time'], data['guests'], session['current_restaurant_id'])
        
    except Exception as e:
        print(f"Booking Parse Error: {e}")
        await update.message.reply_text("❌ I didn't understand that. Please try 'Book for 2 people tomorrow at 8pm'.")

async def finalize_booking(update, context, date, time, guests, rest_id):
    user = update.effective_user
    session = get_user_session(user.id)
    customer_name = session.get('customer_name')
    
    booking_time = f"{date} {time}:00"

    # Check Availability (Simple 10 table rule)
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

async def cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Find active bookings for this user
    # We look for bookings in the future that are NOT cancelled
    now = get_dubai_time().strftime('%Y-%m-%d %H:%M:%S')
    
    res = supabase.table("bookings").select("*")\
        .eq("user_id", str(user_id))\
        .gt("booking_time", now)\
        .neq("status", "cancelled")\
        .order("booking_time", desc=False)\
        .execute()
        
    if not res.data:
        await update.message.reply_text("🔎 I couldn't find any upcoming bookings to cancel.")
        return

    # If multiple, cancel the earliest one (or you could ask user to pick)
    booking = res.data[0]
    supabase.table("bookings").update({"status": "cancelled"}).eq("id", booking['id']).execute()
    
    await update.message.reply_text(f"🗑️ **Booking Cancelled**\n📅 {booking['booking_time']}\nWe hope to see you another time!")


# --- MAIN HANDLER ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    state = context.user_data.get('state')
    
    # 1. STATE: AWAITING NAME (This is priority)
    if state == 'AWAITING_NAME':
        supabase.table("user_sessions").update({"customer_name": text}).eq("user_id", str(user_id)).execute()
        context.user_data['state'] = None # Clear state
        await update.message.reply_text(f"Nice to meet you, {text}! \n\nWhat would you like to do?\n🔹 **Book a table**\n🔹 **Order food**")
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

    # 3. STATE: AWAITING TABLE (For Orders)
    if state == 'AWAITING_TABLE':
        supabase.table("user_sessions").update({"table_number": text}).eq("user_id", str(user_id)).execute()
        del context.user_data['state']
        await update.message.reply_text(f"✅ Table {text} set! You can now order food.")
        return

    # --- INTENT DETECTION ---
    text_lower = text.lower()

    # A. CANCEL INTENT
    if "cancel" in text_lower:
        if "booking" in text_lower or "reservation" in text_lower:
            await cancel_booking(update, context)
            return
        # Otherwise assume it's an order cancellation (handled in order_service logic or here)
        # For now, let's pass generic cancels to order service unless specified
    
    # B. BOOKING INTENT
    if any(k in text_lower for k in ["book", "reserve", "reservation"]):
        await process_booking_request(update, context)
        return

    # C. ORDER INTENT
    if any(k in text_lower for k in ["order", "have", "eat", "drink", "menu", "cancel"]):
        session = get_user_session(user_id)
        if not session:
            await update.message.reply_text("⚠️ Please click the restaurant link again to start.")
            return

        if not session.get('table_number'):
            context.user_data['state'] = 'AWAITING_TABLE'
            await update.message.reply_text("🍽️ **What is your Table Number?**")
            return
            
        # Process Order
        reply = await process_order(text, update.effective_user, session['current_restaurant_id'], session['table_number'], update.message.chat_id)
        await update.message.reply_text(reply)
        return

    # D. FALLBACK (Chat)
    session = get_user_session(user_id)
    if session:
        # Get simplified menu for context
        menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", session['current_restaurant_id']).limit(20).execute()
        menu = "\n".join([m['content'] for m in menu_res.data]) if menu_res.data else ""
        reply, _ = await call_groq(f"Menu: {menu}\nUser: {text}", "Restaurant Concierge")
        if reply: await update.message.reply_text(reply)


# --- STARTUP COMMAND (Fixes Issue 1 & 2) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    
    # 1. GET RESTAURANT ID
    # If a parameter is passed (/start 123), use it. Otherwise default to 1.
    rest_id = args[0] if args else "1" 

    # 2. UPDATE DATABASE SESSION
    # We strictly overwrite the current_restaurant_id for this user
    data = {
        "user_id": str(user_id),
        "current_restaurant_id": str(rest_id),
        "customer_name": None, # Reset name to ensure we ask for it (or keep it if you prefer)
        "table_number": None
    }
    
    # Check if user exists to preserve name if wanted, 
    # BUT request says: "ask the customers name which it uses for the rest of the conversation"
    # So we can reset it to force the "Welcome" flow.
    
    existing = get_user_session(user_id)
    if existing and existing.get('customer_name'):
        data['customer_name'] = existing['customer_name'] # Keep name if they are returning
        msg = f"👋 Welcome back to the Restaurant, {existing['customer_name']}!"
        # No state needed if name exists
        context.user_data['state'] = None
    else:
        msg = "👋 Welcome to the Restaurant Concierge!"
        context.user_data['state'] = 'AWAITING_NAME' # FORCE NAME PROMPT

    supabase.table("user_sessions").upsert(data).execute()
    
    await update.message.reply_text(msg)
    
    if context.user_data.get('state') == 'AWAITING_NAME':
         await update.message.reply_text("To get started, **what is your name?**")

# --- SERVER ---
app = FastAPI()
ptb_app = Application.builder().token(TELEGRAM_TOKEN).build()
ptb_app.add_handler(CommandHandler("start", start))
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

@app.get("/")
async def root(): return {"status": "Online"}