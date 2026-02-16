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

    # --- TAB 1: BOOKINGS (Interactive) ---
with tab1:
    st.subheader("Manage Reservations")
    
    # 1. Fetch Data
    res = supabase.table("bookings").select("*").eq("restaurant_id", current_rest_id).order("booking_time", desc=True).execute()
    
    if res.data:
        # Convert to Pandas DataFrame
        df = pd.DataFrame(res.data)
        
        # 2. Add 'Select' Checkbox Column (Default is False/Unchecked)
        df.insert(0, "Select", False)
        
        # 3. Format Time for Display (Optional: Add +4 hours for Dubai)
        df['booking_time'] = pd.to_datetime(df['booking_time'])
        df['Display Time'] = df['booking_time'] + pd.Timedelta(hours=4)
        
        # 4. Interactive Editor
        # We hide the 'id' and raw 'booking_time' but keep them in data for logic
        edited_df = st.data_editor(
            df,
            column_config={
                "Select": st.column_config.CheckboxColumn("✅", help="Select to delete", default=False),
                "customer_name": "Customer Name",
                "Display Time": "Date & Time",
                "party_size": st.column_config.NumberColumn("Guests"),
                "status": "Status",
            },
            disabled=["customer_name", "Display Time", "party_size", "status"], # Prevent editing text, only allow checkboxes
            hide_index=True,
            column_order=("Select", "customer_name", "Display Time", "party_size", "status"), # Hides ID column visually
            key="booking_editor"
        )

        # 5. Action Buttons
        col1, col2 = st.columns([1, 4])
        
        # BUTTON A: Delete Only Selected
        if col1.button("🗑️ Delete Selected"):
            # Filter rows where 'Select' is True
            to_delete = edited_df[edited_df["Select"] == True]
            
            if not to_delete.empty:
                # Get the IDs of selected rows
                ids_to_remove = to_delete["id"].tolist()
                
                # Loop through and delete (Supabase doesn't always support bulk delete easily in client libs)
                for booking_id in ids_to_remove:
                    supabase.table("bookings").delete().eq("id", booking_id).execute()
                
                st.success(f"✅ Deleted {len(ids_to_remove)} bookings.")
                st.rerun()
            else:
                st.warning("⚠️ No bookings selected.")

        # BUTTON B: Clear All
        if col2.button("⚠️ Clear ALL Bookings", type="primary"):
            # Delete everything for this restaurant
            supabase.table("bookings").delete().eq("restaurant_id", current_rest_id).execute()
            st.toast("🔥 All bookings wiped!")
            st.rerun()

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