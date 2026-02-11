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
        # DEBUG: Print what we are looking for
        print(f"🔍 Attempting cancel for User: {user.id} (Table {table_number})")
        
        # Find the last pending order
        # We cast user.id to string to match the DB column type
        res = supabase.table("orders").select("*")\
            .eq("user_id", str(user.id))\
            .eq("status", "pending")\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()
        
        if not res.data:
            return "❌ You don't have any pending orders to cancel."
        
        order = res.data[0]
        
        # Check if already requested
        if order.get('cancellation_status') == 'requested':
            return "⏳ Request already sent. Waiting for the chef."

        # PERFORM UPDATE (With Verification)
        update_res = supabase.table("orders").update({
            "cancellation_status": "requested",
            "chat_id": chat_id  # Ensure we have the latest chat ID
        }).eq("id", order['id']).execute()
        
        # Verify if update worked
        if update_res.data:
            print(f"✅ Cancellation requested for Order #{order['id']}")
            return f"📩 **Cancellation Request Sent!**\n\nThe chef has been notified for Order #{order['id']}. If they haven't started cooking, they will approve it."
        else:
            print(f"❌ DB Update Failed for Order #{order['id']}")
            return "❌ System Error: Could not update the order. Please call a waiter."

    # --- 2. HANDLE NEW ORDERS ---
    # Fetch Menu
    try:
        res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(50).execute()
        menu_text = "\n".join([i['content'] for i in res.data])
    except:
        menu_text = "Menu unavailable."

    # AI Extraction
    system_prompt = f"""
    You are a waiter. 
    MENU: {menu_text}
    USER SAYS: "{user_text}"
    
    TASK:
    1. Identify items.
    2. Look up prices.
    3. Calculate TOTAL.
    
    RETURN JSON ONLY:
    {{
      "valid": true,
      "items": ["Burger ($10)"], 
      "total_price": 10.0,
      "notes": "No onions"
    }}
    """
    
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}],
            temperature=0
        )
        
        clean_json = completion.choices[0].message.content.replace("```json", "").replace("```", "").strip()
        start = clean_json.find("{")
        end = clean_json.rfind("}") + 1
        data = json.loads(clean_json[start:end])

        if not data.get("valid"):
            return "I couldn't match that to our menu. Please check the item names."

        # SAVE TO DB
        order_data = {
            "restaurant_id": str(rest_id),
            "user_id": str(user.id),
            "chat_id": chat_id,
            "table_number": str(table_number),
            "customer_name": user.full_name or "Guest",
            "items": f"{', '.join(data['items'])} ({data.get('notes', '')})",
            "price": data.get('total_price', 0),
            "status": "pending",
            "cancellation_status": "none"
        }
        
        supabase.table("orders").insert(order_data).execute()
        
        return f"👨‍🍳 **Order Sent!**\n📝 {order_data['items']}\n💰 Total: ${data.get('total_price', 0)}\n🪑 Table: {table_number}"

    except Exception as e:
        print(f"Order Module Error: {e}")
        return "❌ System Error. Please call a waiter."