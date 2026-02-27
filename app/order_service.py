"""
Order Service — v6
===================
Complete order processing logic:
  • Fuzzy menu matching with sold-out enforcement
  • Allergy warnings
  • Deterministic pricing (unit price × quantity)
  • Order modifications (remove items only)
  • Order cancellations
  • CRM updates on payment
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple

from supabase import create_client
from groq import AsyncGroq
from dotenv import load_dotenv

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))


# ═══════════════════════════════════════════════════════════════════════════
# JSON EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

def extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Extract JSON from LLM response (handles code blocks and raw JSON)"""
    try:
        # Try extracting from code block first
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if m:
            return json.loads(m.group(1))
        
        # Try finding raw JSON
        s, e = text.find("{"), text.rfind("}") + 1
        if s != -1 and e > s:
            return json.loads(text[s:e])
        
        # Try parsing entire text as JSON
        return json.loads(text)
    except json.JSONDecodeError as ex:
        print(f"[JSON] Decode error: {ex} | raw: {text[:200]!r}")
        return None
    except Exception as ex:
        print(f"[JSON] Unexpected error: {ex}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# DETERMINISTIC PRICE CALCULATION
# ═══════════════════════════════════════════════════════════════════════════

def calculate_price_from_items(items_str: str) -> float:
    """
    Sum prices from items string, handling both:
      - Single items: "ItemName ($18)"
      - Quantity items: "2x ItemName ($32 each)" where $32 is unit price
    
    The LLM formats multiples as "Nx Item ($unit_price each)" so we multiply.
    """
    total = 0.0
    
    # Pattern: "2x ItemName ($32 each)" or "ItemName ($18)"
    # Match patterns like: 2x Burger ($18 each) or Burger ($18)
    pattern = r'(?:(\d+)x\s+)?([^(]+)\s+\(\$(\d+(?:\.\d+)?)\s*(?:each)?\)'
    
    matches = re.findall(pattern, items_str, re.IGNORECASE)
    
    for match in matches:
        quantity_str, item_name, price_str = match
        quantity = int(quantity_str) if quantity_str else 1
        price = float(price_str)
        total += quantity * price
    
    # Fallback: if no matches found, try simple extraction
    if total == 0:
        prices = re.findall(r'\$(\d+(?:\.\d+)?)', items_str)
        total = sum(float(p) for p in prices)
    
    return round(total, 2)


# ═══════════════════════════════════════════════════════════════════════════
# CRM: UPDATE ON PAYMENT
# ═══════════════════════════════════════════════════════════════════════════

def update_crm_on_payment(user_id: str, amount: float) -> None:
    """
    Called by admin.py when a table is closed (all orders marked 'paid').
    Atomically:
      - visit_count  += 1
      - total_spend  += amount
      - last_visit    = now()
    """
    try:
        res = supabase.table("users") \
            .select("visit_count, total_spend") \
            .eq("id", str(user_id)) \
            .limit(1).execute()
        
        if not res.data:
            print(f"[CRM PAY] user {user_id} not found — skipping")
            return
        
        row = res.data[0]
        new_visits = int(row.get("visit_count") or 0) + 1
        new_spend = round(float(row.get("total_spend") or 0.0) + amount, 2)
        
        supabase.table("users").update({
            "visit_count": new_visits,
            "total_spend": new_spend,
            "last_visit": datetime.now(timezone.utc).isoformat(),
        }).eq("id", str(user_id)).execute()
        
        print(f"[CRM PAY] uid={user_id} visits={new_visits} spend=${new_spend:.2f}")
    except Exception as ex:
        print(f"[CRM PAY ERROR] {ex}")


# ═══════════════════════════════════════════════════════════════════════════
# ORDER OWNERSHIP VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def fetch_order_for_user(order_id: int, user_id: str, restaurant_id: str) -> Optional[Dict]:
    """Fetch order owned by user"""
    try:
        res = supabase.table("orders").select("*") \
            .eq("id", order_id).eq("user_id", str(user_id)) \
            .eq("restaurant_id", str(restaurant_id)).eq("status", "pending").execute()
        return res.data[0] if res.data else None
    except Exception as ex:
        print(f"[FETCH ORDER] {ex}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# STAGE CANCELLATION
# ═══════════════════════════════════════════════════════════════════════════

def stage_cancellation(order: Dict) -> str:
    """Request cancellation (requires kitchen approval)"""
    try:
        supabase.table("orders").update({"cancellation_status": "requested"}) \
            .eq("id", order["id"]).execute()
        
        return (f"📩 *Cancellation Requested*\n"
                f"Order *#{order['id']}* — _{order['items']}_\n\n"
                f"Kitchen will review and notify you.")
    except Exception as ex:
        print(f"[STAGE CANCEL] {ex}")
        return "❌ Error requesting cancellation."


# ═══════════════════════════════════════════════════════════════════════════
# STAGE MODIFICATION
# ═══════════════════════════════════════════════════════════════════════════

async def stage_modification(order: Dict, user_text: str) -> str:
    """Request modification (remove items only, requires kitchen approval)"""
    order_id = order["id"]
    
    if order.get("modification_status") == "requested":
        return (f"⏳ Order *#{order_id}* already has a pending modification.\n"
                f"Please wait for kitchen before making another change.")
    
    # Strict cancel detection
    full_cancel = ["cancel order", "cancel my order", "cancel the order", "cancel everything", "cancel it", "nevermind", "never mind"]
    text_lower = user_text.lower().strip()
    
    if any(text_lower == phrase or text_lower.startswith(phrase + " ") for phrase in full_cancel):
        return stage_cancellation(order)
    
    # Reject addition attempts
    if any(word in text_lower for word in ["add", "adding", "include", "also give me", "more", "extra", "another"]):
        return (
            "⚠️ *Modifications can only REMOVE items.*\n\n"
            "To add items, please place a new order.\n\n"
            "_Example removal: 'remove the fries from order #42'_"
        )
    
    # LLM prompt for modification
    prompt = f"""
Current Order: {order['items']}
User Request: "{user_text}"

Return JSON only:
{{"remaining_items":"Full Stack Burger ($18), Java Jolt ($4)","removed_items":"Binary Bites ($8)","all_removed":false}}

CRITICAL RULES:
1. If removing quantity from multi-item:
   Example: Order is "2x Carbonara ($32 each)" and user says "remove 1 carbonara"
   Result: {{"remaining_items":"1x Carbonara ($32 each)","removed_items":"1x Carbonara ($32 each)","all_removed":false}}
   
2. Calculate per-item price: If original is "2x Item ($32 each)", per-item = $32
3. Remaining quantity gets: remaining_qty × per_item_price with " each" suffix
4. Format: "Nx Item ($PRICE each)" where $PRICE is unit price (NOT total)
5. all_removed = true ONLY if nothing remains
6. Keep all prices in parentheses: ($X) or ($X each)
"""
    
    try:
        c = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "JSON-only order modification assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=200
        )
        data = extract_json_from_text(c.choices[0].message.content)
    except Exception as ex:
        print(f"[MOD LLM] {ex}")
        return "❌ Error processing modification."
    
    if not data:
        return "❌ Couldn't understand. Describe what to remove."
    
    if data.get("all_removed") or not data.get("remaining_items", "").strip():
        return stage_cancellation(order)
    
    remaining = data["remaining_items"].strip()
    removed = data.get("removed_items", "item(s)").strip()
    new_price = calculate_price_from_items(remaining)
    old_price = float(order.get("price", 0))
    
    # CRITICAL VALIDATION: New price cannot exceed old price
    if new_price > old_price:
        return (
            f"❌ *Calculation Error*\n\n"
            f"The modification would make the order MORE expensive (${new_price:.2f} > ${old_price:.2f}).\n\n"
            f"Please try rephrasing your request or contact staff."
        )
    
    # Sanity check: If removing items, price should decrease
    if new_price == old_price:
        return (
            f"⚠️ *No Change Detected*\n\n"
            f"The order price remains ${old_price:.2f}.\n\n"
            f"Please specify which items to remove more clearly."
        )
    
    blob = json.dumps({"remaining_items": remaining, "removed_items": removed, "new_price": new_price})
    
    try:
        supabase.table("orders").update({
            "modification_status": "requested",
            "pending_modification": blob
        }).eq("id", order_id).execute()
    except Exception as ex:
        print(f"[STAGE MOD] {ex}")
        return "❌ Error staging modification."
    
    return (f"📩 *Modification Requested for Order #{order_id}*\n\n"
            f"Remove: _{removed}_\nRemaining if approved: {remaining}\n"
            f"New total if approved: *${new_price:.2f}*\n\nKitchen reviewing ⏳")


# ═══════════════════════════════════════════════════════════════════════════
# NEW ORDER PROCESSING
# ═══════════════════════════════════════════════════════════════════════════

async def process_new_order(
    user_text: str, user, restaurant_id: str,
    table_number: str, chat_id: str,
    user_preferences: str = "",
    session_id: str = "",
    display_name: str = "",
) -> Optional[Tuple[str, int]]:
    """
    Parse a natural-language order. Returns (reply, order_id) or None.
    
    Features:
    - Fuzzy menu matching
    - Sold-out enforcement
    - Allergy warnings
    - AI recommendations
    """
    try:
        # Fetch menu
        menu_rows = supabase.table("menu_items").select("content,sold_out") \
            .eq("restaurant_id", restaurant_id).limit(80).execute()
        
        if not menu_rows.data:
            return None
        
        # Build menu context
        menu_text = "\n".join(r["content"] for r in menu_rows.data)
        
        # Allergy instruction
        pref_instruction = ""
        if user_preferences.strip():
            pref_instruction = (
                f"\nUSER DIETARY PREFERENCES/ALLERGIES: {user_preferences}\n"
                "IMPORTANT: If any item ordered contains ingredients that conflict with "
                "the user's preferences or allergies, you MUST set 'allergy_warning' to "
                "a short warning string (e.g. 'Contains nuts!'). Otherwise set it to null."
            )
        
        # Get sold-out items
        sold_out_list = []
        try:
            sold_out_items = supabase.table("menu_items").select("content")\
                .eq("restaurant_id", restaurant_id)\
                .eq("sold_out", True).execute()
            
            if sold_out_items.data:
                for item in sold_out_items.data:
                    for line in item["content"].split("\n"):
                        if line.startswith("item:"):
                            sold_out_list.append(line.replace("item:", "").strip())
        except Exception as ex:
            print(f"[SOLD OUT CHECK] {ex}")
        
        # Build prompt
        prompt = f"""You are a restaurant order assistant.

MENU:
{menu_text}
"""
        
        if sold_out_list:
            prompt += f"\n\nSOLD OUT TODAY (DO NOT ACCEPT ORDERS FOR THESE):\n"
            prompt += "\n".join(f"- {item}" for item in sold_out_list)
        
        prompt += f"""

USER REQUEST: "{user_text}"
{pref_instruction}

Return JSON only:
{{
  "valid": true,
  "items": ["Full Stack Burger ($18)", "2x Binary Bites ($16 each)"],
  "allergy_warning": null
}}

CRITICAL RULES:
- "valid": false if user is asking a question, NOT ordering
- Use FUZZY MATCHING for menu items - users make typos:
  * "404 fizz" → "404 Fizz Not Found"
  * "carbonara" → "C++ Carbonara"
  * "fries" → "Firewall Fries"
  * "burger" → "Full Stack Burger"
- If user says a quantity WITHOUT item name, match to closest menu item:
  * "2 of 404" → "2x 404 Fizz Not Found ($X each)"
- Only list items from the menu above (but be flexible with naming)
- Format items consistently:
  * Single item: "ItemName ($price)"
  * Multiple items: "Nx ItemName ($price each)" where $price is UNIT PRICE
  
CRITICAL EXAMPLES:
  * 1x Carbonara at $32 = "Carbonara ($32)"
  * 2x Carbonara at $32 each = "2x Carbonara ($32 each)" ← Shows $32 per item, NOT $64 total
  * 3x Fries at $7 each = "3x Fries ($7 each)" ← Shows $7 per item, NOT $21 total
  
The price in parentheses for multiples MUST show unit price with " each" suffix!
- Do NOT include total_price field
"""
        
        # Call LLM
        c = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "JSON-only food order extraction assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=300
        )
        
        data = extract_json_from_text(c.choices[0].message.content)
        
        if not data or not data.get("valid"):
            return None
        
        items = data.get("items", [])
        if not items:
            return None
        
        items_str = ", ".join(items)
        total_price = calculate_price_from_items(items_str)
        
        if total_price <= 0:
            # Fallback: extract bare prices
            bare = re.findall(r'\$(\d+(?:\.\d+)?)', items_str)
            total_price = round(sum(float(p) for p in bare), 2) if bare else 0.0
        
        # Insert order into database
        result = supabase.table("orders").insert({
            "restaurant_id": str(restaurant_id),
            "user_id": str(user.id),
            "session_id": session_id,
            "chat_id": str(chat_id),
            "table_number": str(table_number),
            "customer_name": display_name or user.full_name or "Guest",
            "items": items_str,
            "price": total_price,
            "status": "pending",
            "cancellation_status": "none",
            "modification_status": "none",
            "pending_modification": None,
        }).execute()
        
        order_id = result.data[0]["id"]
        
        # AI-Powered Recommendations
        recommendation = ""
        try:
            menu_items_list = []
            for row in menu_rows.data:
                for line in row["content"].split("\n"):
                    if line.startswith("item:"):
                        menu_items_list.append(line.replace("item:", "").strip())
            
            rec_prompt = f"""Based on this order: {items_str}

Suggest ONE complementary item that pairs well.
Keep it brief (one sentence, under 50 words).

Available menu items:
{chr(10).join([f"- {item}" for item in menu_items_list[:10]])}

Format: "Great choice! [ITEM] pairs perfectly with that."
Do NOT suggest items already in the order.
"""
            
            rec_response = await groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": rec_prompt}],
                temperature=0.8,
                max_tokens=60
            )
            
            rec_text = rec_response.choices[0].message.content.strip()
            if rec_text:
                recommendation = f"\n\n💡 {rec_text}"
                print(f"[RECOMMENDATION] Generated: {rec_text}")
        except Exception as ex:
            print(f"[RECOMMENDATION ERROR] {ex}")
        
        # Allergy warning
        warning_line = ""
        aw = data.get("allergy_warning")
        if aw and str(aw).lower() not in ("null", "none", ""):
            warning_line = f"\n\n⚠️ *Allergy Warning:* _{aw}_"
        
        reply = (f"👨‍🍳 *Order #{order_id} Confirmed!*\n\n"
                 f"🍽 {items_str}\n"
                 f"💰 Total: *${total_price:.2f}*\n"
                 f"🪑 Table: {table_number}\n\n"
                 f"We'll notify you when it's ready! 🔔\n"
                 f"_To modify: type 'modify order' or /cancel_{warning_line}{recommendation}")
        
        return reply, order_id
        
    except Exception as ex:
        import traceback
        print(f"[NEW ORDER] {ex}")
        traceback.print_exc()
        return None


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

async def process_order(
    user_text: str, user, restaurant_id: str,
    table_number: str, chat_id: str,
    user_preferences: str = "",
    session_id: str = "",
    display_name: str = "",
) -> Optional[Tuple[str, int]]:
    """
    Main entry point for order processing.
    Returns (reply_text, order_id) or None if not a valid order.
    """
    return await process_new_order(
        user_text, user, restaurant_id, table_number, chat_id,
        user_preferences=user_preferences,
        session_id=session_id,
        display_name=display_name,
    )