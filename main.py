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

# 2. Clients
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# 3. INITIALIZE BOT (Crucial Step - Definition)
request = HTTPXRequest(connection_pool_size=10, read_timeout=20.0, connect_timeout=20.0)
ptb_app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

# 4. DYNAMIC MODEL DISCOVERY
async def find_working_model():
    global CURRENT_MODEL_NAME
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GOOGLE_API_KEY}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, timeout=10.0)
            data = response.json()
            if "models" not in data: return
            
            # Prefer flash or pro
            for model in data["models"]:
                if "generateContent" in model.get("supportedGenerationMethods", []):
                    name = model["name"]
                    if "flash" in name or "pro" in name:
                        CURRENT_MODEL_NAME = name
                        print(f"✅ Using Model: {CURRENT_MODEL_NAME}")
                        return
            # Fallback
            if data["models"]: CURRENT_MODEL_NAME = data["models"][0]["name"]
        except: pass

# 5. CUSTOM AI CLIENT
async def generate_gemini_response(prompt_text):
    global CURRENT_MODEL_NAME
    if not CURRENT_MODEL_NAME: await find_working_model()
    
    url = f"https://generativelanguage.googleapis.com/v1beta/{CURRENT_MODEL_NAME}:generateContent?key={GOOGLE_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt_text}]}]}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=15.0)
            if response.status_code == 404: CURRENT_MODEL_NAME = None # Reset if failed
            if response.status_code != 200: return None
            
            data = response.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except: return None

# 6. BOOKING LOGIC
async def handle_booking(update: Update, user_text: str, rest_id: str):
    extraction_prompt = f"""
    Extract booking details from this text: "{user_text}"
    Current Date/Time: {datetime.now().strftime("%Y-%m-%d %H:%M")}
    
    Return ONLY a JSON object with these keys:
    - valid (boolean): true if user specified date, time, AND people.
    - date (string): YYYY-MM-DD format.
    - time (string): HH:MM format (24h).
    - guests (integer): number of people.
    - missing_info (string): what to ask for if valid is false.
    
    Do not add markdown formatting. Just the raw JSON string.
    """
    
    ai_response = await generate_gemini_response(extraction_prompt)
    
    try:
        clean_json = ai_response.replace("```json", "").replace("```", "").strip()
        details = json.loads(clean_json)
        
        if not details.get("valid"):
            await update.message.reply_text(details.get("missing_info", "Could you provide the date, time, and party size?"))
            return

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
        await update.message.reply_text(f"✅ Booking Confirmed!\n📅 Date: {details['date']}\n⏰ Time: {details['time']}\n👥 Guests: {details['guests']}")
        
    except Exception as e:
        print(f"Booking Error: {e}")
        await update.message.reply_text("I understood you want to book, but I need the Date, Time, and Number of People clearly stated.")

# 7. TELEGRAM HANDLERS
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

    # Check for Booking Intent
    keywords = ["book", "reserve", "table", "reservation", "booking"]
    if any(k in user_text.lower() for k in keywords):
        await handle_booking(update, user_text, rest_id)
        return

    # Normal Chat / Menu Query
    try:
        menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(15).execute()
        menu_context = "\n".join([item['content'] for item in menu_res.data])
    except:
        menu_context = "Menu currently unavailable."

    prompt = f"""
    You are the AI Concierge.
    Context: {menu_context}
    User: {user_text}
    Answer politely. If answer not in context, say so.
    """
    
    response = await generate_gemini_response(prompt)
    if response:
        await update.message.reply_text(response)
    else:
        await update.message.reply_text("I'm having trouble thinking. Please ask staff.")

# 8. REGISTER HANDLERS (Now ptb_app is definitely defined)
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# 9. FASTAPI APP
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