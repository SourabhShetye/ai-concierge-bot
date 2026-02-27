# 🍽️ Restaurant AI Concierge - Production Ready

Complete AI-powered restaurant management system with Telegram bot + Web chat interface.

## ✨ Features

### Customer Features
- 🤖 **Telegram Bot** - Order via Telegram
- 🌐 **Web Chat** - QR code → instant web chat
- 📋 **Smart Menu** - Fuzzy matching, sold-out enforcement
- 🔐 **PIN Authentication** - Secure returning customer login
- 👑 **VIP Rewards** - Milestone rewards (5th, 10th visit)
- ⚠️ **Allergy Detection** - Automatic warnings
- 🎤 **Voice Orders** - Groq Whisper transcription
- 📅 **Smart Booking** - Bin-packing table allocation
- 💬 **Multi-language** - Auto-detect language support

### Restaurant Features
- 👨‍🍳 **Kitchen Display** - Real-time order queue
- 💰 **Live Billing** - Table management
- 🍽️ **Menu Manager** - Add/edit/sold-out toggle
- 👥 **CRM Insights** - Visit tracking, tags
- 📊 **Admin Dashboard** - Role-based access
- 🪑 **Table Inventory** - Smart availability
- 📈 **Analytics** - Customer insights

## 🚀 Quick Start

### 1. Local Development
```bash
# Clone repository
git clone https://github.com/yourusername/restaurant-ai.git
cd restaurant-ai

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your credentials

# Run main server
python app/main.py

# Run admin dashboard (separate terminal)
streamlit run admin.py
```

### 2. Deploy to Render

#### Method A: Using Dashboard

1. Go to https://dashboard.render.com
2. Click "New +" → "Web Service"
3. Connect your GitHub repo
4. Render auto-detects `render.yaml`
5. Add environment variables:
   - `TELEGRAM_BOT_TOKEN`
   - `SUPABASE_URL`
   - `SUPABASE_KEY`
   - `GROQ_API_KEY`
6. Click "Create Web Service"
7. Wait 5-10 minutes for deployment

#### Method B: Using CLI
```bash
# Install Render CLI
npm install -g render-cli

# Login
render login

# Deploy
render deploy
```

### 3. Configure Telegram Webhook
```bash
curl -X POST https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-app.onrender.com/webhook"}'
```

### 4. Generate QR Codes
```bash
python scripts/generate_qr.py
# Follow prompts to generate QR codes
# Print and place on tables
```

## 📁 Project Structure
```
restaurant-ai-concierge/
├── app/
│   ├── main.py              # Combined Telegram + WebSocket server
│   └── order_service.py     # Order processing logic
├── static/
│   └── chat.html            # Web chat interface
├── scripts/
│   └── generate_qr.py       # QR code generator
├── admin.py                 # Streamlit admin dashboard
├── auth_config.yaml         # Admin authentication
├── requirements.txt         # Python dependencies
├── Dockerfile              # Container config
├── render.yaml             # Render deployment config
├── .env.example            # Environment template
└── README.md               # This file
```

## 🔧 Configuration

### Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | `1234567890:ABC...` |
| `SUPABASE_URL` | Supabase project URL | `https://xyz.supabase.co` |
| `SUPABASE_KEY` | Supabase anon key | `eyJhbGci...` |
| `GROQ_API_KEY` | Groq API key | `gsk_...` |
| `PORT` | Server port | `10000` |

### Admin Users

Edit `auth_config.yaml` to add/modify admin users:
```yaml
credentials:
  usernames:
    admin:
      email: admin@restaurant.com
      password: $2b$12$...  # Generate with bcrypt
```

Generate password hash:
```python
import bcrypt
password = "your_password"
hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
print(hashed.decode('utf-8'))
```

## 📊 Database Schema

Required Supabase tables:

- `restaurants` - Restaurant details
- `menu_items` - Menu with sold-out tracking
- `orders` - Customer orders
- `bookings` - Table reservations
- `user_sessions` - Customer sessions (web + Telegram)
- `users` - Legacy user tracking
- `feedback` - Customer feedback
- `restaurant_policies` - AI context
- `tables_inventory` - Table capacity

## 🎯 Usage

### For Customers (Telegram)

1. Search for your bot: `@YourBotName`
2. Send `/start rest_id=rest_1`
3. Enter name + create PIN
4. Order food or book table

### For Customers (Web Chat)

1. Scan QR code on table
2. Opens chat in browser
3. Enter name + create PIN
4. Order instantly

### For Staff (Admin Dashboard)

1. Go to `https://your-app.onrender.com` (if deployed separately)
2. Login with credentials
3. Access Kitchen Display, Billing, Menu Manager

## 🔒 Security

- ✅ PIN authentication (bcrypt hashed)
- ✅ Rate limiting (60 req/min)
- ✅ Input validation
- ✅ CORS configured
- ✅ Session-based tracking
- ✅ Role-based access control

## 🐛 Troubleshooting

### WebSocket Connection Failed
```javascript
// Check browser console for errors
// Verify BASE_URL in .env matches deployment URL
```

### Telegram Webhook Not Working
```bash
# Check webhook status
curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo

# Delete webhook and reset
curl https://api.telegram.org/bot<TOKEN>/deleteWebhook
```

### Database Connection Error
```python
# Verify Supabase credentials
# Check if tables exist
# Ensure anon key has correct permissions
```

## 📈 Monitoring

- **Health Check**: `https://your-app.onrender.com/`
- **Ping**: `https://your-app.onrender.com/ping`
- **Logs**: Check Render dashboard logs

## 🔄 Updates
```bash
# Pull latest changes
git pull origin main

# Update dependencies
pip install -r requirements.txt --upgrade

# Restart services
# Render auto-deploys on git push
```

## 📝 License

MIT License - See LICENSE file

## 🤝 Support

- 📧 Email: support@yourrestaurant.com
- 💬 Discord: [Your Server]
- 🐛 Issues: [GitHub Issues]

## 🎉 Credits

Built with:
- FastAPI - Web framework
- Telegram Bot API - Messaging
- Supabase - Database
- Groq - AI/LLM
- Streamlit - Admin dashboard
- Render - Hosting

---

Made with ❤️ for restaurants worldwide