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

# Import the separate order service
from order_service import process_order

# --- 1. CONFIGURATION ---
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# --- 2. CLIENTS ---
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

# --- 3. HELPER FUNCTIONS ---
def get_rest_id(user_id):
    res = supabase.table("user_sessions").select("current_restaurant_id").eq("user_id", user_id).execute()
    return res.data[0]['current_restaurant_id'] if res.data else None

async def call_groq(prompt, system_role="You are a helpful assistant."):
    try:
        completion = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_role},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=500,
        )
        return completion.choices[0].message.content, None
    except Exception as e:
        return None, str(e)

# --- 4. CORE LOGIC ---

async def handle_booking(update: Update, user_text: str, rest_id: str):
    # 1. AI Extraction
    extraction_prompt = f"""
    Extract booking details from: "{user_text}"
    Current Date: {datetime.now().strftime("%Y-%m-%d")}
    
    Return JSON ONLY:
    {{
      "valid": true,
      "date": "YYYY-MM-DD",
      "time": "HH:MM",
      "guests": 2,
      "missing_info": "Ask for missing info"
    }}
    """
    
    response_text, error = await call_groq(extraction_prompt, "You are a JSON extractor.")
    
    if error:
        await update.message.reply_text("📉 System busy. Please try again.")
        return

    try:
        clean_json = response_text.replace("```json", "").replace("```", "").strip()
        start = clean_json.find("{")
        end = clean_json.rfind("}") + 1
        clean_json = clean_json[start:end]
            
        details = json.loads(clean_json)
        
        if not details.get("valid"):
            await update.message.reply_text(details.get("missing_info", "Please provide Date, Time, and Party Size."))
            return

        user = update.effective_user
        
        # 2. Save to DB
        booking_data = {
            "restaurant_id": str(rest_id),
            "user_id": str(user.id),
            "customer_name": user.full_name or "Guest",
            "party_size": int(details['guests']),
            "booking_time": f"{details['date']} {details['time']}",
            "status": "confirmed"
        }
        
        supabase.table("bookings").insert(booking_data).execute()
        await update.message.reply_text(f"✅ **Booking Confirmed!**\n📅 {details['date']} at {details['time']}\n👤 {details['guests']} Guests")
        
    except Exception as e:
        print(f"Booking Error: {e}")
        await update.message.reply_text("❌ Database Error. Please contact admin.")

async def handle_chat(update: Update, user_text: str, rest_id: str):
    # Fetch details
    r_res = supabase.table("restaurants").select("*").eq("id", rest_id).execute()
    details = r_res.data[0]

    # Fetch Menu (Limit 50)
    try:
        res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(50).execute()
        menu = "\n".join([i['content'] for i in res.data]) if res.data else "No menu available."
    except:
        menu = "Menu unavailable."

    prompt = f"""
    Role: Concierge for {details['name']}.
    Menu: {menu}
    Wifi: {details.get('wifi_password')}
    User: {user_text}
    Answer politely and briefly.
    """
    
    response, error = await call_groq(prompt)
    if response:
        await update.message.reply_text(response)
    else:
        await update.message.reply_text("I'm having trouble thinking right now.")

# --- 5. GATEKEEPER & ROUTING ---

async def check_and_ask_context(update: Update, context: ContextTypes.DEFAULT_TYPE, required_fields=["name"]):
    user_id = update.effective_user.id
    res = supabase.table("user_sessions").select("*").eq("user_id", user_id).execute()
    
    if not res.data:
        await update.message.reply_text("⚠️ Please scan a QR code first.")
        return False
    
    session = res.data[0]
    
    if "name" in required_fields and not session.get('customer_name'):
        context.user_data['awaiting_info'] = 'name'
        await update.message.reply_text("👋 Welcome! Before we proceed, **what is your name?**")
        return False

    if "table" in required_fields and not session.get('table_number'):
        context.user_data['awaiting_info'] = 'table'
        await update.message.reply_text("🍽️ **What is your Table Number?** (Look for the sticker on the table)")
        return False
        
    return True

# 1. REPLACE THE START FUNCTION
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    
    # Handle "No QR Code"
    if not args:
        await update.message.reply_text("👋 Welcome! Please scan a restaurant QR code to begin.")
        return
    
    rest_id = args[0]
    
    # Verify Restaurant
    res = supabase.table("restaurants").select("*").eq("id", rest_id).execute()
    if not res.data:
        await update.message.reply_text("❌ Restaurant not found.")
        return
    
    details = res.data[0]
    
    # ✅ CRITICAL FIX: Reset Table Number on new scan
    # We keep 'customer_name' (people don't change names), but wipe 'table_number'.
    session_data = {
        "user_id": str(user_id),
        "current_restaurant_id": str(rest_id),
        "table_number": None # <--- THIS FORCES THE BOT TO ASK AGAIN
    }
    
    # Upsert: Updates the existing row or creates a new one
    supabase.table("user_sessions").upsert(session_data).execute()
    
    await update.message.reply_text(f"👋 Welcome to **{details['name']}**!\n\nI've reset your session. When you are ready to order, I will ask for your new table number.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    
    # A. INTERCEPT MISSING INFO
    awaiting = context.user_data.get('awaiting_info')
    
    if awaiting == 'name':
        supabase.table("user_sessions").update({"customer_name": text}).eq("user_id", user_id).execute()
        del context.user_data['awaiting_info']
        await update.message.reply_text(f"Nice to meet you, {text}! How can I help?")
        return

    if awaiting == 'table':
        supabase.table("user_sessions").update({"table_number": text}).eq("user_id", user_id).execute()
        del context.user_data['awaiting_info']
        await update.message.reply_text(f"Perfect! Table {text} is set. You can now order food.")
        return

    # B. ROUTING
    text_lower = text.lower()
    
    # --- NEW: MANUAL RESET COMMAND ---
    if text_lower in ["reset", "change table", "wrong table", "new table", "start over"]:
        # Wipe table number manually
        supabase.table("user_sessions").update({"table_number": None}).eq("user_id", user_id).execute()
        # Clean up context
        if 'awaiting_info' in context.user_data: del context.user_data['awaiting_info']
        
        await update.message.reply_text("🔄 **Table Reset!**\n\nI have forgotten your table number. When you order next, I will ask for it again.")
        return
    
    # 1. Booking Flow
    if any(k in text_lower for k in ["book", "reserve", "reservation"]):
        if not await check_and_ask_context(update, context, required_fields=["name"]): return
        rest_id = get_rest_id(user_id)
        await handle_booking(update, text, rest_id)
        return

    # 2. Ordering Flow (Needs Name + Table)
    if any(k in text_lower for k in ["order", "have", "eat", "cancel"]):
        if not await check_and_ask_context(update, context, required_fields=["name", "table"]): return
        
        session = supabase.table("user_sessions").select("*").eq("user_id", user_id).single().execute().data
        
        # Route to Order Service
        reply = await process_order(text, update.effective_user, session['current_restaurant_id'], session['table_number'], update.message.chat_id)
        await update.message.reply_text(reply)
        return

    # 3. Bill Request
    if "bill" in text_lower or "check please" in text_lower:
        if not await check_and_ask_context(update, context, required_fields=["table"]): return
        session = supabase.table("user_sessions").select("*").eq("user_id", user_id).single().execute().data
        t_num = session['table_number']
        
        orders = supabase.table("orders").select("price").eq("table_number", t_num).neq("status", "paid").execute()
        if not orders.data:
            await update.message.reply_text("You have no active orders to pay for.")
            return
            
        total = sum(float(o['price']) for o in orders.data)
        await update.message.reply_text(f"🧾 **Your Bill (Table {t_num})**\n\nTotal Due: **${total}**\n\nA waiter is coming with the machine.")
        
        # Notify Admin
        supabase.table("service_requests").insert({
            "restaurant_id": session['current_restaurant_id'],
            "table_number": t_num,
            "request_type": "BILL REQUEST",
            "status": "pending"
        }).execute()
        return

    # 4. Chat Flow
    rest_id = get_rest_id(user_id)
    if rest_id:
         await handle_chat(update, text, rest_id)

# --- 6. SERVER SETUP ---
request = HTTPXRequest(connection_pool_size=10, read_timeout=30.0, connect_timeout=30.0)
ptb_app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# ✅ THIS WAS MISSING/OBSCURED BEFORE:
app = FastAPI()

async def process_telegram_update(data: dict):
    try:
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.initialize()
        await ptb_app.process_update(update)
    except Exception as e:
        print(f"Update Error: {e}")

@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    background_tasks.add_task(process_telegram_update, data)
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "Online"}

@app.on_event("startup")
async def on_startup():
    await ptb_app.initialize()
    await ptb_app.start()