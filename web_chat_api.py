"""
Web Chat API - FastAPI WebSocket Server
Replaces Telegram with web-based chat interface
"""

import os, json, uuid, asyncio
from datetime import datetime, timezone
from typing import Dict, Optional, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from supabase import create_client
from groq import AsyncGroq

# Import existing logic from main.py
from order_service import process_order, fetch_order_for_user, stage_cancellation, stage_modification

load_dotenv()

app = FastAPI(title="Restaurant Web Chat API")

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify your domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database setup
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

# ── WebSocket Connection Manager ──────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, session_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[session_id] = websocket
        print(f"[WS] Session {session_id[:8]} connected")
    
    def disconnect(self, session_id: str):
        if session_id in self.active_connections:
            del self.active_connections[session_id]
            print(f"[WS] Session {session_id[:8]} disconnected")
    
    async def send_message(self, session_id: str, message: dict):
        if session_id in self.active_connections:
            try:
                await self.active_connections[session_id].send_json(message)
            except Exception as ex:
                print(f"[WS ERROR] {ex}")
                self.disconnect(session_id)

manager = ConnectionManager()

# ── Session Storage (In-Memory - use Redis in production) ────────────────────

sessions: Dict[str, dict] = {}

def get_session(session_id: str) -> dict:
    if session_id not in sessions:
        sessions[session_id] = {
            "state": "IDLE",
            "mode": "GENERAL",
            "data": {},
            "message_history": []
        }
    return sessions[session_id]

# ── QR Code Generation Endpoint ──────────────────────────────────────────────

@app.get("/api/qr/{restaurant_id}/{table_number}")
async def generate_qr_code(restaurant_id: str, table_number: str):
    """Generate QR code URL for a specific table"""
    import qrcode
    from io import BytesIO
    
    # Chat URL that QR code will link to
    chat_url = f"https://yourdomain.com/chat/{restaurant_id}/{table_number}"
    
    # Generate QR code
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(chat_url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Save to bytes
    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    
    from fastapi.responses import StreamingResponse
    return StreamingResponse(buf, media_type="image/png")

# ── Chat Page HTML ───────────────────────────────────────────────────────────

@app.get("/chat/{restaurant_id}/{table_number}")
async def chat_page(restaurant_id: str, table_number: str):
    """Serve the chat interface HTML"""
    return FileResponse("static/chat.html")

# ── WebSocket Chat Endpoint ──────────────────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await manager.connect(session_id, websocket)
    session = get_session(session_id)
    
    try:
        # Extract restaurant_id and table from session_id format: rest_abc_table_7
        parts = session_id.split("_")
        if len(parts) >= 4:
            restaurant_id = "_".join(parts[:2])  # rest_abc
            table_number = parts[3]  # 7
            
            session["data"]["restaurant_id"] = restaurant_id
            session["data"]["table_number"] = table_number
            session["data"]["qr_scanned"] = True
            
            # Fetch restaurant details
            try:
                rest = supabase.table("restaurants").select("name").eq("id", restaurant_id).limit(1).execute()
                if rest.data:
                    session["data"]["restaurant_name"] = rest.data[0]["name"]
            except Exception:
                pass
        
        # Send welcome message
        await manager.send_message(session_id, {
            "type": "message",
            "content": f"👋 Welcome to {session['data'].get('restaurant_name', 'our restaurant')}!\n\nAre you a new or returning customer?",
            "buttons": [
                {"text": "🆕 New Customer", "action": "customer_type_new"},
                {"text": "🔙 Returning Customer", "action": "customer_type_returning"}
            ]
        })
        
        # Listen for messages
        while True:
            data = await websocket.receive_json()
            await handle_message(session_id, data, session)
            
    except WebSocketDisconnect:
        manager.disconnect(session_id)
    except Exception as ex:
        print(f"[WS ERROR] {ex}")
        manager.disconnect(session_id)

# ── Message Handler ───────────────────────────────────────────────────────────

async def handle_message(session_id: str, data: dict, session: dict):
    """Process incoming messages from web chat"""
    message_type = data.get("type")
    content = data.get("content", "")
    
    state = session["state"]
    mode = session["mode"]
    session_data = session["data"]
    
    # Handle button clicks
    if message_type == "button_click":
        action = data.get("action")
        await handle_button_action(session_id, action, session)
        return
    
    # Handle text messages
    if message_type == "text":
        # State-based routing (similar to Telegram bot)
        
        if state == "AWAITING_NAME":
            await handle_name_input(session_id, content, session)
        
        elif state == "AWAITING_PIN_SETUP":
            await handle_pin_setup(session_id, content, session)
        
        elif state == "AWAITING_PIN_CONFIRM":
            await handle_pin_confirm(session_id, content, session)
        
        elif state == "AWAITING_PIN_LOGIN":
            await handle_pin_login(session_id, content, session)
        
        elif state == "AWAITING_TABLE":
            await handle_table_assignment(session_id, content, session)
        
        elif state == "HAS_TABLE":
            # Check if allergy/preferences asked
            if not session_data.get("preferences_set"):
                await handle_allergy_input(session_id, content, session)
            else:
                # Process order
                await handle_order_input(session_id, content, session)
        
        elif state == "AWAITING_GUESTS":
            await handle_booking_guests(session_id, content, session)
        
        elif state == "AWAITING_TIME":
            await handle_booking_time(session_id, content, session)
        
        elif state == "AWAITING_FEEDBACK":
            await handle_feedback(session_id, content, session)
        
        else:
            # General mode
            await handle_general_chat(session_id, content, session)

# ── Handler Functions (converted from Telegram handlers) ─────────────────────

async def handle_button_action(session_id: str, action: str, session: dict):
    """Handle button clicks"""
    
    if action == "customer_type_new":
        session["state"] = "AWAITING_NAME"
        session["data"]["customer_type"] = "new"
        await manager.send_message(session_id, {
            "type": "message",
            "content": "🆕 **New Customer Setup**\n\nWhat is your name?\n_Example: Sarah or Ahmed_"
        })
    
    elif action == "customer_type_returning":
        session["state"] = "AWAITING_NAME"
        session["data"]["customer_type"] = "returning"
        await manager.send_message(session_id, {
            "type": "message",
            "content": "🔙 **Welcome Back!**\n\nPlease enter your name:"
        })
    
    elif action == "mode_order":
        session["mode"] = "ORDER"
        
        # Check if table was set via QR
        if session["data"].get("qr_scanned") and session["data"].get("table_number"):
            session["state"] = "HAS_TABLE"
            
            # Ask for allergies first
            if not session["data"].get("preferences"):
                await manager.send_message(session_id, {
                    "type": "message",
                    "content": f"🍽️ **Order Mode**\n\n✅ **Table {session['data']['table_number']}** (from QR code)\n\n⚠️ **Do you have any allergies or dietary restrictions?**\n\n_Examples: 'allergic to nuts', 'vegetarian', 'no gluten'_\n\nOr type **'none'** if you have no restrictions."
                })
            else:
                await manager.send_message(session_id, {
                    "type": "message",
                    "content": f"🍽️ **Order Mode**\n\n✅ **Table {session['data']['table_number']}** (from QR code)\n\nWhat would you like to order?\n\n_Type 'menu' to see all available dishes._",
                    "buttons": [
                        {"text": "📋 View Menu", "action": "show_menu"}
                    ]
                })
        else:
            session["state"] = "AWAITING_TABLE"
            await manager.send_message(session_id, {
                "type": "message",
                "content": "🍽️ **Order Mode**\n\n🪑 What is your table number?"
            })
    
    elif action == "mode_booking":
        session["mode"] = "BOOKING"
        await manager.send_message(session_id, {
            "type": "message",
            "content": "📅 **Booking Management**\n\nWhat would you like to do?",
            "buttons": [
                {"text": "📅 Make a Booking", "action": "booking_new"},
                {"text": "❌ Cancel a Booking", "action": "booking_cancel"},
                {"text": "⬅️ Main Menu", "action": "main_menu"}
            ]
        })
    
    elif action == "main_menu":
        session["mode"] = "GENERAL"
        session["state"] = "IDLE"
        await manager.send_message(session_id, {
            "type": "message",
            "content": f"👋 Back to **General Mode**, {session['data'].get('display_name', 'there')}!\n\nAsk me anything, or choose an option:",
            "buttons": [
                {"text": "🍽️ Order Food", "action": "mode_order"},
                {"text": "📅 Book a Table", "action": "mode_booking"}
            ]
        })
    
    elif action == "show_menu":
        await send_menu(session_id, session)

async def handle_name_input(session_id: str, name: str, session: dict):
    """Handle name input from new/returning customer"""
    name = name.strip()
    
    # Validation
    if len(name) < 2 or name.isdigit():
        await manager.send_message(session_id, {
            "type": "message",
            "content": "❌ Please enter a valid name (at least 2 characters).\n_Example: Sarah or Ahmed_"
        })
        return
    
    session["data"]["display_name"] = name
    customer_type = session["data"].get("customer_type", "new")
    restaurant_id = session["data"].get("restaurant_id")
    
    # NEW CUSTOMER: Set up PIN
    if customer_type == "new":
        session["state"] = "AWAITING_PIN_SETUP"
        await manager.send_message(session_id, {
            "type": "message",
            "content": f"✅ Name saved: **{name}**\n\n🔐 Now, create a 4-digit PIN for future visits:\n\n_This PIN will let you access your order history and rewards on future visits._",
            "input_type": "password"  # Signal frontend to mask input
        })
        return
    
    # RETURNING CUSTOMER: Check if exists
    else:
        try:
            # Query sessions with this name at this restaurant
            all_sessions = supabase.table("user_sessions").select(
                "session_id,pin_hash,visit_count,total_spend,last_visit"
            ).eq("display_name", name).eq("restaurant_id", restaurant_id).order("last_visit", desc=True).execute()
            
            sessions_with_pin = [s for s in (all_sessions.data or []) if s.get("pin_hash")]
            
            if sessions_with_pin:
                # Customer found
                target_session = sessions_with_pin[0]
                session["data"]["login_target_session"] = target_session
                session["data"]["login_attempts"] = 0
                session["state"] = "AWAITING_PIN_LOGIN"
                
                last_visit = target_session.get("last_visit")
                days_ago = ""
                if last_visit:
                    try:
                        lv_dt = datetime.fromisoformat(str(last_visit).replace("Z", "+00:00"))
                        days = (datetime.now(timezone.utc) - lv_dt).days
                        days_ago = f" (last visit: {days} days ago)" if days > 0 else " (last visit: today)"
                    except Exception:
                        pass
                
                await manager.send_message(session_id, {
                    "type": "message",
                    "content": f"✅ Found your account, **{name}**!{days_ago}\n\n📊 {target_session['visit_count']} visits • ${float(target_session['total_spend']):.2f} spent\n\n🔐 Please enter your 4-digit PIN:",
                    "input_type": "password"
                })
            else:
                # Customer not found
                await manager.send_message(session_id, {
                    "type": "message",
                    "content": f"❌ No account found for **{name}** at this restaurant.\n\nWould you like to create a new account?",
                    "buttons": [
                        {"text": "🆕 Create New Account", "action": "customer_type_new"},
                        {"text": "🔄 Try Different Name", "action": "customer_type_returning"}
                    ]
                })
        except Exception as ex:
            print(f"[NAME LOOKUP] {ex}")
            await manager.send_message(session_id, {
                "type": "message",
                "content": "❌ Error checking account.\n\nWould you like to create a new account or try again?",
                "buttons": [
                    {"text": "🆕 Create New Account", "action": "customer_type_new"},
                    {"text": "🔄 Try Again", "action": "customer_type_returning"}
                ]
            })

async def handle_pin_setup(session_id: str, pin: str, session: dict):
    """Handle PIN setup for new customer"""
    pin = pin.strip()
    
    if not pin.isdigit() or len(pin) != 4:
        await manager.send_message(session_id, {
            "type": "message",
            "content": "❌ PIN must be exactly 4 digits.\n\nPlease try again:",
            "input_type": "password"
        })
        return
    
    session["data"]["temp_pin"] = pin
    session["state"] = "AWAITING_PIN_CONFIRM"
    
    await manager.send_message(session_id, {
        "type": "message",
        "content": "🔐 **Confirm your PIN**\n\nPlease enter your 4-digit PIN again to confirm:",
        "input_type": "password"
    })

async def handle_pin_confirm(session_id: str, pin: str, session: dict):
    """Handle PIN confirmation"""
    pin = pin.strip()
    temp_pin = session["data"].get("temp_pin")
    
    if pin != temp_pin:
        await manager.send_message(session_id, {
            "type": "message",
            "content": "❌ PINs don't match. Let's try again.\n\nEnter a 4-digit PIN:",
            "input_type": "password"
        })
        session["state"] = "AWAITING_PIN_SETUP"
        session["data"].pop("temp_pin", None)
        return
    
    # Hash PIN
    import bcrypt
    pin_hash = bcrypt.hashpw(pin.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    
    # Save to database
    name = session["data"].get("display_name")
    restaurant_id = session["data"].get("restaurant_id")
    
    try:
        supabase.table("user_sessions").insert({
            "user_id": session_id,  # Use session_id as user_id for web
            "session_id": session_id,
            "restaurant_id": restaurant_id,
            "display_name": name,
            "pin_hash": pin_hash,
            "visit_count": 0,
            "total_spend": 0.0,
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        
        print(f"[PIN SETUP] Created account for {name} (session: {session_id[:8]})")
    except Exception as ex:
        print(f"[PIN SETUP ERROR] {ex}")
    
    session["data"].pop("temp_pin", None)
    session["state"] = "IDLE"
    session["mode"] = "GENERAL"
    
    await manager.send_message(session_id, {
        "type": "message",
        "content": f"✅ **Account Created!**\n\n👋 Welcome to **{session['data'].get('restaurant_name', 'our restaurant')}**, **{name}**!\n\n🔐 Your PIN is saved securely. Use it on your next visit!\n\nReady to order or book? Choose an option:",
        "buttons": [
            {"text": "🍽️ Order Food", "action": "mode_order"},
            {"text": "📅 Book a Table", "action": "mode_booking"}
        ]
    })

async def handle_pin_login(session_id: str, pin: str, session: dict):
    """Handle returning customer PIN login"""
    pin = pin.strip()
    
    if not pin.isdigit() or len(pin) != 4:
        await manager.send_message(session_id, {
            "type": "message",
            "content": "❌ PIN must be 4 digits. Please try again:",
            "input_type": "password"
        })
        return
    
    target_session = session["data"].get("login_target_session")
    if not target_session:
        await manager.send_message(session_id, {
            "type": "message",
            "content": "❌ Session expired. Please refresh and start again."
        })
        return
    
    stored_hash = target_session.get("pin_hash")
    attempts = session["data"].get("login_attempts", 0) + 1
    session["data"]["login_attempts"] = attempts
    
    # Verify PIN
    import bcrypt
    try:
        pin_correct = bcrypt.checkpw(pin.encode('utf-8'), stored_hash.encode('utf-8'))
    except Exception as ex:
        print(f"[PIN VERIFY ERROR] {ex}")
        pin_correct = False
    
    if pin_correct:
        # SUCCESS
        name = session["data"].get("display_name")
        
        # Load CRM data
        vc = int(target_session.get("visit_count", 0))
        ts = float(target_session.get("total_spend", 0.0))
        
        # Compute tags
        tags = []
        if vc > 5: tags.append("Frequent Diner")
        if ts > 500: tags.append("Big Spender")
        if "Frequent Diner" in tags and "Big Spender" in tags: tags.append("VIP")
        
        session["data"]["visit_count"] = vc
        session["data"]["total_spend"] = ts
        session["data"]["tags"] = tags
        session["data"].pop("login_target_session", None)
        session["data"].pop("login_attempts", None)
        
        session["state"] = "IDLE"
        session["mode"] = "GENERAL"
        
        # Build greeting
        restaurant_name = session["data"].get("restaurant_name", "our restaurant")
        greeting = build_personalized_greeting(name, restaurant_name, tags, vc)
        
        tag_str = ("  ·  ".join(f"🏷 {t}" for t in tags) + "\n\n") if tags else ""
        
        await manager.send_message(session_id, {
            "type": "message",
            "content": f"✅ **Welcome Back!**\n\n{greeting}\n\n{tag_str}Ready to order or book? Choose an option:",
            "buttons": [
                {"text": "🍽️ Order Food", "action": "mode_order"},
                {"text": "📅 Book a Table", "action": "mode_booking"}
            ]
        })
        
        print(f"[PIN LOGIN] Success for {name} (session: {session_id[:8]})")
    
    else:
        # FAILED
        if attempts >= 3:
            session["data"].pop("login_target_session", None)
            session["data"].pop("login_attempts", None)
            
            await manager.send_message(session_id, {
                "type": "message",
                "content": "❌ **Too Many Failed Attempts**\n\nFor security, please start over or create a new account.",
                "buttons": [
                    {"text": "🔄 Try Again", "action": "customer_type_returning"},
                    {"text": "🆕 Create New Account", "action": "customer_type_new"}
                ]
            })
        else:
            remaining = 3 - attempts
            await manager.send_message(session_id, {
                "type": "message",
                "content": f"❌ **Incorrect PIN**\n\nYou have {remaining} attempt(s) remaining.\n\nPlease try again:",
                "input_type": "password"
            })

async def handle_allergy_input(session_id: str, text: str, session: dict):
    """Handle allergy/dietary restriction input"""
    text_lower = text.lower().strip()
    
    if text_lower in ["none", "no", "nope", "nothing", "n/a"]:
        session["data"]["preferences"] = ""
        session["data"]["preferences_set"] = True
        await manager.send_message(session_id, {
            "type": "message",
            "content": "✅ **Got it!**\n\nWhat would you like to order?\n\n_Type 'menu' to see all available dishes._",
            "buttons": [
                {"text": "📋 View Menu", "action": "show_menu"}
            ]
        })
        return
    
    # Check if allergy-related
    import re
    allergy_pat = re.compile(
        r"\b(allerg|intoleran|vegan|vegetarian|jain|halal|kosher|gluten.?free"
        r"|nut.?free|dairy.?free|no (nuts?|pork|shellfish|gluten|dairy|egg))\b", re.IGNORECASE)
    
    if allergy_pat.search(text):
        session["data"]["preferences"] = text
        session["data"]["preferences_set"] = True
        await manager.send_message(session_id, {
            "type": "message",
            "content": f"📋 **Dietary restriction saved:** _{text}_\n\nI'll warn you if any dish conflicts with this.\n\nWhat would you like to order?\n\n_Type 'menu' to see all available dishes._",
            "buttons": [
                {"text": "📋 View Menu", "action": "show_menu"}
            ]
        })
        return
    else:
        # Treat as order
        session["data"]["preferences"] = ""
        session["data"]["preferences_set"] = True
        await handle_order_input(session_id, text, session)

async def handle_order_input(session_id: str, text: str, session: dict):
    """Handle order text from customer"""
    
    # Check for menu request
    if any(k in text.lower() for k in ["menu", "what do you serve", "what's available", "food list"]):
        await send_menu(session_id, session)
        return
    
    # Check for bill request
    if any(k in text.lower() for k in ["bill", "check please", "the check", "my total", "how much", "pay", "invoice"]):
        await send_bill(session_id, session)
        return
    
    # Process order using existing order_service.py
    try:
        # Create minimal user object for compatibility
        class WebUser:
            def __init__(self, session_id):
                self.id = session_id
                self.full_name = ""
        
        user = WebUser(session_id)
        restaurant_id = session["data"].get("restaurant_id")
        table_number = session["data"].get("table_number")
        
        result = await process_order(
            text, user, restaurant_id, table_number, session_id,
            user_preferences=session["data"].get("preferences", ""),
            session_id=session_id,
            display_name=session["data"].get("display_name", "")
        )
        
        if result:
            reply, order_id = result
            await manager.send_message(session_id, {
                "type": "message",
                "content": reply
            })
        else:
            # Get examples from menu
            examples = []
            try:
                menu_res = supabase.table("menu_items").select("content")\
                    .eq("restaurant_id", restaurant_id)\
                    .eq("sold_out", False).limit(5).execute()
                
                if menu_res.data:
                    for item in menu_res.data[:3]:
                        for line in item["content"].split("\n"):
                            if line.startswith("item:"):
                                item_name = line.replace("item:", "").strip()
                                examples.append(item_name)
                                break
            except Exception:
                examples = ["items from our menu"]
            
            example_text = ", ".join(examples[:2]) if examples else "items from menu"
            
            await manager.send_message(session_id, {
                "type": "message",
                "content": f"❌ I couldn't understand your order.\n\n💡 Try:\n• Use full item names (e.g., '{example_text}')\n• Say 'menu' to see all available dishes\n• Be specific with quantities (e.g., '2 Burgers')",
                "buttons": [
                    {"text": "📋 View Menu", "action": "show_menu"}
                ]
            })
    except Exception as ex:
        print(f"[ORDER ERROR] {ex}")
        await manager.send_message(session_id, {
            "type": "message",
            "content": "❌ Error processing order. Please try again."
        })

async def send_menu(session_id: str, session: dict):
    """Send restaurant menu"""
    restaurant_id = session["data"].get("restaurant_id")
    restaurant_name = session["data"].get("restaurant_name", "Our Restaurant")
    
    try:
        rows = supabase.table("menu_items").select("content,sold_out").eq("restaurant_id", restaurant_id).execute()
        if not rows.data:
            await manager.send_message(session_id, {
                "type": "message",
                "content": "📋 Menu unavailable."
            })
            return
        
        lines = [f"🍽️ **{restaurant_name} — Menu**\n"]
        cur_cat = None
        
        for row in rows.data:
            sold_out = row.get("sold_out", False)
            for line in row["content"].split("\n"):
                line = line.strip()
                if not line: continue
                
                if line.startswith("category:"):
                    cat = line.replace("category:", "").strip()
                    if cat != cur_cat:
                        lines.append(f"\n**{cat.upper()}**")
                        cur_cat = cat
                elif line.startswith("item:"):
                    item_name = line.replace("item:", "").strip()
                    if sold_out:
                        lines.append(f"  • ~~{item_name}~~ ❌ SOLD OUT")
                    else:
                        lines.append(f"  • {item_name}")
                elif line.startswith("price:"):
                    if not sold_out:
                        lines[-1] += f"  —  {line.replace('price:', '').strip()}"
                elif line.startswith("description:"):
                    if not sold_out:
                        lines.append(f"    _{line.replace('description:', '').strip()}_")
        
        lines.append("\n_Tell me what you'd like and I'll place the order!_")
        menu_text = "\n".join(lines)
        
        await manager.send_message(session_id, {
            "type": "message",
            "content": menu_text
        })
    except Exception as ex:
        print(f"[MENU ERROR] {ex}")
        await manager.send_message(session_id, {
            "type": "message",
            "content": "❌ Error loading menu."
        })

async def send_bill(session_id: str, session: dict):
    """Send bill for current table"""
    table_number = session["data"].get("table_number")
    restaurant_id = session["data"].get("restaurant_id")
    
    if not table_number:
        await manager.send_message(session_id, {
            "type": "message",
            "content": "🪑 Please specify your table number first."
        })
        return
    
    try:
        res = supabase.table("orders").select("id,items,price")\
            .eq("session_id", session_id).eq("restaurant_id", restaurant_id)\
            .eq("table_number", str(table_number))\
            .neq("status", "paid").neq("status", "cancelled").execute()
        
        if not res.data:
            await manager.send_message(session_id, {
                "type": "message",
                "content": f"🧾 **Table {table_number}** — No active orders."
            })
            return
        
        total = round(sum(float(r["price"]) for r in res.data), 2)
        lines = "\n".join(f"  • **#{r['id']}** {r['items']}  —  ${float(r['price']):.2f}" for r in res.data)
        
        await manager.send_message(session_id, {
            "type": "message",
            "content": f"🧾 **Bill — Table {table_number}**\n\n{lines}\n\n💰 **Total: ${total:.2f}**\n\n_(Ask a waiter to pay)_"
        })
    except Exception as ex:
        print(f"[BILL ERROR] {ex}")
        await manager.send_message(session_id, {
            "type": "message",
            "content": "❌ Error fetching bill."
        })

async def handle_feedback(session_id: str, text: str, session: dict):
    """Handle customer feedback"""
    try:
        supabase.table("feedback").insert({
            "restaurant_id": session["data"].get("restaurant_id"),
            "user_id": session_id,
            "session_id": session_id,
            "ratings": text,
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        
        await manager.send_message(session_id, {
            "type": "message",
            "content": "⭐ Thank you for your feedback!\n\nSee you again soon! 😊",
            "buttons": [
                {"text": "🍽️ Order Again", "action": "mode_order"},
                {"text": "📅 Book a Table", "action": "mode_booking"}
            ]
        })
        
        session["state"] = "IDLE"
        session["mode"] = "GENERAL"
    except Exception as ex:
        print(f"[FEEDBACK ERROR] {ex}")
        await manager.send_message(session_id, {
            "type": "message",
            "content": "✅ Feedback received. Thank you!"
        })

async def handle_general_chat(session_id: str, text: str, session: dict):
    """Handle general Q&A using Groq"""
    restaurant_id = session["data"].get("restaurant_id")
    
    try:
        rows = supabase.table("menu_items").select("content").eq("restaurant_id", restaurant_id).limit(30).execute()
        menu_ctx = "\n".join(r["content"] for r in rows.data) if rows.data else ""
        
        # Get policy
        pol = supabase.table("restaurant_policies").select("policy_text").eq("restaurant_id", restaurant_id).limit(1).execute()
        policy_ctx = pol.data[0].get("policy_text", "") if pol.data else ""
        
        system = ("You are a helpful restaurant concierge.\n\n"
                  + (f"MENU:\n{menu_ctx}\n\n" if menu_ctx else "")
                  + (f"RESTAURANT INFO:\n{policy_ctx}\n\n" if policy_ctx else "")
                  + "Answer questions about menu/WiFi/parking/hours concisely (2-3 sentences). "
                    "Never take orders or handle bookings.")
        
        c = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
            temperature=0.7,
            max_tokens=250
        )
        
        await manager.send_message(session_id, {
            "type": "message",
            "content": c.choices[0].message.content
        })
    except Exception as ex:
        print(f"[GENERAL CHAT ERROR] {ex}")
        await manager.send_message(session_id, {
            "type": "message",
            "content": "I'm here to help! Ask me about our menu, WiFi, parking, or policies."
        })

# Helper function
def build_personalized_greeting(name: str, restaurant_name: str, tags: list, visit_count: int = 0) -> str:
    """Build personalized greeting (copied from main.py)"""
    # Milestone rewards
    if visit_count == 5:
        return (f"🎉 **Congratulations, {name}!**\n\n"
                f"This is your **5th visit** to {restaurant_name}!\n"
                f"🎁 Enjoy a **FREE appetizer** on us today!\n\n"
                f"_Mention this message to your server._")
    elif visit_count == 10:
        return (f"🏆 **WOW! Visit #{visit_count}, {name}!**\n\n"
                f"You're officially a {restaurant_name} Legend!\n"
                f"🍰 Enjoy a **FREE dessert** today!\n\n"
                f"_Show this to your server._")
    elif visit_count % 10 == 0 and visit_count > 0:
        return (f"🌟 **Amazing! Visit #{visit_count}, {name}!**\n\n"
                f"We appreciate your loyalty!\n"
                f"🎁 **10% off** your bill today!")
    
    # Regular greetings
    if "VIP" in tags or "Big Spender" in tags:
        msg = f"👑 Welcome back, **{name}**! As one of our VIP guests, you're very special to us."
        import random
        if random.random() < 0.20:
            msg += "\n\n🍹 **Complimentary drink on us today — mention this when you order!**"
        return msg
    if "Frequent Diner" in tags:
        return f"😊 Welcome back, **{name}**! Great to see you again at **{restaurant_name}**. (Visit #{visit_count})"
    if "Churn Risk" in tags:
        return f"👋 **{name}**, we've missed you! So glad you're back at **{restaurant_name}**.\n\n🎁 **Welcome back gift:** 15% off today!"
    return f"👋 Welcome to **{restaurant_name}**, **{name}**!"

# Additional booking handlers would go here (handle_booking_guests, handle_booking_time, etc.)
# These follow the same pattern as above

# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)