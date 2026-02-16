import os
import json
import asyncio
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from supabase import create_client
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from groq import AsyncGroq
from order_service import process_order

# --- CONFIGURATION ---
load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# --- HELPER: TIMEZONE (DUBAI UTC+4) ---
def get_dubai_time():
    return datetime.now(timezone.utc) + timedelta(hours=4)

def get_user_session(user_id):
    try:
        res = supabase.table("user_sessions").select("*").eq("user_id", str(user_id)).execute()
        return res.data[0] if res.data else None
    except:
        return None

# --- AI WRAPPER ---
async def call_groq(prompt, system_role="You are a helpful assistant."):
    try:
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_role}, {"role": "user", "content": prompt}],
            temperature=0, max_tokens=500
        )
        return completion.choices[0].message.content, None
    except Exception as e:
        return None, str(e)

# --- BOOKING LOGIC ---
async def process_booking_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    user_id = update.effective_user.id
    
    session = get_user_session(user_id)
    if not session or not session.get('customer_name'):
        context.user_data['state'] = 'AWAITING_NAME'
        await update.message.reply_text("👋 Before we book, **what is your name?**")
        return

    now_dubai = get_dubai_time()
    prompt = f"""
    Extract booking details from: "{user_text}"
    CONTEXT: Current Time (Dubai): {now_dubai.strftime('%Y-%m-%d %H:%M')}, Today is {now_dubai.strftime('%A')}
    RULES:
    1. Calculate YYYY-MM-DD from words like "tomorrow".
    2. Convert time to 24-hour HH:MM.
    3. If guests/party size is NOT mentioned, set "guests": null.
    
    Return JSON ONLY: {{ "valid": true, "date": "YYYY-MM-DD", "time": "HH:MM", "guests": null or int }}
    """
    response, error = await call_groq(prompt, "You are a JSON extractor. Output ONLY raw JSON.")

    if error or not response:
        await update.message.reply_text("📉 System busy. Please try again.")
        return

    try:
        clean_json = response[response.find("{"):response.rfind("}")+1]
        data = json.loads(clean_json)
        
        if not data.get("valid"):
            context.user_data['state'] = 'AWAITING_BOOKING_DETAILS'
            await update.message.reply_text("🤔 I didn't catch the date or time. Please say it clearly (e.g., 'Tomorrow at 7pm').")
            return

        if data.get('guests') is None:
            context.user_data['partial_booking'] = data
            context.user_data['state'] = 'AWAITING_GUESTS'
            await update.message.reply_text("🗓️ Date & Time look good! **How many people** will be joining?")
            return

        await finalize_booking(update, context, data['date'], data['time'], data['guests'], session['current_restaurant_id'])
        
    except Exception as e:
        context.user_data['state'] = 'AWAITING_BOOKING_DETAILS'
        await update.message.reply_text("❌ I didn't understand that. Please try 'Book for 2 people tomorrow at 8pm'.")

async def finalize_booking(update, context, date, time, guests, rest_id):
    user = update.effective_user
    session = get_user_session(user.id)
    customer_name = session.get('customer_name', 'Guest')
    
    booking_time = f"{date} {time}:00"

    # Check for existing bookings to prevent double booking
    slot_bookings = supabase.table("bookings").select("*", count="exact")\
        .eq("restaurant_id", rest_id)\
        .eq("booking_time", booking_time)\
        .neq("status", "cancelled")\
        .execute()
        
    if slot_bookings.count >= 10:
        await update.message.reply_text(f"❌ {time} is fully booked. Please try another time.")
        return

    booking = {
        "restaurant_id": str(rest_id),
        "user_id": str(user.id),
        "customer_name": customer_name,
        "party_size": int(guests),
        "booking_time": booking_time,
        "status": "confirmed"
    }
    supabase.table("bookings").insert(booking).execute()
    context.user_data.clear()
    await update.message.reply_text(f"✅ **Booking Confirmed!**\n👤 {customer_name}\n📅 {date} at {time}\n👥 {guests} Guests")

# --- BILLING LOGIC (FIXED) ---
async def calculate_bill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_user_session(user_id)
    
    if not session:
        await update.message.reply_text("⚠️ No active session.")
        return

    # FIX: Added .eq("restaurant_id", ...) to prevent cross-restaurant billing
    orders = supabase.table("orders").select("*")\
        .eq("user_id", str(user_id))\
        .eq("restaurant_id", session['current_restaurant_id'])\
        .neq("status", "paid")\
        .neq("status", "cancelled")\
        .execute().data
        
    if not orders:
        await update.message.reply_text("🧾 **Your Bill:** $0.00\n(No active orders found).")
        return
        
    total = sum(float(o['price']) for o in orders)
    items_list = "\n".join([f"• {o['items']} (${o['price']})" for o in orders])
    
    await update.message.reply_text(f"🧾 **Current Bill:**\n\n{items_list}\n\n💰 **Total To Pay: ${total}**\n\n(Ask for a waiter to pay)")

# --- MAIN ROUTER ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    text_lower = text.lower()
    state = context.user_data.get('state')
    
    # 0. GLOBAL CANCEL
    if text_lower in ["cancel", "stop", "reset"]:
        context.user_data.clear()
        await update.message.reply_text("🔄 Action cancelled. How can I help?")
        return

    # 1. STATE: AWAITING NAME
    if state == 'AWAITING_NAME':
        forbidden = ["book", "order", "food", "table", "menu"]
        if any(w in text_lower for w in forbidden) and len(text.split()) < 3:
            await update.message.reply_text("⚠️ Please enter your **Name** first.")
            return

        supabase.table("user_sessions").update({"customer_name": text}).eq("user_id", str(user_id)).execute()
        context.user_data['state'] = None
        await update.message.reply_text(f"Nice to meet you, {text}! \n\nWhat would you like to do?\n🔹 **Book a table**\n🔹 **Order food**")
        return

    # 2. STATE: AWAITING GUESTS
    if state == 'AWAITING_GUESTS':
        try:
            guests = int(''.join(filter(str.isdigit, text)))
            partial = context.user_data.get('partial_booking')
            session = get_user_session(user_id)
            await finalize_booking(update, context, partial['date'], partial['time'], guests, session['current_restaurant_id'])
            return
        except:
            await update.message.reply_text("🔢 Please enter a number (e.g. '4').")
            return

    # 3. STATE: AWAITING TABLE
    if state == 'AWAITING_TABLE':
        supabase.table("user_sessions").update({"table_number": text}).eq("user_id", str(user_id)).execute()
        del context.user_data['state']
        await update.message.reply_text(f"✅ Table {text} set! You can now order food.")
        return
    
    # 3.5 STATE: AWAITING BOOKING DETAILS (Recovery)
    if state == 'AWAITING_BOOKING_DETAILS':
        await process_booking_request(update, context)
        return

    # --- INTENTS ---

    # A. BILL
    if any(k in text_lower for k in ["bill", "check", "total", "pay"]) and "order" not in text_lower:
        await calculate_bill(update, context)
        return

    # B. MENU (FIXED: Using AI Formatter)
    if "menu" in text_lower and "order" not in text_lower:
        session = get_user_session(user_id)
        if session:
            menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", session['current_restaurant_id']).limit(40).execute()
            if menu_res.data:
                # Use AI to format the menu nicely instead of brittle parsing
                raw_text = "\n".join([m['content'] for m in menu_res.data])
                formatted, _ = await call_groq(f"Format this restaurant data into a clean, readable menu list:\n{raw_text}", "Menu Formatter")
                await update.message.reply_text(f"📜 **Menu:**\n\n{formatted}\n\nSay 'I want the [Item]' to order.")
            else:
                await update.message.reply_text("🚫 No menu found for this location.")
        return

    # C. BOOKING
    if any(k in text_lower for k in ["book", "reserve", "reservation"]):
        await process_booking_request(update, context)
        return

    # D. ORDER
    if any(k in text_lower for k in ["order", "have", "eat", "drink", "want"]):
        session = get_user_session(user_id)
        if not session:
            await update.message.reply_text("⚠️ Please type /start first.")
            return

        if not session.get('table_number'):
            context.user_data['state'] = 'AWAITING_TABLE'
            await update.message.reply_text("🍽️ **What is your Table Number?**")
            return
            
        reply = await process_order(text, update.effective_user, session['current_restaurant_id'], session['table_number'], update.message.chat_id)
        await update.message.reply_text(reply)
        return

    # E. FALLBACK (FIXED: Anti-Hallucination)
    session = get_user_session(user_id)
    if session:
        menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", session['current_restaurant_id']).limit(10).execute()
        menu = "\n".join([m['content'] for m in menu_res.data]) if menu_res.data else ""
        
        # STRICT SYSTEM PROMPT
        system_prompt = """
        You are a Restaurant Concierge. 
        CRITICAL RULES:
        1. Do NOT confirm bookings yourself. If the user mentions a date/time, say: "Please explicitly say 'Book a table' to start the reservation process."
        2. Do NOT take orders here. If the user mentions food, say: "Please say 'Order [item]'."
        3. Be brief and helpful.
        """
        
        reply, _ = await call_groq(f"Menu Context: {menu}\nUser: {text}", system_prompt)
        if reply: await update.message.reply_text(reply)

# --- STARTUP ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    
    # Smart ID Logic
    target_id = None
    if args:
        raw = args[0]
        if "=" in raw: target_id = raw.split("=")[1]
        else: target_id = raw
    
    final_id = None
    if target_id:
        candidates = [target_id, f"rest_{target_id}", f"restaurant_{target_id}"]
        for cand in candidates:
            check = supabase.table("restaurants").select("id").eq("id", cand).execute()
            if check.data:
                final_id = cand
                break
    
    if not final_id:
        fallback = supabase.table("restaurants").select("id").limit(1).execute()
        final_id = fallback.data[0]['id'] if fallback.data else "error"

    try:
        existing = get_user_session(user_id)
        data = {"user_id": str(user_id), "current_restaurant_id": str(final_id)}

        if existing and existing.get('customer_name'):
             supabase.table("user_sessions").update(data).eq("user_id", str(user_id)).execute()
             msg = f"👋 Welcome back, {existing['customer_name']}! (Location: {final_id})"
             context.user_data['state'] = None
        else:
            data["customer_name"] = None 
            data["table_number"] = None
            supabase.table("user_sessions").upsert(data).execute()
            msg = f"👋 Welcome to Restaurant {final_id}!"
            context.user_data['state'] = 'AWAITING_NAME'

        await update.message.reply_text(msg)
        if context.user_data.get('state') == 'AWAITING_NAME':
            await update.message.reply_text("To get started, **what is your name?**")
            
    except Exception as e:
        print(f"Start Error: {e}")
        await update.message.reply_text("⚠️ System Error.")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    supabase.table("user_sessions").delete().eq("user_id", str(update.effective_user.id)).execute()
    context.user_data.clear()
    await update.message.reply_text("🔄 **System Reset.**\nType /start to begin.")

# --- SERVER ---
app = FastAPI()
ptb_app = Application.builder().token(TELEGRAM_TOKEN).build()
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("reset", reset))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

@app.post("/webhook")
async def webhook(request: Request):
    if not ptb_app._initialized: await ptb_app.initialize(); await ptb_app.start()
    await ptb_app.process_update(Update.de_json(await request.json(), ptb_app.bot))
    return {"status": "ok"}

@app.api_route("/", methods=["GET", "HEAD"])
async def root(): return {"status": "Online"}