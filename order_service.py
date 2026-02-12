import json
import os
from supabase import create_client
from groq import AsyncGroq # Changed to Async
from dotenv import load_dotenv

load_dotenv()

# Init Clients
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

async def process_order(user_text, user, rest_id, table_number, chat_id):
    # 1. CANCEL LOGIC
    if "cancel" in user_text.lower():
        try:
            # Find the most recent pending order for this user
            res = supabase.table("orders").select("*")\
                .eq("user_id", str(user.id))\
                .eq("status", "pending")\
                .neq("cancellation_status", "requested")\
                .order("created_at", desc=True).limit(1).execute()
            
            if not res.data:
                return "❌ No active pending orders found to cancel."
                
            order = res.data[0]
            
            # Update status
            supabase.table("orders").update({
                "cancellation_status": "requested", 
                "chat_id": str(chat_id)
            }).eq("id", order['id']).execute()
            
            return f"📩 **Cancellation Requested** for Order #{order['id']}. Waiting for kitchen approval."
        except Exception as e:
            print(f"Cancel Error: {e}")
            return "❌ Error processing cancellation."

    # 2. NEW ORDER LOGIC
    try:
        # Get Menu Context for this restaurant
        menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(60).execute()
        menu_txt = "\n".join([m['content'] for m in menu_res.data]) if menu_res.data else "No menu available."

        prompt = f"""
        Role: Waiter. 
        Menu Context: 
        {menu_txt}
        
        User Request: "{user_text}"
        
        Task: Extract items from the user request that match the menu. Calculate total.
        Output JSON ONLY: {{"valid": true, "items": ["Burger ($10)", "Fries ($5)"], "total_price": 15.0}}
        If item not on menu, set "valid": false.
        """
        
        # Async Call
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": "You are a JSON ordering assistant."}, {"role": "user", "content": prompt}],
            temperature=0, max_tokens=200
        )
        
        resp_content = completion.choices[0].message.content
        # Clean potential markdown formatting
        clean_json = resp_content.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        
        if not data.get("valid"): 
            return "❌ I couldn't find that item on the menu. Please check the menu and try again."

        # Prepare Order Data
        # IMPORTANT: Ensure numeric types are safe and cancellation_status is 'none' (not null)
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
        
        # Insert
        supabase.table("orders").insert(order).execute()
        
        return f"👨‍🍳 **Order Sent!**\n📝 {order['items']}\n💰 Total: ${order['price']}\n🪑 Table: {table_number}"
        
    except Exception as e:
        print(f"Order Processing Error: {e}")
        return "❌ Sorry, I had trouble processing that order. Please try again."