import qrcode

restaurant_id = "your_restaurant_id"
bot_username = "your_bot_username"

for table_num in range(1, 21):  # Tables 1-20
    url = f"https://t.me/{bot_username}?start=table_{table_num}_rest_{restaurant_id}"
    
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(f"qr_table_{table_num}.png")
    print(f"Generated QR for Table {table_num}")