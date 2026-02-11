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

# --- 1. CONFIGURATION ---
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# --- 2. SETUP CLIENTS ---
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

# --- 3. ROBUST AI FUNCTION (GROQ) ---
async def call_ai(prompt_text, system_role="You are a helpful assistant."):
    try:
        completion = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_role},
                {"role": "user", "content": prompt_text}
            ],
            temperature=0.3, # Slight creativity for natural conversation
            max_tokens=500,
        )
        return completion.choices[0].message.content, None
    except Exception as e:
        return None, str(e)

# --- 4. LOGIC HANDLERS ---

async def handle_booking(update: Update, user_text: str, rest_id: str):
    extraction_prompt = f"""
    Extract booking details from this text: "{user_text}"
    Current Date: {datetime.now().strftime("%Y-%m-%d")}
    
    Return ONLY a JSON object with this format:
    {{
      "valid": true,
      "date": "YYYY-MM-DD",
      "time": "HH:MM",
      "guests": 2,
      "missing_info": "Ask for missing info"
    }}
    """
    
    response_text, error = await call_ai(extraction_prompt, "You are a JSON extractor.")
    
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
        await update.message.reply_text("❌ Database Error. Please contact admin.")

async def handle_chat(update: Update, user_text: str, rest_id: str, details: dict):
    # ✅ SMART UPGRADE: Fetch WHOLE Menu (Limit 100 items)
    # Llama 3 has a huge context window, so we can feed it EVERYTHING.
    try:
        res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(100).execute()
        if res.data:
            menu_list = [f"- {item['content']}" for item in res.data]
            menu_context = "\n".join(menu_list)
        else:
            menu_context = "Menu is currently empty."
    except:
        menu_context = "Menu unavailable."

    # Robust System Prompt
    system_role = f"""
    You are the AI Concierge for {details['name']}.
    Your goal is to be helpful, polite, and drive sales.
    
    RESTAURANT DETAILS:
    - WiFi Password: {details.get('wifi_password', 'Not available')}
    - Policies: {details.get('policy_docs', 'Ask staff')}
    
    FULL MENU:
    {menu_context}
    
    INSTRUCTIONS:
    1. Answer based ONLY on the Menu above.
    2. If the user asks for recommendations (e.g., "spicy", "vegan"), scan the FULL MENU to find matches.
    3. If the answer is not in the menu, apologize and say you don't know.
    4. Keep answers short (under 3 sentences).
    """
    
    response_text, error = await call_ai(user_text, system_role)
    
    if response_text:
        await update.message.reply_text(response_text)
    else:
        await update.message.reply_text("I'm having trouble thinking right now. Please ask staff.")

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
    
    res = supabase.table("user_sessions").select("current_restaurant_id").eq("user_id", user_id).execute()
    if not res.data:
        await update.message.reply_text("⚠️ Please scan QR code first.")
        return
    
    rest_id = res.data[0]['current_restaurant_id']
    r_res = supabase.table("restaurants").select("*").eq("id", rest_id).execute()
    details = r_res.data[0]

    keywords = ["book", "reserve", "reservation", "party", "table"]
    if any(k in text.lower() for k in keywords):
        await handle_booking(update, text, rest_id)
    else:
        await handle_chat(update, text, rest_id, details)

# --- 6. SETUP SERVER ---
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
    return {"status": "Online", "model": "Groq Llama 3"}

@app.on_event("startup")
async def on_startup():
    await ptb_app.initialize()
    await ptb_app.start()