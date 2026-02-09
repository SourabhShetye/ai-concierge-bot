import os
from dotenv import load_dotenv
from supabase import create_client, Client
from langchain_google_genai import GoogleGenerativeAIEmbeddings

# 1. Load Environment Variables
load_dotenv()

url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")
google_api_key = os.getenv("GOOGLE_API_KEY")

if not url or not key or not google_api_key:
    print("❌ Error: Missing API keys in .env file")
    exit()

# 2. Initialize Clients
supabase: Client = create_client(url, key)
embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")

def seed_database():
    print("📂 Reading menu.txt...")
    
    with open("menu.txt", "r") as f:
        raw_text = f.read()

    # Split by double newlines to separate items
    menu_items = raw_text.split("\n\n")
    
    print(f"🧩 Found {len(menu_items)} menu items. Generatings vectors...")

    for item in menu_items:
        if item.strip(): # Skip empty lines
            # Generate Embedding (Vector)
            vector = embeddings.embed_query(item)
            
            # Prepare data for Supabase
            data = {
                "content": item,
                "embedding": vector
            }
            
            # Insert into DB
            response = supabase.table("menu_items").insert(data).execute()
            
            # Simple log
            first_line = item.split('\n')[0] 
            print(f"✅ Inserted: {first_line}")

    print("🎉 Database seeding complete!")

if __name__ == "__main__":
    seed_database()