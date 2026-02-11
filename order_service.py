import json
import os
from datetime import datetime
from supabase import create_client
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

# Initialize dedicated clients for this module
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

async def process_order(user_text, user, rest_id):
    """
    Isolates the complex logic of understanding a food order.
    Returns: A reply string to send to the user.
    """
    # 1. Fetch Menu (Limit 50 items for context)
    try:
        res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(50).execute()
        menu_text = "\n".join([i['content'] for i in res.data])
    except:
        menu_text = "Menu unavailable."

    # 2. AI Extraction Prompt
    system_prompt = f"""
    You are a waiter.
    MENU:
    {menu_text}
    
    USER SAYS: "{user_text}"
    
    TASK:
    Extract the order items.
    If the item is NOT in the menu, mark it as "invalid".
    
    RETURN JSON ONLY:
    {{
      "valid": true,
      "items": ["Burger", "Fries"],
      "notes": "No onions",
      "missing_info": "Ask clarification if needed"
    }}
    """
    
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}],
            temperature=0,
            max_tokens=300
        )
        response_text = completion.choices[0].message.content
        
        # Clean JSON
        clean_json = response_text.replace("```json", "").replace("```", "").strip()
        start = clean_json.find("{")
        end = clean_json.rfind("}") + 1
        clean_json = clean_json[start:end]
        
        order_details = json.loads(clean_json)

        if not order_details.get("valid"):
            return order_details.get("missing_info", "I couldn't match that to our menu. Could you repeat?")

        # 3. Save Order to Database
        order_data = {
            "restaurant_id": str(rest_id),
            "user_id": str(user.id),
            "customer_name": user.full_name or "Guest",
            "items": f"{', '.join(order_details['items'])} ({order_details.get('notes', '')})",
            "status": "pending"
        }
        
        supabase.table("orders").insert(order_data).execute()
        
        return f"👨‍🍳 **Order Sent to Kitchen!**\n📝 Items: {order_data['items']}\n🕒 Status: Pending"

    except Exception as e:
        print(f"Order Module Error: {e}")
        return "❌ I had trouble taking your order. Please call a waiter."