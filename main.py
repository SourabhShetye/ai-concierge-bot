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

# --- 1. CONFIGURATION ---
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# --- 2. ROBUST AI CLIENT (The Fix) ---
class RobustGeminiClient:
    def __init__(self, api_key):
        self.api_key = api_key
        # Priority list: Try newest/fastest first, fall back to older/stable
        self.models = [
            "gemini-1.5-flash",
            "gemini-1.5-pro",
            "gemini-1.0-pro",
            "gemini-pro"
        ]
        self.versions = ["v1beta", "v1"]
        self.working_url = None

    async def generate(self, prompt_text):
        """
        Tries every combination of Model + Version until one works.
        """
        # If we found a working URL before, try it first
        if self.working_url:
            result = await self._call_api(self.working_url, prompt_text)
            if result: return result
            print("⚠️ Previously working model failed. Entering retry mode...")

        # If not, iterate through everything
        for version in self.versions:
            for model in self.models:
                url = f"https://generativelanguage.googleapis.com/{version}/models/{model}:generateContent?key={self.api_key}"
                print(f"🔄 Trying model: {model} ({version})...")
                
                result = await self._call_api(url, prompt_text)
                if result:
                    self.working_url = url # Save the winner
                    print(f"✅ Locked on to: {model}")
                    return result
        
        print("❌ ALL AI MODELS FAILED.")
        return None

    async def _call_api(self, url, prompt):
        headers = {"Content-Type": "application/json"}
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        
        async with httpx.AsyncClient() as client:
            try:
                # 30s timeout for stability in your region
                response = await client.post(url, json=payload, headers=headers, timeout=30.0)
                
                if response.status_code == 200:
                    data = response.json()
                    try:
                        return data["candidates"][0]["content"]["parts"][0]["text"]
                    except (KeyError, IndexError):
                        return None
                elif response.status_code == 429:
                    print("⚠️ Rate Limit (429).")
                else:
                    print(f"⚠️ API Error {response.status_code}: {response.text}")
            except Exception as e:
                print(f"⚠️ Network Error: {e}")
        return None

# Initialize Global Clients
ai_client = RobustGeminiClient(GOOGLE_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialize Bot Application (Global Scope)
request = HTTPXRequest(connection_pool_size=10, read_timeout=30.0, connect_timeout=30.0)
ptb_app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

# --- 3. LOGIC HANDLERS ---

async def handle_booking(update: Update, user_text: str, rest_id: str):
    print(f"📝 Processing Booking: {user_text}")
    
    extraction_prompt = f"""
    You are a booking assistant. Extract details from: "{user_text}"
    Current Date: {datetime.now().strftime("%Y-%m-%d")}
    
    Return JSON ONLY (no markdown):
    {{
      "valid": true or false,
      "date": "YYYY-MM-DD",
      "time": "HH:MM",
      "guests": 2,
      "missing_info": "Ask for missing date/time/people"
    }}
    """
    
    response = await ai_client.generate(extraction_prompt)
    
    if not response:
        await update.message.reply_text("📉 Connection unstable. Please try: 'Book table for 2 tomorrow at 8pm'")
        return

    try:
        clean_json = response.replace("```json", "").replace("```", "").strip()
        details = json.loads(clean_json)
        
        if not details.get("valid"):
            await update.message.reply_text(details.get("missing_info", "Please provide Date, Time, and Party Size."))
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
        
        print(f"💾 Saving to DB: {booking_data}")
        supabase.table("bookings").insert(booking_data).execute()
        
        await update.message.reply_text(f"✅ **Booking Confirmed!**\n📅 {details['date']}\n⏰ {details['time']}\n👤 {details['guests']} Guests")
        
    except Exception as e:
        print(f"❌ DB Error: {e}")
        await update.message.reply_text("❌ Database Error. Please contact admin to Disable RLS.")

async def handle_chat(update: Update, user_text: str, rest_id: str, details: dict):
    # Fetch Menu (Safe Limit)
    try:
        res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(15).execute()
        menu = "\n".join([i['content'] for i in res.data])
    except:
        menu = "Menu unavailable."

    prompt = f"""
    Role: Concierge for {details['name']}.
    Menu: {menu}
    Wifi: {details.get('wifi_password')}
    User: {user_text}
    Task: Answer nicely.
    """
    
    response = await ai_client.generate(prompt)
    if response:
        await update.message.reply_text(response)
    else:
        await update.message.reply_text("I'm having trouble thinking. Please ask a waiter.")

# --- 4. TELEGRAM EVENTS ---

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

# Register Handlers
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# --- 5. FASTAPI SERVER ---
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
    return {"status": "Online", "ai_model": ai_client.working_url or "Searching..."}

@app.on_event("startup")
async def on_startup():
    await ptb_app.initialize()
    await ptb_app.start()
    # Pre-warm the AI
    print("🔥 Warming up AI...")
    await ai_client.generate("Hello")