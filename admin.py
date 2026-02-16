import streamlit as st
import pandas as pd
import requests
import os
from streamlit_autorefresh import st_autorefresh
from dotenv import load_dotenv
from supabase import create_client

st.set_page_config(page_title="Concierge Admin", layout="wide", page_icon="👨‍🍳")
load_dotenv()

# Setup Supabase
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# Sidebar
st.sidebar.title("📍 Manager")
rests = supabase.table("restaurants").select("id, name").execute().data
options = {r['name']: r['id'] for r in rests}
selected = st.sidebar.selectbox("Select Location", options.keys())
current_rest_id = options[selected]

# Main Dashboard
if current_rest_id:
    st.title(f"Dashboard: {selected}")
    
    # TABS
    tab1, tab2, tab3 = st.tabs(["📅 Bookings", "👨‍🍳 Kitchen", "💰 Live Tables"])

    # --- TAB 1: BOOKINGS (Fixed with Cancel Button) ---
    with tab1:
        st.subheader("Reservations Management")
        if st.button("🔄 Refresh"): st.rerun()

        # Fetch bookings
        res = supabase.table("bookings").select("*").eq("restaurant_id", current_rest_id).order("booking_time", desc=True).execute()
        
        if res.data:
            for b in res.data:
                # Color code based on status
                status_color = "red" if b['status'] == 'cancelled' else "green"
                
                with st.container(border=True):
                    c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
                    
                    c1.markdown(f"**{b['customer_name']}** ({b['party_size']} ppl)")
                    c1.caption(f"Status: :{status_color}[{b['status'].upper()}]")
                    
                    # Time formatting
                    time_str = pd.to_datetime(b['booking_time']).strftime("%Y-%m-%d %H:%M")
                    c2.write(f"📅 {time_str}")

                    # CANCEL BUTTON
                    if b['status'] != 'cancelled':
                        if c4.button("❌ Cancel", key=f"cnl_{b['id']}"):
                            supabase.table("bookings").update({"status": "cancelled"}).eq("id", b['id']).execute()
                            st.toast(f"Booking for {b['customer_name']} cancelled.")
                            st.rerun()
                    else:
                        c4.write("🚫 Void")
        else:
            st.info("No bookings found.")

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
                    
                    if o.get('cancellation_status') == 'requested':
                        st.error("⚠️ Customer requested cancellation!")
                        if st.button("Approve Cancel", key=f"app_{o['id']}"):
                            supabase.table("orders").update({"status": "cancelled", "cancellation_status": "approved"}).eq("id", o['id']).execute()
                            st.rerun()
                    
                    elif c2.button("✅ Ready", key=f"rdy_{o['id']}"):
                        supabase.table("orders").update({"status": "completed"}).eq("id", o['id']).execute()
                        st.rerun()
        else:
            st.success("Kitchen is all clear!")

    # --- TAB 3: LIVE TABLES ---
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