import json
import os
from supabase import create_client
from groq import AsyncGroq
from dotenv import load_dotenv

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

async def process_order(user_text, user, rest_id, table_number, chat_id):
    # 1. CANCEL LOGIC
    if "cancel" in user_text.lower():
        try:
            res = supabase.table("orders").select("*")\
                .eq("user_id", str(user.id))\
                .eq("status", "pending")\
                .neq("cancellation_status", "requested")\
                .order("created_at", desc=True).limit(1).execute()
            
            if not res.data:
                return "❌ No active pending orders found to cancel."
                
            order = res.data[0]
            supabase.table("orders").update({
                "cancellation_status": "requested", 
                "chat_id": str(chat_id)
            }).eq("id", order['id']).execute()
            
            return f"📩 **Cancellation Requested** for Order #{order['id']}."
        except Exception as e:
            return "❌ Error processing cancellation."

    # 2. NEW ORDER LOGIC
    try:
        menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(60).execute()
        menu_txt = "\n".join([m['content'] for m in menu_res.data]) if menu_res.data else "No menu available."

        # STRICT PROMPT
        prompt = f"""
        Role: Waiter. 
        Menu Context: 
        {menu_txt}
        
        User Request: "{user_text}"
        
        INSTRUCTIONS:
        1. Extract food items specifically requested by the user.
        2. Calculate total price.
        3. CRITICAL: If the user is asking to SEE the menu (e.g., "Show me menu", "What do you have?"), return "valid": false. DO NOT CREATE AN ORDER.
        4. CRITICAL: Only return "valid": true if the user clearly wants to consume/buy something.
        
        Output JSON ONLY: {{"valid": true, "items": ["Burger ($10)"], "total_price": 10.0}}
        """
        
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": "You are a JSON ordering assistant."}, {"role": "user", "content": prompt}],
            temperature=0, max_tokens=200
        )
        
        clean_json = completion.choices[0].message.content.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        
        if not data.get("valid"): 
            return "❌ I didn't find those items or I'm not sure what you want to order. \nTip: Say 'I want the Burger'."

        # Insert Order
        order = {
            "restaurant_id": str(rest_id),
            "user_id": str(user.id),
            "chat_id": str(chat_id),
            "table_number": str(table_number),
            "customer_name": user.full_name or "Guest",
            "items": ", ".join(data['items']),
            "price": float(data.get('total_price', 0.0)),
            "status": "pending",
            "cancellation_status": "none" 
        }
        
        supabase.table("orders").insert(order).execute()
        return f"👨‍🍳 **Order Sent!**\n📝 {order['items']}\n💰 Total: ${order['price']}\n🪑 Table: {table_number}"
        
    except Exception as e:
        print(f"Order Error: {e}")
        return "❌ Sorry, I couldn't process that order."