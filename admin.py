"""
Restaurant Admin Dashboard — Streamlit  v5
===========================================
New in v5:
  SIDEBAR — Dynamic Restaurant Management
    • "➕ Add New Restaurant" option in the location dropdown.
    • Shows a form to type a name; Supabase generates the UUID automatically.
    • Restaurant ID displayed prominently for copy-paste into bot commands.
    • `/start rest_id=<id>` instructions shown next to every location.

  TAB 5 — Policies & AI Context
    • Free-text area saved to restaurant_policies table (upsert, one row per
      restaurant).
    • Loaded by main.py's fetch_policy_text() and injected into the AI system
      prompt in GENERAL mode so the bot answers accurately per location.

  Existing tabs (1-4) unchanged from v4:
    Bookings | Kitchen Display | Live Tables | Menu Manager
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

DUBAI_TZ  = ZoneInfo("Asia/Dubai")
UTC_PLUS4 = timedelta(hours=4)


def to_dubai(utc_dt: datetime) -> datetime:
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(DUBAI_TZ)


# ============================================================================
# PAGE CONFIG
# ============================================================================

st.set_page_config(
    page_title="Restaurant Admin Dashboard",
    layout="wide",
    page_icon="👨‍🍳",
    initial_sidebar_state="expanded",
)

refresh_count = st_autorefresh(interval=5000, key="global_refresh")
load_dotenv()

# ============================================================================
# DATABASE
# ============================================================================

try:
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
except Exception as e:
    st.error(f"❌ Database connection error: {e}")
    st.stop()

# ============================================================================
# HELPERS
# ============================================================================

def send_telegram_message(chat_id: str, text: str) -> bool:
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
        print(f"[TG ERROR] {e}")
        return False


def fmt(amount: float) -> str:
    return f"${amount:.2f}"


def get_ts() -> str:
    return datetime.now(DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")


def build_menu_content(category: str, item: str, price: str, description: str) -> str:
    lines = [
        f"category: {category.strip()}",
        f"item: {item.strip()}",
        f"price: {price.strip()}",
    ]
    if description.strip():
        lines.append(f"description: {description.strip()}")
    return "\n".join(lines)


def parse_menu_content(content: str) -> dict:
    r = {"category": "", "item": "", "price": "", "description": ""}
    for line in content.split("\n"):
        line = line.strip()
        for field in ("category", "item", "price", "description"):
            if line.startswith(f"{field}:"):
                r[field] = line.replace(f"{field}:", "").strip()
    return r


# ============================================================================
# SIDEBAR — RESTAURANT SELECTION + CREATION
# ============================================================================

st.sidebar.title("🏢 Restaurant Manager")

_ADD_LABEL = "➕ Add New Restaurant"

try:
    rests     = supabase.table("restaurants").select("id, name").execute()
    rest_rows = rests.data or []
except Exception as e:
    st.error(f"Error loading restaurants: {e}")
    st.stop()

# Build dropdown options: real restaurants + the add-new sentinel
rest_name_to_id = {r["name"]: r["id"] for r in rest_rows}
dropdown_opts   = list(rest_name_to_id.keys()) + [_ADD_LABEL]

sel_name = st.sidebar.selectbox(
    "Select Location",
    dropdown_opts,
    key="rest_selector",
)

# ── Add New Restaurant form ──────────────────────────────────────────────────
if sel_name == _ADD_LABEL:
    st.sidebar.markdown("---")
    st.sidebar.subheader("Create New Location")
    with st.sidebar.form("new_restaurant_form"):
        new_name = st.text_input("Restaurant Name *", placeholder="e.g. Tech Bites Marina")
        submitted = st.form_submit_button("💾 Create Restaurant", type="primary",
                                          use_container_width=True)
        if submitted:
            if not new_name.strip():
                st.error("Please enter a restaurant name.")
            else:
                try:
                    # Let Supabase generate the UUID (uses gen_random_uuid() default)
                    result = supabase.table("restaurants").insert(
                        {"name": new_name.strip()}
                    ).execute()
                    new_rid  = result.data[0]["id"]
                    new_rname = result.data[0]["name"]
                    st.success(f"✅ Created: **{new_rname}**")
                    st.info(f"**Restaurant ID:**\n`{new_rid}`\n\nBot command:\n`/start rest_id={new_rid}`")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error creating restaurant: {e}")
    st.stop()   # Don't render the rest of the dashboard for the add form

# ── Normal restaurant selected ───────────────────────────────────────────────
cur_rid = rest_name_to_id[sel_name]

# Display restaurant ID prominently for copy-paste
st.sidebar.success(f"📍 {sel_name}")
st.sidebar.markdown("**Restaurant ID** _(for bot command)_:")
st.sidebar.code(cur_rid, language=None)
st.sidebar.caption(f"Bot command: `/start rest_id={cur_rid}`")
st.sidebar.info(f"🔄 {get_ts()}")

# ============================================================================
# DASHBOARD HEADER + TABS
# ============================================================================

st.title(f"📊 Dashboard: {sel_name}")
st.markdown("---")

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📅 Bookings",
    "👨‍🍳 Kitchen Display",
    "💰 Live Tables & Billing",
    "🍽️ Menu Manager",
    "ℹ️ Policies & Settings",
])

# ============================================================================
# TAB 1: BOOKINGS  (unchanged)
# ============================================================================

with tab1:
    st.header("📅 Reservations & Bookings")

    a1, a2, _, _ = st.columns(4)
    with a1:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()
    with a2:
        if st.button("🗑️ Purge Cancelled", use_container_width=True, type="secondary"):
            try:
                supabase.table("bookings").delete() \
                    .eq("status", "cancelled").eq("restaurant_id", cur_rid).execute()
                st.toast("✅ Cancelled bookings deleted", icon="🗑️")
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")

    st.markdown("---")

    try:
        bk_res   = supabase.table("bookings").select("*") \
            .eq("restaurant_id", cur_rid).order("booking_time").execute()
        bookings = bk_res.data

        if bookings:
            c1, c2, c3 = st.columns(3)
            c1.metric("Total",     len(bookings))
            c2.metric("Confirmed", sum(1 for b in bookings if b["status"] == "confirmed"))
            c3.metric("Cancelled", sum(1 for b in bookings if b["status"] == "cancelled"))
            st.markdown("---")

            with st.form("bulk_cancel"):
                st.subheader("📋 Booking List")
                selected = []

                for b in bookings:
                    cols = st.columns([0.5, 2, 1.5, 1.5, 1])
                    if cols[0].checkbox("", key=f"bc_{b['id']}", label_visibility="collapsed"):
                        selected.append(b["id"])
                    cols[1].write(f"**{b['customer_name']}**")
                    cols[2].write(f"👥 {b['party_size']} guests")
                    try:
                        bdt      = datetime.fromisoformat(b["booking_time"].replace("Z", "+00:00"))
                        time_str = to_dubai(bdt).strftime("%b %d, %I:%M %p (Dubai)")
                    except Exception:
                        time_str = b["booking_time"]
                    cols[3].write(f"📅 {time_str}")
                    s = b["status"]
                    if s == "confirmed": cols[4].success("✅")
                    elif s == "cancelled": cols[4].error("❌")
                    else: cols[4].info(s)
                    st.divider()

                if st.form_submit_button("❌ Cancel Selected", type="primary",
                                         use_container_width=True):
                    if selected:
                        for bid in selected:
                            supabase.table("bookings") \
                                .update({"status": "cancelled"}).eq("id", bid).execute()
                        st.success(f"✅ Cancelled {len(selected)} booking(s)")
                        st.rerun()
                    else:
                        st.warning("No bookings selected")
        else:
            st.info("📭 No bookings found")

    except Exception as e:
        st.error(f"Error: {e}")

# ============================================================================
# TAB 2: KITCHEN DISPLAY SYSTEM  (unchanged)
# ============================================================================

with tab2:
    st.header("🔥 Kitchen Display System")
    st_autorefresh(interval=3000, key="kds_refresh")

    try:
        ord_res = supabase.table("orders").select("*") \
            .eq("restaurant_id", cur_rid).eq("status", "pending") \
            .order("created_at").execute()
        orders = ord_res.data

        if orders:
            st.info(f"📋 {len(orders)} order(s) in queue")
            st.markdown("---")

            for order in orders:
                oid = order["id"]

                with st.container(border=True):

                    h1, h2, h3 = st.columns([2, 1, 1])
                    h1.markdown(f"### 🪑 Table {order['table_number']}  —  Order *#{oid}*")
                    h2.markdown(f"**{order['customer_name']}**")

                    try:
                        created_utc = datetime.fromisoformat(order["created_at"].replace("Z", "+00:00"))
                        now_utc     = datetime.now(timezone.utc)
                        mins        = max(0, int((now_utc - created_utc).total_seconds() / 60))
                        wall        = to_dubai(created_utc).strftime("%I:%M %p")
                        label       = "Just now" if mins == 0 else (
                            f"{mins} min ago" if mins < 60 else f"{mins//60}h {mins%60}m ago"
                        )
                        h3.caption(f"⏱️ {label}  ({wall})")
                    except Exception:
                        h3.caption("⏱️ Just now")

                    st.write(f"🍽️ {order['items']}")
                    st.write(f"💰 {fmt(order['price'])}")
                    st.markdown("---")

                    # Priority 1: MODIFICATION REQUEST
                    mod_status   = order.get("modification_status", "none")
                    pending_blob = order.get("pending_modification")

                    if mod_status == "requested" and pending_blob:
                        try:
                            pending = json.loads(pending_blob)
                        except Exception:
                            pending = {}

                        removed   = pending.get("removed_items",   "item(s)")
                        remaining = pending.get("remaining_items", "")
                        new_price = float(pending.get("new_price", 0.0))
                        all_gone  = not remaining.strip()

                        st.warning(
                            f"✏️ **MODIFICATION REQUEST — Order #{oid}**\n\n"
                            f"Table **{order['table_number']}** wants to remove: **{removed}**\n\n"
                            + (f"Remaining if approved: _{remaining}_\n"
                               f"New total if approved: **{fmt(new_price)}**"
                               if not all_gone else
                               "_All items removed — this will cancel the order._")
                        )

                        mc1, mc2 = st.columns(2)
                        with mc1:
                            if st.button("✅ Approve Change", key=f"amod_{oid}",
                                         use_container_width=True, type="primary"):
                                try:
                                    if all_gone:
                                        supabase.table("orders").update({
                                            "status": "cancelled",
                                            "cancellation_status": "approved",
                                            "modification_status": "approved",
                                            "pending_modification": None,
                                        }).eq("id", oid).execute()
                                        tg_msg = (f"🗑️ *Order #{oid} Cancelled*\n"
                                                  f"All items removed — approved by kitchen.")
                                    else:
                                        supabase.table("orders").update({
                                            "items": remaining, "price": new_price,
                                            "modification_status": "approved",
                                            "pending_modification": None,
                                        }).eq("id", oid).execute()
                                        tg_msg = (f"✅ *Kitchen approved your change — Order #{oid}*\n\n"
                                                  f"Updated: {remaining}\nNew total: {fmt(new_price)}")
                                    if order.get("chat_id"):
                                        send_telegram_message(order["chat_id"], tg_msg)
                                    st.success("✅ Modification approved")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error: {e}")

                        with mc2:
                            if st.button("❌ Reject Change", key=f"rmod_{oid}",
                                         use_container_width=True):
                                try:
                                    supabase.table("orders").update({
                                        "modification_status": "rejected",
                                        "pending_modification": None,
                                    }).eq("id", oid).execute()
                                    if order.get("chat_id"):
                                        send_telegram_message(
                                            order["chat_id"],
                                            f"❌ *Change rejected — Order #{oid}*\n"
                                            f"Original order stands: {order['items']}"
                                        )
                                    st.success("Rejected — original kept")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error: {e}")
                        continue

                    # Priority 2: CANCELLATION REQUEST
                    if order.get("cancellation_status") == "requested":
                        st.warning(f"⚠️ **CANCELLATION REQUESTED — Order #{oid}**")
                        cc1, cc2 = st.columns(2)
                        with cc1:
                            if st.button("✅ Approve Cancel", key=f"acan_{oid}",
                                         use_container_width=True, type="primary"):
                                try:
                                    supabase.table("orders").update({
                                        "status": "cancelled", "cancellation_status": "approved",
                                    }).eq("id", oid).execute()
                                    if order.get("chat_id"):
                                        send_telegram_message(order["chat_id"],
                                            f"✅ *Order #{oid} cancelled* — approved.")
                                    st.success("Cancellation approved")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error: {e}")
                        with cc2:
                            if st.button("❌ Reject Cancel", key=f"rcan_{oid}",
                                         use_container_width=True):
                                try:
                                    supabase.table("orders").update({
                                        "cancellation_status": "rejected"
                                    }).eq("id", oid).execute()
                                    if order.get("chat_id"):
                                        send_telegram_message(order["chat_id"],
                                            f"❌ *Cancellation rejected — Order #{oid}.*\n"
                                            f"Kitchen is preparing your food.")
                                    st.success("Cancellation rejected")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error: {e}")

                    else:
                        if st.button("✅ Mark as Ready", key=f"ready_{oid}",
                                     use_container_width=True, type="primary"):
                            try:
                                supabase.table("orders").update({"status": "completed"}) \
                                    .eq("id", oid).execute()
                                if order.get("chat_id"):
                                    send_telegram_message(order["chat_id"],
                                        f"🍽️ *Order #{oid} is ready!* (Table {order['table_number']})")
                                st.success(f"✅ Order #{oid} ready")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error: {e}")
        else:
            st.success("🎉 Kitchen clear — no pending orders!")

    except Exception as e:
        st.error(f"Error loading orders: {e}")

# ============================================================================
# TAB 3: LIVE TABLES & BILLING  (unchanged — always queries fresh from DB)
# ============================================================================

with tab3:
    st.header("💰 Live Tables & Billing")
    st.caption(
        "Totals are calculated fresh from the database on every 5-second refresh. "
        "Approved modifications and cancellations appear automatically on the next cycle."
    )

    if st.button("🔄 Refresh Now", use_container_width=False):
        st.rerun()

    st.markdown("---")

    try:
        live_res    = supabase.table("orders").select("*") \
            .eq("restaurant_id", cur_rid) \
            .neq("status", "paid").neq("status", "cancelled").execute()
        live_orders = live_res.data

        if live_orders:
            tables: dict = {}
            for o in live_orders:
                tnum = o["table_number"]
                if tnum not in tables:
                    tables[tnum] = {"orders": [], "total": 0.0,
                                    "dish_names": set(), "chat_id": o.get("chat_id"),
                                    "order_ids": []}
                tables[tnum]["orders"].append(o)
                tables[tnum]["total"]     += float(o["price"])
                tables[tnum]["order_ids"].append(o["id"])
                for item in o["items"].split(","):
                    clean = item.split("(")[0].strip()
                    if clean:
                        tables[tnum]["dish_names"].add(clean)

            for t in tables.values():
                t["total"] = round(t["total"], 2)

            st.info(f"🪑 {len(tables)} active table(s)")
            st.markdown("---")

            for tnum, data in sorted(tables.items()):
                with st.container(border=True):
                    tc1, tc2 = st.columns([3, 1])
                    tc1.markdown(f"### 🪑 Table {tnum}")
                    tc2.markdown(f"### {fmt(data['total'])}")
                    st.markdown("---")
                    st.markdown("**Orders:**")
                    for o in data["orders"]:
                        badge = " ⚠️ _mod pending_" if o.get("modification_status") == "requested" else ""
                        st.write(f"  • *#{o['id']}* {o['items']} — {fmt(float(o['price']))}{badge}")
                    st.markdown("---")

                    if st.button("💳 Close Table & Request Payment",
                                 key=f"pay_{tnum}", use_container_width=True, type="primary"):
                        try:
                            for oid in data["order_ids"]:
                                supabase.table("orders").update({"status": "paid"}).eq("id", oid).execute()
                            dishes  = "\n".join(f"• {d}" for d in sorted(data["dish_names"]))
                            fb_msg  = (f"✅ *Payment Received — Thank You!*\n\n"
                                       f"💰 Total: {fmt(data['total'])}\n\n"
                                       f"⭐ *Please rate (1-5):*\n\n{dishes}\n\n"
                                       f"Reply: 5, 4, 5 _(per dish + overall)_")
                            if data["chat_id"]:
                                ok  = send_telegram_message(data["chat_id"], fb_msg)
                                msg = (f"✅ Table {tnum} closed & feedback sent!"
                                       if ok else f"✅ Table {tnum} closed")
                            else:
                                msg = f"✅ Table {tnum} closed"
                            st.success(msg)
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error: {e}")
        else:
            st.info("📭 No active tables")

    except Exception as e:
        st.error(f"Error: {e}")

# ============================================================================
# TAB 4: MENU MANAGER  (unchanged)
# ============================================================================

with tab4:
    st.header("🍽️ Menu Manager")
    st.caption("Changes take effect immediately — the bot reads live DB data on every request.")
    st.markdown("---")

    try:
        menu_res   = supabase.table("menu_items").select("id, content") \
            .eq("restaurant_id", cur_rid).execute()
        menu_items = menu_res.data or []
    except Exception as e:
        st.error(f"Error loading menu: {e}")
        menu_items = []

    with st.expander("➕ Add New Menu Item", expanded=False):
        with st.form("add_item", clear_on_submit=True):
            c1, c2 = st.columns(2)
            n_cat   = c1.text_input("Category *", placeholder="Starters")
            n_name  = c2.text_input("Item Name *", placeholder="Full Stack Burger")
            d1, d2  = st.columns(2)
            n_price = d1.text_input("Price *", placeholder="$18")
            n_desc  = d2.text_input("Description", placeholder="Double beef patty…")
            if st.form_submit_button("➕ Add Item", type="primary", use_container_width=True):
                if not all([n_cat.strip(), n_name.strip(), n_price.strip()]):
                    st.error("Category, Item Name, and Price are required.")
                else:
                    pstr = n_price.strip()
                    if not pstr.startswith("$"):
                        pstr = f"${pstr}"
                    try:
                        supabase.table("menu_items").insert({
                            "restaurant_id": cur_rid,
                            "content": build_menu_content(n_cat, n_name, pstr, n_desc),
                        }).execute()
                        st.success(f"✅ Added: {n_name.strip()} ({pstr})")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

    st.markdown("---")
    st.subheader(f"📋 Current Menu ({len(menu_items)} items)")

    if not menu_items:
        st.info("No items yet. Add some above.")
    else:
        grouped: dict = {}
        for row in menu_items:
            p   = parse_menu_content(row["content"])
            cat = p["category"] or "Uncategorised"
            grouped.setdefault(cat, []).append({"id": row["id"], "p": p})

        for cat, items in sorted(grouped.items()):
            st.markdown(f"#### {cat.upper()}")
            for entry in items:
                rid_row = entry["id"]
                p       = entry["p"]
                ekey    = f"edit_{rid_row}"
                if ekey not in st.session_state:
                    st.session_state[ekey] = False

                with st.container(border=True):
                    dc1, dc2, dc3, dc4 = st.columns([3, 1.5, 1, 1])
                    dc1.write(f"**{p['item']}**")
                    if p["description"]:
                        dc1.caption(p["description"])
                    dc2.write(f"💰 {p['price']}")

                    if dc3.button("✏️ Edit", key=f"eb_{rid_row}", use_container_width=True):
                        st.session_state[ekey] = not st.session_state[ekey]
                    if dc4.button("🗑️ Delete", key=f"db_{rid_row}", use_container_width=True):
                        try:
                            supabase.table("menu_items").delete().eq("id", rid_row).execute()
                            st.success(f"Deleted: {p['item']}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error: {e}")

                    if st.session_state.get(ekey):
                        with st.form(f"ef_{rid_row}"):
                            ec1, ec2 = st.columns(2)
                            e_cat   = ec1.text_input("Category", value=p["category"])
                            e_name  = ec2.text_input("Item Name", value=p["item"])
                            fc1, fc2 = st.columns(2)
                            e_price = fc1.text_input("Price", value=p["price"])
                            e_desc  = fc2.text_input("Description", value=p["description"])
                            if st.form_submit_button("💾 Save", type="primary", use_container_width=True):
                                if not all([e_cat.strip(), e_name.strip(), e_price.strip()]):
                                    st.error("Category, Item Name, Price required.")
                                else:
                                    pv = e_price.strip()
                                    if not pv.startswith("$"):
                                        pv = f"${pv}"
                                    try:
                                        supabase.table("menu_items").update({
                                            "content": build_menu_content(e_cat, e_name, pv, e_desc)
                                        }).eq("id", rid_row).execute()
                                        st.success(f"✅ Updated: {e_name.strip()}")
                                        st.session_state[ekey] = False
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Error: {e}")
                            st.form_submit_button("✖ Cancel", use_container_width=True)
            st.markdown("---")

    with st.expander("📥 Bulk Import", expanded=False):
        st.caption("One block per item, separated by a blank line.\n\n"
                   "Format:\n```\ncategory: Starters\nitem: Binary Bites\n"
                   "price: $8\ndescription: Crispy jalapeño poppers\n```")
        bulk = st.text_area("Paste here:", height=200)
        if st.button("📥 Import"):
            if not bulk.strip():
                st.warning("Nothing to import.")
            else:
                blocks  = re.split(r"\n\s*\n", bulk.strip())
                done, fail = 0, 0
                for block in blocks:
                    if not block.strip():
                        continue
                    try:
                        supabase.table("menu_items").insert({
                            "restaurant_id": cur_rid, "content": block.strip(),
                        }).execute()
                        done += 1
                    except Exception as e:
                        fail += 1
                        st.warning(f"Block failed: {e}")
                if done:
                    st.success(f"✅ Imported {done} item(s)")
                if fail:
                    st.error(f"❌ {fail} block(s) failed")
                if done:
                    st.rerun()

# ============================================================================
# TAB 5: POLICIES & AI CONTEXT  (NEW in v5)
# ============================================================================

with tab5:
    st.header("ℹ️ Policies & AI Context")
    st.caption(
        "This text is injected into the AI's system prompt in **General Mode** so the bot "
        "can accurately answer questions about WiFi, parking, hours, allergens, and policies "
        "for this specific location. Changes take effect on the next user message — no restart needed."
    )
    st.markdown("---")

    # Load existing policy for this restaurant
    existing_policy = ""
    policy_row_id   = None
    try:
        pol_res = supabase.table("restaurant_policies") \
            .select("id, policy_text") \
            .eq("restaurant_id", cur_rid) \
            .limit(1).execute()
        if pol_res.data:
            existing_policy = pol_res.data[0].get("policy_text", "")
            policy_row_id   = pol_res.data[0].get("id")
    except Exception as e:
        st.warning(f"Could not load existing policy: {e}")

    # Policy editor
    col_main, col_tips = st.columns([2, 1])

    with col_main:
        st.subheader(f"📝 {sel_name} — Policy Text")
        policy_draft = st.text_area(
            "Restaurant info & policies:",
            value=existing_policy,
            height=350,
            placeholder=(
                "WiFi password: TechBites2025\n"
                "Parking: Free on-site parking available Sunday–Thursday\n"
                "Hours: Mon–Sat 8am–11pm, Fri–Sat 8am–12am\n"
                "Wheelchair accessible: Yes, full access including restrooms\n"
                "Vegan options: Yes — marked with 🌱 on the menu\n"
                "Allergen info: Ask staff for our allergen matrix\n"
                "Kids menu: Available for children under 12\n"
                "Reservations: Walk-ins welcome, bookings recommended for groups of 6+"
            ),
            key="policy_editor",
        )

        save_col, clear_col = st.columns([3, 1])

        with save_col:
            if st.button("💾 Save Policy", type="primary", use_container_width=True):
                try:
                    # Upsert: update if exists, insert if new
                    supabase.table("restaurant_policies").upsert({
                        "restaurant_id": cur_rid,
                        "policy_text":   policy_draft.strip(),
                        "updated_at":    datetime.now(DUBAI_TZ).isoformat(),
                    }, on_conflict="restaurant_id").execute()
                    st.success("✅ Policy saved! The AI will use this on the next user message.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error saving policy: {e}")

        with clear_col:
            if st.button("🗑️ Clear", use_container_width=True):
                try:
                    supabase.table("restaurant_policies").upsert({
                        "restaurant_id": cur_rid,
                        "policy_text":   "",
                        "updated_at":    datetime.now(DUBAI_TZ).isoformat(),
                    }, on_conflict="restaurant_id").execute()
                    st.success("Policy cleared.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

    with col_tips:
        st.subheader("💡 What to include")
        st.markdown("""
**Essentials the AI needs to know:**

🔑 **WiFi**
- Password, network name

🅿️ **Parking**
- Free/paid, hours, location

♿ **Accessibility**
- Wheelchair access, lifts, restrooms

🕐 **Opening Hours**
- Days + times, holiday hours

🌿 **Dietary**
- Vegan, halal, gluten-free options

🍼 **Kids**
- Kids menu, highchairs

📋 **Reservations**
- Walk-in policy, group sizes

💳 **Payments**
- Cash/card/online, service charge

📞 **Contact**
- Phone, address, email
        """)

        if existing_policy:
            word_count = len(existing_policy.split())
            st.metric("Words saved", word_count)
            st.caption(f"Policy last loaded at {get_ts()}")
        else:
            st.info("No policy saved yet for this location.")

    # Preview how it will appear to the AI
    if existing_policy:
        with st.expander("🔍 Preview AI System Prompt Injection", expanded=False):
            st.caption("This is exactly what gets injected into the AI's context:")
            st.code(
                f"RESTAURANT INFO (WiFi, parking, policies, hours):\n{existing_policy}",
                language=None,
            )

# ============================================================================
# FOOTER
# ============================================================================

st.markdown("---")
st.caption(f"🔄 Auto-refresh active • {get_ts()}")