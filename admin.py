"""
Restaurant Admin Dashboard — Streamlit  v4
===========================================
Changes from v3:
  • KDS Tab: Modification card shows "Order #ID — Table X requested to remove Y"
  • Approve-modification path commits items + price atomically in a single UPDATE,
    then calls st.rerun() immediately — no stale intermediate state.
  • If remaining_items is empty after approval → order is marked status='cancelled'
    so it drops from Live Tables on the next refresh automatically.
  • Live Tables: dynamically sums price from DB rows; no local state caching.
    All stale-render risk eliminated by always fetching fresh data on every render.
  • Menu Manager tab unchanged from v3.
  • Timezone helpers (to_dubai) unchanged.
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
# DB CONNECTION
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
# SIDEBAR
# ============================================================================

st.sidebar.title("🏢 Restaurant Manager")

try:
    rests = supabase.table("restaurants").select("id, name").execute()
    if not rests.data:
        st.error("No restaurants found")
        st.stop()
    rest_opts = {r["name"]: r["id"] for r in rests.data}
    sel_name  = st.sidebar.selectbox("Select Location", list(rest_opts.keys()),
                                      key="rest_selector")
    cur_rid   = rest_opts[sel_name]
    st.sidebar.success(f"📍 {sel_name}")
    st.sidebar.info(f"🔄 {get_ts()}")
except Exception as e:
    st.error(f"Error loading restaurants: {e}")
    st.stop()

# ============================================================================
# TABS
# ============================================================================

st.title(f"📊 Dashboard: {sel_name}")
st.markdown("---")

tab1, tab2, tab3, tab4 = st.tabs([
    "📅 Bookings",
    "👨‍🍳 Kitchen Display",
    "💰 Live Tables & Billing",
    "🍽️ Menu Manager",
])

# ============================================================================
# TAB 1: BOOKINGS
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
# TAB 2: KITCHEN DISPLAY SYSTEM
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

                    # ── Header ───────────────────────────────────────────────
                    h1, h2, h3 = st.columns([2, 1, 1])
                    h1.markdown(f"### 🪑 Table {order['table_number']}  —  Order *#{oid}*")
                    h2.markdown(f"**{order['customer_name']}**")

                    try:
                        created_utc  = datetime.fromisoformat(
                            order["created_at"].replace("Z", "+00:00"))
                        now_utc      = datetime.now(timezone.utc)
                        mins         = max(0, int((now_utc - created_utc).total_seconds() / 60))
                        wall         = to_dubai(created_utc).strftime("%I:%M %p")
                        label        = "Just now" if mins == 0 else (
                            f"{mins} min ago" if mins < 60 else
                            f"{mins//60}h {mins%60}m ago"
                        )
                        h3.caption(f"⏱️ {label}  ({wall})")
                    except Exception:
                        h3.caption("⏱️ Just now")

                    st.write(f"🍽️ {order['items']}")
                    st.write(f"💰 {fmt(order['price'])}")
                    st.markdown("---")

                    # ── Priority 1: MODIFICATION REQUEST ─────────────────────
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
                            f"Table **{order['table_number']}** wants to remove: "
                            f"**{removed}**\n\n"
                            + (f"Remaining if approved: _{remaining}_\n"
                               f"New total if approved: **{fmt(new_price)}**"
                               if not all_gone else
                               "_All items removed — this will cancel the order._")
                        )

                        mc1, mc2 = st.columns(2)

                        with mc1:
                            if st.button("✅ Approve Change",
                                         key=f"amod_{oid}",
                                         use_container_width=True,
                                         type="primary"):
                                try:
                                    if all_gone:
                                        # All items removed → cancel the order
                                        supabase.table("orders").update({
                                            "status":               "cancelled",
                                            "cancellation_status":  "approved",
                                            "modification_status":  "approved",
                                            "pending_modification": None,
                                        }).eq("id", oid).execute()
                                        tg_msg = (
                                            f"🗑️ *Order #{oid} Cancelled*\n"
                                            f"Kitchen approved your removal request — "
                                            f"all items removed."
                                        )
                                    else:
                                        # Partial removal — update in a SINGLE atomic UPDATE
                                        supabase.table("orders").update({
                                            "items":                remaining,
                                            "price":                new_price,
                                            "modification_status":  "approved",
                                            "pending_modification": None,
                                        }).eq("id", oid).execute()
                                        tg_msg = (
                                            f"✅ *Kitchen approved your change — Order #{oid}*\n\n"
                                            f"Updated order: {remaining}\n"
                                            f"New total: {fmt(new_price)}"
                                        )

                                    if order.get("chat_id"):
                                        send_telegram_message(order["chat_id"], tg_msg)

                                    st.success("✅ Modification approved")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error: {e}")

                        with mc2:
                            if st.button("❌ Reject Change",
                                         key=f"rmod_{oid}",
                                         use_container_width=True):
                                try:
                                    supabase.table("orders").update({
                                        "modification_status":  "rejected",
                                        "pending_modification": None,
                                    }).eq("id", oid).execute()

                                    if order.get("chat_id"):
                                        send_telegram_message(
                                            order["chat_id"],
                                            f"❌ *Kitchen rejected your change — Order #{oid}*\n\n"
                                            f"Your original order stands: {order['items']}\n"
                                            f"Food is already being prepared.",
                                        )

                                    st.success("Modification rejected — original kept")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error: {e}")

                        # Don't show cancel/ready while mod is pending
                        continue

                    # ── Priority 2: CANCELLATION REQUEST ─────────────────────
                    if order.get("cancellation_status") == "requested":
                        st.warning(f"⚠️ **CANCELLATION REQUESTED — Order #{oid}**")

                        cc1, cc2 = st.columns(2)
                        with cc1:
                            if st.button("✅ Approve Cancel",
                                         key=f"acan_{oid}",
                                         use_container_width=True,
                                         type="primary"):
                                try:
                                    supabase.table("orders").update({
                                        "status":              "cancelled",
                                        "cancellation_status": "approved",
                                    }).eq("id", oid).execute()
                                    if order.get("chat_id"):
                                        send_telegram_message(
                                            order["chat_id"],
                                            f"✅ *Order #{oid} cancelled* — request approved."
                                        )
                                    st.success("Cancellation approved")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error: {e}")

                        with cc2:
                            if st.button("❌ Reject Cancel",
                                         key=f"rcan_{oid}",
                                         use_container_width=True):
                                try:
                                    supabase.table("orders").update({
                                        "cancellation_status": "rejected"
                                    }).eq("id", oid).execute()
                                    if order.get("chat_id"):
                                        send_telegram_message(
                                            order["chat_id"],
                                            f"❌ *Cancellation rejected — Order #{oid}.*\n"
                                            f"Kitchen is preparing your food.",
                                        )
                                    st.success("Cancellation rejected")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error: {e}")

                    else:
                        # ── Normal: Mark as Ready ────────────────────────────
                        if st.button("✅ Mark as Ready",
                                     key=f"ready_{oid}",
                                     use_container_width=True,
                                     type="primary"):
                            try:
                                supabase.table("orders") \
                                    .update({"status": "completed"}) \
                                    .eq("id", oid).execute()
                                if order.get("chat_id"):
                                    send_telegram_message(
                                        order["chat_id"],
                                        f"🍽️ *Order #{oid} is ready!* "
                                        f"(Table {order['table_number']})",
                                    )
                                st.success(f"✅ Order #{oid} marked ready")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error: {e}")
        else:
            st.success("🎉 Kitchen clear — no pending orders!")

    except Exception as e:
        st.error(f"Error loading orders: {e}")

# ============================================================================
# TAB 3: LIVE TABLES & BILLING
# ============================================================================

with tab3:
    st.header("💰 Live Tables & Billing")
    st.caption(
        "Totals are calculated fresh from the database on every 5-second refresh. "
        "Approved modifications and cancellations appear immediately on the next cycle."
    )

    if st.button("🔄 Refresh Now", use_container_width=False):
        st.rerun()

    st.markdown("---")

    try:
        # Always query live — never trust any local variable from previous renders.
        # Filter: status NOT IN ('paid', 'cancelled').
        # After KDS approves a modification, the row's price column is already
        # updated in DB, so the next refresh here shows the correct lower total.
        # After KDS approves a full-cancel, status='cancelled' so the row is
        # excluded automatically.
        live_res = supabase.table("orders").select("*") \
            .eq("restaurant_id", cur_rid) \
            .neq("status", "paid") \
            .neq("status", "cancelled") \
            .execute()
        live_orders = live_res.data

        if live_orders:
            tables: dict = {}

            for o in live_orders:
                tnum = o["table_number"]
                if tnum not in tables:
                    tables[tnum] = {
                        "orders":     [],
                        "total":      0.0,
                        "dish_names": set(),
                        "chat_id":    o.get("chat_id"),
                        "order_ids":  [],
                    }
                price = float(o["price"])
                tables[tnum]["orders"].append(o)
                tables[tnum]["total"]      += price
                tables[tnum]["order_ids"].append(o["id"])

                for item in o["items"].split(","):
                    clean = item.split("(")[0].strip()
                    if clean:
                        tables[tnum]["dish_names"].add(clean)

            # Round totals once after summing to avoid float drift
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
                        mod_badge = (
                            " ⚠️ _mod pending_"
                            if o.get("modification_status") == "requested" else ""
                        )
                        st.write(f"  • *#{o['id']}* {o['items']} — {fmt(float(o['price']))}{mod_badge}")

                    st.markdown("---")

                    if st.button("💳 Close Table & Request Payment",
                                 key=f"pay_{tnum}",
                                 use_container_width=True,
                                 type="primary"):
                        try:
                            for oid in data["order_ids"]:
                                supabase.table("orders") \
                                    .update({"status": "paid"}).eq("id", oid).execute()

                            dishes     = "\n".join(f"• {d}" for d in sorted(data["dish_names"]))
                            fb_msg     = (
                                f"✅ *Payment Received — Thank You!*\n\n"
                                f"💰 Total: {fmt(data['total'])}\n\n"
                                f"⭐ *Please rate your experience (1-5):*\n\n"
                                f"{dishes}\n\n"
                                f"Reply: 5, 4, 5  _(per dish + overall)_"
                            )
                            if data["chat_id"]:
                                ok = send_telegram_message(data["chat_id"], fb_msg)
                                msg = (f"✅ Table {tnum} closed & feedback sent!"
                                       if ok else f"✅ Table {tnum} closed (feedback not sent)")
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
# TAB 4: MENU MANAGER  (unchanged from v3)
# ============================================================================

with tab4:
    st.header("🍽️ Menu Manager")
    st.caption("Changes take effect immediately — the bot fetches live DB data on every request.")
    st.markdown("---")

    try:
        menu_res   = supabase.table("menu_items").select("id, content") \
            .eq("restaurant_id", cur_rid).execute()
        menu_items = menu_res.data or []
    except Exception as e:
        st.error(f"Error loading menu: {e}")
        menu_items = []

    # ── Add New Item ──────────────────────────────────────────────────────────
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
                            "content":       build_menu_content(n_cat, n_name, pstr, n_desc),
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
                            sv, cl  = st.columns(2)
                            if sv.form_submit_button("💾 Save", type="primary",
                                                      use_container_width=True):
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
                            cl.form_submit_button("✖ Cancel", use_container_width=True)

            st.markdown("---")

    with st.expander("📥 Bulk Import", expanded=False):
        st.caption("One block per item, blocks separated by a blank line.\n\n"
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
                            "restaurant_id": cur_rid,
                            "content":       block.strip(),
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
# FOOTER
# ============================================================================

st.markdown("---")
st.caption(f"🔄 Auto-refresh active • {get_ts()}")