"""
Order Service  —  AI-Powered Order Processing  (v3)
====================================================
Changes from v2:
  - calculate_price_from_items() matches only ($X) parenthesised prices to extract
    ONLY values inside "($X)" parentheses, so quantity prefixes like "2x"
    are never summed as prices.  (Fixes $8+$4=$13 hallucination.)
  • process_new_order() ALWAYS recomputes total via Python — LLM total_price
    is intentionally discarded.
  • handle_modification() no longer commits changes immediately.
    Instead it writes a pending_modification JSON blob and sets
    modification_status='requested' for kitchen approval.
  • Full-order cancellation still uses cancellation_status='requested'.
"""

import json
import os
import re
from typing import Optional, Dict, Any

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
    Sum prices deterministically from a string like:
        "Full Stack Burger ($18), 2x Binary Bites ($16), Java Jolt ($4)"

    Only matches values wrapped in literal ($X) so that quantity prefixes
    such as '2x' are never mistaken for prices.

    Pure Python — guaranteed accuracy.
    """
    # Primary pattern: values inside ($...)
    prices = re.findall(r'\(\$(\d+(?:\.\d+)?)\)', items_str)
    if prices:
        return round(sum(float(p) for p in prices), 2)

    # Fallback: bare dollar signs e.g. "$18"
    bare = re.findall(r'\$(\d+(?:\.\d+)?)', items_str)
    if bare:
        return round(sum(float(p) for p in bare), 2)

    return 0.0


# ============================================================================
# MODIFICATION REQUEST  (staged for kitchen approval)
# ============================================================================

async def handle_modification(
    user_text:     str,
    user,
    restaurant_id: str,
) -> Optional[str]:
    """
    Detect item-removal/modification requests and STAGE them for kitchen
    approval rather than committing immediately.

    Workflow:
      1. Find user's most recent pending order.
      2. Use LLM to determine remaining_items / removed_items.
      3. Compute new_price in Python (deterministic).
      4. Write pending_modification JSON + set modification_status='requested'.
      5. Kitchen approves or rejects in admin.py KDS tab.

    Full-order cancellation uses the existing cancellation_status pathway.

    Returns user-facing string, or None if message is not a mod request.
    """
    mod_keywords = ["cancel", "remove", "delete", "take off", "no more",
                    "don't want", "drop the", "without"]
    if not any(kw in user_text.lower() for kw in mod_keywords):
        return None

    try:
        res = supabase.table("orders")\
            .select("*")\
            .eq("user_id", str(user.id))\
            .eq("restaurant_id", restaurant_id)\
            .eq("status", "pending")\
            .neq("cancellation_status", "requested")\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()

        if not res.data:
            return "❌ No active orders to modify."

        order = res.data[0]

        # Already has a pending modification? Don't stack another.
        if order.get("modification_status") == "requested":
            return (
                "⏳ You already have a pending modification request.\n"
                "Please wait for kitchen to respond before making another."
            )

        # Full-order cancellation?
        full_cancel = [
            "cancel order", "cancel my order", "cancel the order",
            "cancel everything", "nevermind", "never mind"
        ]
        if any(phrase in user_text.lower() for phrase in full_cancel):
            supabase.table("orders")\
                .update({"cancellation_status": "requested"})\
                .eq("id", order["id"])\
                .execute()
            return (
                f"📩 *Cancellation Requested* for Order #{order['id']}\n"
                f"Items: _{order['items']}_\n"
                f"Kitchen will review shortly."
            )

        # Partial modification — ask LLM what to remove
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

        if not data:
            return "❌ Couldn't parse your request. Please describe it differently."

        # All items removed → escalate to full cancellation
        if data.get("all_removed") or not data.get("remaining_items", "").strip():
            supabase.table("orders")\
                .update({"cancellation_status": "requested"})\
                .eq("id", order["id"])\
                .execute()
            return (
                f"📩 *Cancellation Requested* for Order #{order['id']}\n"
                f"Kitchen will confirm shortly."
            )

        remaining = data["remaining_items"].strip()
        removed   = data.get("removed_items", "item(s)").strip()
        new_price = calculate_price_from_items(remaining)   # Python, not LLM

        # Stage the modification — do NOT update items/price yet
        pending_blob = json.dumps({
            "remaining_items": remaining,
            "removed_items":   removed,
            "new_price":       new_price,
        })

        supabase.table("orders")\
            .update({
                "modification_status":  "requested",
                "pending_modification": pending_blob,
            })\
            .eq("id", order["id"])\
            .execute()

        return (
            f"📩 *Modification Requested*\n"
            f"Remove: _{removed}_\n"
            f"Remaining if approved: {remaining}\n"
            f"New total if approved: *${new_price:.2f}*\n\n"
            f"Kitchen is reviewing — you'll be notified shortly."
        )

    except Exception as e:
        print(f"[MODIFICATION ERROR] {e}")
        return "❌ Error submitting modification. Please try /cancel or ask staff."


# ============================================================================
# NEW ORDER PROCESSING
# ============================================================================

async def process_new_order(
    user_text:     str,
    user,
    restaurant_id: str,
    table_number:  str,
    chat_id:       str,
) -> Optional[str]:
    """
    Parse a natural-language order against the live menu_items table.

    Price is ALWAYS computed by calculate_price_from_items() — the LLM's
    total_price field is intentionally discarded to prevent hallucinated sums.
    """
    try:
        # Always fetch live menu from DB (not a static file)
        menu_rows = supabase.table("menu_items")\
            .select("content")\
            .eq("restaurant_id", restaurant_id)\
            .limit(80)\
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
- Only list items from the menu
- Format every item as: ItemName ($price)
- For multiples: Nx ItemName ($price_x_quantity)  e.g. 2x Binary Bites at $8 = ($16)
- Do NOT include a total_price field (we calculate it ourselves)
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

        if not data:
            print("[ORDER] Failed to parse AI response")
            return None

        if not data.get("valid"):
            print("[ORDER] Not a valid order request")
            return None

        items = data.get("items", [])
        if not items:
            print("[ORDER] No items extracted")
            return None

        items_str   = ", ".join(items)
        total_price = calculate_price_from_items(items_str)  # deterministic

        # Edge case: AI forgot parentheses — try bare $ amounts
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

        supabase.table("orders").insert(order_row).execute()

        return (
            f"👨‍🍳 *Order Sent to Kitchen!*\n\n"
            f"🍽 {items_str}\n"
            f"💰 Total: *${total_price:.2f}*\n"
            f"🪑 Table: {table_number}\n\n"
            f"We'll notify you when it's ready! 🔔"
        )

    except Exception as e:
        import traceback
        print(f"[NEW ORDER ERROR] {e}")
        traceback.print_exc()
        return None


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

async def process_order(
    user_text:     str,
    user,
    restaurant_id: str,
    table_number:  str,
    chat_id:       str,
) -> Optional[str]:
    """
    Called by main.py message_handler.

    1. Modification/cancellation request → stage for kitchen approval
    2. New food order                    → insert with deterministic price
    3. Neither                           → return None (main.py falls to chat)
    """
    mod = await handle_modification(user_text, user, restaurant_id)
    if mod:
        return mod

    return await process_new_order(
        user_text, user, restaurant_id, table_number, chat_id
    )