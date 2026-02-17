"""
Restaurant Admin Dashboard — Streamlit  (v3)
==============================================
New in v3:
  • Tab 2 KDS: Modification-approval workflow
    - Shows "Table X requested to remove Y" alerts
    - Approve:  commits item/price change + notifies customer via Telegram
    - Reject:   clears pending change + notifies customer
  • Tab 4: Menu Manager — live add / edit / delete of menu_items rows
    - Structured form: category, item name, price, description
    - Writes content column in the canonical multi-line format the bot parses
  • Timezone: all timestamps converted to Dubai (UTC+4) via to_dubai()
"""

import json
import re
import streamlit as st
import requests
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh
from dotenv import load_dotenv
from supabase import create_client

# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------
DUBAI_TZ  = ZoneInfo("Asia/Dubai")
UTC_PLUS4 = timedelta(hours=4)


def to_dubai(utc_dt: datetime) -> datetime:
    """Convert a UTC-aware or naive datetime to Dubai time (UTC+4)."""
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
    initial_sidebar_state="expanded",
)

# Global auto-refresh every 5 s
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
    """Send a Telegram message to a customer. Returns True on success."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=5,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
        return False


def format_currency(amount: float) -> str:
    return f"${amount:.2f}"


def get_timestamp() -> str:
    """Current time in Dubai timezone for the dashboard footer."""
    return datetime.now(DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")


def parse_price_from_pending(pending_json: str) -> float:
    """
    Safely extract new_price from a pending_modification JSON blob.
    Falls back to 0.0 on any parse error.
    """
    try:
        data = json.loads(pending_json)
        return float(data.get("new_price", 0.0))
    except Exception:
        return 0.0


def build_menu_content(category: str, item: str, price: str, description: str) -> str:
    """
    Build the canonical multi-line content string stored in menu_items.content.
    This is the exact format _send_menu() in main.py parses.

    Format:
        category: Starters
        item: Binary Bites
        price: $8
        description: Crispy fried jalapeno poppers
    """
    lines = [
        f"category: {category.strip()}",
        f"item: {item.strip()}",
        f"price: {price.strip()}",
    ]
    if description.strip():
        lines.append(f"description: {description.strip()}")
    return "\n".join(lines)


def parse_menu_content(content: str) -> dict:
    """
    Parse a menu_items.content string back into its component fields
    so they can be pre-filled in the edit form.
    """
    result = {"category": "", "item": "", "price": "", "description": ""}
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("category:"):
            result["category"] = line.replace("category:", "").strip()
        elif line.startswith("item:"):
            result["item"] = line.replace("item:", "").strip()
        elif line.startswith("price:"):
            result["price"] = line.replace("price:", "").strip()
        elif line.startswith("description:"):
            result["description"] = line.replace("description:", "").strip()
    return result


# ============================================================================
# SIDEBAR — RESTAURANT SELECTION
# ============================================================================

st.sidebar.title("🏢 Restaurant Manager")

try:
    restaurants_response = supabase.table("restaurants").select("id, name").execute()
    if not restaurants_response.data:
        st.error("❌ No restaurants found in database")
        st.stop()

    restaurant_options = {r["name"]: r["id"] for r in restaurants_response.data}
    selected_restaurant_name = st.sidebar.selectbox(
        "Select Location", list(restaurant_options.keys()), key="restaurant_selector"
    )
    current_restaurant_id = restaurant_options[selected_restaurant_name]

    st.sidebar.success(f"📍 {selected_restaurant_name}")
    st.sidebar.info(f"🔄 Last refresh: {get_timestamp()}")

except Exception as e:
    st.error(f"❌ Error loading restaurants: {e}")
    st.stop()

# ============================================================================
# MAIN HEADER
# ============================================================================

st.title(f"📊 Dashboard: {selected_restaurant_name}")
st.markdown("---")

# 4 tabs: Bookings | KDS | Billing | Menu Manager
tab1, tab2, tab3, tab4 = st.tabs([
    "📅 Bookings Management",
    "👨‍🍳 Kitchen Display System",
    "💰 Live Tables & Billing",
    "🍽️ Menu Manager",
])

# ============================================================================
# TAB 1: BOOKINGS MANAGEMENT  (unchanged from v2)
# ============================================================================

with tab1:
    st.header("📅 Reservations & Bookings")

    col_action1, col_action2, col_action3, col_action4 = st.columns(4)

    with col_action1:
        if st.button("🔄 Refresh Data", use_container_width=True):
            st.rerun()

    with col_action2:
        if st.button("🗑️ Purge Cancelled", use_container_width=True, type="secondary"):
            try:
                supabase.table("bookings") \
                    .delete() \
                    .eq("status", "cancelled") \
                    .eq("restaurant_id", current_restaurant_id) \
                    .execute()
                st.toast("✅ Cancelled bookings permanently deleted", icon="🗑️")
                st.rerun()
            except Exception as e:
                st.error(f"Error deleting bookings: {e}")

    st.markdown("---")

    try:
        bookings_response = supabase.table("bookings") \
            .select("*") \
            .eq("restaurant_id", current_restaurant_id) \
            .order("booking_time", desc=False) \
            .execute()
        bookings = bookings_response.data

        if bookings:
            total_bookings = len(bookings)
            confirmed      = sum(1 for b in bookings if b["status"] == "confirmed")
            cancelled      = sum(1 for b in bookings if b["status"] == "cancelled")

            s1, s2, s3 = st.columns(3)
            s1.metric("Total Bookings", total_bookings)
            s2.metric("Confirmed",      confirmed)
            s3.metric("Cancelled",      cancelled)
            st.markdown("---")

            with st.form("bulk_booking_actions"):
                st.subheader("📋 Booking List")
                st.caption("Select bookings to cancel in bulk")
                selected_booking_ids = []

                for booking in bookings:
                    c1, c2, c3, c4, c5 = st.columns([0.5, 2, 1.5, 1.5, 1])

                    if c1.checkbox("Select", key=f"booking_check_{booking['id']}",
                                   label_visibility="collapsed"):
                        selected_booking_ids.append(booking["id"])

                    c2.write(f"**{booking['customer_name']}**")
                    c3.write(f"👥 {booking['party_size']} guests")

                    try:
                        bdt = datetime.fromisoformat(
                            booking["booking_time"].replace("Z", "+00:00")
                        )
                        time_str = to_dubai(bdt).strftime("%b %d, %I:%M %p (Dubai)")
                    except Exception:
                        time_str = booking["booking_time"]
                    c4.write(f"📅 {time_str}")

                    status = booking["status"]
                    if status == "confirmed":
                        c5.success("✅")
                    elif status == "cancelled":
                        c5.error("❌")
                    else:
                        c5.info(status)

                    st.divider()

                sc1, sc2 = st.columns(2)
                with sc1:
                    if st.form_submit_button("❌ Cancel Selected", type="primary",
                                             use_container_width=True):
                        if selected_booking_ids:
                            for bid in selected_booking_ids:
                                supabase.table("bookings") \
                                    .update({"status": "cancelled"}) \
                                    .eq("id", bid) \
                                    .execute()
                            st.success(f"✅ Cancelled {len(selected_booking_ids)} booking(s)")
                            st.rerun()
                        else:
                            st.warning("⚠️ No bookings selected")
        else:
            st.info("📭 No bookings found for this location")

    except Exception as e:
        st.error(f"❌ Error loading bookings: {e}")

# ============================================================================
# TAB 2: KITCHEN DISPLAY SYSTEM (KDS)
# New in v3: modification-approval alerts shown BEFORE the cancel/ready buttons
# ============================================================================

with tab2:
    st.header("🔥 Kitchen Display System")
    st_autorefresh(interval=3000, key="kds_refresh")

    try:
        orders_response = supabase.table("orders") \
            .select("*") \
            .eq("restaurant_id", current_restaurant_id) \
            .eq("status", "pending") \
            .order("created_at", desc=False) \
            .execute()
        orders = orders_response.data

        if orders:
            st.info(f"📋 {len(orders)} order(s) in queue")
            st.markdown("---")

            for order in orders:
                with st.container(border=True):

                    # ── Header: table / name / time ──────────────────────────
                    hc1, hc2, hc3 = st.columns([2, 1, 1])
                    hc1.markdown(f"### 🪑 Table {order['table_number']}")
                    hc2.markdown(f"**{order['customer_name']}**")

                    try:
                        created_utc   = datetime.fromisoformat(
                            order["created_at"].replace("Z", "+00:00")
                        )
                        now_utc       = datetime.now(timezone.utc)
                        elapsed_secs  = (now_utc - created_utc).total_seconds()
                        minutes_ago   = max(0, int(elapsed_secs / 60))
                        wall_clock    = to_dubai(created_utc).strftime("%I:%M %p")

                        if minutes_ago == 0:
                            hc3.caption(f"⏱️ Just now  ({wall_clock})")
                        elif minutes_ago < 60:
                            hc3.caption(f"⏱️ {minutes_ago} min ago  ({wall_clock})")
                        else:
                            h, m = divmod(minutes_ago, 60)
                            hc3.caption(f"⏱️ {h}h {m}m ago  ({wall_clock})")
                    except Exception:
                        hc3.caption("⏱️ Just now")

                    # ── Order detail ─────────────────────────────────────────
                    st.markdown(f"**Order #{order['id']}**")
                    st.write(f"🍽️ {order['items']}")
                    st.write(f"💰 {format_currency(order['price'])}")
                    st.markdown("---")

                    # ── Priority 1: MODIFICATION REQUEST  (new in v3) ────────
                    # Shown BEFORE the cancellation check so staff handles
                    # item changes distinctly from full-order cancellations.
                    mod_status   = order.get("modification_status", "none")
                    pending_blob = order.get("pending_modification")

                    if mod_status == "requested" and pending_blob:
                        try:
                            pending = json.loads(pending_blob)
                        except Exception:
                            pending = {}

                        removed   = pending.get("removed_items",   "item(s)")
                        remaining = pending.get("remaining_items", order["items"])
                        new_price = pending.get("new_price", 0.0)

                        st.warning(
                            f"✏️ **MODIFICATION REQUEST**\n\n"
                            f"Table **{order['table_number']}** wants to remove: "
                            f"**{removed}**\n\n"
                            f"Remaining if approved: _{remaining}_\n"
                            f"New total if approved: **{format_currency(new_price)}**"
                        )

                        mc1, mc2 = st.columns(2)

                        with mc1:
                            if st.button(
                                "✅ Approve Change",
                                key=f"approve_mod_{order['id']}",
                                use_container_width=True,
                                type="primary",
                            ):
                                try:
                                    # Commit the approved modification
                                    supabase.table("orders") \
                                        .update({
                                            "items":               remaining,
                                            "price":               new_price,
                                            "modification_status": "approved",
                                            "pending_modification": None,
                                        }) \
                                        .eq("id", order["id"]) \
                                        .execute()

                                    # Notify customer
                                    if order.get("chat_id"):
                                        send_telegram_message(
                                            order["chat_id"],
                                            f"✅ *Kitchen approved your change*\n\n"
                                            f"Updated order: {remaining}\n"
                                            f"New total: {format_currency(new_price)}",
                                        )

                                    st.success("✅ Modification approved and committed")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error approving modification: {e}")

                        with mc2:
                            if st.button(
                                "❌ Reject Change",
                                key=f"reject_mod_{order['id']}",
                                use_container_width=True,
                            ):
                                try:
                                    # Clear pending modification, keep original order
                                    supabase.table("orders") \
                                        .update({
                                            "modification_status":  "rejected",
                                            "pending_modification":  None,
                                        }) \
                                        .eq("id", order["id"]) \
                                        .execute()

                                    # Notify customer
                                    if order.get("chat_id"):
                                        send_telegram_message(
                                            order["chat_id"],
                                            "❌ *Kitchen rejected your change — "
                                            "food is already being prepared.*\n\n"
                                            f"Your original order stands: {order['items']}",
                                        )

                                    st.success("Modification rejected — original order kept")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error rejecting modification: {e}")

                        # Don't show cancel/ready buttons while a mod is pending
                        continue

                    # ── Priority 2: CANCELLATION REQUEST (existing) ──────────
                    cancel_status = order.get("cancellation_status", "none")

                    if cancel_status == "requested":
                        st.warning("⚠️ **CANCELLATION REQUESTED BY CUSTOMER**")

                        ac1, ac2 = st.columns(2)

                        with ac1:
                            if st.button(
                                "✅ Approve Cancellation",
                                key=f"approve_cancel_{order['id']}",
                                use_container_width=True,
                                type="primary",
                            ):
                                try:
                                    supabase.table("orders") \
                                        .update({
                                            "status":              "cancelled",
                                            "cancellation_status": "approved",
                                        }) \
                                        .eq("id", order["id"]) \
                                        .execute()

                                    if order.get("chat_id"):
                                        send_telegram_message(
                                            order["chat_id"],
                                            "✅ Your cancellation request has been approved.",
                                        )

                                    st.success("Cancellation approved")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error: {e}")

                        with ac2:
                            if st.button(
                                "❌ Reject Cancellation",
                                key=f"reject_cancel_{order['id']}",
                                use_container_width=True,
                            ):
                                try:
                                    supabase.table("orders") \
                                        .update({"cancellation_status": "rejected"}) \
                                        .eq("id", order["id"]) \
                                        .execute()

                                    if order.get("chat_id"):
                                        send_telegram_message(
                                            order["chat_id"],
                                            "❌ Cancellation rejected. "
                                            "Kitchen is preparing your food.",
                                        )

                                    st.success("Cancellation rejected")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error: {e}")

                    else:
                        # ── Normal order — Mark as Ready ────────────────────
                        if st.button(
                            "✅ Mark as Ready",
                            key=f"ready_{order['id']}",
                            use_container_width=True,
                            type="primary",
                        ):
                            try:
                                supabase.table("orders") \
                                    .update({"status": "completed"}) \
                                    .eq("id", order["id"]) \
                                    .execute()

                                if order.get("chat_id"):
                                    send_telegram_message(
                                        order["chat_id"],
                                        f"🍽️ Your order is ready! "
                                        f"(Table {order['table_number']})",
                                    )

                                st.success(f"✅ Order #{order['id']} marked as ready")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error: {e}")
        else:
            st.success("🎉 Kitchen Clear — No pending orders!")

    except Exception as e:
        st.error(f"❌ Error loading orders: {e}")

# ============================================================================
# TAB 3: LIVE TABLES & BILLING  (unchanged from v2)
# ============================================================================

with tab3:
    st.header("💰 Live Tables & Billing")

    if st.button("🔄 Refresh Tables", use_container_width=False):
        st.rerun()

    st.markdown("---")

    try:
        active_orders_response = supabase.table("orders") \
            .select("*") \
            .eq("restaurant_id", current_restaurant_id) \
            .neq("status", "paid") \
            .neq("status", "cancelled") \
            .execute()
        active_orders = active_orders_response.data

        if active_orders:
            tables_data: dict = {}

            for order in active_orders:
                tnum = order["table_number"]
                if tnum not in tables_data:
                    tables_data[tnum] = {
                        "total":      0.0,
                        "items":      [],
                        "dish_names": set(),
                        "chat_id":    order.get("chat_id"),
                        "order_ids":  [],
                    }
                tables_data[tnum]["total"] += float(order["price"])
                tables_data[tnum]["items"].append(
                    f"{order['items']} ({format_currency(order['price'])})"
                )
                tables_data[tnum]["order_ids"].append(order["id"])

                for item in order["items"].split(","):
                    clean = item.split("(")[0].strip()
                    if clean:
                        tables_data[tnum]["dish_names"].add(clean)

            st.info(f"🪑 {len(tables_data)} active table(s)")
            st.markdown("---")

            for table_num, data in sorted(tables_data.items()):
                with st.container(border=True):
                    tc1, tc2 = st.columns([3, 1])
                    tc1.markdown(f"### 🪑 Table {table_num}")
                    tc2.markdown(f"### {format_currency(data['total'])}")
                    st.markdown("---")

                    st.markdown("**Orders:**")
                    for item in data["items"]:
                        st.write(f"• {item}")

                    st.markdown("---")

                    if st.button(
                        "💳 Close Table & Request Payment",
                        key=f"pay_table_{table_num}",
                        use_container_width=True,
                        type="primary",
                    ):
                        try:
                            for oid in data["order_ids"]:
                                supabase.table("orders") \
                                    .update({"status": "paid"}) \
                                    .eq("id", oid) \
                                    .execute()

                            dishes_list = "\n".join(
                                f"• {d}" for d in sorted(data["dish_names"])
                            )
                            feedback_msg = (
                                f"✅ *Payment Received — Thank You!*\n\n"
                                f"💰 Total: {format_currency(data['total'])}\n\n"
                                f"We hope you enjoyed your meal! 😊\n\n"
                                f"⭐ *Please rate your experience:*\n\n"
                                f"*Dishes:*\n{dishes_list}\n\n"
                                f"Reply with ratings (1-5) for each dish "
                                f"and your overall experience.\n\n"
                                f"Example: 5, 4, 5  (Overall: 5)"
                            )

                            if data["chat_id"]:
                                ok = send_telegram_message(data["chat_id"], feedback_msg)
                                if ok:
                                    st.success(
                                        f"✅ Table {table_num} closed & feedback sent!"
                                    )
                                else:
                                    st.warning(
                                        f"✅ Table {table_num} closed (feedback not sent)"
                                    )
                            else:
                                st.success(f"✅ Table {table_num} closed")

                            st.rerun()
                        except Exception as e:
                            st.error(f"Error closing table: {e}")
        else:
            st.info("📭 No active tables at the moment")

    except Exception as e:
        st.error(f"❌ Error loading tables: {e}")

# ============================================================================
# TAB 4: MENU MANAGER  (new in v3)
# Full CRUD for menu_items filtered to the selected restaurant.
# Writes the canonical "category:/item:/price:/description:" content format
# that main.py's _send_menu() and order_service.py's process_new_order() parse.
# ============================================================================

with tab4:
    st.header("🍽️ Menu Manager")
    st.caption(
        "Changes here are live immediately — the bot fetches menu data from the "
        "database on every order and menu request."
    )
    st.markdown("---")

    # ── Fetch current menu items ─────────────────────────────────────────────
    try:
        menu_response = supabase.table("menu_items") \
            .select("id, content") \
            .eq("restaurant_id", current_restaurant_id) \
            .execute()
        menu_items = menu_response.data or []
    except Exception as e:
        st.error(f"❌ Error loading menu: {e}")
        menu_items = []

    # ── Section A: Add New Item ───────────────────────────────────────────────
    with st.expander("➕ Add New Menu Item", expanded=False):
        with st.form("add_menu_item_form", clear_on_submit=True):
            st.subheader("New Item Details")

            a_col1, a_col2 = st.columns(2)
            new_category    = a_col1.text_input(
                "Category *",
                placeholder="e.g. Starters, Mains, Desserts, Drinks",
            )
            new_item_name   = a_col2.text_input(
                "Item Name *",
                placeholder="e.g. Full Stack Burger",
            )

            b_col1, b_col2 = st.columns(2)
            new_price       = b_col1.text_input(
                "Price *",
                placeholder="e.g. $18  or  18",
            )
            new_description = b_col2.text_input(
                "Description",
                placeholder="e.g. Double beef patty with caramelised onions",
            )

            if st.form_submit_button("➕ Add Item", type="primary",
                                     use_container_width=True):
                if not new_category.strip() or not new_item_name.strip() or not new_price.strip():
                    st.error("❌ Category, Item Name, and Price are required.")
                else:
                    # Normalise price — add $ prefix if missing
                    price_str = new_price.strip()
                    if not price_str.startswith("$"):
                        price_str = f"${price_str}"

                    content = build_menu_content(
                        new_category, new_item_name, price_str, new_description
                    )
                    try:
                        supabase.table("menu_items").insert({
                            "restaurant_id": current_restaurant_id,
                            "content":       content,
                        }).execute()
                        st.success(
                            f"✅ Added: **{new_item_name.strip()}** "
                            f"({price_str}) to {new_category.strip()}"
                        )
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ Error adding item: {e}")

    st.markdown("---")

    # ── Section B: Current Menu — Edit & Delete ───────────────────────────────
    st.subheader(f"📋 Current Menu ({len(menu_items)} items)")

    if not menu_items:
        st.info("📭 No menu items yet. Use the form above to add some.")
    else:
        # Group items by category for readability
        grouped: dict = {}
        for row in menu_items:
            parsed = parse_menu_content(row["content"])
            cat    = parsed["category"] or "Uncategorised"
            if cat not in grouped:
                grouped[cat] = []
            grouped[cat].append({"id": row["id"], "parsed": parsed, "raw": row["content"]})

        for category, items in sorted(grouped.items()):
            st.markdown(f"#### {category.upper()}")

            for entry in items:
                row_id = entry["id"]
                parsed = entry["parsed"]

                with st.container(border=True):
                    # Display row — item name | price | action buttons
                    d_col1, d_col2, d_col3, d_col4 = st.columns([3, 1.5, 1, 1])

                    d_col1.write(f"**{parsed['item']}**")
                    if parsed["description"]:
                        d_col1.caption(parsed["description"])

                    d_col2.write(f"💰 {parsed['price']}")

                    # ── Edit button toggles an inline form ───────────────────
                    edit_key = f"edit_toggle_{row_id}"
                    if edit_key not in st.session_state:
                        st.session_state[edit_key] = False

                    if d_col3.button("✏️ Edit", key=f"edit_btn_{row_id}",
                                     use_container_width=True):
                        st.session_state[edit_key] = not st.session_state[edit_key]

                    # ── Delete button ────────────────────────────────────────
                    if d_col4.button("🗑️ Delete", key=f"del_{row_id}",
                                     use_container_width=True):
                        try:
                            supabase.table("menu_items") \
                                .delete() \
                                .eq("id", row_id) \
                                .execute()
                            st.success(f"🗑️ Deleted: {parsed['item']}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error deleting: {e}")

                    # ── Inline edit form (shown when toggle is True) ─────────
                    if st.session_state.get(edit_key):
                        with st.form(f"edit_form_{row_id}"):
                            st.caption("Edit item details:")

                            ec1, ec2 = st.columns(2)
                            e_cat   = ec1.text_input("Category", value=parsed["category"],
                                                     key=f"e_cat_{row_id}")
                            e_name  = ec2.text_input("Item Name", value=parsed["item"],
                                                     key=f"e_name_{row_id}")

                            fc1, fc2 = st.columns(2)
                            e_price = fc1.text_input("Price", value=parsed["price"],
                                                     key=f"e_price_{row_id}")
                            e_desc  = fc2.text_input("Description",
                                                     value=parsed["description"],
                                                     key=f"e_desc_{row_id}")

                            save_col, cancel_col = st.columns(2)
                            save_clicked = save_col.form_submit_button(
                                "💾 Save Changes", type="primary",
                                use_container_width=True
                            )
                            cancel_col.form_submit_button(
                                "✖ Cancel", use_container_width=True
                            )

                            if save_clicked:
                                if not e_cat.strip() or not e_name.strip() or not e_price.strip():
                                    st.error("Category, Item Name, and Price are required.")
                                else:
                                    price_val = e_price.strip()
                                    if not price_val.startswith("$"):
                                        price_val = f"${price_val}"

                                    new_content = build_menu_content(
                                        e_cat, e_name, price_val, e_desc
                                    )
                                    try:
                                        supabase.table("menu_items") \
                                            .update({"content": new_content}) \
                                            .eq("id", row_id) \
                                            .execute()
                                        st.success(f"✅ Updated: {e_name.strip()}")
                                        st.session_state[edit_key] = False
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Error saving: {e}")

            st.markdown("---")

    # ── Section C: Bulk import helper ────────────────────────────────────────
    with st.expander("📥 Bulk Import (Advanced)", expanded=False):
        st.caption(
            "Paste raw content lines — one item per block, separated by a blank line. "
            "Format each block as:\n\n"
            "```\ncategory: Starters\nitem: Binary Bites\nprice: $8\n"
            "description: Crispy jalapeno poppers\n```"
        )
        bulk_text = st.text_area(
            "Paste menu content here:",
            height=200,
            placeholder="category: Starters\nitem: Binary Bites\nprice: $8\n"
                        "description: Crispy jalapeno poppers\n\n"
                        "category: Mains\nitem: Full Stack Burger\nprice: $18",
        )
        if st.button("📥 Import Items", use_container_width=False):
            if not bulk_text.strip():
                st.warning("Nothing to import.")
            else:
                blocks = re.split(r"\n\s*\n", bulk_text.strip())
                imported = 0
                errors   = 0
                for block in blocks:
                    if not block.strip():
                        continue
                    try:
                        supabase.table("menu_items").insert({
                            "restaurant_id": current_restaurant_id,
                            "content":       block.strip(),
                        }).execute()
                        imported += 1
                    except Exception as e:
                        errors += 1
                        st.warning(f"Block failed: {e}")

                if imported:
                    st.success(f"✅ Imported {imported} item(s)")
                if errors:
                    st.error(f"❌ {errors} block(s) failed")
                if imported:
                    st.rerun()

# ============================================================================
# FOOTER
# ============================================================================

st.markdown("---")
st.caption(f"🔄 Auto-refresh enabled • Last updated: {get_timestamp()}")