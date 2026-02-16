import json
import os
import re
from supabase import create_client
from groq import AsyncGroq
from dotenv import load_dotenv

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

async def process_order(user_text, user, rest_id, table_number, chat_id):
    # 1. MODIFY/CANCEL LOGIC
    if "cancel" in user_text.lower() or "remove" in user_text.lower():
        try:
            res = supabase.table("orders").select("*")\
                .eq("user_id", str(user.id))\
                .eq("status", "pending")\
                .neq("cancellation_status", "requested")\
                .order("created_at", desc=True).limit(1).execute()
            
            if not res.data: return "❌ No active orders to modify."
            order = res.data[0]

            # If requesting full cancellation
            if "order" in user_text.lower() and "item" not in user_text.lower() and "remove" not in user_text.lower():
                 supabase.table("orders").update({"cancellation_status": "requested", "chat_id": str(chat_id)}).eq("id", order['id']).execute()
                 return f"📩 **Cancellation Requested** for Order #{order['id']}."

            # AI Edit Logic
            prompt = f"Current Items: {order['items']}\nUser Request: {user_text}\nReturn JSON: {{ \"remaining_items\": \"Burger ($10)\", \"removed_item\": \"Fries\" }}"
            completion = await groq_client.chat.completions.create(model="llama-3.3-70b-versatile", messages=[{"role": "user", "content": prompt}])
            
            # --- ROBUST PARSER ---
            raw = completion.choices[0].message.content
            start, end = raw.find("{"), raw.rfind("}") + 1
            if start == -1 or end == 0: return "❌ System Error (Parse)."
            data = json.loads(raw[start:end])
            # ---------------------

            if not data.get("remaining_items"):
                supabase.table("orders").update({"status": "cancelled", "cancellation_status": "approved"}).eq("id", order['id']).execute()
                return "🗑️ Order cancelled."
            
            # Recalculate Price
            price_resp = await groq_client.chat.completions.create(model="llama-3.3-70b-versatile", messages=[{"role": "user", "content": f"Sum prices in: {data['remaining_items']}. Return number only."}])
            # Extract number safely
            price_nums = re.findall(r"[-+]?\d*\.\d+|\d+", price_resp.choices[0].message.content)
            new_price = float(price_nums[0]) if price_nums else 0.0

            supabase.table("orders").update({"items": data['remaining_items'], "price": new_price}).eq("id", order['id']).execute()
            return f"✅ Removed {data.get('removed_item')}.\n💰 New Total: ${new_price}"
        except Exception as e: 
            print(f"Modify Error: {e}")
            return "❌ Error modifying order."

    # 2. NEW ORDER LOGIC
    try:
        # Fetch Menu
        menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(60).execute()
        menu_txt = "\n".join([m['content'] for m in menu_res.data]) if menu_res.data else "No menu."

        # STRICT PROMPT
        prompt = f"""
        Role: Waiter. 
        Menu: {menu_txt}
        User Request: "{user_text}"
        
        INSTRUCTIONS:
        1. Extract food items matching the menu.
        2. Calculate total price.
        3. OUTPUT RAW JSON ONLY. Do not write "Here is the JSON".
        4. If user asks to SEE menu, return "valid": false.
        
        Format: {{ "valid": true, "items": ["Item Name ($Price)"], "total_price": 10.0 }}
        """
        
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": "You are a JSON ordering assistant."}, {"role": "user", "content": prompt}],
            temperature=0
        )
        
        # --- ROBUST JSON PARSER (Fixes the Error) ---
        raw_content = completion.choices[0].message.content
        # Find the first open brace and last close brace
        start_idx = raw_content.find("{")
        end_idx = raw_content.rfind("}") + 1
        
        if start_idx == -1 or end_idx == 0:
            print(f"DEBUG: Failed to find JSON in: {raw_content}")
            return "❌ Error: AI response was not valid JSON."
            
        json_str = raw_content[start_idx:end_idx]
        data = json.loads(json_str)
        # --------------------------------------------
        
        if not data.get("valid"): 
            return "❌ Item not found. Say 'I want the [Item]'."

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
        print(f"Order Process Error: {e}")
        return "❌ Sorry, I couldn't process that order."