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


def generate_table_qr(restaurant_id: str, table_number: int, base_url: str, output_dir: str = "qr_codes"):
    """
    Generate QR code for a specific table
    
    Args:
        restaurant_id: Restaurant ID (e.g., 'rest_1')
        table_number: Table number (e.g., 5)
        base_url: Your deployment URL (e.g., 'https://your-app.onrender.com')
        output_dir: Output directory for QR codes
    
    Returns:
        Path to generated QR code file
    """
    # Create output directory
    os.makedirs(output_dir, exist_ok=True)
    
    # Get restaurant name from database
    try:
        rest = supabase.table("restaurants").select("name").eq("id", restaurant_id).limit(1).execute()
        restaurant_name = rest.data[0]["name"] if rest.data else "Restaurant"
    except Exception as ex:
        print(f"[WARN] Could not fetch restaurant name: {ex}")
        restaurant_name = "Restaurant"
    
    # Generate chat URL
    chat_url = f"{base_url}/chat/{restaurant_id}/{table_number}"
    
    # Create QR code with high error correction
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
    
    # Save with descriptive filename
    filename = f"{output_dir}/{restaurant_name.replace(' ', '_')}_Table_{table_number}.png"
    img.save(filename)
    
    print(f"✅ Generated: {filename}")
    print(f"   URL: {chat_url}")
    
    return filename


def generate_all_tables(restaurant_id: str, num_tables: int, base_url: str):
    """
    Generate QR codes for all tables in a restaurant
    
    Args:
        restaurant_id: Restaurant ID
        num_tables: Total number of tables
        base_url: Your deployment URL
    """
    print(f"🎨 Generating QR codes for {num_tables} tables...\n")
    
    for table_num in range(1, num_tables + 1):
        generate_table_qr(restaurant_id, table_num, base_url)
    
    print(f"\n✅ Generated {num_tables} QR codes!")
    print(f"📁 Files saved in: qr_codes/")
    print(f"\n📋 Next steps:")
    print(f"   1. Print the QR codes (300 DPI recommended)")
    print(f"   2. Laminate for durability")
    print(f"   3. Place on tables with stands or adhesive")
    print(f"   4. Test by scanning with your phone")


def generate_for_multiple_restaurants():
    """Interactive mode for multiple restaurants"""
    print("🏢 Multiple Restaurant QR Code Generator\n")
    
    # Get all restaurants from database
    try:
        restaurants = supabase.table("restaurants").select("id,name").execute()
        
        if not restaurants.data:
            print("❌ No restaurants found in database.")
            return
        
        print("📍 Available restaurants:")
        for i, rest in enumerate(restaurants.data, 1):
            print(f"   {i}. {rest['name']} (ID: {rest['id']})")
        
        print("\n")
        
        # Get base URL
        base_url = input("Enter your deployment URL (e.g., https://your-app.onrender.com): ").strip()
        
        if not base_url:
            print("❌ Base URL required.")
            return
        
        # Generate for each restaurant
        for rest in restaurants.data:
            num_tables = int(input(f"\nHow many tables for {rest['name']}? ").strip())
            print("")
            generate_all_tables(rest['id'], num_tables, base_url)
        
        print("\n🎉 All QR codes generated successfully!")
        
    except Exception as ex:
        print(f"❌ Error: {ex}")


if __name__ == "__main__":
    print("=" * 70)
    print("🍽️  Restaurant QR Code Generator")
    print("=" * 70)
    print("\nChoose an option:")
    print("  1. Generate for single restaurant")
    print("  2. Generate for all restaurants")
    print("")
    
    choice = input("Enter choice (1 or 2): ").strip()
    
    if choice == "1":
        print("\n📋 Single Restaurant Mode\n")
        restaurant_id = input("Enter restaurant ID (e.g., rest_1): ").strip()
        num_tables = int(input("Enter number of tables: ").strip())
        base_url = input("Enter your deployment URL (e.g., https://your-app.onrender.com): ").strip()
        
        if restaurant_id and num_tables and base_url:
            generate_all_tables(restaurant_id, num_tables, base_url)
        else:
            print("❌ All fields required.")
    
    elif choice == "2":
        generate_for_multiple_restaurants()
    
    else:
        print("❌ Invalid choice.")