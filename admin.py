import streamlit as st
import pandas as pd
import os
from dotenv import load_dotenv
from supabase import create_client
from langchain_google_genai import GoogleGenerativeAIEmbeddings

# 1. SETUP & CONFIG
st.set_page_config(page_title="Concierge Admin", layout="wide", page_icon="👨‍🍳")
load_dotenv()

# Verify API Keys exist
if not os.getenv("SUPABASE_URL") or not os.getenv("GOOGLE_API_KEY"):
    st.error("❌ Missing API Keys in .env file. Please check SUPABASE_URL and GOOGLE_API_KEY.")
    st.stop()

# Initialize Clients
try:
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    # ✅ FIX: Using the modern, working embedding model
    embeddings = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
except Exception as e:
    st.error(f"Connection Error: {e}")
    st.stop()

st.title("👨‍🍳 Restaurant Manager Dashboard")

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
        new_id = st.text_input("Restaurant ID (Unique)", placeholder="e.g. pizza_downtown")
        new_name = st.text_input("Display Name", placeholder="e.g. Mario's Pizza")
        new_wifi = st.text_input("WiFi Password", placeholder="e.g. pizza123")
        new_policy = st.text_area("Policies", placeholder="Open 9am-10pm. No pets.")
        
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
    tab1, tab2, tab3, tab4 = st.tabs(["📅 Bookings", "👨‍🍳 Kitchen (Live)", "📜 Menu", "⚙️ Settings"])

    # --- TAB 1: BOOKINGS ---
    with tab1:
        st.subheader("Upcoming Reservations")
        if st.button("🔄 Refresh Bookings"):
            st.rerun()
            
        try:
            # Fetch bookings for this restaurant
            res = supabase.table("bookings").select("*").eq("restaurant_id", current_rest_id).order("created_at", desc=True).execute()
            
            if res.data:
                df = pd.DataFrame(res.data)
                
                # Clean up columns for display if they exist
                display_cols = ['customer_name', 'booking_time', 'party_size', 'status']
                # Filter to only show columns that actually exist in the DB response
                available_cols = [c for c in display_cols if c in df.columns]
                
                # Show Data Table
                st.dataframe(
                    df[available_cols].style.applymap(
                        lambda x: 'background-color: #d4edda' if x == 'confirmed' else '', subset=['status']
                    ),
                    use_container_width=True
                )
            else:
                st.info("No bookings found yet.")
                
        except Exception as e:
            st.error(f"Could not load bookings: {e}")

    # --- TAB 2: KITCHEN DISPLAY SYSTEM (KDS) ---
    with tab2:
        st.header("🔥 Live Kitchen Orders")
        
        col_a, col_b = st.columns([1, 4])
        with col_a:
            if st.button("🔄 Refresh"):
                st.rerun()
        
        # 1. Fetch PENDING orders from Supabase
        try:
            orders = supabase.table("orders").select("*")\
                .eq("restaurant_id", current_rest_id)\
                .eq("status", "pending")\
                .order("created_at", desc=False)\
                .execute()
            
            if not orders.data:
                st.success("✅ All caught up! No pending orders.")
                st.balloons()
            else:
                # 2. Display Orders as "Tickets"
                for order in orders.data:
                    # Create a "Ticket" styling container
                    with st.container():
                        st.markdown("---")
                        c1, c2, c3 = st.columns([3, 2, 1])
                        
                        with c1:
                            st.subheader(f"🥣 {order.get('items', 'Unknown Items')}")
                            st.caption(f"Order ID: #{order['id']}")
                        
                        with c2:
                            st.write(f"**Customer:** {order.get('customer_name', 'Guest')}")
                            # Calculate time elapsed if 'created_at' exists
                            if 'created_at' in order:
                                st.caption(f"Time: {order['created_at'].split('T')[1][:5]}")
                        
                        with c3:
                            # The "Done" Button
                            if st.button("✅ Ready", key=f"btn_{order['id']}"):
                                # Update DB status to 'completed' or 'delivered'
                                supabase.table("orders").update({"status": "completed"}).eq("id", order['id']).execute()
                                st.toast(f"Order #{order['id']} marked complete!")
                                time.sleep(1) # Small delay for visual feedback
                                st.rerun()
                                
        except Exception as e:
            st.error(f"Error fetching orders: {e}")

    # --- TAB 3: SETTINGS ---
    with tab3:
        st.subheader("Restaurant Configuration")
        try:
            details = supabase.table("restaurants").select("*").eq("id", current_rest_id).single().execute()
            d = details.data
            st.write(f"**Name:** {d['name']}")
            st.write(f"**WiFi Password:** `{d['wifi_password']}`")
            st.text_area("Policy Docs (ReadOnly)", value=d['policy_docs'], disabled=True)
            st.info("To edit these details, please contact the Super Admin.")
        except:
            st.warning("Could not load settings.")

else:
    st.info("👈 Please select a restaurant from the sidebar to begin.")