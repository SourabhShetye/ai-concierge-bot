"""
Order Service  —  v4
=====================
Changes from v3:
  • process_new_order() captures the Supabase-generated order ID from the
    INSERT response and returns it so main.py can display "Order #123".
  • handle_modification() now accepts an explicit validated order_id instead
    of blindly grabbing the "latest" order. Ownership is verified by main.py
    before calling this function.
  • stage_cancellation() is a new thin helper for the /cancel flow — also
    accepts an explicit validated order_id.
  • calculate_price_from_items() unchanged (deterministic, no LLM).
  • extract_json_from_text() unchanged.
"""

import json
import os
import re
from typing import Optional, Dict, Any, Tuple

from supabase import create_client
from groq import AsyncGroq
from dotenv import load_dotenv

load_dotenv()

supabase    = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))


# ============================================================================
# ROBUST JSON EXTRACTION
# ============================================================================

def extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """
    Extract the first JSON object from an LLM response, handling:
      - Code fences  ```json{...}```
      - Prose prefix "Here is the JSON: {...}"
      - Bare JSON    {...}
    """
    try:
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if m:
            return json.loads(m.group(1))
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start != -1 and end > start:
            return json.loads(text[start:end])
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[JSON PARSE ERROR] {e} | raw: {text[:200]!r}")
        return None
    except Exception as e:
        print(f"[EXTRACTION ERROR] {e}")
        return None


# ============================================================================
# DETERMINISTIC PRICE CALCULATION  (no LLM)
# ============================================================================

def calculate_price_from_items(items_str: str) -> float:
    """
    Sum prices from a string like:
        "Full Stack Burger ($18), 2x Binary Bites ($16), Java Jolt ($4)"

    Only matches values inside ($...) parentheses — quantity prefixes like
    "2x" are structurally excluded from the match.
    """
    prices = re.findall(r'\(\$(\d+(?:\.\d+)?)\)', items_str)
    if prices:
        return round(sum(float(p) for p in prices), 2)
    bare = re.findall(r'\$(\d+(?:\.\d+)?)', items_str)
    if bare:
        return round(sum(float(p) for p in bare), 2)
    return 0.0


# ============================================================================
# ORDER OWNERSHIP LOOKUP  (used by main.py before any modification)
# ============================================================================

def fetch_order_for_user(order_id: int, user_id: str, restaurant_id: str) -> Optional[Dict]:
    """
    Fetch a specific order and verify it belongs to this user at this restaurant.
    Returns the order dict if valid, None if not found or not owned by this user.

    Called by main.py BEFORE invoking stage_modification() or stage_cancellation()
    so that every modification path is validated explicitly.
    """
    try:
        res = supabase.table("orders") \
            .select("*") \
            .eq("id", order_id) \
            .eq("user_id", str(user_id)) \
            .eq("restaurant_id", str(restaurant_id)) \
            .eq("status", "pending") \
            .execute()
        return res.data[0] if res.data else None
    except Exception as e:
        print(f"[FETCH ORDER ERROR] {e}")
        return None


# ============================================================================
# STAGE CANCELLATION  (explicit order ID — no blind "latest" lookup)
# ============================================================================

def stage_cancellation(order: Dict) -> str:
    """
    Mark a validated order for cancellation review by kitchen.
    The order dict must already be validated by fetch_order_for_user().

    Returns a user-facing confirmation string.
    """
    try:
        supabase.table("orders") \
            .update({"cancellation_status": "requested"}) \
            .eq("id", order["id"]) \
            .execute()
        return (
            f"📩 *Cancellation Requested*\n"
            f"Order *#{order['id']}* — _{order['items']}_\n\n"
            f"Kitchen will review shortly and notify you."
        )
    except Exception as e:
        print(f"[STAGE CANCEL ERROR] {e}")
        return "❌ Error requesting cancellation. Please ask staff."


# ============================================================================
# STAGE MODIFICATION  (explicit order ID — no blind "latest" lookup)
# ============================================================================

async def stage_modification(order: Dict, user_text: str) -> str:
    """
    Stage a partial item-removal request for kitchen approval.
    The order dict must already be validated by fetch_order_for_user().

    Uses LLM to parse what to remove, then writes a pending_modification
    blob — does NOT commit items/price changes until kitchen approves.

    Returns a user-facing confirmation string.
    """
    order_id = order["id"]

    # Guard: don't stack a second modification on top of a pending one
    if order.get("modification_status") == "requested":
        return (
            f"⏳ Order *#{order_id}* already has a pending modification.\n"
            f"Please wait for kitchen to respond before making another change."
        )

    # Full-cancel phrases inside a mod flow
    full_cancel = [
        "cancel order", "cancel my order", "cancel the order",
        "cancel everything", "nevermind", "never mind"
    ]
    if any(phrase in user_text.lower() for phrase in full_cancel):
        return stage_cancellation(order)

    prompt = f"""
Current Order: {order['items']}
User Request: "{user_text}"

Identify what the user wants removed. Return JSON only:
{{
  "remaining_items": "Full Stack Burger ($18), Java Jolt ($4)",
  "removed_items":   "Binary Bites ($8)",
  "all_removed":     false
}}

Rules:
- Keep prices in parentheses: ($X)
- all_removed: true only if every item is removed
- If request is unclear, keep the original order unchanged
"""
    try:
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system",
                 "content": "You are a JSON-only order modification assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=200,
        )
        data = extract_json_from_text(completion.choices[0].message.content)
    except Exception as e:
        print(f"[MOD LLM ERROR] {e}")
        return "❌ Error processing modification. Please try again."

    if not data:
        return "❌ Couldn't understand your request. Please describe what to remove."

    if data.get("all_removed") or not data.get("remaining_items", "").strip():
        return stage_cancellation(order)

    remaining = data["remaining_items"].strip()
    removed   = data.get("removed_items", "item(s)").strip()
    new_price = calculate_price_from_items(remaining)

    pending_blob = json.dumps({
        "remaining_items": remaining,
        "removed_items":   removed,
        "new_price":       new_price,
    })

    try:
        supabase.table("orders") \
            .update({
                "modification_status":  "requested",
                "pending_modification": pending_blob,
            }) \
            .eq("id", order_id) \
            .execute()
    except Exception as e:
        print(f"[STAGE MOD ERROR] {e}")
        return "❌ Error staging modification. Please ask staff."

    return (
        f"📩 *Modification Requested for Order #{order_id}*\n\n"
        f"Remove: _{removed}_\n"
        f"Remaining if approved: {remaining}\n"
        f"New total if approved: *${new_price:.2f}*\n\n"
        f"Kitchen is reviewing — you'll be notified shortly. ⏳"
    )


# ============================================================================
# NEW ORDER PROCESSING  (returns order_id for display)
# ============================================================================

async def process_new_order(
    user_text:     str,
    user,
    restaurant_id: str,
    table_number:  str,
    chat_id:       str,
) -> Optional[Tuple[str, int]]:
    """
    Parse a natural-language order against the live menu_items table.

    Returns (reply_string, order_id) on success, or None if not a food order.

    Price is ALWAYS computed by calculate_price_from_items() — the LLM's
    total_price field is intentionally discarded to prevent hallucinated sums.
    The generated order_id is captured from the INSERT response so main.py
    can display "✅ Order #123 Confirmed" to the user.
    """
    try:
        menu_rows = supabase.table("menu_items") \
            .select("content") \
            .eq("restaurant_id", restaurant_id) \
            .limit(80) \
            .execute()

        menu_text = (
            "\n".join(r["content"] for r in menu_rows.data)
            if menu_rows.data else "No menu available"
        )

        prompt = f"""
You are a restaurant order assistant.

MENU:
{menu_text}

USER REQUEST: "{user_text}"

Is this a FOOD ORDER or a QUESTION/CHAT? Return JSON only:
{{
  "valid": true,
  "items": ["Full Stack Burger ($18)", "2x Binary Bites ($16)", "Java Jolt ($4)"]
}}

RULES:
- "valid": false if user is asking a question, NOT ordering
- Only list items from the menu above
- Format every item as: ItemName ($price)
- For multiples: Nx ItemName ($price_x_quantity) e.g. 2x Binary Bites at $8 each = ($16)
- Do NOT include a total_price field (calculated by Python)
"""
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system",
                 "content": "You are a JSON-only food order extraction assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=300,
        )
        data = extract_json_from_text(completion.choices[0].message.content)

        if not data or not data.get("valid"):
            print("[ORDER] Not a valid order request")
            return None

        items = data.get("items", [])
        if not items:
            print("[ORDER] No items extracted")
            return None

        items_str   = ", ".join(items)
        total_price = calculate_price_from_items(items_str)

        if total_price <= 0:
            bare = re.findall(r'\$(\d+(?:\.\d+)?)', items_str)
            total_price = round(sum(float(p) for p in bare), 2) if bare else 0.0

        order_row = {
            "restaurant_id":        str(restaurant_id),
            "user_id":              str(user.id),
            "chat_id":              str(chat_id),
            "table_number":         str(table_number),
            "customer_name":        user.full_name or "Guest",
            "items":                items_str,
            "price":                total_price,
            "status":               "pending",
            "cancellation_status":  "none",
            "modification_status":  "none",
            "pending_modification": None,
        }

        result    = supabase.table("orders").insert(order_row).execute()
        order_id  = result.data[0]["id"]   # capture DB-generated ID

        reply = (
            f"👨\u200d🍳 *Order #{order_id} Confirmed!*\n\n"
            f"🍽 {items_str}\n"
            f"💰 Total: *${total_price:.2f}*\n"
            f"🪑 Table: {table_number}\n\n"
            f"We'll notify you when it's ready! 🔔\n"
            f"_To modify, say \"modify order #{order_id}\" or \"/cancel\"_"
        )
        return reply, order_id

    except Exception as e:
        import traceback
        print(f"[NEW ORDER ERROR] {e}")
        traceback.print_exc()
        return None


# ============================================================================
# MAIN ENTRY POINT  (called by main.py for new orders only)
# ============================================================================

async def process_order(
    user_text:     str,
    user,
    restaurant_id: str,
    table_number:  str,
    chat_id:       str,
) -> Optional[Tuple[str, int]]:
    """
    Entry point for new food order processing only.

    Modification and cancellation are now handled explicitly in main.py
    via the AWAITING_ORDER_ID state — they are NOT routed through here.

    Returns (reply_string, order_id) on success, or None if not an order.
    """
    return await process_new_order(
        user_text, user, restaurant_id, table_number, chat_id
    )