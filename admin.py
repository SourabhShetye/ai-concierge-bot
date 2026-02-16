import streamlit as st
import pandas as pd
import requests
import os
from streamlit_autorefresh import st_autorefresh
from dotenv import load_dotenv
from supabase import create_client

st.set_page_config(page_title="Concierge Admin", layout="wide", page_icon="👨‍🍳")

# Refresh the entire dashboard every 5000ms (5 seconds)
st_autorefresh(interval=5000, key="global_refresh")

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# --- HELPER ---
def send_telegram_msg(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if token and chat_id:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            requests.post(url, json={"chat_id": chat_id, "text": text})
        except Exception as e:
            st.error(f"Telegram Error: {e}")

# --- SIDEBAR ---
st.sidebar.title("📍 Manager")
try:
    rests = supabase.table("restaurants").select("id, name").execute().data
    options = {r['name']: r['id'] for r in rests}
    selected = st.sidebar.selectbox("Select Location", list(options.keys()))
    current_rest_id = options[selected]
except:
    st.error("Database Connection Error")
    st.stop()

# --- MAIN ---
st.title(f"Dashboard: {selected}")
tab1, tab2, tab3 = st.tabs(["📅 Bookings", "👨‍🍳 Kitchen", "💰 Live Tables"])

# --- TAB 1: BOOKINGS (Fixed: Bulk Actions) ---
with tab1:
    col_a, col_b = st.columns([1, 4])
    # --- NEW: PURGE BUTTON ---
    if col_a.button("🗑️ Purge Cancelled"):
        supabase.table("bookings").delete().eq("status", "cancelled").eq("restaurant_id", current_rest_id).execute()
        st.toast("Cancelled bookings permanently deleted.")
        st.rerun()
    if col_a.button("🔄 Refresh"): st.rerun()
    
    # Fetch Data
    res = supabase.table("bookings").select("*").eq("restaurant_id", current_rest_id).order("booking_time", desc=True).execute()
    bookings = res.data
    
    if bookings:
        # Bulk Action Container
        selected_ids = []
        
        # Header for Bulk Actions
        with st.expander("🗑️ Bulk Actions", expanded=True):
            if st.button("❌ Cancel Selected Bookings"):
                # We need to collect IDs from the session state or the checkboxes below
                # Streamlit reruns on every interaction, so we use a form or session state.
                # simpler approach: We iterate the checkboxes that were just rendered (requires session state)
                # OR: We just show the checkboxes and the user clicks the button AFTER.
                pass # Logic handled below inside the loop? No, needs to be outside.
                # Actually, the standard Streamlit way without forms is tricky.
                # Let's use a Form for the list to allow "Select All" behavior logic.
                st.info("Select bookings below and click 'Process Bulk Cancel'")

        with st.form("bulk_cancel_form"):
            st.write("### Select Bookings to Cancel")
            
            # Create a dataframe-like toggle list
            for b in bookings:
                c1, c2, c3, c4 = st.columns([1, 3, 2, 2])
                # Checkbox for selection
                is_selected = c1.checkbox(f"Select", key=f"chk_{b['id']}", label_visibility="collapsed")
                if is_selected:
                    selected_ids.append(b['id'])
                
                # Info
                status_color = "red" if b['status'] == 'cancelled' else "green"
                c2.markdown(f"**{b['customer_name']}** ({b['party_size']} ppl)")
                c3.write(f"{b['booking_time']}")
                c4.markdown(f":{status_color}[{b['status']}]")
                st.divider()

            if st.form_submit_button("🗑️ Process Bulk Cancel"):
                if selected_ids:
                    for bid in selected_ids:
                        supabase.table("bookings").update({"status": "cancelled"}).eq("id", bid).execute()
                    st.success(f"Cancelled {len(selected_ids)} bookings.")
                    st.rerun()
                else:
                    st.warning("No bookings selected.")
    else:
        st.info("No bookings found.")

# --- TAB 2: KITCHEN ---
with tab2:
    st_autorefresh(interval=10000, key="kds")
    st.header("🔥 Live Kitchen")
    
    orders = supabase.table("orders").select("*").eq("restaurant_id", current_rest_id).eq("status", "pending").execute().data
    
    if orders:
        for o in orders:
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                c1.markdown(f"**Table {o['table_number']}**")
                c1.write(f"📝 {o['items']}")
                
                if o.get('cancellation_status') == 'requested':
                    st.error("⚠️ Cancellation Requested")
                    col_a, col_b = st.columns(2)
                    if col_a.button("Approve", key=f"app_{o['id']}"):
                        supabase.table("orders").update({"status": "cancelled", "cancellation_status": "approved"}).eq("id", o['id']).execute()
                        send_telegram_msg(o.get('chat_id'), "✅ Cancellation Approved.")
                        st.rerun()
                    if col_b.button("Reject", key=f"rej_{o['id']}"):
                         supabase.table("orders").update({"cancellation_status": "rejected"}).eq("id", o['id']).execute()
                         send_telegram_msg(o.get('chat_id'), "❌ Cancellation Rejected. Kitchen is preparing the food.")
                         st.rerun()
                elif c2.button("✅ Ready", key=f"rdy_{o['id']}"):
                    supabase.table("orders").update({"status": "completed"}).eq("id", o['id']).execute()
                    send_telegram_msg(o.get('chat_id'), f"🍽️ Order Ready! (Table {o.get('table_number')})")
                    st.rerun()
    else:
        st.success("Kitchen Clear")

# --- TAB 3: TABLES (Fixed: Feedback Request) ---
with tab3:
    if st.button("Refresh Tables"): st.rerun()
    active = supabase.table("orders").select("*").eq("restaurant_id", current_rest_id).neq("status", "paid").neq("status", "cancelled").execute().data
    
    tables = {}
    for o in active:
        tn = o['table_number']
        if tn not in tables: 
            tables[tn] = {"total": 0, "items": [], "dish_names": set(), "chat_id": o.get("chat_id")}
        
        tables[tn]["total"] += float(o['price'])
        tables[tn]["items"].append(f"{o['items']} (${o['price']})")
        # Extract dish names for rating
        for item in o['items'].split(','):
            clean_name = item.split('(')[0].strip() # Remove price/notes
            tables[tn]["dish_names"].add(clean_name)
            
    for tn, data in tables.items():
        with st.container(border=True):
            st.subheader(f"Table {tn} - Total: ${data['total']}")
            st.text("\n".join(data['items']))
            
            if st.button(f"💰 Close & Pay", key=f"pay_{tn}"):
                # 1. Update DB
                supabase.table("orders").update({"status": "paid"}).eq("table_number", tn).eq("restaurant_id", current_rest_id).execute()
                
                # 2. Construct Feedback Message
                dishes_list = "\n".join([f"• {d}" for d in data['dish_names']])
                feedback_msg = (
                    f"✅ **Payment Received!**\n"
                    f"Thank you for visiting. We hope to see you again soon!\n\n"
                    f"⭐ **Please rate your experience:**\n"
                    f"Reply with a rating (1-5) for:\n{dishes_list}\n\n"
                    f"And your **Overall Restaurant Rating**."
                )
                
                # 3. Send
                if data['chat_id']:
                    send_telegram_msg(data['chat_id'], feedback_msg)
                
                st.success(f"Table {tn} Closed & Feedback Request Sent!")
                st.rerun()