import streamlit as st
import pandas as pd
import requests
import os
from streamlit_autorefresh import st_autorefresh
from dotenv import load_dotenv
from supabase import create_client

st.set_page_config(page_title="Concierge Admin", layout="wide", page_icon="👨‍🍳")
load_dotenv()

# Initialize Supabase
url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")
if not url or not key:
    st.error("❌ Supabase credentials missing in .env")
    st.stop()

supabase = create_client(url, key)

# --- HELPER ---
def send_telegram_msg(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if token and chat_id:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            requests.post(url, json={"chat_id": chat_id, "text": text})
        except Exception as e:
            print(f"Telegram Error: {e}")

# --- SIDEBAR ---
st.sidebar.title("📍 Manager")
try:
    rests = supabase.table("restaurants").select("id, name").execute().data
    if not rests:
        st.sidebar.warning("No restaurants found in DB.")
        options = {}
        current_rest_id = None
    else:
        options = {r['name']: r['id'] for r in rests}
        selected = st.sidebar.selectbox("Select Location", list(options.keys()))
        current_rest_id = options[selected]
except Exception as e:
    st.sidebar.error(f"DB Error: {e}")
    current_rest_id = None

# --- MAIN ---
if current_rest_id:
    st.title(f"Dashboard: {selected}")
    tab1, tab2, tab3 = st.tabs(["📅 Bookings", "👨‍🍳 Kitchen", "💰 Live Tables"])

    # --- TAB 1: BOOKINGS ---
    with tab1:
        st.subheader("Upcoming Reservations")
        if st.button("🔄 Refresh Bookings"):
            st.rerun()
            
        try:
            res = supabase.table("bookings").select("*").eq("restaurant_id", current_rest_id).order("booking_time", desc=True).execute()
            if res.data:
                df = pd.DataFrame(res.data)
                
                # --- TIMEZONE FIX ---
                df['booking_time'] = pd.to_datetime(df['booking_time'])
                # Adjust for display only (assuming UTC in DB, +4 for Dubai)
                df['display_time'] = df['booking_time'] + pd.Timedelta(hours=4)
                
                # Reorder columns
                df = df[['customer_name', 'display_time', 'party_size', 'status']]
                df.columns = ['Name', 'Time (Dubai)', 'Guests', 'Status']
                
                st.dataframe(df, use_container_width=True)
            else:
                st.info("No bookings found.")
        except Exception as e:
            st.error(f"Could not load bookings: {e}")

    # --- TAB 2: KITCHEN ---
    with tab2:
        st_autorefresh(interval=10000, key="kds") # Refresh every 10s
        st.header("🔥 Live Kitchen")
        
        # Fetch ALL pending orders for this restaurant
        # We filter in Python to avoid Supabase NULL/Empty string filter issues
        try:
            raw_orders = supabase.table("orders").select("*").eq("restaurant_id", current_rest_id).eq("status", "pending").execute().data
            
            # Split into Cancellations and New Orders
            cancellations = [o for o in raw_orders if o.get('cancellation_status') == 'requested']
            active_orders = [o for o in raw_orders if o.get('cancellation_status') != 'requested']

            # 1. CANCELLATIONS
            if cancellations:
                st.error("🚨 CANCELLATION REQUESTS")
                for c in cancellations:
                    with st.container(border=True):
                        col1, col2 = st.columns([3, 1])
                        col1.write(f"**Table {c.get('table_number')}** wants to cancel:\n{c.get('items')}")
                        if col2.button("Approve Cancel", key=f"app_{c['id']}"):
                            supabase.table("orders").update({"status": "cancelled", "cancellation_status": "approved"}).eq("id", c['id']).execute()
                            send_telegram_msg(c.get('chat_id'), "✅ Cancellation Approved.")
                            st.rerun()

            # 2. ACTIVE ORDERS
            if active_orders:
                st.subheader("New Orders")
                for o in active_orders:
                    with st.container(border=True):
                        c1, c2 = st.columns([3, 1])
                        c1.markdown(f"### Table {o.get('table_number')}")
                        c1.markdown(f"**{o.get('items')}**")
                        c1.caption(f"Customer: {o.get('customer_name')}")
                        
                        if c2.button("✅ Ready", key=f"rdy_{o['id']}"):
                            supabase.table("orders").update({"status": "completed"}).eq("id", o['id']).execute()
                            send_telegram_msg(o.get('chat_id'), f"🍽️ Order Ready! (Table {o.get('table_number')})")
                            st.rerun()
            elif not cancellations:
                st.info("Kitchen is clear. No pending orders.")

            # 3. WAITER BELLS
            reqs = supabase.table("service_requests").select("*").eq("restaurant_id", current_rest_id).eq("status", "pending").execute().data
            if reqs:
                st.warning("🔔 SERVICE REQUESTS")
                for r in reqs:
                    if st.button(f"Resolve {r['request_type']} (Table {r['table_number']})", key=f"srv_{r['id']}"):
                        supabase.table("service_requests").update({"status": "completed"}).eq("id", r['id']).execute()
                        st.rerun()

        except Exception as e:
            st.error(f"Error fetching orders: {e}")

    # --- TAB 3: LIVE TABLES ---
    with tab3:
        if st.button("Refresh Tables"): st.rerun()
        
        try:
            # Fetch active (unpaid) orders
            active = supabase.table("orders").select("*").eq("restaurant_id", current_rest_id).neq("status", "paid").neq("status", "cancelled").execute().data
            
            tables = {}
            for o in active:
                tn = o.get('table_number', 'Unknown')
                if tn not in tables: 
                    tables[tn] = {"total": 0, "items": [], "chat_id": o.get('chat_id')}
                
                tables[tn]["total"] += float(o.get('price', 0))
                tables[tn]["items"].append(f"{o.get('items')} (${o.get('price')})")
                
            if tables:
                for tn, data in tables.items():
                    with st.container(border=True):
                        c1, c2 = st.columns([3,1])
                        c1.subheader(f"Table {tn} - Total: ${data['total']:.2f}")
                        c1.text("\n".join(data['items']))
                        if c2.button(f"💰 Close & Pay", key=f"cls_{tn}"):
                            # Mark all orders for this table as paid
                            supabase.table("orders").update({"status": "paid"}).eq("table_number", tn).eq("restaurant_id", current_rest_id).neq("status", "cancelled").execute()
                            send_telegram_msg(data['chat_id'], "✅ Payment Received. Thank you for visiting!")
                            st.rerun()
            else:
                st.info("No active tables.")
        except Exception as e:
            st.error(f"Error loading tables: {e}")
else:
    st.warning("Please select a restaurant or check database connection.")