import os
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()
api_key = os.getenv("GOOGLE_API_KEY")

if not api_key:
    print("❌ No API Key found.")
    exit()

genai.configure(api_key=api_key)

print("🔍 Listing available CHAT models...")
try:
    for m in genai.list_models():
        # We are looking for models that can "generateContent"
        if 'generateContent' in m.supported_generation_methods:
            print(f"✅ AVAILABLE CHAT MODEL: {m.name}")
except Exception as e:
    print(f"❌ Error connecting to Google: {e}")