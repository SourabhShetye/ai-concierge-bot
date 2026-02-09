import streamlit as st
import os
from dotenv import load_dotenv
from supabase import create_client
from langchain_google_genai import GoogleGenerativeAIEmbeddings

load_dotenv()

# Setup
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")

st.set_page_config(page_title="Concierge Admin", layout="wide")
st.title("👨‍🍳 Restaurant Manager Dashboard")

# 1. SIDEBAR: Select or Create Restaurant
st.sidebar.header("📍 Location")

# Fetch existing restaurants
existing_rests = supabase.table("restaurants").select("id, name").execute()
rest_options = {r['name']: r['id'] for r in existing_rests.data}

selected_name = st.sidebar.selectbox("Select Restaurant", list(rest_options.keys()) + ["+ Create New"])

current_rest_id = None

if selected_name == "+ Create New":
    with st.sidebar.form("create_rest"):
        new_id = st.text_input("ID (e.g. rest_001)")
        new_name = st.text_input("Name (e.g. Mario's Pizza)")
        new_wifi = st.text_input("WiFi Password")
        new_policy = st.text_area("Policies (Hours, Pets, etc.)")
        submitted = st.form_submit_button("Create")
        if submitted:
            supabase.table("restaurants").insert({
                "id": new_id, "name": new_name, "wifi_password": new_wifi, "policy_docs": new_policy
            }).execute()
            st.success("Restaurant Created! Refresh page.")
else:
    current_rest_id = rest_options[selected_name]
    st.sidebar.success(f"Managing: {selected_name}")

# 2. MAIN CONTENT
if current_rest_id:
    tab1, tab2, tab3 = st.tabs(["📜 Menu Management", "📅 Bookings", "⚙️ Settings"])

    with tab1:
        st.subheader("Add New Menu Item")
        col1, col2 = st.columns(2)
        with col1:
            dish_name = st.text_input("Dish Name")
            dish_price = st.text_input("Price")
        with col2:
            dish_cat = st.selectbox("Category", ["Starter", "Main", "Dessert", "Drink"])
            dish_desc = st.text_area("Description (Ingredients, Taste)")
        
        if st.button("Add Dish"):
            full_text = f"{dish_name} - ${dish_price} - {dish_cat} - {dish_desc}"
            # Generate Vector
            vector = embeddings.embed_query(full_text)
            # Upload
            supabase.table("menu_items").insert({
                "restaurant_id": current_rest_id,
                "content": full_text,
                "category": dish_cat,
                "embedding": vector
            }).execute()
            st.success(f"Added {dish_name} to database!")

        st.divider()
        st.subheader("Current Menu")
        menu_data = supabase.table("menu_items").select("*").eq("restaurant_id", current_rest_id).execute()
        for item in menu_data.data:
            st.text(item['content'])

    with tab2:
        st.subheader("Live Reservations")
        bookings = supabase.table("bookings").select("*").eq("restaurant_id", current_rest_id).execute()
        st.dataframe(bookings.data)