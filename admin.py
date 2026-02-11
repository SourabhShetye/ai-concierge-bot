import streamlit as st
import pandas as pd
import requests
import time
import os
from streamlit_autorefresh import st_autorefresh
from dotenv import load_dotenv
from supabase import create_client

# 1. SETUP & CONFIG
st.set_page_config(page_title="Concierge Admin", layout="wide", page_icon="👨‍🍳")
load_dotenv()

# Verify API Keys exist
if not os.getenv("SUPABASE_URL") or not os.getenv("GOOGLE_API_KEY"):
    st.error("❌ Missing API Keys. Please check .env file.")
    st.stop()

# Initialize Clients
try:
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
except Exception as e:
    st.error(f"Connection Error: {e}")
    st.stop()

st.title("👨‍🍳 Restaurant Manager Dashboard")

# --- HELPER FUNCTIONS ---
def send_telegram_msg(chat_id, text):
    """Sends a message back to the user via Telegram Bot API"""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": int(chat_id), "text": text}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Failed to send TG message: {e}")

# 2. SIDEBAR: SELECT RESTAURANT
st.sidebar.header("📍 Location Manager")

# Fetch restaurants
try:
    response = supabase.table("restaurants").select("id, name").execute()
    existing_rests = response.data
    rest_options = {r['name']: r['id'] for r in existing_rests}
except Exception as e:
    st.sidebar.error(f"DB Error: {e}")
    existing_rests = []
    rest_options = {}

selected_name = st.sidebar.selectbox(
    "Select Restaurant", 
    ["Select..."] + list(rest_options.keys()) + ["+ Create New"]
)

current_rest_id = None

# Logic to Create New Restaurant
if selected_name == "+ Create New":
    st.sidebar.markdown("---")
    st.sidebar.subheader("New Location Details")
    with st.sidebar.form("create_rest"):
        new_id = st.text_input("Restaurant ID", placeholder="e.g. pizza_downtown")
        new_name = st.text_input("Display Name", placeholder="e.g. Mario's Pizza")
        new_wifi = st.text_input("WiFi Password")
        new_policy = st.text_area("Policies")
        
        submitted = st.form_submit_button("🚀 Launch Restaurant")
        if submitted:
            if new_id and new_name:
                try:
                    supabase.table("restaurants").insert({
                        "id": new_id, 
                        "name": new_name, 
                        "wifi_password": new_wifi, 
                        "policy_docs": new_policy
                    }).execute()
                    st.sidebar.success("✅ Created! Refresh the page.")
                except Exception as e:
                    st.sidebar.error(f"Error: {e}")
            else:
                st.sidebar.warning("ID and Name are required.")

elif selected_name != "Select...":
    current_rest_id = rest_options[selected_name]
    st.sidebar.success(f"🟢 Online: {selected_name}")
    st.sidebar.info(f"ID: `{current_rest_id}`")

# 3. MAIN DASHBOARD
if current_rest_id:
    # ✅ UPDATED: Added Tab 3 for Live Tables
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["📅 Bookings", "👨‍🍳 Kitchen", "💰 Live Tables", "📜 Menu", "⚙️ Settings"])

    # --- TAB 1: BOOKINGS ---
    with tab1:
        st.subheader("Upcoming Reservations")
        if st.button("🔄 Refresh Bookings"):
            st.rerun()
            
        try:
            res = supabase.table("bookings").select("*").eq("restaurant_id", current_rest_id).order("created_at", desc=True).execute()
            if res.data:
                df = pd.DataFrame(res.data)
                display_cols = ['customer_name', 'booking_time', 'party_size', 'status']
                available_cols = [c for c in display_cols if c in df.columns]
                st.dataframe(df[available_cols], use_container_width=True)
            else:
                st.info("No bookings found yet.")
        except Exception as e:
            st.error(f"Could not load bookings: {e}")

    # --- TAB 2: KITCHEN DISPLAY SYSTEM (KDS) ---
    with tab2:
        st.header("🔥 Live Kitchen & Service")
        st_autorefresh(interval=10000, limit=None, key="kitchen_refresh")

        col_a, col_b = st.columns([1, 4])
        with col_a:
            if st.button("🔄 Refresh Now"):
                st.rerun()

        # SECTION A: SERVICE REQUESTS
        st.subheader("🔔 Service Bells")
        try:
            reqs = supabase.table("service_requests").select("*").eq("restaurant_id", current_rest_id).eq("status", "pending").execute()
            if reqs.data:
                for req in reqs.data:
                    with st.container():
                        c1, c2 = st.columns([4, 1])
                        c1.warning(f"🚨 **Table {req.get('table_number', '?')}** requests: **{req['request_type'].upper()}**")
                        if c2.button("✅ Done", key=f"srv_{req['id']}"):
                            supabase.table("service_requests").update({"status": "completed"}).eq("id", req['id']).execute()
                            st.toast("Service request cleared!")
                            time.sleep(0.5)
                            st.rerun()
            else:
                st.caption("No active service calls.")
        except Exception as e:
            st.error(f"Error loading service requests: {e}")

        st.divider()

        # SECTION B: FOOD ORDERS
        st.subheader("🥣 Active Orders")
        try:
            orders = supabase.table("orders").select("*").eq("restaurant_id", current_rest_id).eq("status", "pending").order("created_at", desc=False).execute()
            if not orders.data:
                st.success("Kitchen is clear! No pending orders.")
            else:
                for order in orders.data:
                    is_cancel_requested = order.get('cancellation_status') == 'requested'
                    with st.container(border=True):
                        c1, c2, c3 = st.columns([2, 1, 1])
                        with c1:
                            st.subheader(f"Table {order.get('table_number', 'Unknown')}")
                            st.markdown(f"**{order.get('items', 'Unknown Items')}**")
                            st.caption(f"Customer: {order.get('customer_name', 'Guest')} | ID: #{order['id']}")
                            if is_cancel_requested:
                                st.error("🚨 CUSTOMER WANTS TO CANCEL!")
                        
                        with c2:
                            if is_cancel_requested:
                                if st.button("👍 Accept", key=f"acc_{order['id']}"):
                                    supabase.table("orders").update({"status": "cancelled", "cancellation_status": "approved"}).eq("id", order['id']).execute()
                                    send_telegram_msg(order.get('chat_id'), f"✅ **Cancellation Approved**\nOrder #{order['id']} has been cancelled.")
                                    st.success("Cancelled & Refunded.")
                                    st.rerun()
                                if st.button("👎 Reject", key=f"rej_{order['id']}"):
                                    supabase.table("orders").update({"cancellation_status": "rejected"}).eq("id", order['id']).execute()
                                    send_telegram_msg(order.get('chat_id'), f"❌ **Cancellation Rejected**\nThe chef has already started cooking.")
                                    st.warning("Cancellation rejected.")
                                    st.rerun()
                        
                        with c3:
                            if st.button("✅ Ready", key=f"rdy_{order['id']}"):
                                supabase.table("orders").update({"status": "completed"}).eq("id", order['id']).execute()
                                if order.get('chat_id'):
                                    send_telegram_msg(order['chat_id'], f"🍽️ **Order Ready!**\nYour food for Table {order.get('table_number')} is coming out now.")
                                st.success(f"Order #{order['id']} Ready!")
                                time.sleep(0.5)
                                st.rerun()
        except Exception as e:
            st.error(f"Error fetching orders: {e}")

    # --- TAB 3: LIVE TABLES & BILLING (NEW) ---
    with tab3:
        st.header("🪑 Active Tables & Billing")
        
        if st.button("🔄 Refresh Tables"):
            st.rerun()
            
        # 1. Fetch active orders (status is NOT 'paid')
        # This includes 'pending' and 'completed' (served but not paid)
        try:
            active_orders = supabase.table("orders").select("*")\
                .eq("restaurant_id", current_rest_id)\
                .neq("status", "paid")\
                .execute()
            
            if not active_orders.data:
                st.info("No active tables right now.")
            else:
                # 2. Group Orders by Table Number
                tables = {}
                for order in active_orders.data:
                    # Skip cancelled items from the bill
                    if order.get('status') == 'cancelled':
                        continue
                        
                    t_num = order.get('table_number', 'Unknown')
                    if t_num not in tables:
                        tables[t_num] = {
                            "name": order.get('customer_name', 'Guest'), 
                            "orders": [], 
                            "total": 0.0, 
                            "chat_id": order.get('chat_id')
                        }
                    
                    tables[t_num]["orders"].append(order)
                    # Safely handle price calculation
                    price = float(order.get('price', 0)) if order.get('price') else 0.0
                    tables[t_num]["total"] += price

                # 3. Display Cards
                cols = st.columns(3)
                for i, (t_num, data) in enumerate(tables.items()):
                    with cols[i % 3]:
                        with st.container(border=True):
                            st.subheader(f"Table {t_num}")
                            st.caption(f"Guest: {data['name']}")
                            st.divider()
                            
                            # List Items
                            for o in data['orders']:
                                status_icon = "🍳" if o['status'] == 'pending' else "🍽️"
                                price_disp = f"${o.get('price', 0)}"
                                st.text(f"{status_icon} {o.get('items', '')[:25]}... ({price_disp})")
                            
                            st.divider()
                            st.markdown(f"### Total: ${round(data['total'], 2)}")
                            
                            # BILLING ACTIONS
                            c1, c2 = st.columns(2)
                            with c1:
                                if st.button("🖨️ Send Bill", key=f"bill_{t_num}"):
                                    msg = f"🧾 **BILL FOR TABLE {t_num}**\n\nTotal Due: **${round(data['total'], 2)}**\n\nPlease pay at the counter or wait for the staff."
                                    send_telegram_msg(data['chat_id'], msg)
                                    st.toast("Bill Sent to User!")
                            
                            with c2:
                                if st.button("💰 Mark Paid", key=f"pay_{t_num}"):
                                    # Mark all orders as paid
                                    for o in data['orders']:
                                        supabase.table("orders").update({"status": "paid"}).eq("id", o['id']).execute()
                                    
                                    # Send Thank You
                                    send_telegram_msg(data['chat_id'], "✅ **Payment Received!**\n\nThank you for dining with us! Please rate us ⭐⭐⭐⭐⭐")
                                    st.success("Table Closed!")
                                    time.sleep(1)
                                    st.rerun()
        except Exception as e:
            st.error(f"Error loading tables: {e}")

    # --- TAB 4: MENU MANAGEMENT ---
    with tab4:
        st.subheader("Add New Menu Item")
        with st.form("add_dish"):
            col1, col2 = st.columns(2)
            with col1:
                dish_name = st.text_input("Dish Name", placeholder="Margherita Pizza")
                dish_price = st.text_input("Price", placeholder="$12.50")
            with col2:
                dish_cat = st.selectbox("Category", ["Starter", "Main", "Dessert", "Drink"])
                dish_desc = st.text_area("Description", placeholder="Tomato sauce, mozzarella...")
            
            submit_dish = st.form_submit_button("➕ Add to Menu")
            
            if submit_dish and dish_name:
                full_text = f"{dish_name} ({dish_cat}) - {dish_price} - {dish_desc}"
                try:
                    supabase.table("menu_items").insert({
                        "restaurant_id": current_rest_id,
                        "content": full_text,
                        "category": dish_cat,
                    }).execute()
                    st.success(f"✅ Added {dish_name} to menu!")
                except Exception as e:
                    st.error(f"Failed to add dish: {e}")

        st.divider()
        st.subheader("Current Menu")
        try:
            menu_res = supabase.table("menu_items").select("*").eq("restaurant_id", current_rest_id).execute()
            if menu_res.data:
                for item in menu_res.data:
                    with st.expander(f"{item.get('content', 'Unknown Item')[:50]}..."):
                        st.write(f"**Full Text:** {item.get('content')}")
                        if st.button("Delete", key=f"del_{item['id']}"):
                            supabase.table("menu_items").delete().eq("id", item['id']).execute()
                            st.rerun()
            else:
                st.info("Menu is empty.")
        except Exception as e:
            st.error(f"Error loading menu: {e}")

    # --- TAB 5: SETTINGS ---
    with tab5:
        st.subheader("Restaurant Configuration")
        try:
            details = supabase.table("restaurants").select("*").eq("id", current_rest_id).single().execute()
            d = details.data
            st.write(f"**Name:** {d['name']}")
            st.write(f"**WiFi Password:** `{d['wifi_password']}`")
            st.text_area("Policy Docs (ReadOnly)", value=d['policy_docs'], disabled=True)
        except:
            st.warning("Could not load settings.")

else:
    st.info("👈 Please select a restaurant from the sidebar to begin.")