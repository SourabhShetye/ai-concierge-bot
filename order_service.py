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
    # ... (Keep Cancellation Logic from previous steps) ...
    if "cancel" in user_text.lower():
         # ... (Use previous cancellation code) ...
         return "Cancellation requested."

    # --- NEW: ORDER WITH PRICE EXTRACTION ---
    try:
        # 1. Fetch Menu
        res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(50).execute()
        menu_text = "\n".join([i['content'] for i in res.data])

        # 2. AI Extraction (Now asks for PRICE)
        system_prompt = f"""
        You are a waiter. 
        MENU: 
        {menu_text}
        
        USER SAYS: "{user_text}"
        
        TASK:
        1. Identify items ordered.
        2. Look up their prices in the MENU.
        3. Calculate the TOTAL price for this order.
        
        RETURN JSON ONLY:
        {{
          "valid": true,
          "items": ["Burger ($10)", "Coke ($2)"], 
          "total_price": 12.0,
          "notes": "No ice"
        }}
        """
        
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}],
            temperature=0
        )
        
        # Clean JSON
        clean_json = completion.choices[0].message.content.replace("```json", "").replace("```", "").strip()
        start = clean_json.find("{")
        end = clean_json.rfind("}") + 1
        data = json.loads(clean_json[start:end])

        if not data.get("valid"):
            return "I couldn't match that to our menu. Please check the item names."

        # 3. Save to DB (With Price)
        order_data = {
            "restaurant_id": str(rest_id),
            "user_id": str(user.id),
            "chat_id": chat_id,
            "table_number": str(table_number),
            "customer_name": user.full_name or "Guest",
            "items": f"{', '.join(data['items'])} ({data.get('notes', '')})",
            "price": data.get('total_price', 0), # Save the price!
            "status": "pending"
        }
        
        supabase.table("orders").insert(order_data).execute()
        
        return f"👨‍🍳 **Order Sent!**\n📝 {order_data['items']}\n💰 Order Total: ${data.get('total_price', 0)}\n🪑 Table: {table_number}"

    except Exception as e:
        print(f"Order Error: {e}")
        return "❌ System Error. Please call a waiter."