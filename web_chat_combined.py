"""
Combined Restaurant Concierge - Telegram Bot + Web Chat
Deployed on Render with both interfaces
"""

import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

# Import your existing bot setup
from main import app as telegram_app_instance, telegram_app as bot_app

# Create combined FastAPI app
app = FastAPI(title="Restaurant Concierge - Combined API")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Telegram webhook routes from existing app
@app.post("/webhook")
async def telegram_webhook(request):
    """Telegram bot webhook (existing)"""
    from main import telegram_webhook
    return await telegram_webhook(request)

@app.get("/")
@app.head("/")
async def health():
    """Health check"""
    return {"status": "healthy", "services": ["telegram", "webchat"]}

# ── WebSocket Connection Manager ──────────────────────────────────────────────

sessions = {}

class ConnectionManager:
    def __init__(self):
        self.active_connections = {}
    
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

def get_session(session_id: str):
    if session_id not in sessions:
        sessions[session_id] = {
            "state": "IDLE",
            "mode": "GENERAL",
            "data": {},
            "message_history": []
        }
    return sessions[session_id]

# ── Web Chat Routes ───────────────────────────────────────────────────────────

@app.get("/chat/{restaurant_id}/{table_number}")
async def chat_page(restaurant_id: str, table_number: str):
    """Serve chat interface"""
    return FileResponse("static/chat.html")

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket connection for web chat"""
    await manager.connect(session_id, websocket)
    session = get_session(session_id)
    
    try:
        # Parse restaurant and table from session_id
        parts = session_id.split("_")
        if len(parts) >= 4:
            restaurant_id = "_".join(parts[:2])
            table_number = parts[3]
            
            session["data"]["restaurant_id"] = restaurant_id
            session["data"]["table_number"] = table_number
            session["data"]["qr_scanned"] = True
            
            # Get restaurant name
            from main import supabase
            try:
                rest = supabase.table("restaurants").select("name").eq("id", restaurant_id).limit(1).execute()
                if rest.data:
                    session["data"]["restaurant_name"] = rest.data[0]["name"]
            except Exception:
                pass
        
        # Send welcome
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
            await handle_web_message(session_id, data, session)
            
    except WebSocketDisconnect:
        manager.disconnect(session_id)
    except Exception as ex:
        print(f"[WS ERROR] {ex}")
        manager.disconnect(session_id)

async def handle_web_message(session_id: str, data: dict, session: dict):
    """Handle messages from web chat"""
    message_type = data.get("type")
    content = data.get("content", "")
    
    # Simple echo for now (you'll add full logic later)
    if message_type == "text":
        await manager.send_message(session_id, {
            "type": "message",
            "content": f"You said: {content}\n\n(Full logic coming soon - this is a deployment test)"
        })
    
    elif message_type == "button_click":
        action = data.get("action")
        
        if action == "customer_type_new":
            session["state"] = "AWAITING_NAME"
            await manager.send_message(session_id, {
                "type": "message",
                "content": "🆕 **New Customer**\n\nWhat is your name?"
            })
        
        elif action == "customer_type_returning":
            session["state"] = "AWAITING_NAME"
            await manager.send_message(session_id, {
                "type": "message",
                "content": "🔙 **Welcome Back!**\n\nPlease enter your name:"
            })

# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    """Initialize both Telegram bot and web chat"""
    # Start Telegram bot
    from main import startup_event
    await startup_event()
    
    print("✅ Combined server started:")
    print("   - Telegram bot: /webhook")
    print("   - Web chat: /chat/{restaurant_id}/{table}")

@app.on_event("shutdown")
async def shutdown():
    """Cleanup"""
    from main import shutdown_event
    await shutdown_event()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)