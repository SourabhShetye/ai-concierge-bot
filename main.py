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
import google.generativeai as genai

# --- 1. CONFIGURATION ---
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# --- 2. SETUP CLIENTS ---
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Configure Google AI
genai.configure(api_key=GOOGLE_API_KEY)

# --- 3. ROBUST AI FUNCTION ---
async def call_ai(prompt_text):
    """
    Tries multiple models using the Official SDK.
    Returns: (text_response, error_message)
    """
    # List of models to try in order
    models_to_try = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-1.0-pro", "gemini-pro"]
    
    last_error = None

    for model_name in models_to_try:
        try:
            print(f"🔄 Attempting to use: {model_name}")
            model = genai.GenerativeModel(model_name)
            
            # Run in a separate thread to not block Telegram
            response = await asyncio.to_thread(model.generate_content, prompt_text)
            
            if response.text:
                print(f"✅ Success with {model_name}")
                return response.text, None
                
        except Exception as e:
            print(f"❌ {model_name} Failed: {e}")
            last_error = str(e)
            
    return None, last_error

# --- 4. LOGIC HANDLERS ---

async def handle_booking(update: Update, user_text: str, rest_id: str):
    extraction_prompt = f"""
    Extract booking details from: "{user_text}"
    Current Date: {datetime.now().strftime("%Y-%m-%d")}
    
    Return JSON ONLY (no markdown):
    {{
      "valid": true,
      "date": "YYYY-MM-DD",
      "time": "HH:MM",
      "guests": 2
    }}
    """
    
    # Call AI
    response_text, error = await call_ai(extraction_prompt)
    
    if error:
        # 🚨 DEBUG MODE: Show the user exactly why it failed
        await update.message.reply_text(f"⚠️ **System Error:** Google refused the connection.\n\nError details: `{error}`")
        return

    try:
        clean_json = response_text.replace("```json", "").replace("```", "").strip()
        details = json.loads(clean_json)
        
        if not details.get("valid"):
            await update.message.reply_text("I couldn't understand the details. Please provide Date, Time, and Number of People.")
            return

        user = update.effective_user
        
        # Save to DB
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
        await update.message.reply_text(f"❌ Database Error: {e}")

async def handle_chat(update: Update, user_text: str, rest_id: str, details: dict):
    # Fetch Menu
    try:
        res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(20).execute()
        menu = "\n".join([i['content'] for i in res.data])
    except:
        menu = "Menu unavailable."

    prompt = f"""
    You are the concierge for {details['name']}.
    Menu: {menu}
    Wifi: {details.get('wifi_password')}
    User: {user_text}
    Answer politely.
    """
    
    response_text, error = await call_ai(prompt)
    
    if response_text:
        await update.message.reply_text(response_text)
    else:
        # 🚨 DEBUG MODE: Show error to user
        await update.message.reply_text(f"⚠️ **AI Error:** I cannot think right now.\n\nReason: `{error}`")

# --- 5. TELEGRAM HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("👋 Please scan a restaurant QR code.")
        return
    
    rest_id = args[0]
    res = supabase.table("restaurants").select("*").eq("id", rest_id).execute()
    
    if not res.data:
        await update.message.reply_text("❌ Restaurant not found.")
        return
        
    details = res.data[0]
    supabase.table("user_sessions").upsert({"user_id": user_id, "current_restaurant_id": rest_id}).execute()
    await update.message.reply_text(f"👋 Welcome to {details['name']}! How can I help?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    
    # Check Session
    res = supabase.table("user_sessions").select("current_restaurant_id").eq("user_id", user_id).execute()
    if not res.data:
        await update.message.reply_text("⚠️ Please scan QR code first.")
        return
    
    rest_id = res.data[0]['current_restaurant_id']
    
    # Get Rest Details
    r_res = supabase.table("restaurants").select("*").eq("id", rest_id).execute()
    details = r_res.data[0]

    # Router
    keywords = ["book", "reserve", "reservation", "party", "table"]
    if any(k in text.lower() for k in keywords):
        await handle_booking(update, text, rest_id)
    else:
        await handle_chat(update, text, rest_id, details)

# --- 6. SETUP SERVER ---
# Initialize Bot
request = HTTPXRequest(connection_pool_size=10, read_timeout=30.0, connect_timeout=30.0)
ptb_app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

app = FastAPI()

@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    async def process():
        await ptb_app.initialize()
        await ptb_app.process_update(Update.de_json(data, ptb_app.bot))
    background_tasks.add_task(process)
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "Online"}

@app.on_event("startup")
async def on_startup():
    await ptb_app.initialize()
    await ptb_app.start()