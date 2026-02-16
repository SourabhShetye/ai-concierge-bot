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
    # 1. CANCEL / MODIFY LOGIC
    if "cancel" in user_text.lower() or "remove" in user_text.lower():
        try:
            # Fetch the latest pending order
            res = supabase.table("orders").select("*")\
                .eq("user_id", str(user.id))\
                .eq("status", "pending")\
                .neq("cancellation_status", "requested")\
                .order("created_at", desc=True).limit(1).execute()
            
            if not res.data:
                return "❌ No active pending orders found to modify."
            
            order = res.data[0]
            
            # If "cancel order" (entirely), do the old logic
            if "order" in user_text.lower() and "item" not in user_text.lower():
                 supabase.table("orders").update({"cancellation_status": "requested", "chat_id": str(chat_id)}).eq("id", order['id']).execute()
                 return f"📩 **Cancellation Requested** for Order #{order['id']}."

            # NEW: PARTIAL CANCELLATION (AI Editing)
            prompt = f"""
            Current Order Items: "{order['items']}"
            User Request: "{user_text}"
            
            Task: Remove the item the user wants to cancel. Keep the rest.
            Return JSON: {{ "remaining_items": "Burger ($10), Fries ($5)", "removed_item": "Coke" }}
            If the resulting list is empty, return "remaining_items": "".
            """
            
            completion = await groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}], temperature=0
            )
            data = json.loads(completion.choices[0].message.content.replace("```json","").replace("```","").strip())
            
            new_items = data.get("remaining_items", "")
            
            if not new_items:
                # If everything removed, cancel the order
                supabase.table("orders").update({"status": "cancelled", "cancellation_status": "approved"}).eq("id", order['id']).execute()
                return "🗑️ All items removed. Order cancelled."
            
            # Recalculate Price (Simple parser or ask AI again, here we assume prices are in brackets ($10))
            # Safe way: Asking AI to sum it up is safer given the format
            price_prompt = f"Calculate total sum of these items: {new_items}. Return ONLY the number (e.g. 15.0)"
            price_resp = await groq_client.chat.completions.create(model="llama-3.3-70b-versatile", messages=[{"role": "user", "content": price_prompt}])
            new_price = float(''.join(c for c in price_resp.choices[0].message.content if c.isdigit() or c == '.'))

            # Update DB
            supabase.table("orders").update({"items": new_items, "price": new_price}).eq("id", order['id']).execute()
            return f"✅ Removed **{data['removed_item']}**.\n📝 **Updated Order:** {new_items}\n💰 **New Total:** ${new_price}"

        except Exception as e:
            print(f"Modify Error: {e}")
            return "❌ Error modifying order. You might need to cancel the whole order."

    # 2. NEW ORDER LOGIC (Standard Flow)
    # ... (Keep your existing New Order logic here) ...
    try:
        menu_res = supabase.table("menu_items").select("content").eq("restaurant_id", rest_id).limit(60).execute()
        menu_txt = "\n".join([m['content'] for m in menu_res.data]) if menu_res.data else "No menu."

        prompt = f"""
        Role: Waiter. Menu: {menu_txt}
        User Request: "{user_text}"
        INSTRUCTIONS:
        1. Extract food items.
        2. Calculate total price.
        3. IF user asks to SEE menu (e.g. "Show menu"), return "valid": false.
        Output JSON: {{"valid": true, "items": ["Burger ($10)"], "total_price": 10.0}}
        """
        
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": "You are a JSON ordering assistant."}, {"role": "user", "content": prompt}],
            temperature=0, max_tokens=200
        )
        
        content = completion.choices[0].message.content
        match = re.search(r"\{.*\}", content, re.DOTALL)
        
        if not match:
            return "❌ Error: AI response was not valid JSON."
            
        data = json.loads(match.group(0))
        
        if not data.get("valid"): return "❌ I didn't find those items. Tip: Say 'I want the Burger'."

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
        print(e)
        return "❌ Sorry, I couldn't process that order."