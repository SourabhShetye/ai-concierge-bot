"""
Feedback Monitor - Real-time sentiment analysis and alerts
Run this as a background service alongside your bot
"""
import asyncio
import os
from datetime import datetime, timedelta
from supabase import create_client
from groq import AsyncGroq
from dotenv import load_dotenv

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

async def send_alert_to_manager(restaurant_id: str, table: str, feedback: str, sentiment: str, score: float):
    """Send urgent alert to manager via Telegram"""
    try:
        # Get manager's chat_id from database
        manager = supabase.table("managers").select("telegram_chat_id")\
            .eq("restaurant_id", restaurant_id).limit(1).execute()
        
        if manager.data:
            import requests
            token = os.getenv("TELEGRAM_BOT_TOKEN")
            chat_id = manager.data[0]["telegram_chat_id"]
            
            alert = (
                f"🚨 *URGENT: Negative Feedback Alert*\n\n"
                f"📍 Table: {table}\n"
                f"😟 Sentiment: {sentiment.upper()} ({score:.1f}/1.0)\n"
                f"💬 Feedback: _{feedback}_\n\n"
                f"⚡ *Action Required:* Please address this immediately!"
            )
            
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": alert, "parse_mode": "Markdown"}
            )
            print(f"[ALERT] Sent to manager for table {table}")
    except Exception as ex:
        print(f"[ALERT ERROR] {ex}")

async def monitor_feedback():
    """Continuously monitor new feedback"""
    print("[MONITOR] Feedback monitoring started")
    
    while True:
        try:
            # Get recent feedback (last 5 minutes)
            recent = supabase.table("feedback").select("*")\
                .gte("created_at", (datetime.utcnow() - timedelta(minutes=5)).isoformat())\
                .order("created_at", desc=True).execute()
            
            for fb in recent.data or []:
                # Check if already analyzed
                if fb.get("sentiment_analyzed"):
                    continue
                
                # Analyze sentiment
                sentiment, score = await analyze_sentiment(fb["ratings"])
                
                # Update database
                supabase.table("feedback").update({
                    "sentiment": sentiment,
                    "sentiment_score": score,
                    "sentiment_analyzed": True
                }).eq("id", fb["id"]).execute()
                
                # Alert manager if negative
                if sentiment == "negative" and score < 0.4:
                    # Get table info
                    pending = supabase.table("pending_feedback").select("*")\
                        .eq("chat_id", fb.get("chat_id")).limit(1).execute()
                    
                    if pending.data:
                        await send_alert_to_manager(
                            fb["restaurant_id"],
                            pending.data[0]["table_number"],
                            fb["ratings"],
                            sentiment,
                            score
                        )
                
                print(f"[FEEDBACK] Analyzed: {sentiment} ({score:.2f})")
        
        except Exception as ex:
            print(f"[MONITOR ERROR] {ex}")
        
        await asyncio.sleep(30)  # Check every 30 seconds

if __name__ == "__main__":
    asyncio.run(monitor_feedback())