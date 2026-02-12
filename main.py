import os
import json
import asyncio
from datetime import datetime
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

# --- CORE LOGIC: BOOKING HANDLER ---
async def process_booking_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    user = update.effective_user
    
    # Get stored session info
    session = get_user_session(user.id)
    rest_id = session['current_restaurant_id']
    
    # AI Extraction
    # We explicitly provide the Year to fix the date issue
    now = datetime.now()
    current_context = f"{now.strftime('%Y-%m-%d')} (Year: {now.year})"
    
    prompt = f"""
    Extract booking details from: "{user_text}"
    Current Context: {current_context}
    
    INSTRUCTIONS:
    1. If the user mentions a month (e.g. August), assume it is for the current year ({now.year}) unless specified.
    2. Convert time to 24-hour format (HH:MM).
    3. Return JSON ONLY.
    
    Output Format:
    {{
      "valid": true,
      "date": "YYYY-MM-DD",
      "time": "HH:MM",
      "guests": 2
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
            await update.message.reply_text("Please specify the **Date**, **Time**, and **Number of Guests**.")
            return

        booking_time = f"{data['date']} {data['time']}:00"

        # Check for conflicts (Simple Check)
        slot_bookings = supabase.table("bookings").select("*", count="exact")\
            .eq("restaurant_id", rest_id)\
            .eq("booking_time", booking_time)\
            .execute()
            
        if slot_bookings.count >= 10:
            await update.message.reply_text(f"❌ {data['time']} is fully booked. Please try another time.")
            return

        # Insert Booking (Using the NAME from Session)
        booking = {
            "restaurant_id": str(rest_id),
            "user_id": str(user.id),
            "customer_name": session.get('customer_name', user.full_name), # Use the recorded name!
            "party_size": int(data['guests']),
            "booking_time": booking_time,
            "status": "confirmed"
        }
        supabase.table("bookings").insert(booking).execute()
        
        # Success! Clear state.
        del context.user_data['state']
        await update.message.reply_text(f"✅ **Booking Confirmed!**\n📅 {data['date']}\n⏰ {data['time']}\n👤 {data['guests']} Guests\n\nName: {booking['customer_name']}")
        
    except Exception as e:
        print(f"Booking Error: {e}")
        await update.message.reply_text("❌ Error processing details. Please try saying: 'Table for 2 tomorrow at 5pm'")

# --- GATEKEEPER ---
async def check_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ensures we have a name. Returns True if good to go."""
    user_id = update.effective_user.id
    session = get_user_session(user_id)
    
    if not session:
        await update.message.reply_text("⚠️ Please scan QR code first.")
        return False

    if not session.get('customer_name'):
        context.user_data['state'] = 'AWAITING_NAME' # Lock state
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
        # Name saved. Now, what was the user trying to do?
        # Default to asking for booking details
        context.user_data['state'] = 'AWAITING_BOOKING'
        await update.message.reply_text(f"Nice to meet you, {text}! Now, please tell me: **When would you like to book?** (e.g., 'August 4th at 3pm')")
        return

    # 2. STATE: AWAITING BOOKING DETAILS
    if state == 'AWAITING_BOOKING':
        await process_booking_details(update, context)
        return

    # 3. STATE: AWAITING TABLE (For Orders)
    if state == 'AWAITING_TABLE':
        supabase.table("user_sessions").update({"table_number": text}).eq("user_id", str(user_id)).execute()
        del context.user_data['state'] # Unlock
        await update.message.reply_text(f"Table {text} set! You can now order food.")
        return

    # --- NO STATE? CHECK INTENT ---
    text_lower = text.lower()

    # A. Booking Intent
    if any(k in text_lower for k in ["book", "reserve"]):
        # Check Name First
        if await check_name(update, context):
            # Name exists -> Set State & Ask Details
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
            
        # Process Order
        rest_id = session['current_restaurant_id']
        reply = await process_order(text, update.effective_user, rest_id, session['table_number'], update.message.chat_id)
        await update.message.reply_text(reply)
        return

    # C. General Chat
    session = get_user_session(user_id)
    if session:
        # Fetch Menu Context
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
        # Fallback for testing without QR
        first = supabase.table("restaurants").select("id").limit(1).execute()
        rest_id = first.data[0]['id'] if first.data else "test"

    # Reset Session (Wipe Table, Keep Name)
    current = get_user_session(user_id)
    name = current['customer_name'] if current else None
    
    supabase.table("user_sessions").upsert({
        "user_id": str(user_id),
        "current_restaurant_id": rest_id,
        "customer_name": name, # Preserve name if exists
        "table_number": None   # Wipe table
    }).execute()
    
    # Clear any stuck states
    context.user_data.clear()
    
    msg = f"👋 Welcome back, {name}!" if name else "👋 Welcome!"
    await update.message.reply_text(f"{msg}\n\nSay **'Book a table'** or **'Order food'**.")

# --- SERVER ---
request = HTTPXRequest(connection_pool_size=10, read_timeout=30.0, connect_timeout=30.0)
ptb_app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

app = FastAPI()
@app.post("/webhook")
async def webhook(request: Request, bg: BackgroundTasks):
    data = await request.json()
    bg.add_task(lambda: asyncio.create_task(ptb_app.process_update(Update.de_json(data, ptb_app.bot))))
    return {"status": "ok"}

@app.on_event("startup")
async def startup(): await ptb_app.initialize(); await ptb_app.start()