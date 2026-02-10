import os
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks
from supabase import create_client
from langchain_google_genai import GoogleGenerativeAIEmbeddings, ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

# 1. Load Config & Global Clients (Initialize ONCE to save memory)
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Clients
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")

# ✅ Stable Model Name for this library version
llm = ChatGoogleGenerativeAI(model="gemini-pro", temperature=0)

# Initialize Bot Instance Globally
request = HTTPXRequest(connection_pool_size=10, read_timeout=20.0, connect_timeout=20.0)
ptb_app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

# 2. CORE LOGIC FUNCTIONS (Same as before)
def get_user_restaurant(user_id):
    response = supabase.table("user_sessions").select("current_restaurant_id").eq("user_id", user_id).execute()
    return response.data[0]['current_restaurant_id'] if response.data else None

def get_restaurant_details(rest_id):
    res = supabase.table("restaurants").select("*").eq("id", rest_id).execute()
    return res.data[0] if res.data else None

def retrieve_info(query_text: str, restaurant_id: str):
    if any(k in query_text.lower() for k in ["full menu", "all dishes"]):
        res = supabase.table("menu_items").select("content").eq("restaurant_id", restaurant_id).limit(20).execute()
        return "\n".join([item['content'] for item in res.data])

    vector = embeddings.embed_query(query_text)
    res = supabase.rpc("match_menu_items_v2", {
        "query_embedding": vector,
        "filter_restaurant_id": restaurant_id,
        "match_threshold": 0.5,
        "match_count": 5
    }).execute()
    return "\n".join([item['content'] for item in res.data]) if res.data else "No specific info found."

# 3. AI CHAIN
template = """
You are the AI Concierge for {rest_name}.
Use the Context below to answer the user.
If asking for WiFi, use the policy data.

Restaurant Policy/WiFi: {policy}
Menu Context: {context}

User: {question}
"""
prompt = PromptTemplate.from_template(template)
chain = (
    RunnablePassthrough.assign(
        context=lambda x: retrieve_info(x["question"], x["rest_id"]),
    )
    | prompt
    | llm
    | StrOutputParser()
)

# 4. TELEGRAM HANDLERS
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

    # Check for simple keywords first (Fast Response)
    if "wifi" in user_text.lower():
        details = get_restaurant_details(rest_id)
        await update.message.reply_text(f"📶 WiFi Password: {details.get('wifi_password', 'Ask staff')}")
        return

    # Heavy AI Processing
    details = get_restaurant_details(rest_id)
    policy_info = f"WiFi: {details.get('wifi_password')}. Docs: {details.get('policy_docs')}"
    
    response = await chain.ainvoke({
        "question": user_text,
        "rest_id": rest_id,
        "rest_name": details['name'],
        "policy": policy_info
    })
    await update.message.reply_text(response)

# Register Handlers
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# 5. FASTAPI SERVER (The "Background Task" Fix)
app = FastAPI()

async def process_telegram_update(data: dict):
    """Processes the update in the background so Telegram doesn't timeout."""
    async with ptb_app:
        await ptb_app.initialize()
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)

@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    1. Receive message.
    2. Tell Telegram '200 OK' INSTANTLY.
    3. Process logic later.
    """
    data = await request.json()
    # Add processing to background queue
    background_tasks.add_task(process_telegram_update, data)
    return {"status": "ok"}

@app.get("/")
@app.head("/")
async def root():
    return {"status": "alive", "message": "Concierge Bot is running"}

# Startup Event to initialize App
@app.on_event("startup")
async def on_startup():
    await ptb_app.initialize()