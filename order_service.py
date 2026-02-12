import json
import os
from supabase import create_client
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

async def process_order(user_text, user, rest_id, table_number, chat_id):
    # 1. CANCEL LOGIC
    if "cancel" in user_text.lower():
        # Strict filter: user_id AND pending status
        res = supabase.table("orders").select("*")\
            .eq("user_id", str(user.id))\
            .eq("status", "pending")\
            .order("created_at", desc=True).limit(1).execute()
        
        if not res.data:
            return "❌ No pending orders found to cancel."
            
        order = res.data[0]
        if order.get('cancellation_status') == 'requested':
            return "⏳ Cancellation already requested."
            
        supabase.table("orders").update({"cancellation_status": "requested", "chat_id": chat_id}).eq("id", order['id']).execute()
        return f"📩 **Cancellation Requested** for Order #{order['id']}."

    # 2. NEW ORDER LOGIC
    # Get Menu Context
    menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(50).execute()
    menu_txt = "\n".join([m['content'] for m in menu_res.data]) if menu_res.data else "No menu."

    prompt = f"""
    Role: Waiter. Menu: {menu_txt}
    User: "{user_text}"
    Extract items and calculate total.
    Return JSON: {{"valid": true, "items": ["Burger ($10)"], "total_price": 10.0, "notes": "None"}}
    """
    
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": prompt}], temperature=0
        )
        data = json.loads(completion.choices[0].message.content.replace("```json","").replace("```","").strip())
        
        if not data.get("valid"): return "❌ Item not found in menu."

        # INSERT ORDER (Ensuring all IDs are strings)
        order = {
            "restaurant_id": str(rest_id),
            "user_id": str(user.id),
            "chat_id": chat_id,
            "table_number": str(table_number),
            "customer_name": user.full_name,
            "items": ", ".join(data['items']),
            "price": data.get('total_price', 0),
            "status": "pending",
            "cancellation_status": "none"
        }
        supabase.table("orders").insert(order).execute()
        return f"👨‍🍳 **Order Sent!**\n📝 {order['items']}\n💰 Total: ${order['price']}\n🪑 Table: {table_number}"
        
    except Exception as e:
        print(f"Order Error: {e}")
        return "❌ System Error."