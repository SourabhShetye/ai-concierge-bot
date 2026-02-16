"""
Order Service - Robust AI-Powered Order Processing
Handles: New Orders, Modifications, Cancellations
"""

import json
import os
import re
from typing import Optional, Dict, Any
from supabase import create_client
from groq import AsyncGroq
from dotenv import load_dotenv

load_dotenv()

# Initialize clients
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))


# ============================================================================
# ROBUST JSON EXTRACTION
# ============================================================================

def extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """
    Robust JSON extractor that handles AI "hallucinations" and conversational filler.
    
    Handles cases like:
    - "Here is your JSON: {...}"
    - "```json\n{...}\n```"
    - Pure JSON: {...}
    """
    try:
        # Method 1: Find JSON code block
        code_block_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if code_block_match:
            json_str = code_block_match.group(1)
            return json.loads(json_str)
        
        # Method 2: Find raw JSON object
        # Find first { and last }
        start_idx = text.find("{")
        end_idx = text.rfind("}") + 1
        
        if start_idx != -1 and end_idx > start_idx:
            json_str = text[start_idx:end_idx]
            return json.loads(json_str)
        
        # Method 3: Try parsing entire text (fallback)
        return json.loads(text)
        
    except json.JSONDecodeError as e:
        print(f"[JSON PARSE ERROR] {e}")
        print(f"[RAW TEXT] {text[:200]}")
        return None
    except Exception as e:
        print(f"[EXTRACTION ERROR] {e}")
        return None


# ============================================================================
# ORDER MODIFICATION & CANCELLATION
# ============================================================================

async def handle_modification(user_text: str, user, restaurant_id: str) -> Optional[str]:
    """
    Handle order modifications and cancellations.
    Returns success message if processed, None if not a modification request.
    """
    # Check if this is a modification/cancellation request
    mod_keywords = ["cancel", "remove", "delete", "take off", "no more"]
    if not any(keyword in user_text.lower() for keyword in mod_keywords):
        return None
    
    try:
        # Fetch user's most recent pending order
        response = supabase.table("orders")\
            .select("*")\
            .eq("user_id", str(user.id))\
            .eq("restaurant_id", restaurant_id)\
            .eq("status", "pending")\
            .neq("cancellation_status", "requested")\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()
        
        if not response.data:
            return "❌ No active orders to modify."
        
        order = response.data[0]
        
        # Check if user wants to cancel ENTIRE order
        full_cancel_phrases = [
            "cancel order", 
            "cancel my order", 
            "cancel the order",
            "cancel everything",
            "nevermind"
        ]
        
        if any(phrase in user_text.lower() for phrase in full_cancel_phrases):
            # Request full cancellation
            supabase.table("orders")\
                .update({
                    "cancellation_status": "requested",
                    "chat_id": order.get("chat_id")
                })\
                .eq("id", order["id"])\
                .execute()
            
            return (
                f"📩 **Cancellation Requested**\n"
                f"Order ID: #{order['id']}\n"
                f"Items: {order['items']}\n\n"
                f"Kitchen will review your request shortly."
            )
        
        # Otherwise, it's a PARTIAL modification (remove specific items)
        prompt = f"""
        Current Order: {order['items']}
        User Request: "{user_text}"
        
        Task: Remove the requested items from the order.
        
        Return JSON:
        {{
            "remaining_items": "Burger ($10), Coffee ($4)",
            "removed_items": "Fries",
            "all_removed": false
        }}
        
        Rules:
        - If ALL items are removed, set "all_removed": true and "remaining_items": ""
        - Keep price info in parentheses
        - If request is unclear, keep original order
        """
        
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a JSON-only order modification assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )
        
        raw_response = completion.choices[0].message.content
        data = extract_json_from_text(raw_response)
        
        if not data:
            return "❌ Unable to process modification. Please try again."
        
        # Case 1: All items removed
        if data.get("all_removed") or not data.get("remaining_items"):
            supabase.table("orders")\
                .update({
                    "status": "cancelled",
                    "cancellation_status": "approved"
                })\
                .eq("id", order["id"])\
                .execute()
            
            return "🗑️ Order cancelled successfully."
        
        # Case 2: Partial removal
        remaining_items = data["remaining_items"]
        removed_items = data.get("removed_items", "items")
        
        # Calculate new price
        new_price = await calculate_price_from_items(remaining_items)
        
        # Update order
        supabase.table("orders")\
            .update({
                "items": remaining_items,
                "price": new_price
            })\
            .eq("id", order["id"])\
            .execute()
        
        return (
            f"✅ **Order Updated**\n"
            f"Removed: {removed_items}\n"
            f"Remaining: {remaining_items}\n"
            f"💰 New Total: ${new_price:.2f}"
        )
        
    except Exception as e:
        print(f"[MODIFICATION ERROR] {e}")
        return "❌ Error modifying order. Please try /cancel or contact staff."


async def calculate_price_from_items(items_str: str) -> float:
    """
    Extract and sum prices from item string like "Burger ($10), Fries ($5)".
    Falls back to AI if extraction fails.
    """
    try:
        # Method 1: Regex extraction
        prices = re.findall(r'\$?(\d+(?:\.\d{2})?)', items_str)
        if prices:
            return sum(float(p) for p in prices)
        
        # Method 2: AI fallback
        prompt = f"Sum all prices in this text: '{items_str}'. Return ONLY the number."
        
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=20
        )
        
        response_text = completion.choices[0].message.content
        numbers = re.findall(r'\d+(?:\.\d{2})?', response_text)
        
        if numbers:
            return float(numbers[0])
        
        return 0.0
        
    except Exception as e:
        print(f"[PRICE CALC ERROR] {e}")
        return 0.0


# ============================================================================
# NEW ORDER PROCESSING
# ============================================================================

async def process_new_order(user_text: str, user, restaurant_id: str, table_number: str, chat_id: str) -> Optional[str]:
    """
    Process a new food order using AI.
    Returns success message if valid order, None if not an order.
    """
    try:
        # Fetch menu from database
        menu_response = supabase.table("menu_items")\
            .select("content")\
            .eq("restaurant_id", restaurant_id)\
            .limit(60)\
            .execute()
        
        menu_text = "\n".join([m["content"] for m in menu_response.data]) if menu_response.data else "No menu available"
        
        # AI Prompt for order extraction
        prompt = f"""
        You are a restaurant order assistant.
        
        MENU:
        {menu_text}
        
        USER REQUEST: "{user_text}"
        
        TASK:
        Determine if this is a FOOD ORDER or just a QUESTION/CHAT.
        
        Examples of ORDERS:
        - "I'll have 2 burgers and a coffee"
        - "Can I get the pasta please"
        - "One full stack burger"
        
        Examples of NOT ORDERS:
        - "What's in the burger?"
        - "Do you have vegan options?"
        - "Show me the menu"
        
        Return JSON:
        {{
            "valid": true/false,
            "items": ["Full Stack Burger ($18)", "Java Jolt Espresso ($4)"],
            "total_price": 22.0
        }}
        
        RULES:
        - Set "valid": false if user is asking a question or not ordering
        - Only include items that exist on the menu
        - Include quantity if specified (e.g., "2x Full Stack Burger ($36)")
        - Calculate total_price accurately
        """
        
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a JSON-only food order extraction assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )
        
        raw_response = completion.choices[0].message.content
        data = extract_json_from_text(raw_response)
        
        if not data:
            print("[ORDER] Failed to parse AI response")
            return None
        
        # Check if valid order
        if not data.get("valid"):
            print("[ORDER] Not a valid order request")
            return None
        
        # Validate items exist
        items = data.get("items", [])
        if not items:
            print("[ORDER] No items extracted")
            return None
        
        # Calculate price (validate AI calculation)
        total_price = data.get("total_price", 0.0)
        if total_price <= 0:
            # Recalculate if AI gave invalid price
            items_str = ", ".join(items)
            total_price = await calculate_price_from_items(items_str)
        
        # Create order in database
        order_data = {
            "restaurant_id": str(restaurant_id),
            "user_id": str(user.id),
            "chat_id": str(chat_id),
            "table_number": str(table_number),
            "customer_name": user.full_name or "Guest",
            "items": ", ".join(items),
            "price": float(total_price),
            "status": "pending",
            "cancellation_status": "none"
        }
        
        result = supabase.table("orders").insert(order_data).execute()
        
        # Success response
        return (
            f"👨‍🍳 **Order Sent to Kitchen!**\n\n"
            f"🍽 {order_data['items']}\n"
            f"💰 Total: ${order_data['price']:.2f}\n"
            f"🪑 Table: {table_number}\n\n"
            f"We'll notify you when it's ready!"
        )
        
    except Exception as e:
        print(f"[NEW ORDER ERROR] {e}")
        import traceback
        traceback.print_exc()
        return None


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

async def process_order(user_text: str, user, restaurant_id: str, table_number: str, chat_id: str) -> Optional[str]:
    """
    Main entry point for order processing.
    
    Flow:
    1. Check if modification/cancellation request
    2. If not, try to process as new order
    3. Return None if neither (let main.py handle as chat)
    
    Args:
        user_text: User's message
        user: Telegram user object
        restaurant_id: Current restaurant ID
        table_number: User's table number
        chat_id: Telegram chat ID
    
    Returns:
        Success message if order processed, None otherwise
    """
    # Step 1: Check for modifications first
    modification_result = await handle_modification(user_text, user, restaurant_id)
    if modification_result:
        return modification_result
    
    # Step 2: Try to process as new order
    new_order_result = await process_new_order(user_text, user, restaurant_id, table_number, chat_id)
    if new_order_result:
        return new_order_result
    
    # Step 3: Not an order - return None (main.py will handle as chat)
    return None
