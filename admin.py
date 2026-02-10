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
    tab1, tab2, tab3 = st.tabs(["📅 Live Bookings", "📜 Menu Management", "⚙️ Settings"])

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

    # --- TAB 2: MENU ---
    with tab2:
        st.subheader("Add New Menu Item")
        with st.form("add_dish"):
            col1, col2 = st.columns(2)
            with col1:
                dish_name = st.text_input("Dish Name", placeholder="Margherita Pizza")
                dish_price = st.text_input("Price", placeholder="$12.50")
            with col2:
                dish_cat = st.selectbox("Category", ["Starter", "Main", "Dessert", "Drink"])
                dish_desc = st.text_area("Description", placeholder="Tomato sauce, mozzarella, basil...")
            
            submit_dish = st.form_submit_button("➕ Add to Menu")
            
            if submit_dish and dish_name:
                full_text = f"{dish_name} ({dish_cat}) - {dish_price} - {dish_desc}"
                try:
                    with st.spinner("Generating AI Embedding..."):
                        # ✅ Generates vector using text-embedding-004
                        vector = embeddings.embed_query(full_text)
                        
                        supabase.table("menu_items").insert({
                            "restaurant_id": current_rest_id,
                            "content": full_text,
                            "category": dish_cat,
                            "embedding": vector
                        }).execute()
                        st.success(f"✅ Added {dish_name} to menu!")
                except Exception as e:
                    st.error(f"Failed to add dish: {e}")

        st.divider()
        st.subheader("Current Menu Items")
        try:
            menu_res = supabase.table("menu_items").select("*").eq("restaurant_id", current_rest_id).execute()
            if menu_res.data:
                for item in menu_res.data:
                    with st.expander(f"{item.get('content', 'Unknown Item')[:50]}..."):
                        st.write(f"**Full Text:** {item.get('content')}")
                        if st.button("Delete", key=item['id']):
                            supabase.table("menu_items").delete().eq("id", item['id']).execute()
                            st.rerun()
            else:
                st.info("Menu is empty.")
        except Exception as e:
            st.error(f"Error loading menu: {e}")

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