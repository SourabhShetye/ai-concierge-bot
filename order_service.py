import json
import os
from supabase import create_client
from groq import Groq
from dotenv import load_dotenv

# 1. Load Config
load_dotenv()

# 2. Initialize Clients
# We use separate clients here to keep this module independent
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

import json
import os
from supabase import create_client
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

async def process_order(user_text, user, rest_id, table_number, chat_id):
    # --- 1. HANDLE CANCELLATION REQUESTS ---
    if "cancel" in user_text.lower():
        # Find the last pending order for this user
        res = supabase.table("orders").select("*").eq("user_id", user.id).eq("status", "pending").order("created_at", desc=True).limit(1).execute()
        
        if not res.data:
            return "❌ You don't have any pending orders to cancel."
        
        order = res.data[0]
        
        # Check if already requested
        if order.get('cancellation_status') == 'requested':
            return "⏳ You already requested a cancellation. Waiting for the chef's approval."

        # Mark as 'requested'
        supabase.table("orders").update({"cancellation_status": "requested"}).eq("id", order['id']).execute()
        
        return f"📩 **Cancellation Requested** for Order #{order['id']}.\n\nI have notified the chef. If they haven't started cooking yet, they will approve it shortly."

    # --- 2. HANDLE NEW ORDERS ---
    # Fetch Menu (Limit 50 items)
    try:
        res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(50).execute()
        menu_text = "\n".join([i['content'] for i in res.data])
    except:
        menu_text = "Menu unavailable."

    # AI Extraction
    system_prompt = f"""
    You are a waiter. Extract order items from: "{user_text}"
    MENU: {menu_text}
    Return JSON ONLY: {{"valid": true, "items": ["Burger", "Coke"], "notes": "No ice"}}
    """
    
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}],
            temperature=0
        )
        # Clean JSON
        clean_json = completion.choices[0].message.content.replace("```json", "").replace("```", "").strip()
        start = clean_json.find("{")
        end = clean_json.rfind("}") + 1
        order_details = json.loads(clean_json[start:end])

        if not order_details.get("valid"):
            return "I couldn't match that to our menu. Please check the item names."

        # SAVE TO DB
        order_data = {
            "restaurant_id": str(rest_id),
            "user_id": str(user.id),
            "chat_id": chat_id,
            "table_number": str(table_number),
            "customer_name": user.full_name or "Guest",
            "items": f"{', '.join(order_details['items'])} ({order_details.get('notes', '')})",
            "status": "pending",
            "cancellation_status": "none"
        }
        
        supabase.table("orders").insert(order_data).execute()
        
        # UPDATE CURRENT CUSTOMER BILL (Simple counter for now)
        # In a real app, you'd fetch prices. Here we just ensure they are in the 'active' list.
        cust_res = supabase.table("current_customers").select("*").eq("user_id", user.id).eq("status", "active").execute()
        if not cust_res.data:
            supabase.table("current_customers").insert({
                "user_id": str(user.id),
                "restaurant_id": str(rest_id),
                "customer_name": user.full_name,
                "table_number": str(table_number),
                "status": "active"
            }).execute()
        
        return f"👨‍🍳 **Order Sent to Kitchen!**\n📝 Items: {order_data['items']}\n🪑 Table: {table_number}"

    except Exception as e:
        print(f"Order Error: {e}")
        return "❌ System Error. Please call a waiter."