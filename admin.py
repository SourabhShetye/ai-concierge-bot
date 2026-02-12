import streamlit as st
import pandas as pd
import requests
import time
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
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})

# --- SIDEBAR ---
st.sidebar.title("📍 Manager")
rests = supabase.table("restaurants").select("id, name").execute().data
options = {r['name']: r['id'] for r in rests}
selected = st.sidebar.selectbox("Select Location", options.keys())
current_rest_id = options[selected]

# --- MAIN ---
st.title(f"Dashboard: {selected}")
st.caption(f"Restaurant ID: {current_rest_id}") # DEBUG ID

tab1, tab2, tab3 = st.tabs(["📅 Bookings", "👨‍🍳 Kitchen", "💰 Live Tables"])

with tab1:
    if st.button("Refresh Bookings"): st.rerun()
    data = supabase.table("bookings").select("*").eq("restaurant_id", current_rest_id).order("booking_time", desc=True).execute().data
    if data: st.dataframe(pd.DataFrame(data)[['customer_name', 'booking_time', 'party_size']])
    else: st.info("No bookings.")

with tab2:
    st_autorefresh(interval=5000, key="kds")
    st.header("🔥 Live Kitchen")
    
    # 1. CANCELLATIONS
    cancels = supabase.table("orders").select("*").eq("restaurant_id", current_rest_id).eq("cancellation_status", "requested").execute().data
    if cancels:
        st.error("🚨 CANCELLATION REQUESTS")
        for c in cancels:
            col1, col2 = st.columns(2)
            col1.write(f"Table {c['table_number']} wants to cancel: {c['items']}")
            if col2.button("Approve", key=f"app_{c['id']}"):
                supabase.table("orders").update({"status": "cancelled", "cancellation_status": "approved"}).eq("id", c['id']).execute()
                send_telegram_msg(c['chat_id'], "✅ Cancellation Approved.")
                st.rerun()

    # 2. WAITER BELLS
    reqs = supabase.table("service_requests").select("*").eq("restaurant_id", current_rest_id).eq("status", "pending").execute().data
    if reqs:
        st.warning("🔔 SERVICE REQUESTS")
        for r in reqs:
            if st.button(f"✅ Clear {r['request_type']} (Table {r['table_number']})", key=f"srv_{r['id']}"):
                supabase.table("service_requests").update({"status": "completed"}).eq("id", r['id']).execute()
                st.rerun()

    # 3. ORDERS
    orders = supabase.table("orders").select("*").eq("restaurant_id", current_rest_id).eq("status", "pending").neq("cancellation_status", "requested").execute().data
    for o in orders:
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"**{o['items']}**")
            c1.caption(f"Table {o['table_number']} | {o['customer_name']}")
            if c2.button("Ready", key=f"rdy_{o['id']}"):
                supabase.table("orders").update({"status": "completed"}).eq("id", o['id']).execute()
                send_telegram_msg(o['chat_id'], f"🍽️ Order Ready! (Table {o['table_number']})")
                st.rerun()

with tab3:
    if st.button("Refresh Tables"): st.rerun()
    active = supabase.table("orders").select("*").eq("restaurant_id", current_rest_id).neq("status", "paid").execute().data
    
    tables = {}
    for o in active:
        tn = o['table_number']
        if tn not in tables: tables[tn] = {"total": 0, "items": [], "chat_id": o['chat_id']}
        tables[tn]["total"] += float(o['price'])
        tables[tn]["items"].append(f"{o['items']} (${o['price']})")
        
    for tn, data in tables.items():
        with st.container(border=True):
            st.subheader(f"Table {tn} - Total: ${data['total']}")
            st.text("\n".join(data['items']))
            if st.button(f"💰 Close Table {tn}", key=f"cls_{tn}"):
                supabase.table("orders").update({"status": "paid"}).eq("table_number", tn).eq("restaurant_id", current_rest_id).execute()
                send_telegram_msg(data['chat_id'], "✅ Payment Received. Thank you!")
                st.rerun()