import os
import httpx
import asyncio
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

# 2. Clients
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# 3. CUSTOM GOOGLE CLIENT (Direct REST API)
# This bypasses the buggy Python library completely.
async def generate_gemini_response(prompt_text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GOOGLE_API_KEY}"
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{
            "parts": [{"text": prompt_text}]
        }]
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=10.0)
            
            # If 1.5-flash fails (404), fallback to gemini-pro automatically
            if response.status_code == 404:
                print("Gemini 1.5 Flash not found, falling back to Gemini Pro...")
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GOOGLE_API_KEY}"
                response = await client.post(url, json=payload, headers=headers, timeout=10.0)

            if response.status_code != 200:
                print(f"Google API Error: {response.text}")
                return "I am currently overloaded. Please ask a staff member."

            data = response.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
            
        except Exception as e:
            print(f"Network Error: {e}")
            return "I'm having trouble thinking. Please ask staff."

# 4. Initialize Bot
request = HTTPXRequest(connection_pool_size=10, read_timeout=20.0, connect_timeout=20.0)
ptb_app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

# 5. Helper Functions
def get_restaurant_details(rest_id):
    res = supabase.table("restaurants").select("*").eq("id", rest_id).execute()
    return res.data[0] if res.data else None

def retrieve_info(restaurant_id: str):
    # Safe Mode: Database Search (Grab first 15 items)
    try:
        res = supabase.table("menu_items").select("content").eq("restaurant_id", restaurant_id).limit(15).execute()
        all_items = [item['content'] for item in res.data]
        return "\n".join(all_items)
    except Exception as e:
        return "Menu information currently unavailable."

# 6. Telegram Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("👋 Please scan a restaurant QR code to start.")
        return
    rest_id = args[0]
    details = get_restaurant_details(rest_id)
    if not details:
        await update.message.reply_text("❌ Restaurant not found.")
        return
    supabase.table("user_sessions").upsert({"user_id": user_id, "current_restaurant_id": rest_id}).execute()
    await update.message.reply_text(f"👋 Welcome to {details['name']}! How can I help?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    
    # Session Check
    res = supabase.table("user_sessions").select("current_restaurant_id").eq("user_id", user_id).execute()
    rest_id = res.data[0]['current_restaurant_id'] if res.data else None

    if not rest_id:
        await update.message.reply_text("⚠️ Please scan a QR code first.")
        return

    # Database Retrieval
    details = get_restaurant_details(rest_id)
    menu_context = retrieve_info(rest_id)
    policy_info = f"WiFi: {details.get('wifi_password')}. Docs: {details.get('policy_docs')}"

    # Prompt Engineering
    prompt = f"""
    You are the AI Concierge for {details['name']}.
    Use the Menu Context below to answer the user.
    If the answer isn't in the menu, be polite and say you don't know.

    Restaurant Policy/WiFi: {policy_info}
    Menu Context: {menu_context}

    User: {user_text}
    """

    # Call AI (Directly)
    ai_reply = await generate_gemini_response(prompt)
    await update.message.reply_text(ai_reply)

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