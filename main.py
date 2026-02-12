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
from order_service import process_order

# --- CONFIGURATION ---
load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# --- HELPER: GET USER CONTEXT ---
def get_user_session(user_id):
    res = supabase.table("user_sessions").select("*").eq("user_id", str(user_id)).execute()
    return res.data[0] if res.data else None

# --- CORE: SMART AI CLIENT ---
async def call_groq(prompt, system_role="You are a helpful assistant."):
    try:
        completion = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_role}, {"role": "user", "content": prompt}],
            temperature=0, max_tokens=500
        )
        return completion.choices[0].message.content, None
    except Exception as e:
        return None, str(e)

# --- FEATURE 1: ROBUST BOOKING (Prevents Double Booking) ---
async def handle_booking(update: Update, user_text: str, rest_id: str):
    user = update.effective_user
    
    # 1. Check if user ALREADY has a booking today (Prevent Spam)
    today = datetime.now().strftime("%Y-%m-%d")
    existing = supabase.table("bookings").select("*")\
        .eq("user_id", str(user.id))\
        .ilike("booking_time", f"{today}%")\
        .execute()
        
    if existing.data:
        await update.message.reply_text(f"⚠️ You already have a reservation for today at {existing.data[0]['booking_time'].split(' ')[1]}.")
        return

    # 2. Extract Details
    prompt = f"""
    Extract booking: "{user_text}"
    Today: {today}
    Return JSON: {{"valid": true, "date": "YYYY-MM-DD", "time": "HH:MM", "guests": 2}}
    """
    response, error = await call_groq(prompt, "JSON Extractor")
    
    if error or not response:
        await update.message.reply_text("📉 System busy. Try again.")
        return

    try:
        data = json.loads(response.replace("```json", "").replace("```", "").strip())
        if not data.get("valid"):
            await update.message.reply_text("Please specify Date, Time, and Number of Guests.")
            return

        booking_time = f"{data['date']} {data['time']}"

        # 3. CHECK AVAILABILITY (Using the SQL function we created)
        # We manually check count instead of calling the function to avoid RPC complexity issues
        slot_count = supabase.table("bookings").select("*", count="exact")\
            .eq("restaurant_id", rest_id)\
            .eq("booking_time", booking_time)\
            .execute().count
            
        if slot_count >= 10:
            await update.message.reply_text(f"❌ Sorry, {data['time']} is fully booked. Please try a different time.")
            return

        # 4. Insert Booking
        booking = {
            "restaurant_id": str(rest_id),
            "user_id": str(user.id),
            "customer_name": user.full_name,
            "party_size": int(data['guests']),
            "booking_time": booking_time,
            "status": "confirmed"
        }
        supabase.table("bookings").insert(booking).execute()
        await update.message.reply_text(f"✅ **Confirmed!**\n📅 {data['date']} @ {data['time']}\n👤 {data['guests']} Guests")
        
    except Exception as e:
        print(f"Booking Error: {e}")
        await update.message.reply_text("❌ Error processing booking.")

# --- FEATURE 2: MENU CHAT ---
async def handle_chat(update: Update, user_text: str, rest_id: str):
    # Fetch menu items dynamically
    try:
        menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(40).execute()
        menu_txt = "\n".join([m['content'] for m in menu_res.data]) if menu_res.data else "Menu not available."
    except:
        menu_txt = "Menu not available."
        
    prompt = f"Context: Restaurant Menu.\nMENU: {menu_txt}\nUser Query: {user_text}\nAnswer briefly."
    reply, _ = await call_groq(prompt)
    if reply: await update.message.reply_text(reply)

# --- GATEKEEPER: THE STRICT INTERCEPTOR ---
async def check_context(update: Update, context: ContextTypes.DEFAULT_TYPE, needs_table=False):
    user_id = update.effective_user.id
    session = get_user_session(user_id)
    
    if not session:
        await update.message.reply_text("⚠️ Please scan a QR code to start.")
        return False
        
    # 1. Enforce Name
    if not session.get('customer_name'):
        context.user_data['awaiting'] = 'name'
        await update.message.reply_text("👋 Welcome! **What is your name?**")
        return False
        
    # 2. Enforce Table (Only if ordering/service)
    if needs_table and not session.get('table_number'):
        context.user_data['awaiting'] = 'table'
        await update.message.reply_text("🍽️ **What is your Table Number?** (Check the sticker on your table)")
        return False
        
    return True

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    
    # FALLBACK: If no ID provided, check if we have a default one or ask user to scan
    rest_id = args[0] if args else "default_rest_id" 
    
    # Verify ID exists, if "default_rest_id", maybe fetch the first one from DB
    if rest_id == "default_rest_id":
        first_rest = supabase.table("restaurants").select("id").limit(1).execute()
        if first_rest.data:
            rest_id = first_rest.data[0]['id']
        else:
            await update.message.reply_text("❌ System Error: No restaurants found in DB.")
            return

    # Reset Session (Force Table Check on new scan)
    session_data = {
        "user_id": str(user_id),
        "current_restaurant_id": rest_id,
        "table_number": None # <--- WIPE TABLE
    }
    supabase.table("user_sessions").upsert(session_data).execute()
    
    await update.message.reply_text("👋 Welcome! I've reset your session.\nSay 'Book a table' or 'Order food'.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    
    # 1. Handle Context Inputs (Name/Table)
    awaiting = context.user_data.get('awaiting')
    if awaiting == 'name':
        supabase.table("user_sessions").update({"customer_name": text}).eq("user_id", str(user_id)).execute()
        del context.user_data['awaiting']
        await update.message.reply_text(f"Nice to meet you, {text}!")
        return
    if awaiting == 'table':
        supabase.table("user_sessions").update({"table_number": text}).eq("user_id", str(user_id)).execute()
        del context.user_data['awaiting']
        await update.message.reply_text(f"Table {text} set! You can now order.")
        return

    # 2. Routing
    session = get_user_session(user_id)
    if not session: 
        await update.message.reply_text("⚠️ Scan QR code first.")
        return
    
    rest_id = session['current_restaurant_id']
    text_lower = text.lower()

    # A. Booking
    if any(k in text_lower for k in ["book", "reserve"]):
        if await check_context(update, context, needs_table=False):
            await handle_booking(update, text, rest_id)
        return

    # B. Ordering / Bill / Waiter (Needs Table)
    if any(k in text_lower for k in ["order", "have", "cancel", "bill", "check", "waiter"]):
        if await check_context(update, context, needs_table=True):
            # Service Routing
            if "bill" in text_lower or "check" in text_lower:
                # Bill Logic
                orders = supabase.table("orders").select("price").eq("table_number", session['table_number']).neq("status", "paid").execute()
                total = sum(o['price'] for o in orders.data)
                await update.message.reply_text(f"🧾 **Bill Total: ${total}**\nWaiter notified.")
                supabase.table("service_requests").insert({"restaurant_id": rest_id, "table_number": session['table_number'], "request_type": "BILL", "status": "pending"}).execute()
            elif "waiter" in text_lower:
                # Waiter Logic
                await update.message.reply_text("🔔 Waiter called.")
                supabase.table("service_requests").insert({"restaurant_id": rest_id, "table_number": session['table_number'], "request_type": "WAITER", "status": "pending"}).execute()
            else:
                # Order Logic
                reply = await process_order(text, update.effective_user, rest_id, session['table_number'], update.message.chat_id)
                await update.message.reply_text(reply)
        return

    # C. General Chat
    await handle_chat(update, text, rest_id)

# --- APP SETUP ---
request = HTTPXRequest(connection_pool_size=10, read_timeout=30.0, connect_timeout=30.0)
ptb_app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

app = FastAPI()
@app.post("/webhook")
async def webhook(request: Request, bg: BackgroundTasks):
    data = await request.json()
    bg.add_task(lambda: ptb_app.process_update(Update.de_json(data, ptb_app.bot)))
    return {"status": "ok"}
@app.on_event("startup")
async def startup(): await ptb_app.initialize(); await ptb_app.start()