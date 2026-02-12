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

# --- HELPER: TIMEZONE (DUBAI UTC+4) ---
def get_dubai_time():
    return datetime.now(timezone.utc) + timedelta(hours=4)

# --- HELPER: GET USER SESSION ---
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
async def process_booking_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    user = update.effective_user
    
    session = get_user_session(user.id)
    if not session:
        await update.message.reply_text("⚠️ Session expired. Please /start again.")
        return

    now_dubai = get_dubai_time()
    
    prompt = f"""
    Extract booking details from: "{user_text}"
    CONTEXT: Current Time (Dubai): {now_dubai.strftime('%Y-%m-%d %H:%M')}, Today is {now_dubai.strftime('%A')}
    RULES:
    1. Calculate actual YYYY-MM-DD from words like "tomorrow".
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

        # Check for Guests
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
    customer_name = session.get('customer_name') if session else user.full_name
    
    booking_time = f"{date} {time}:00"

    # Check Availability (Simple 10 table rule)
    try:
        slot_bookings = supabase.table("bookings").select("*", count="exact")\
            .eq("restaurant_id", rest_id)\
            .eq("booking_time", booking_time)\
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
        
        # Cleanup
        context.user_data.clear()
        
        await update.message.reply_text(f"✅ **Booking Confirmed!**\n👤 {customer_name}\n📅 {date} at {time}\n👥 {guests} Guests")
    except Exception as e:
        await update.message.reply_text("❌ Database error during booking.")
        print(e)

# --- MAIN ROUTER ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    text_lower = text.lower()
    
    # 0. GLOBAL ESCAPE COMMANDS
    if text_lower in ["cancel", "stop", "reset"]:
        context.user_data.clear()
        await update.message.reply_text("🔄 State reset. How can I help?")
        return

    state = context.user_data.get('state')
    
    # 1. STATE: AWAITING NAME
    if state == 'AWAITING_NAME':
        supabase.table("user_sessions").update({"customer_name": text}).eq("user_id", str(user_id)).execute()
        context.user_data['state'] = 'AWAITING_BOOKING'
        await update.message.reply_text(f"Nice to meet you, {text}! **When** would you like to book? (e.g., 'Tomorrow at 7pm')")
        return

    # 2. STATE: AWAITING BOOKING TIME
    if state == 'AWAITING_BOOKING':
        await process_booking_details(update, context)
        return

    # 3. STATE: AWAITING GUESTS
    if state == 'AWAITING_GUESTS':
        # Safety: If user tries to change topic to food, break out of booking
        if any(w in text_lower for w in ["burger", "order", "food", "menu"]):
            context.user_data.clear()
            await update.message.reply_text("⚠️ Booking cancelled. Switching to ordering...")
            # Fall through to Order Intent below
        else:
            try:
                guests = int(''.join(filter(str.isdigit, text)))
                if guests < 1: raise ValueError
                
                partial = context.user_data.get('partial_booking')
                session = get_user_session(user_id)
                await finalize_booking(update, context, partial['date'], partial['time'], guests, session['current_restaurant_id'])
                return
            except:
                await update.message.reply_text("🔢 Please enter a valid number of guests (e.g. '4').")
                return

    # 4. STATE: AWAITING TABLE
    if state == 'AWAITING_TABLE':
        supabase.table("user_sessions").update({"table_number": text}).eq("user_id", str(user_id)).execute()
        del context.user_data['state']
        await update.message.reply_text(f"✅ Table {text} confirmed! You can now order food.")
        return

    # --- INTENT DETECTION ---

    # A. Booking Intent
    if any(k in text_lower for k in ["book", "reserve", "reservation"]):
        session = get_user_session(user_id)
        if not session:
            await update.message.reply_text("Please type /start first.")
            return

        if not session.get('customer_name'):
            context.user_data['state'] = 'AWAITING_NAME'
            await update.message.reply_text("👋 Before we book, **what is your name?**")
        else:
            context.user_data['state'] = 'AWAITING_BOOKING'
            await update.message.reply_text("Sure! **When** would you like to book? (e.g., 'Tomorrow at 7pm')")
        return

    # B. Order Intent
    if any(k in text_lower for k in ["order", "have", "eat", "menu", "drink"]) or "cancel" in text_lower:
        session = get_user_session(user_id)
        if not session:
             await update.message.reply_text("Please type /start first.")
             return
             
        if not session.get('table_number'):
            context.user_data['state'] = 'AWAITING_TABLE'
            await update.message.reply_text("🍽️ **What is your Table Number?**")
            return
            
        # Process Order
        reply = await process_order(text, update.effective_user, session['current_restaurant_id'], session['table_number'], update.message.chat_id)
        await update.message.reply_text(reply)
        return

    # C. General Chat / Questions
    session = get_user_session(user_id)
    if session:
        # Fetch small menu snippet for context
        menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", session['current_restaurant_id']).limit(30).execute()
        menu = "\n".join([m['content'] for m in menu_res.data]) if menu_res.data else ""
        
        reply, _ = await call_groq(f"Menu Info: {menu}\nUser Question: {text}", "Restaurant Concierge. Be brief and helpful.")
        if reply: await update.message.reply_text(reply)

# --- STARTUP ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Defaults
    rest_id = "test" 
    
    # Try to get a real restaurant ID from DB if not provided
    try:
        first = supabase.table("restaurants").select("id").limit(1).execute()
        if first.data:
            rest_id = first.data[0]['id']
    except:
        pass

    # Upsert Session
    data = {
        "user_id": str(user_id),
        "current_restaurant_id": str(rest_id),
        "customer_name": update.effective_user.full_name, # Default to telegram name
        "table_number": None
    }
    supabase.table("user_sessions").upsert(data).execute()
    
    context.user_data.clear()
    await update.message.reply_text(f"👋 Welcome to the Restaurant AI!\n\n🔹 To Book: Say **'Book a table'**\n🔹 To Order: Say **'I want a burger'**")

# --- SERVER ---
app = FastAPI()

if TELEGRAM_TOKEN:
    ptb_app = Application.builder().token(TELEGRAM_TOKEN).build()
    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
else:
    print("⚠️ WARNING: TELEGRAM_BOT_TOKEN not found.")

@app.on_event("startup")
async def startup():
    if TELEGRAM_TOKEN:
        await ptb_app.initialize()
        await ptb_app.start()
        print("🤖 Bot Started")

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)
    except Exception as e:
        print(f"Webhook error: {e}")
    return {"status": "ok"}

@app.get("/")
async def root(): return {"status": "System Online"}