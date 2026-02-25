"""
QR Code Generator for Restaurant Tables
Generates QR codes that link to web chat interface
"""

import qrcode
import os
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def generate_table_qr(restaurant_id: str, table_number: int, output_dir: str = "qr_codes"):
    """Generate QR code for a specific table"""
    
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    # Get restaurant name
    try:
        rest = supabase.table("restaurants").select("name").eq("id", restaurant_id).limit(1).execute()
        restaurant_name = rest.data[0]["name"] if rest.data else "Restaurant"
    except Exception:
        restaurant_name = "Restaurant"
    
    # Generate URL
    chat_url = f"https://yourdomain.com/chat/{restaurant_id}/{table_number}"
    
    # Create QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(chat_url)
    qr.make(fit=True)
    
    # Create image
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Save
    filename = f"{output_dir}/{restaurant_name.replace(' ', '_')}_Table_{table_number}.png"
    img.save(filename)
    
    print(f"✅ Generated QR code: {filename}")
    print(f"   URL: {chat_url}")
    
    return filename

def generate_all_tables(restaurant_id: str, num_tables: int):
    """Generate QR codes for all tables in a restaurant"""
    print(f"Generating QR codes for {num_tables} tables...")
    
    for table_num in range(1, num_tables + 1):
        generate_table_qr(restaurant_id, table_num)
    
    print(f"\n✅ Generated {num_tables} QR codes!")

if __name__ == "__main__":
    # Example usage
    RESTAURANT_ID = "rest_1"  # Replace with your restaurant ID
    NUM_TABLES = 20
    
    generate_all_tables(RESTAURANT_ID, NUM_TABLES)