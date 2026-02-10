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

# Global variables
CURRENT_MODEL_NAME = None

# 2. Initialize Clients
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ✅ CRITICAL FIX: Initialize Bot Application HERE (Global Scope)
request = HTTPXRequest(connection_pool_size=10, read_timeout=20.0, connect_timeout=20.0)
ptb_app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

# 3. DYNAMIC MODEL DISCOVERY (Auto-Fixes 404 Errors)
async def find_working_model():
    global CURRENT_MODEL_NAME
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GOOGLE_API_KEY}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, timeout=10.0)
            data = response.json()
            if "models" not in data: return
            
            # Prefer flash or pro models
            for model in data["models"]:
                if "generateContent" in model.get("supportedGenerationMethods", []):
                    name = model["name"]
                    if "flash" in name or "pro" in name:
                        CURRENT_MODEL_NAME = name
                        print(f"✅ Found Model: {CURRENT_MODEL_NAME}")
                        return
            # Fallback to any working model
            if data["models"]: 
                CURRENT_MODEL_NAME = data["models"][0]["name"]
                print(f"⚠️ Using Fallback Model: {CURRENT_MODEL_NAME}")
        except Exception as e:
            print(f"Discovery Error: {e}")

# 4. CUSTOM AI CLIENT (With Retries & Null Checks)
async def generate_gemini_response(prompt_text, retries=2):
    global CURRENT_MODEL_NAME
    if not CURRENT_MODEL_NAME: await find_working_model()
    
    if not CURRENT_MODEL_NAME: 
        print("❌ No AI Model Found.")
        return None
    
    url = f"https://generativelanguage.googleapis.com/v1beta/{CURRENT_MODEL_NAME}:generateContent?key={GOOGLE_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt_text}]}]}

    async with httpx.AsyncClient() as client:
        for attempt in range(retries + 1):
            try:
                response = await client.post(url, json=payload, headers=headers, timeout=15.0)
                
                # If 404, force re-discovery and retry
                if response.status_code == 404: 
                    print("⚠️ Model 404. Re-discovering...")
                    await find_working_model()
                    url = f"https://generativelanguage.googleapis.com/v1beta/{CURRENT_MODEL_NAME}:generateContent?key={GOOGLE_API_KEY}"
                    continue 

                # If successful
                if response.status_code == 200:
                    data = response.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                
                # If rate limited (429), wait and retry
                if response.status_code == 429:
                    await asyncio.sleep(2)
                    continue

                print(f"⚠️ API Error {response.status_code}: {response.text}")

            except Exception as e:
                print(f"⚠️ Network Exception: {e}")
        
    return None

# 5. BOOKING LOGIC
async def handle_booking(update: Update, user_text: str, rest_id: str):
    extraction_prompt = f"""
    Extract booking details from: "{user_text}"
    Current Date: {datetime.now().strftime("%Y-%m-%d")}
    
    Return JSON ONLY:
    {{
      "valid": true/false,
      "date": "YYYY-MM-DD",
      "time": "HH:MM",
      "guests": 2,
      "missing_info": "What is missing?"
    }}
    """
    
    ai_response = await generate_gemini_response(extraction_prompt)
    
    if not ai_response:
        await update.message.reply_text("📉 System overloaded. Please try again.")
        return

    try:
        clean_json = ai_response.replace("```json", "").replace("```", "").strip()
        details = json.loads(clean_json)
        
        if not details.get("valid"):
            await update.message.reply_text(details.get("missing_info", "I need Date, Time, and Number of People."))
            return

        user = update.effective_user
        
        booking_data = {
            "restaurant_id": str(rest_id),
            "user_id": str(user.id),
            "customer_name": user.full_name or "Guest",
            "party_size": int(details['guests']),
            "booking_time": f"{details['date']} {details['time']}",
            "status": "confirmed"
        }
        
        # Save to DB
        supabase.table("bookings").insert(booking_data).execute()
        
        await update.message.reply_text(f"✅ Booking Confirmed!\n👤 Name: {user.full_name}\n📅 Date: {details['date']}\n⏰ Time: {details['time']}\n👥 Guests: {details['guests']}")
        
    except Exception as e:
        print(f"DB Error: {e}")
        error_msg = str(e)
        if "policy" in error_msg:
            await update.message.reply_text("❌ Database Permission Error. Please Disable RLS in Supabase.")
        elif "foreign key" in error_msg:
            await update.message.reply_text("❌ Error: Invalid Restaurant ID.")
        else:
            await update.message.reply_text("❌ Booking Failed. Please check with staff.")

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
    await update.message.reply_text(f"👋 Welcome to {details['name']}! Ask me about the menu or say 'Book a table'.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    
    res = supabase.table("user_sessions").select("current_restaurant_id").eq("user_id", user_id).execute()
    rest_id = res.data[0]['current_restaurant_id'] if res.data else None

    if not rest_id:
        await update.message.reply_text("⚠️ Please scan a QR code first.")
        return

    # Check for keywords
    keywords = ["book", "reserve", "table", "reservation", "booking", "party", "seat", "slot"]
    if any(k in user_text.lower() for k in keywords):
        await handle_booking(update, user_text, rest_id)
        return

    # Menu Search
    try:
        menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(15).execute()
        menu_context = "\n".join([item['content'] for item in menu_res.data])
    except:
        menu_context = "Menu unavailable."

    prompt = f"""
    You are the AI Concierge.
    Context: {menu_context}
    User: {user_text}
    Answer politely.
    """
    
    response = await generate_gemini_response(prompt)
    if response:
        await update.message.reply_text(response)
    else:
        await update.message.reply_text("I'm having trouble thinking right now. Please ask staff.")

# Register Handlers
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# 7. FastAPI App
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