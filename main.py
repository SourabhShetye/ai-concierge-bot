import os
import json
import httpx
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks
from supabase import create_client
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

# 1. Load Config
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# Global Cache for Model Name
CURRENT_MODEL_NAME = None

# 2. Clients
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
request = HTTPXRequest(connection_pool_size=10, read_timeout=20.0, connect_timeout=20.0)

# ✅ FIX 1: Initialize App GLOBALLY at the top to prevent NameError
ptb_app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

# 3. DYNAMIC MODEL DISCOVERY (Fixes 404 Errors)
async def find_working_model():
    global CURRENT_MODEL_NAME
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GOOGLE_API_KEY}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, timeout=10.0)
            data = response.json()
            if "models" not in data: return
            
            # Prefer 'flash' or 'pro'
            for model in data["models"]:
                if "generateContent" in model.get("supportedGenerationMethods", []):
                    name = model["name"]
                    if "flash" in name or "pro" in name:
                        CURRENT_MODEL_NAME = name
                        print(f"✅ Auto-Selected Model: {CURRENT_MODEL_NAME}")
                        return
            # Fallback
            if data["models"]: CURRENT_MODEL_NAME = data["models"][0]["name"]
        except Exception as e:
            print(f"Model Discovery Error: {e}")

# 4. CUSTOM AI CLIENT (Direct & Smart)
async def generate_gemini_response(prompt_text):
    global CURRENT_MODEL_NAME
    if not CURRENT_MODEL_NAME: await find_working_model()
    
    url = f"https://generativelanguage.googleapis.com/v1beta/{CURRENT_MODEL_NAME}:generateContent?key={GOOGLE_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt_text}]}]}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=15.0)
            
            # If 404, force rediscovery next time
            if response.status_code == 404: 
                CURRENT_MODEL_NAME = None
                return "I'm re-calibrating. Please ask again in a moment."
                
            if response.status_code != 200: 
                print(f"API Error: {response.text}")
                return None
            
            data = response.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"AI Error: {e}")
            return None

# 5. BOOKING LOGIC
async def handle_booking(update: Update, user_text: str, rest_id: str):
    """
    Uses AI to extract booking details and save to Supabase.
    """
    extraction_prompt = f"""
    Extract booking details from: "{user_text}"
    Current Date: {datetime.now().strftime("%Y-%m-%d")}
    
    Return ONLY a JSON object (no markdown) with keys:
    - valid (bool): true if date, time AND guests are present.
    - date (string): YYYY-MM-DD.
    - time (string): HH:MM.
    - guests (int).
    - missing_info (string): Question to ask user if valid is false.
    """
    
    ai_response = await generate_gemini_response(extraction_prompt)
    
    try:
        # Clean JSON
        clean_json = ai_response.replace("```json", "").replace("```", "").strip()
        details = json.loads(clean_json)
        
        if not details.get("valid"):
            await update.message.reply_text(details.get("missing_info", "Please provide date, time, and number of people."))
            return

        # Save to Supabase
        user = update.effective_user
        booking_data = {
            "user_id": user.id,
            "restaurant_id": rest_id,
            "booking_time": f"{details['date']}T{details['time']}:00",
            "party_size": details['guests'],
            "status": "confirmed",
            "customer_name": user.full_name or "Guest"
        }
        
        supabase.table("bookings").insert(booking_data).execute()
        await update.message.reply_text(f"✅ Booking Confirmed!\n📅 {details['date']} at {details['time']}\n👥 {details['guests']} People")
        
    except Exception as e:
        print(f"Booking Error: {e}")
        await update.message.reply_text("Please specify Date, Time, and Party Size clearly.")

# 6. Telegram Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("👋 Please scan a restaurant QR code to start.")
        return
    rest_id = args[0]
    
    res = supabase.table("restaurants").select("*").eq("id", rest_id).execute()
    details = res.data[0] if res.data else None
    
    if not details:
        await update.message.reply_text("❌ Restaurant not found.")
        return
        
    supabase.table("user_sessions").upsert({"user_id": user_id, "current_restaurant_id": rest_id}).execute()
    await update.message.reply_text(f"👋 Welcome to {details['name']}! Ask about the menu or say 'Book a table'.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    
    res = supabase.table("user_sessions").select("current_restaurant_id").eq("user_id", user_id).execute()
    rest_id = res.data[0]['current_restaurant_id'] if res.data else None

    if not rest_id:
        await update.message.reply_text("⚠️ Please scan a QR code first.")
        return

    # Check for Booking
    if any(k in user_text.lower() for k in ["book", "reserve", "table"]):
        await handle_booking(update, user_text, rest_id)
        return

    # Direct WiFi
    if "wifi" in user_text.lower():
        details = supabase.table("restaurants").select("*").eq("id", rest_id).execute().data[0]
        await update.message.reply_text(f"📶 WiFi: {details.get('wifi_password', 'Ask staff')}")
        return

    # AI Chat
    try:
        menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(15).execute()
        menu_context = "\n".join([item['content'] for item in menu_res.data])
    except:
        menu_context = "Menu unavailable."

    prompt = f"""
    You are the AI Concierge.
    Menu: {menu_context}
    User: {user_text}
    Answer politely.
    """
    
    response = await generate_gemini_response(prompt)
    if response:
        await update.message.reply_text(response)
    else:
        await update.message.reply_text("I'm having trouble thinking. Please ask staff.")

# ✅ FIX 2: Register Handlers AFTER everything is defined
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# 7. FastAPI Server
app = FastAPI()

async def process_telegram_update(data: dict):
    try:
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)
    except Exception as e:
        print(f"Update Error: {e}")

@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    background_tasks.add_task(process_telegram_update, data)
    return {"status": "ok"}

@app.get("/")
@app.head("/")
async def root():
    return {"status": "alive"}

@app.on_event("startup")
async def on_startup():
    await ptb_app.initialize()
    await ptb_app.start()
    await find_working_model()