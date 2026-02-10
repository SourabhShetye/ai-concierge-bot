import os
import google.generativeai as genai
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks
from supabase import create_client
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough, RunnableLambda
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

# ✅ FIX: Configure Google DIRECTLY (Bypassing broken LangChain wrapper)
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Custom function to call Google Native API
def call_gemini_flash(prompt_object):
    # LangChain passes a "PromptValue", we need the string
    prompt_text = prompt_object.to_string()
    try:
        response = model.generate_content(prompt_text)
        return response.text
    except Exception as e:
        print(f"Gemini Error: {e}")
        return "I'm having trouble thinking right now. Please ask staff."

# 3. Initialize Bot
request = HTTPXRequest(connection_pool_size=10, read_timeout=20.0, connect_timeout=20.0)
ptb_app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

# 4. Helper Functions
def get_user_restaurant(user_id):
    response = supabase.table("user_sessions").select("current_restaurant_id").eq("user_id", user_id).execute()
    return response.data[0]['current_restaurant_id'] if response.data else None

def get_restaurant_details(rest_id):
    res = supabase.table("restaurants").select("*").eq("id", rest_id).execute()
    return res.data[0] if res.data else None

def retrieve_info(query_text: str, restaurant_id: str):
    # Safe Mode: Database Search (No Embeddings)
    try:
        res = supabase.table("menu_items").select("content").eq("restaurant_id", restaurant_id).limit(15).execute()
        all_items = [item['content'] for item in res.data]
        return "\n".join(all_items)
    except Exception as e:
        return "Menu information currently unavailable."

# 5. AI Chain (Modified for Direct Call)
template = """
You are the AI Concierge for {rest_name}.
Use the Menu Context below to answer the user.
If the answer isn't in the menu, be polite and say you don't know.

Restaurant Policy/WiFi: {policy}
Menu Context: {context}

User: {question}
"""
prompt = PromptTemplate.from_template(template)

# We use RunnableLambda to inject our custom Google call into the chain
chain = (
    RunnablePassthrough.assign(
        context=lambda x: retrieve_info(x["question"], x["rest_id"]),
    )
    | prompt
    | RunnableLambda(call_gemini_flash) 
)

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
    
    rest_id = get_user_restaurant(user_id)
    if not rest_id:
        await update.message.reply_text("⚠️ Please scan a QR code first.")
        return

    if "wifi" in user_text.lower():
        details = get_restaurant_details(rest_id)
        await update.message.reply_text(f"📶 WiFi Password: {details.get('wifi_password', 'Ask staff')}")
        return

    details = get_restaurant_details(rest_id)
    policy_info = f"WiFi: {details.get('wifi_password')}. Docs: {details.get('policy_docs')}"
    
    # Run AI
    try:
        # Note: chain.invoke() is blocking, but fast enough for Flash. 
        # Using ainvoke wrapper for compatibility.
        response = await chain.ainvoke({
            "question": user_text,
            "rest_id": rest_id,
            "rest_name": details['name'],
            "policy": policy_info
        })
        await update.message.reply_text(response)
    except Exception as e:
        print(f"Chain Error: {e}")
        await update.message.reply_text("I'm having trouble thinking right now. Please ask staff.")

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