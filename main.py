import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from supabase import create_client
from langchain_google_genai import GoogleGenerativeAIEmbeddings, ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

from intent import analyze_request
from dateutil import parser as date_parser # You might need: pip install python-dateutil

# 1. Load Config
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# UPDATE THIS WITH YOUR NEW NGROK URL
WEBHOOK_URL = "https://ai-concierge-bot.onrender.com" 

# 2. Initialize Clients
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
llm = ChatGoogleGenerativeAI(model="models/gemini-flash-latest", temperature=0)

# 3. CORE LOGIC: Get Context
def get_user_restaurant(user_id):
    """Checks which restaurant the user is currently at."""
    response = supabase.table("user_sessions").select("current_restaurant_id").eq("user_id", user_id).execute()
    if response.data:
        return response.data[0]['current_restaurant_id']
    return None

def get_restaurant_details(rest_id):
    """Fetches Name, WiFi, Policy for the bot to know."""
    res = supabase.table("restaurants").select("*").eq("id", rest_id).execute()
    return res.data[0] if res.data else None

# 4. RAG SEARCH (Filtered by Restaurant)
def retrieve_info(query_text: str, restaurant_id: str):
    # If user asks for "Full Menu", skip vector search
    if any(k in query_text.lower() for k in ["full menu", "whole menu", "all dishes"]):
        res = supabase.table("menu_items").select("content").eq("restaurant_id", restaurant_id).limit(20).execute()
        return "\n".join([item['content'] for item in res.data])

    # Otherwise, Vector Search
    vector = embeddings.embed_query(query_text)
    res = supabase.rpc("match_menu_items_v2", {
        "query_embedding": vector,
        "filter_restaurant_id": restaurant_id, # <--- ISOLATION HAPPENS HERE
        "match_threshold": 0.5,
        "match_count": 5
    }).execute()
    
    return "\n".join([item['content'] for item in res.data]) if res.data else "No specific info found."

# 5. AI CHAIN
template = """
You are the AI Concierge for {rest_name}.
Use the Context below to answer the user.
If asking for WiFi, use the policy data.
If asking for booking, ask for party size and time.

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

# 6. TELEGRAM HANDLERS

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles QR Code Scan: /start rest_123"""
    user_id = update.effective_user.id
    args = context.args # Captures 'rest_123'
    
    if not args:
        await update.message.reply_text("👋 Please scan a restaurant QR code to start.")
        return

    rest_id = args[0]
    
    # Verify Restaurant Exists
    details = get_restaurant_details(rest_id)
    if not details:
        await update.message.reply_text("❌ Restaurant not found.")
        return

    # SAVE SESSION
    supabase.table("user_sessions").upsert({
        "user_id": user_id, "current_restaurant_id": rest_id
    }).execute()

    await update.message.reply_text(f"👋 Welcome to {details['name']}! I have loaded the menu and policies. How can I help?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    
    # 1. Identify Restaurant (Keep existing logic)
    rest_id = get_user_restaurant(user_id)
    if not rest_id:
        await update.message.reply_text("⚠️ Please scan a QR code first.")
        return

    # 2. ANALYZE INTENT (The New Brain) 🧠
    try:
        analysis = analyze_request(user_text)
        intent = analysis.get("intent_type")
        entities = analysis.get("entities", {})
        print(f"🧠 Intent: {intent} | Entities: {entities}")
    except Exception as e:
        print(f"Intent Error: {e}")
        intent = "general_chat"

    # 3. ROUTING LOGIC
    
    # --- A. BOOKING HANDLER ---
    if intent == "booking":
        # Check if we have enough info
        if not entities.get("time") or not entities.get("party_size"):
             await update.message.reply_text("I can help with that! Please specify the **Time** and **Number of People**.")
             return

        # Parse Time (Simple version)
        try:
            booking_dt = date_parser.parse(entities["time"])
            
            # Check DB Availability
            is_available = supabase.rpc("check_availability", {
                "check_rest_id": rest_id,
                "check_time": booking_dt.isoformat()
            }).execute()
            
            if is_available.data:
                # Insert Booking
                supabase.table("bookings").insert({
                    "restaurant_id": rest_id,
                    "user_phone": str(user_id),
                    "party_size": entities["party_size"],
                    "booking_time": booking_dt.isoformat()
                }).execute()
                await update.message.reply_text(f"✅ Confirmed! Table for {entities['party_size']} at {booking_dt.strftime('%H:%M')}.")
            else:
                await update.message.reply_text("❌ Sorry, we are fully booked at that time.")
                
        except Exception as e:
            await update.message.reply_text("⚠️ I couldn't understand that date. Try 'Tomorrow at 7pm'.")

    # --- B. MENU SEARCH ---
    elif intent == "menu_search":
        # Use your existing retrieve_info function, but focus it
        response = await chain.ainvoke({
            "question": user_text,
            "rest_id": rest_id,
            "rest_name": "Restaurant", # You can fetch the real name
            "policy": "" # Not needed for menu search
        })
        await update.message.reply_text(response)

    # --- C. POLICY / GENERAL ---
    else:
        # Fallback to general RAG
        details = get_restaurant_details(rest_id)
        policy_info = f"WiFi: {details['wifi_password']}. Docs: {details['policy_docs']}"
        
        response = await chain.ainvoke({
            "question": user_text,
            "rest_id": rest_id,
            "rest_name": details['name'],
            "policy": policy_info
        })
        await update.message.reply_text(response)

# 7. SERVER SETUP
app = FastAPI()
req = HTTPXRequest(connection_pool_size=8, read_timeout=30.0, connect_timeout=30.0)
ptb_app = Application.builder().token(TELEGRAM_TOKEN).request(req).build()

ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

@app.on_event("startup")
async def on_startup():
    await ptb_app.initialize()
    await ptb_app.start()
    await ptb_app.bot.set_webhook(f"{WEBHOOK_URL}/webhook")

@app.get("/")
def home():
    return {"status": "alive", "message": "Concierge Bot is running"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return {"status": "ok"}