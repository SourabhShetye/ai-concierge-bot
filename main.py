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
from groq import Groq
from order_service import process_order

# --- CONFIGURATION ---
load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# --- HELPER: TIMEZONE (DUBAI UTC+4) ---
def get_dubai_time():
    """Returns current time in Dubai"""
    return datetime.now(timezone.utc) + timedelta(hours=4)

# --- HELPER: GET USER SESSION ---
def get_user_session(user_id):
    res = supabase.table("user_sessions").select("*").eq("user_id", str(user_id)).execute()
    return res.data[0] if res.data else None

# --- AI WRAPPER ---
async def call_groq(prompt, system_role="You are a helpful assistant."):
    try:
        completion = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_role}, {"role": "user", "content": prompt}],
            temperature=0, max_tokens=500
        )
        return completion.choices[0].message.content, None
    except Exception as e:
        return None, str(e)

# --- CORE LOGIC: BOOKING HANDLER (Step-by-Step) ---
async def process_booking_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    user = update.effective_user
    
    # 1. Get Context
    session = get_user_session(user.id)
    rest_id = session['current_restaurant_id']
    now_dubai = get_dubai_time()
    
    # 2. AI Extraction (With STRICT Rules)
    prompt = f"""
    Extract booking details from: "{user_text}"
    
    CONTEXT:
    - Current Time (Dubai): {now_dubai.strftime('%Y-%m-%d %H:%M')}
    - Today is: {now_dubai.strftime('%A')}
    
    RULES:
    1. If the user mentions a date (e.g. "tomorrow"), calculate the actual date based on Dubai time above.
    2. Convert time to 24-hour format (HH:MM).
    3. If the user DID NOT mention the number of people, set "guests": null. (DO NOT GUESS).
    
    Return JSON ONLY:
    {{
      "valid": true,
      "date": "YYYY-MM-DD",
      "time": "HH:MM",
      "guests": null or integer
    }}
    """
    
    response, error = await call_groq(prompt, "You are a JSON extractor. Output ONLY raw JSON.")
    
    if error or not response:
        await update.message.reply_text("📉 System busy. Please try again.")
        return

    try:
        # Robust JSON Parsing
        clean_json = response[response.find("{"):response.rfind("}")+1]
        data = json.loads(clean_json)
        
        if not data.get("valid"):
            await update.message.reply_text("Please specify the **Date** and **Time** for your reservation.")
            return

        # --- STEP 3: MISSING GUESTS CHECK ---
        if data.get('guests') is None:
            # We store the Date/Time we found, and switch state to ask for guests
            context.user_data['partial_booking'] = data
            context.user_data['state'] = 'AWAITING_GUESTS'
            await update.message.reply_text("Great! And **how many people** will be joining?")
            return

        # If we have everything, finalize it
        await finalize_booking(update, context, data['date'], data['time'], data['guests'], rest_id)
        
    except Exception as e:
        print(f"Booking Error: {e}")
        await update.message.reply_text("❌ Error processing details. Please try saying: 'Tomorrow at 7pm'")

async def finalize_booking(update, context, date, time, guests, rest_id):
    user = update.effective_user
    session = get_user_session(user.id)
    
    # Combine Date & Time
    booking_time = f"{date} {time}:00"

    # Check Availability (10 table limit)
    slot_bookings = supabase.table("bookings").select("*", count="exact")\
        .eq("restaurant_id", rest_id)\
        .eq("booking_time", booking_time)\
        .execute()
        
    if slot_bookings.count >= 10:
        await update.message.reply_text(f"❌ {time} is fully booked. Please try another time.")
        return

    # Insert Booking
    booking = {
        "restaurant_id": str(rest_id),
        "user_id": str(user.id),
        "customer_name": session.get('customer_name', user.full_name),
        "party_size": int(guests),
        "booking_time": booking_time,
        "status": "confirmed"
    }
    supabase.table("bookings").insert(booking).execute()
    
    # Clear state
    if 'state' in context.user_data: del context.user_data['state']
    if 'partial_booking' in context.user_data: del context.user_data['partial_booking']
    
    await update.message.reply_text(f"✅ **Booking Confirmed!**\n📅 {date}\n⏰ {time}\n👤 {guests} Guests")

# --- GATEKEEPER ---
async def check_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_user_session(user_id)
    
    if not session:
        await update.message.reply_text("⚠️ Please scan QR code first.")
        return False

    if not session.get('customer_name'):
        context.user_data['state'] = 'AWAITING_NAME'
        await update.message.reply_text("👋 Welcome! Before we book, **what is your name?**")
        return False
    return True

# --- MAIN ROUTER ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    state = context.user_data.get('state')

    # 1. STATE: AWAITING NAME
    if state == 'AWAITING_NAME':
        supabase.table("user_sessions").update({"customer_name": text}).eq("user_id", str(user_id)).execute()
        context.user_data['state'] = 'AWAITING_BOOKING'
        await update.message.reply_text(f"Nice to meet you, {text}! Now, **When would you like to book?** (e.g. 'Tomorrow at 7pm')")
        return

    # 2. STATE: AWAITING BOOKING TIME
    if state == 'AWAITING_BOOKING':
        await process_booking_details(update, context)
        return

    # 3. STATE: AWAITING GUESTS (New Step!)
    if state == 'AWAITING_GUESTS':
        # Simple extraction of number from text
        try:
            # Simple heuristic: find first number in text
            guests = int(''.join(filter(str.isdigit, text)))
            # Retrieve partial info
            partial = context.user_data.get('partial_booking')
            session = get_user_session(user_id)
            await finalize_booking(update, context, partial['date'], partial['time'], guests, session['current_restaurant_id'])
            return
        except:
            await update.message.reply_text("🔢 Please enter a valid number (e.g. '4').")
            return

    # 4. STATE: AWAITING TABLE (Orders)
    if state == 'AWAITING_TABLE':
        supabase.table("user_sessions").update({"table_number": text}).eq("user_id", str(user_id)).execute()
        del context.user_data['state']
        await update.message.reply_text(f"Table {text} set! You can now order food.")
        return

    # --- NO STATE? CHECK INTENT ---
    text_lower = text.lower()

    # A. Booking Intent
    if any(k in text_lower for k in ["book", "reserve"]):
        if await check_name(update, context):
            context.user_data['state'] = 'AWAITING_BOOKING'
            await update.message.reply_text("Sure! **When** would you like to book? (e.g. 'Tomorrow at 7pm')")
        return

    # B. Order Intent
    if any(k in text_lower for k in ["order", "have", "cancel", "eat"]):
        session = get_user_session(user_id)
        if not session.get('table_number'):
            context.user_data['state'] = 'AWAITING_TABLE'
            await update.message.reply_text("🍽️ **What is your Table Number?**")
            return
        rest_id = session['current_restaurant_id']
        reply = await process_order(text, update.effective_user, rest_id, session['table_number'], update.message.chat_id)
        await update.message.reply_text(reply)
        return

    # C. General Chat
    session = get_user_session(user_id)
    if session:
        menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", session['current_restaurant_id']).limit(40).execute()
        menu = "\n".join([m['content'] for m in menu_res.data]) if menu_res.data else "No menu."
        reply, _ = await call_groq(f"Context: {menu}\nUser: {text}", "Restaurant Concierge")
        if reply: await update.message.reply_text(reply)

# --- STARTUP ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    rest_id = args[0] if args else "default_rest_id"
    
    if rest_id == "default_rest_id":
        first = supabase.table("restaurants").select("id").limit(1).execute()
        rest_id = first.data[0]['id'] if first.data else "test"

    current = get_user_session(user_id)
    name = current['customer_name'] if current else None
    
    supabase.table("user_sessions").upsert({
        "user_id": str(user_id),
        "current_restaurant_id": rest_id,
        "customer_name": name,
        "table_number": None
    }).execute()
    
    context.user_data.clear()
    msg = f"👋 Welcome back, {name}!" if name else "👋 Welcome!"
    await update.message.reply_text(f"{msg}\n\nSay **'Book a table'** or **'Order food'**.")

# --- SERVER ---
request = HTTPXRequest(connection_pool_size=10, read_timeout=30.0, connect_timeout=30.0)
ptb_app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

app = FastAPI()
async def process_telegram_update(data):
    await ptb_app.process_update(Update.de_json(data, ptb_app.bot))

@app.post("/webhook")
async def webhook(request: Request, bg: BackgroundTasks):
    data = await request.json()
    bg.add_task(process_telegram_update, data)
    return {"status": "ok"}

@app.get("/")
async def root(): return {"status": "Online"}

@app.on_event("startup")
async def startup(): await ptb_app.initialize(); await ptb_app.start()