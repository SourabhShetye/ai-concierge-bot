import streamlit as st
import pandas as pd
import requests
import os
from streamlit_autorefresh import st_autorefresh
from dotenv import load_dotenv
from supabase import create_client

st.set_page_config(page_title="Concierge Admin", layout="wide", page_icon="👨‍🍳")
load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# --- HELPER ---
def send_telegram_msg(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if token and chat_id:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            r = requests.post(url, json={"chat_id": chat_id, "text": text})
            if r.status_code != 200:
                st.error(f"Telegram Failed: {r.text}")
        except Exception as e:
            st.error(f"Telegram Connection Error: {e}")
    else:
        st.warning(f"Cannot send Telegram: Missing Token or Chat ID ({chat_id})")

# --- SIDEBAR ---
st.sidebar.title("📍 Manager")
try:
    rests = supabase.table("restaurants").select("id, name").execute().data
    options = {r['name']: r['id'] for r in rests}
    selected = st.sidebar.selectbox("Select Location", list(options.keys()))
    current_rest_id = options[selected]
except:
    st.error("Database Error")
    st.stop()

# --- MAIN ---
st.title(f"Dashboard: {selected}")
tab1, tab2, tab3 = st.tabs(["📅 Bookings", "👨‍🍳 Kitchen", "💰 Live Tables"])

# --- TAB 1: BOOKINGS ---
with tab1:
    if st.button("🔄 Refresh Bookings"): st.rerun()
    res = supabase.table("bookings").select("*").eq("restaurant_id", current_rest_id).order("booking_time", desc=True).execute()
    
    if res.data:
        for b in res.data:
            color = "red" if b['status'] == 'cancelled' else "green"
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 2, 1])
                c1.markdown(f"**{b['customer_name']}** ({b['party_size']} ppl)")
                c1.caption(f"Status: :{color}[{b['status'].upper()}]")
                c2.write(f"📅 {b['booking_time']}")
                if b['status'] != 'cancelled':
                    if c3.button("❌ Cancel", key=f"cnl_{b['id']}"):
                        supabase.table("bookings").update({"status": "cancelled"}).eq("id", b['id']).execute()
                        st.toast("Booking Cancelled")
                        st.rerun()

# --- TAB 2: KITCHEN ---
with tab2:
    st_autorefresh(interval=10000, key="kds")
    st.header("🔥 Live Kitchen")
    
    # Fetch pending orders
    orders = supabase.table("orders").select("*").eq("restaurant_id", current_rest_id).eq("status", "pending").execute().data
    
    if orders:
        for o in orders:
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                c1.markdown(f"**Table {o['table_number']}**")
                c1.write(f"📝 {o['items']}")
                
                # CANCELLATION LOGIC
                if o.get('cancellation_status') == 'requested':
                    st.error("⚠️ Cancellation Requested")
                    col_a, col_b = st.columns(2)
                    if col_a.button("✅ Approve", key=f"app_{o['id']}"):
                        supabase.table("orders").update({"status": "cancelled", "cancellation_status": "approved"}).eq("id", o['id']).execute()
                        send_telegram_msg(o.get('chat_id'), "✅ Cancellation Approved.")
                        st.rerun()
                    if col_b.button("🚫 Reject", key=f"rej_{o['id']}"):
                        supabase.table("orders").update({"cancellation_status": "rejected"}).eq("id", o['id']).execute()
                        send_telegram_msg(o.get('chat_id'), "❌ Cancellation Rejected. The kitchen has already started preparing your food.")
                        st.rerun()
                
                elif o.get('cancellation_status') == 'rejected':
                     st.caption("❌ Cancellation was rejected previously.")
                     if c2.button("✅ Ready", key=f"rdy_{o['id']}"):
                        supabase.table("orders").update({"status": "completed"}).eq("id", o['id']).execute()
                        send_telegram_msg(o.get('chat_id'), f"🍽️ Order Ready! (Table {o.get('table_number')})")
                        st.rerun()

                else:
                    if c2.button("✅ Ready", key=f"rdy_{o['id']}"):
                        supabase.table("orders").update({"status": "completed"}).eq("id", o['id']).execute()
                        send_telegram_msg(o.get('chat_id'), f"🍽️ Order Ready! (Table {o.get('table_number')})")
                        st.rerun()
    else:
        st.success("Kitchen Clear")

# --- TAB 3: TABLES ---
with tab3:
    if st.button("Refresh Tables"): st.rerun()
    active = supabase.table("orders").select("*").eq("restaurant_id", current_rest_id).neq("status", "paid").neq("status", "cancelled").execute().data
    
    tables = {}
    for o in active:
        tn = o['table_number']
        if tn not in tables: tables[tn] = {"total": 0, "items": []}
        tables[tn]["total"] += float(o['price'])
        tables[tn]["items"].append(f"{o['items']} (${o['price']})")
        
    for tn, data in tables.items():
        with st.container(border=True):
            st.subheader(f"Table {tn} - Total: ${data['total']}")
            st.text("\n".join(data['items']))
            if st.button(f"💰 Close & Pay", key=f"pay_{tn}"):
                supabase.table("orders").update({"status": "paid"}).eq("table_number", tn).eq("restaurant_id", current_rest_id).execute()
                st.rerun()