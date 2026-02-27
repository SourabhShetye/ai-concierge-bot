"""
WebSocket handlers for web chat interface
"""

from typing import Dict
from fastapi import WebSocket
import bcrypt
from datetime import datetime, timezone

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.sessions: Dict[str, dict] = {}
    
    async def connect(self, session_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[session_id] = websocket
        
        # Initialize session
        if session_id not in self.sessions:
            self.sessions[session_id] = {
                "state": "IDLE",
                "mode": "GENERAL",
                "data": {}
            }
        
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
                print(f"[WS SEND ERROR] {ex}")
                self.disconnect(session_id)

async def handle_websocket_message(session_id: str, data: dict, manager: ConnectionManager, supabase, groq_client):
    """Process incoming WebSocket messages"""
    
    session = manager.sessions.get(session_id, {})
    message_type = data.get("type")
    content = data.get("content", "")
    
    # Button click handler
    if message_type == "button_click":
        action = data.get("action")
        
        if action == "customer_type_new":
            session["state"] = "AWAITING_NAME"
            session["data"]["customer_type"] = "new"
            await manager.send_message(session_id, {
                "type": "message",
                "content": "🆕 **New Customer**\n\nWhat is your name?"
            })
        
        elif action == "customer_type_returning":
            session["state"] = "AWAITING_NAME"
            session["data"]["customer_type"] = "returning"
            await manager.send_message(session_id, {
                "type": "message",
                "content": "🔙 **Returning Customer**\n\nEnter your name:"
            })
        
        elif action == "mode_order":
            await manager.send_message(session_id, {
                "type": "message",
                "content": "🍽️ **Order Mode**\n\nWhat would you like to order?\n\n_Type 'menu' to see dishes._",
                "buttons": [{"text": "📋 View Menu", "action": "show_menu"}]
            })
        
        elif action == "show_menu":
            await send_menu(session_id, session, manager, supabase)
    
    # Text message handler
    elif message_type == "text":
        state = session.get("state", "IDLE")
        
        if state == "AWAITING_NAME":
            await handle_name_input(session_id, content, session, manager, supabase)
        
        elif state == "AWAITING_PIN_SETUP":
            await handle_pin_setup(session_id, content, session, manager, supabase)
        
        else:
            # General chat or order
            await handle_general_message(session_id, content, session, manager, supabase, groq_client)

async def handle_name_input(session_id: str, name: str, session: dict, manager: ConnectionManager, supabase):
    """Handle customer name input"""
    name = name.strip()
    
    if len(name) < 2:
        await manager.send_message(session_id, {
            "type": "message",
            "content": "❌ Please enter a valid name (at least 2 characters)."
        })
        return
    
    session["data"]["display_name"] = name
    customer_type = session["data"].get("customer_type", "new")
    
    if customer_type == "new":
        session["state"] = "AWAITING_PIN_SETUP"
        await manager.send_message(session_id, {
            "type": "message",
            "content": f"✅ Name saved: **{name}**\n\n🔐 Create a 4-digit PIN:",
            "input_type": "password"
        })
    else:
        # Check if customer exists
        restaurant_id = session["data"].get("restaurant_id")
        try:
            all_sessions = supabase.table("user_sessions").select("*")\
                .eq("display_name", name)\
                .eq("restaurant_id", restaurant_id)\
                .order("last_visit", desc=True).execute()
            
            sessions_with_pin = [s for s in (all_sessions.data or []) if s.get("pin_hash")]
            
            if sessions_with_pin:
                session["data"]["login_target"] = sessions_with_pin[0]
                session["state"] = "AWAITING_PIN_LOGIN"
                await manager.send_message(session_id, {
                    "type": "message",
                    "content": f"✅ Found account for **{name}**!\n\n🔐 Enter your PIN:",
                    "input_type": "password"
                })
            else:
                await manager.send_message(session_id, {
                    "type": "message",
                    "content": f"❌ No account found. Create one?",
                    "buttons": [
                        {"text": "🆕 Create Account", "action": "customer_type_new"}
                    ]
                })
        except Exception as ex:
            print(f"[NAME LOOKUP ERROR] {ex}")

async def handle_pin_setup(session_id: str, pin: str, session: dict, manager: ConnectionManager, supabase):
    """Handle PIN creation"""
    pin = pin.strip()
    
    if not pin.isdigit() or len(pin) != 4:
        await manager.send_message(session_id, {
            "type": "message",
            "content": "❌ PIN must be 4 digits. Try again:",
            "input_type": "password"
        })
        return
    
    # Hash PIN
    pin_hash = bcrypt.hashpw(pin.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    
    # Save to database
    name = session["data"].get("display_name")
    restaurant_id = session["data"].get("restaurant_id")
    
    try:
        supabase.table("user_sessions").insert({
            "user_id": session_id,
            "session_id": session_id,
            "restaurant_id": restaurant_id,
            "display_name": name,
            "pin_hash": pin_hash,
            "visit_count": 0,
            "total_spend": 0.0,
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        
        session["state"] = "IDLE"
        await manager.send_message(session_id, {
            "type": "message",
            "content": f"✅ Account created, **{name}**!\n\nReady to order?",
            "buttons": [
                {"text": "🍽️ Order Food", "action": "mode_order"},
                {"text": "📅 Book Table", "action": "mode_booking"}
            ]
        })
    except Exception as ex:
        print(f"[PIN SETUP ERROR] {ex}")

async def send_menu(session_id: str, session: dict, manager: ConnectionManager, supabase):
    """Send restaurant menu"""
    restaurant_id = session["data"].get("restaurant_id")
    
    try:
        rows = supabase.table("menu_items").select("content,sold_out").eq("restaurant_id", restaurant_id).execute()
        
        lines = ["🍽️ **Menu**\n"]
        cur_cat = None
        
        for row in rows.data or []:
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
                    item = line.replace("item:", "").strip()
                    if sold_out:
                        lines.append(f"  • ~~{item}~~ ❌ SOLD OUT")
                    else:
                        lines.append(f"  • {item}")
                elif line.startswith("price:") and not sold_out:
                    lines[-1] += f"  —  {line.replace('price:', '').strip()}"
        
        lines.append("\n_Tell me what you'd like!_")
        
        await manager.send_message(session_id, {
            "type": "message",
            "content": "\n".join(lines)
        })
    except Exception as ex:
        print(f"[MENU ERROR] {ex}")

async def handle_general_message(session_id: str, text: str, session: dict, manager: ConnectionManager, supabase, groq_client):
    """Handle general chat or order"""
    
    # Check if it's an order
    if "menu" in text.lower():
        await send_menu(session_id, session, manager, supabase)
        return
    
    # Echo for now (implement full logic as needed)
    await manager.send_message(session_id, {
        "type": "message",
        "content": f"You said: {text}\n\n_(Full order processing coming soon)_"
    })