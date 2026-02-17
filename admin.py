"""
Restaurant Admin Dashboard - Streamlit
Production-Ready with Auto-Refresh, KDS, and Live Billing
"""

import streamlit as st
import pandas as pd
import requests
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh
from dotenv import load_dotenv
from supabase import create_client

# Dubai is UTC+4 — all timestamps from Supabase are stored in UTC.
# Every display conversion must add this offset.
DUBAI_TZ  = ZoneInfo("Asia/Dubai")
UTC_PLUS4 = timedelta(hours=4)


def to_dubai(utc_dt: datetime) -> datetime:
    """
    Convert a UTC-aware or UTC-naive datetime to Dubai time (UTC+4).
    Supabase returns ISO strings in UTC; this helper centralises the conversion
    so the +4 offset is applied consistently in every tab.
    """
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(DUBAI_TZ)

# ============================================================================
# PAGE CONFIGURATION
# ============================================================================

st.set_page_config(
    page_title="Restaurant Admin Dashboard",
    layout="wide",
    page_icon="👨‍🍳",
    initial_sidebar_state="expanded"
)

# Global auto-refresh: 5 seconds
refresh_count = st_autorefresh(interval=5000, key="global_dashboard_refresh")

load_dotenv()

# ============================================================================
# DATABASE CONNECTION
# ============================================================================

try:
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
except Exception as e:
    st.error(f"❌ Database Connection Error: {e}")
    st.stop()

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def send_telegram_message(chat_id: str, text: str) -> bool:
    """
    Send a message to a Telegram user.
    Returns True if successful, False otherwise.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not token or not chat_id:
        return False
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    try:
        response = requests.post(
            url,
            json={"chat_id": chat_id, "text": text},
            timeout=5
        )
        return response.status_code == 200
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
        return False


def format_currency(amount: float) -> str:
    """Format number as currency"""
    return f"${amount:.2f}"


def get_timestamp() -> str:
    """Get current timestamp in Dubai time (UTC+4) for the dashboard footer."""
    return datetime.now(DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ============================================================================
# SIDEBAR - RESTAURANT SELECTION
# ============================================================================

st.sidebar.title("🏢 Restaurant Manager")

try:
    # Fetch all restaurants
    restaurants_response = supabase.table("restaurants")\
        .select("id, name")\
        .execute()
    
    if not restaurants_response.data:
        st.error("❌ No restaurants found in database")
        st.stop()
    
    # Create selection dropdown
    restaurant_options = {r["name"]: r["id"] for r in restaurants_response.data}
    
    selected_restaurant_name = st.sidebar.selectbox(
        "Select Location",
        list(restaurant_options.keys()),
        key="restaurant_selector"
    )
    
    current_restaurant_id = restaurant_options[selected_restaurant_name]
    
    # Display selection info
    st.sidebar.success(f"📍 {selected_restaurant_name}")
    st.sidebar.info(f"🔄 Last refresh: {get_timestamp()}")
    
except Exception as e:
    st.error(f"❌ Error loading restaurants: {e}")
    st.stop()

# ============================================================================
# MAIN DASHBOARD HEADER
# ============================================================================

st.title(f"📊 Dashboard: {selected_restaurant_name}")
st.markdown("---")

# Create tabs
tab1, tab2, tab3 = st.tabs([
    "📅 Bookings Management",
    "👨‍🍳 Kitchen Display System",
    "💰 Live Tables & Billing"
])

# ============================================================================
# TAB 1: BOOKINGS MANAGEMENT
# ============================================================================

with tab1:
    st.header("📅 Reservations & Bookings")
    
    # Action buttons row
    col_action1, col_action2, col_action3, col_action4 = st.columns(4)
    
    with col_action1:
        if st.button("🔄 Refresh Data", use_container_width=True):
            st.rerun()
    
    with col_action2:
        if st.button("🗑️ Purge Cancelled", use_container_width=True, type="secondary"):
            try:
                delete_result = supabase.table("bookings")\
                    .delete()\
                    .eq("status", "cancelled")\
                    .eq("restaurant_id", current_restaurant_id)\
                    .execute()
                
                st.toast("✅ Cancelled bookings permanently deleted", icon="🗑️")
                st.rerun()
            except Exception as e:
                st.error(f"Error deleting bookings: {e}")
    
    st.markdown("---")
    
    # Fetch bookings
    try:
        bookings_response = supabase.table("bookings")\
            .select("*")\
            .eq("restaurant_id", current_restaurant_id)\
            .order("booking_time", desc=False)\
            .execute()
        
        bookings = bookings_response.data
        
        if bookings:
            # Statistics
            total_bookings = len(bookings)
            confirmed = len([b for b in bookings if b["status"] == "confirmed"])
            cancelled = len([b for b in bookings if b["status"] == "cancelled"])
            
            stat_col1, stat_col2, stat_col3 = st.columns(3)
            stat_col1.metric("Total Bookings", total_bookings)
            stat_col2.metric("Confirmed", confirmed, delta=None)
            stat_col3.metric("Cancelled", cancelled, delta=None)
            
            st.markdown("---")
            
            # Bulk action form
            with st.form("bulk_booking_actions"):
                st.subheader("📋 Booking List")
                st.caption("Select bookings to cancel in bulk")
                
                selected_booking_ids = []
                
                # Create table-like display
                for booking in bookings:
                    col1, col2, col3, col4, col5 = st.columns([0.5, 2, 1.5, 1.5, 1])
                    
                    # Checkbox
                    is_selected = col1.checkbox(
                        "Select",
                        key=f"booking_check_{booking['id']}",
                        label_visibility="collapsed"
                    )
                    
                    if is_selected:
                        selected_booking_ids.append(booking["id"])
                    
                    # Customer name
                    col2.write(f"**{booking['customer_name']}**")
                    
                    # Party size
                    col3.write(f"👥 {booking['party_size']} guests")
                    
                    # Booking time — convert UTC → Dubai (UTC+4) before display
                    try:
                        booking_dt_utc = datetime.fromisoformat(
                            booking['booking_time'].replace('Z', '+00:00')
                        )
                        booking_dt_dubai = to_dubai(booking_dt_utc)
                        time_str = booking_dt_dubai.strftime("%b %d, %I:%M %p (Dubai)")
                    except Exception:
                        time_str = booking['booking_time']

                    col4.write(f"📅 {time_str}")
                    
                    # Status badge
                    status = booking['status']
                    if status == "confirmed":
                        col5.success("✅ Confirmed")
                    elif status == "cancelled":
                        col5.error("❌ Cancelled")
                    else:
                        col5.info(f"ℹ️ {status}")
                    
                    st.divider()
                
                # Bulk action buttons
                submit_col1, submit_col2 = st.columns(2)
                
                with submit_col1:
                    if st.form_submit_button("❌ Cancel Selected Bookings", type="primary", use_container_width=True):
                        if selected_booking_ids:
                            try:
                                for booking_id in selected_booking_ids:
                                    supabase.table("bookings")\
                                        .update({"status": "cancelled"})\
                                        .eq("id", booking_id)\
                                        .execute()
                                
                                st.success(f"✅ Cancelled {len(selected_booking_ids)} booking(s)")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error cancelling bookings: {e}")
                        else:
                            st.warning("⚠️ No bookings selected")
        
        else:
            st.info("📭 No bookings found for this location")
    
    except Exception as e:
        st.error(f"❌ Error loading bookings: {e}")

# ============================================================================
# TAB 2: KITCHEN DISPLAY SYSTEM (KDS)
# ============================================================================

with tab2:
    st.header("🔥 Kitchen Display System")
    
    # KDS-specific auto-refresh (faster)
    st_autorefresh(interval=3000, key="kds_refresh")
    
    # Fetch pending orders
    try:
        orders_response = supabase.table("orders")\
            .select("*")\
            .eq("restaurant_id", current_restaurant_id)\
            .eq("status", "pending")\
            .order("created_at", desc=False)\
            .execute()
        
        orders = orders_response.data
        
        if orders:
            st.info(f"📋 {len(orders)} order(s) in queue")
            st.markdown("---")
            
            for idx, order in enumerate(orders):
                # Container for each order
                with st.container(border=True):
                    # Header row
                    header_col1, header_col2, header_col3 = st.columns([2, 1, 1])
                    
                    header_col1.markdown(f"### 🪑 Table {order['table_number']}")
                    header_col2.markdown(f"**{order['customer_name']}**")
                    
                    # Calculate time since order
                    # Both sides MUST be timezone-aware UTC so Render's server
                    # clock (UTC) produces the correct elapsed-time diff.
                    # We then display the order's wall-clock time in Dubai.
                    try:
                        created_utc = datetime.fromisoformat(
                            order['created_at'].replace('Z', '+00:00')
                        )
                        # Elapsed time: compare two UTC-aware datetimes
                        now_utc      = datetime.now(timezone.utc)
                        elapsed_secs = (now_utc - created_utc).total_seconds()
                        minutes_ago  = max(0, int(elapsed_secs / 60))

                        # Display time in Dubai local clock for kitchen staff
                        created_dubai = to_dubai(created_utc)
                        wall_clock    = created_dubai.strftime("%I:%M %p")

                        if minutes_ago == 0:
                            header_col3.caption(f"⏱️ Just now  ({wall_clock})")
                        elif minutes_ago < 60:
                            header_col3.caption(f"⏱️ {minutes_ago} min ago  ({wall_clock})")
                        else:
                            hours = minutes_ago // 60
                            mins  = minutes_ago % 60
                            header_col3.caption(f"⏱️ {hours}h {mins}m ago  ({wall_clock})")
                    except Exception as e:
                        print(f"[KDS TIME] {e}")
                        header_col3.caption("⏱️ Just now")
                    
                    # Order items
                    st.markdown(f"**Order #{order['id']}**")
                    st.write(f"🍽️ {order['items']}")
                    st.write(f"💰 {format_currency(order['price'])}")
                    
                    st.markdown("---")
                    
                    # Check cancellation status
                    cancellation_status = order.get('cancellation_status', 'none')
                    
                    if cancellation_status == 'requested':
                        # Cancellation request pending
                        st.warning("⚠️ **CANCELLATION REQUESTED BY CUSTOMER**")
                        
                        action_col1, action_col2 = st.columns(2)
                        
                        with action_col1:
                            if st.button(
                                "✅ Approve Cancellation",
                                key=f"approve_cancel_{order['id']}",
                                use_container_width=True,
                                type="primary"
                            ):
                                try:
                                    # Update order status
                                    supabase.table("orders")\
                                        .update({
                                            "status": "cancelled",
                                            "cancellation_status": "approved"
                                        })\
                                        .eq("id", order["id"])\
                                        .execute()
                                    
                                    # Notify customer
                                    if order.get('chat_id'):
                                        send_telegram_message(
                                            order['chat_id'],
                                            "✅ Your cancellation request has been approved."
                                        )
                                    
                                    st.success("Cancellation approved")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error: {e}")
                        
                        with action_col2:
                            if st.button(
                                "❌ Reject Cancellation",
                                key=f"reject_cancel_{order['id']}",
                                use_container_width=True
                            ):
                                try:
                                    # Update cancellation status
                                    supabase.table("orders")\
                                        .update({"cancellation_status": "rejected"})\
                                        .eq("id", order["id"])\
                                        .execute()
                                    
                                    # Notify customer
                                    if order.get('chat_id'):
                                        send_telegram_message(
                                            order['chat_id'],
                                            "❌ Cancellation rejected. Kitchen is preparing your food."
                                        )
                                    
                                    st.success("Cancellation rejected")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error: {e}")
                    
                    else:
                        # Normal order - show "Ready" button
                        if st.button(
                            "✅ Mark as Ready",
                            key=f"ready_{order['id']}",
                            use_container_width=True,
                            type="primary"
                        ):
                            try:
                                # Update order status
                                supabase.table("orders")\
                                    .update({"status": "completed"})\
                                    .eq("id", order["id"])\
                                    .execute()
                                
                                # Notify customer
                                if order.get('chat_id'):
                                    send_telegram_message(
                                        order['chat_id'],
                                        f"🍽️ Your order is ready! (Table {order['table_number']})"
                                    )
                                
                                st.success(f"✅ Order #{order['id']} marked as ready")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error: {e}")
        
        else:
            st.success("🎉 Kitchen Clear - No pending orders!")
    
    except Exception as e:
        st.error(f"❌ Error loading orders: {e}")

# ============================================================================
# TAB 3: LIVE TABLES & BILLING
# ============================================================================

with tab3:
    st.header("💰 Live Tables & Billing")
    
    # Refresh button
    if st.button("🔄 Refresh Tables", use_container_width=False):
        st.rerun()
    
    st.markdown("---")
    
    # Fetch active orders (not paid, not cancelled)
    try:
        active_orders_response = supabase.table("orders")\
            .select("*")\
            .eq("restaurant_id", current_restaurant_id)\
            .neq("status", "paid")\
            .neq("status", "cancelled")\
            .execute()
        
        active_orders = active_orders_response.data
        
        if active_orders:
            # Group orders by table number
            tables_data = {}
            
            for order in active_orders:
                table_num = order['table_number']
                
                if table_num not in tables_data:
                    tables_data[table_num] = {
                        "total": 0.0,
                        "items": [],
                        "dish_names": set(),
                        "chat_id": order.get("chat_id"),
                        "order_ids": []
                    }
                
                # Add to totals
                tables_data[table_num]["total"] += float(order['price'])
                tables_data[table_num]["items"].append(
                    f"{order['items']} ({format_currency(order['price'])})"
                )
                tables_data[table_num]["order_ids"].append(order['id'])
                
                # Extract dish names for feedback
                for item in order['items'].split(','):
                    # Remove price info in parentheses
                    clean_name = item.split('(')[0].strip()
                    if clean_name:
                        tables_data[table_num]["dish_names"].add(clean_name)
            
            # Display statistics
            st.info(f"🪑 {len(tables_data)} active table(s)")
            st.markdown("---")
            
            # Display each table
            for table_number, data in sorted(tables_data.items()):
                with st.container(border=True):
                    # Table header
                    table_col1, table_col2 = st.columns([3, 1])
                    
                    table_col1.markdown(f"### 🪑 Table {table_number}")
                    table_col2.markdown(f"### {format_currency(data['total'])}")
                    
                    st.markdown("---")
                    
                    # Order items
                    st.markdown("**Orders:**")
                    for item in data['items']:
                        st.write(f"• {item}")
                    
                    st.markdown("---")
                    
                    # Payment button
                    if st.button(
                        "💳 Close Table & Request Payment",
                        key=f"pay_table_{table_number}",
                        use_container_width=True,
                        type="primary"
                    ):
                        try:
                            # Update all orders for this table to "paid"
                            for order_id in data['order_ids']:
                                supabase.table("orders")\
                                    .update({"status": "paid"})\
                                    .eq("id", order_id)\
                                    .execute()
                            
                            # Construct feedback request message
                            dishes_list = "\n".join([f"• {dish}" for dish in sorted(data['dish_names'])])
                            
                            feedback_message = (
                                f"✅ **Payment Received - Thank You!**\n\n"
                                f"💰 Total: {format_currency(data['total'])}\n\n"
                                f"We hope you enjoyed your meal! 😊\n\n"
                                f"⭐ **Please rate your experience:**\n\n"
                                f"**Dishes:**\n{dishes_list}\n\n"
                                f"Reply with ratings (1-5 stars) for each dish "
                                f"and your overall restaurant experience.\n\n"
                                f"Example: 5, 4, 5 (Overall: 5)"
                            )
                            
                            # Send feedback request to customer
                            if data['chat_id']:
                                telegram_success = send_telegram_message(
                                    data['chat_id'],
                                    feedback_message
                                )
                                
                                if telegram_success:
                                    st.success(f"✅ Table {table_number} closed & feedback request sent!")
                                else:
                                    st.warning(f"✅ Table {table_number} closed (feedback not sent)")
                            else:
                                st.success(f"✅ Table {table_number} closed")
                            
                            st.rerun()
                            
                        except Exception as e:
                            st.error(f"Error closing table: {e}")
        
        else:
            st.info("📭 No active tables at the moment")
    
    except Exception as e:
        st.error(f"❌ Error loading tables: {e}")

# ============================================================================
# FOOTER
# ============================================================================

st.markdown("---")
st.caption(f"🔄 Auto-refresh enabled • Last updated: {get_timestamp()}")