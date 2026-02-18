"""
Restaurant Admin Dashboard — Streamlit  v6
===========================================
New in v6:
  • Tab 6 "👥 Customer Insights" — CRM table with tags, spend, visit count,
    churn risk, and preferences; filterable by tag; summary metrics.
  • Tab 7 "🪑 Table Inventory" — View and edit tables_inventory per restaurant.
  • "Close Table" button now calls order_service.update_crm_on_payment()
    for every unique user in the closing set, so CRM stats stay current.
  • Sidebar: restaurant creation + ID display unchanged from v5.
  • Tabs 1-5 (Bookings, KDS, Live Tables, Menu Manager, Policies) unchanged.
"""

import json, re, os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
import requests
from streamlit_autorefresh import st_autorefresh
from dotenv import load_dotenv
from supabase import create_client

# Import CRM payment updater (order_service must be on PYTHONPATH)
import sys
sys.path.insert(0, os.path.dirname(__file__))
from order_service import update_crm_on_payment

DUBAI_TZ = ZoneInfo("Asia/Dubai")

def to_dubai(utc_dt):
    if utc_dt.tzinfo is None: utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(DUBAI_TZ)

# ── Page config ────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Restaurant Admin", layout="wide",
                   page_icon="👨‍🍳", initial_sidebar_state="expanded")
st_autorefresh(interval=5000, key="global_refresh")
load_dotenv()

try:
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
except Exception as ex:
    st.error(f"❌ DB error: {ex}"); st.stop()

# ── Helpers ────────────────────────────────────────────────────────────────────

def send_telegram(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or not chat_id: return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id":chat_id,"text":text,"parse_mode":"Markdown"}, timeout=5)
        return r.status_code == 200
    except Exception as ex:
        print(f"[TG] {ex}"); return False

def fmt(x): return f"${float(x):.2f}"
def get_ts(): return datetime.now(DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")

def compute_tags(row):
    tags = []
    vc = int(row.get("visit_count") or 0)
    ts = float(row.get("total_spend") or 0.0)
    lv = row.get("last_visit")
    if vc > 5:  tags.append("Frequent Diner")
    if ts > 500: tags.append("Big Spender")
    if "Frequent Diner" in tags and "Big Spender" in tags: tags.append("VIP")
    if lv and vc > 0:
        try:
            lv_dt = datetime.fromisoformat(str(lv).replace("Z","+00:00"))
            if (datetime.now(timezone.utc) - lv_dt) > timedelta(days=30):
                tags.append("Churn Risk")
        except Exception: pass
    return tags

def build_menu_content(cat, item, price, desc):
    lines = [f"category: {cat.strip()}", f"item: {item.strip()}", f"price: {price.strip()}"]
    if desc.strip(): lines.append(f"description: {desc.strip()}")
    return "\n".join(lines)

def parse_menu_content(content):
    r = {"category":"","item":"","price":"","description":""}
    for line in content.split("\n"):
        line = line.strip()
        for f in ("category","item","price","description"):
            if line.startswith(f+":"): r[f] = line.replace(f+":","").strip()
    return r

# ── Sidebar ────────────────────────────────────────────────────────────────────

st.sidebar.title("🏢 Restaurant Manager")
_ADD = "➕ Add New Restaurant"
try:
    rests = supabase.table("restaurants").select("id,name").execute()
    rest_rows = rests.data or []
except Exception as ex:
    st.error(f"Error: {ex}"); st.stop()

name_to_id = {r["name"]: r["id"] for r in rest_rows}
opts = list(name_to_id.keys()) + [_ADD]
sel_name = st.sidebar.selectbox("Select Location", opts, key="rest_selector")

if sel_name == _ADD:
    st.sidebar.markdown("---")
    st.sidebar.subheader("Create New Location")
    with st.sidebar.form("new_rest"):
        new_name = st.text_input("Restaurant Name *", placeholder="Tech Bites Marina")
        if st.form_submit_button("💾 Create", type="primary", use_container_width=True):
            if not new_name.strip(): st.error("Enter a name.")
            else:
                try:
                    res = supabase.table("restaurants").insert({"name":new_name.strip()}).execute()
                    nid = res.data[0]["id"]
                    st.success(f"✅ Created: **{new_name.strip()}**")
                    st.info(f"**ID:** `{nid}`\n\nBot: `/start rest_id={nid}`")
                    st.rerun()
                except Exception as ex: st.error(f"Error: {ex}")
    st.stop()

cur_rid = name_to_id[sel_name]
st.sidebar.success(f"📍 {sel_name}")
st.sidebar.markdown("**Restaurant ID:**")
st.sidebar.code(cur_rid, language=None)
st.sidebar.caption(f"Bot: `/start rest_id={cur_rid}`")
st.sidebar.info(f"🔄 {get_ts()}")

# ── Tabs ───────────────────────────────────────────────────────────────────────

st.title(f"📊 Dashboard: {sel_name}")
st.markdown("---")

tab1,tab2,tab3,tab4,tab5,tab6,tab7 = st.tabs([
    "📅 Bookings","👨‍🍳 Kitchen Display","💰 Live Tables",
    "🍽️ Menu Manager","ℹ️ Policies & Settings",
    "👥 Customer Insights","🪑 Table Inventory",
])

# ── TAB 1: Bookings ────────────────────────────────────────────────────────────

with tab1:
    st.header("📅 Reservations & Bookings")
    c1,c2,_,_ = st.columns(4)
    with c1:
        if st.button("🔄 Refresh", use_container_width=True): st.rerun()
    with c2:
        if st.button("🗑️ Purge Cancelled", use_container_width=True, type="secondary"):
            try:
                supabase.table("bookings").delete().eq("status","cancelled").eq("restaurant_id",cur_rid).execute()
                st.toast("✅ Purged"); st.rerun()
            except Exception as ex: st.error(f"{ex}")
    st.markdown("---")
    try:
        bks = supabase.table("bookings").select("*").eq("restaurant_id",cur_rid).order("booking_time").execute().data
        if bks:
            cc1,cc2,cc3 = st.columns(3)
            cc1.metric("Total",len(bks)); cc2.metric("Confirmed",sum(1 for b in bks if b["status"]=="confirmed"))
            cc3.metric("Cancelled",sum(1 for b in bks if b["status"]=="cancelled"))
            st.markdown("---")
            with st.form("bulk_cancel"):
                st.subheader("📋 Booking List"); sel = []
                for b in bks:
                    cols = st.columns([0.5,2,1.5,1.5,1])
                    if cols[0].checkbox("",key=f"bc_{b['id']}",label_visibility="collapsed"): sel.append(b["id"])
                    cols[1].write(f"**{b['customer_name']}**"); cols[2].write(f"👥 {b['party_size']} guests")
                    try:
                        bdt = datetime.fromisoformat(b["booking_time"].replace("Z","+00:00"))
                        ts  = to_dubai(bdt).strftime("%b %d, %I:%M %p (Dubai)")
                    except Exception: ts = b["booking_time"]
                    cols[3].write(f"📅 {ts}")
                    s = b["status"]
                    if s=="confirmed": cols[4].success("✅")
                    elif s=="cancelled": cols[4].error("❌")
                    else: cols[4].info(s)
                    st.divider()
                if st.form_submit_button("❌ Cancel Selected", type="primary", use_container_width=True):
                    if sel:
                        for bid in sel: supabase.table("bookings").update({"status":"cancelled"}).eq("id",bid).execute()
                        st.success(f"✅ Cancelled {len(sel)}"); st.rerun()
                    else: st.warning("None selected")
        else: st.info("📭 No bookings")
    except Exception as ex: st.error(f"{ex}")

# ── TAB 2: Kitchen Display ─────────────────────────────────────────────────────

with tab2:
    st.header("🔥 Kitchen Display System")
    st_autorefresh(interval=3000, key="kds_refresh")
    try:
        orders = supabase.table("orders").select("*").eq("restaurant_id",cur_rid)\
            .eq("status","pending").order("created_at").execute().data
        if orders:
            st.info(f"📋 {len(orders)} order(s) in queue"); st.markdown("---")
            for order in orders:
                oid = order["id"]
                with st.container(border=True):
                    h1,h2,h3 = st.columns([2,1,1])
                    h1.markdown(f"### 🪑 Table {order['table_number']}  —  Order *#{oid}*")
                    h2.markdown(f"**{order['customer_name']}**")
                    try:
                        cu = datetime.fromisoformat(order["created_at"].replace("Z","+00:00"))
                        mins = max(0,int((datetime.now(timezone.utc)-cu).total_seconds()/60))
                        lbl = "Just now" if mins==0 else (f"{mins}m ago" if mins<60 else f"{mins//60}h {mins%60}m ago")
                        h3.caption(f"⏱️ {lbl}  ({to_dubai(cu).strftime('%I:%M %p')})")
                    except Exception: h3.caption("⏱️ Just now")
                    st.write(f"🍽️ {order['items']}"); st.write(f"💰 {fmt(order['price'])}"); st.markdown("---")

                    mod_status = order.get("modification_status","none")
                    pending_blob = order.get("pending_modification")
                    if mod_status=="requested" and pending_blob:
                        try: pending = json.loads(pending_blob)
                        except Exception: pending = {}
                        removed=pending.get("removed_items","item(s)"); remaining=pending.get("remaining_items","")
                        new_price=float(pending.get("new_price",0.0)); all_gone=not remaining.strip()
                        st.warning(f"✏️ **MOD REQUEST — Order #{oid}**\n\nTable **{order['table_number']}** remove: **{removed}**\n\n"
                            +(f"Remaining: _{remaining}_\nNew total: **{fmt(new_price)}**" if not all_gone else "_All items — will cancel._"))
                        mc1,mc2=st.columns(2)
                        with mc1:
                            if st.button("✅ Approve",key=f"amod_{oid}",use_container_width=True,type="primary"):
                                try:
                                    if all_gone:
                                        supabase.table("orders").update({"status":"cancelled","cancellation_status":"approved",
                                            "modification_status":"approved","pending_modification":None}).eq("id",oid).execute()
                                        msg=f"🗑️ *Order #{oid} Cancelled* — all items removed."
                                    else:
                                        supabase.table("orders").update({"items":remaining,"price":new_price,
                                            "modification_status":"approved","pending_modification":None}).eq("id",oid).execute()
                                        msg=f"✅ *Change approved — Order #{oid}*\n{remaining}\nNew total: {fmt(new_price)}"
                                    if order.get("chat_id"): send_telegram(order["chat_id"],msg)
                                    st.success("✅ Approved"); st.rerun()
                                except Exception as ex: st.error(f"{ex}")
                        with mc2:
                            if st.button("❌ Reject",key=f"rmod_{oid}",use_container_width=True):
                                try:
                                    supabase.table("orders").update({"modification_status":"rejected","pending_modification":None}).eq("id",oid).execute()
                                    if order.get("chat_id"): send_telegram(order["chat_id"],f"❌ *Change rejected — Order #{oid}*\nOriginal: {order['items']}")
                                    st.success("Rejected"); st.rerun()
                                except Exception as ex: st.error(f"{ex}")
                        continue

                    if order.get("cancellation_status")=="requested":
                        st.warning(f"⚠️ **CANCELLATION — Order #{oid}**")
                        cc1,cc2=st.columns(2)
                        with cc1:
                            if st.button("✅ Approve Cancel",key=f"acan_{oid}",use_container_width=True,type="primary"):
                                try:
                                    supabase.table("orders").update({"status":"cancelled","cancellation_status":"approved"}).eq("id",oid).execute()
                                    if order.get("chat_id"): send_telegram(order["chat_id"],f"✅ *Order #{oid} cancelled* — approved.")
                                    st.success("Cancelled"); st.rerun()
                                except Exception as ex: st.error(f"{ex}")
                        with cc2:
                            if st.button("❌ Reject",key=f"rcan_{oid}",use_container_width=True):
                                try:
                                    supabase.table("orders").update({"cancellation_status":"rejected"}).eq("id",oid).execute()
                                    if order.get("chat_id"): send_telegram(order["chat_id"],f"❌ *Cancellation rejected — Order #{oid}.*")
                                    st.success("Rejected"); st.rerun()
                                except Exception as ex: st.error(f"{ex}")
                    else:
                        if st.button("✅ Mark Ready",key=f"ready_{oid}",use_container_width=True,type="primary"):
                            try:
                                supabase.table("orders").update({"status":"completed"}).eq("id",oid).execute()
                                if order.get("chat_id"): send_telegram(order["chat_id"],f"🍽️ *Order #{oid} ready!* (Table {order['table_number']})")
                                st.success(f"✅ Ready"); st.rerun()
                            except Exception as ex: st.error(f"{ex}")
        else: st.success("🎉 Kitchen clear!")
    except Exception as ex: st.error(f"{ex}")

# ── TAB 3: Live Tables ─────────────────────────────────────────────────────────

with tab3:
    st.header("💰 Live Tables & Billing")
    st.caption("Fresh from DB on every 5-second refresh. Approved mods appear instantly.")
    if st.button("🔄 Refresh Now"): st.rerun()
    st.markdown("---")
    try:
        live = supabase.table("orders").select("*").eq("restaurant_id",cur_rid)\
            .neq("status","paid").neq("status","cancelled").execute().data
        if live:
            tables = {}
            for o in live:
                tn = o["table_number"]
                if tn not in tables:
                    tables[tn] = {"orders":[],"total":0.0,"dish_names":set(),
                                  "chat_id":o.get("chat_id"),"order_ids":[],"user_ids":set()}
                tables[tn]["orders"].append(o); tables[tn]["total"] += float(o["price"])
                tables[tn]["order_ids"].append(o["id"]); tables[tn]["user_ids"].add(o.get("user_id",""))
                for item in o["items"].split(","):
                    c = item.split("(")[0].strip()
                    if c: tables[tn]["dish_names"].add(c)
            for t in tables.values(): t["total"] = round(t["total"],2)
            st.info(f"🪑 {len(tables)} active table(s)"); st.markdown("---")
            for tn, data in sorted(tables.items()):
                with st.container(border=True):
                    tc1,tc2 = st.columns([3,1])
                    tc1.markdown(f"### 🪑 Table {tn}"); tc2.markdown(f"### {fmt(data['total'])}")
                    st.markdown("---"); st.markdown("**Orders:**")
                    for o in data["orders"]:
                        badge = " ⚠️ _mod pending_" if o.get("modification_status")=="requested" else ""
                        st.write(f"  • *#{o['id']}* {o['items']} — {fmt(float(o['price']))}{badge}")
                    st.markdown("---")
                    if st.button("💳 Close Table & Payment",key=f"pay_{tn}",use_container_width=True,type="primary"):
                        try:
                            for oid in data["order_ids"]:
                                supabase.table("orders").update({"status":"paid"}).eq("id",oid).execute()
                            # CRM: update each unique user's spend and visit count
                            # Compute per-user spend from this close
                            user_spend: dict = {}
                            for o in data["orders"]:
                                uid = o.get("user_id","")
                                if uid: user_spend[uid] = user_spend.get(uid,0.0) + float(o["price"])
                            for uid, amt in user_spend.items():
                                if uid: update_crm_on_payment(uid, amt)
                            dishes = "\n".join(f"• {d}" for d in sorted(data["dish_names"]))
                            fb_msg = (f"✅ *Payment Received!*\n\n💰 Total: {fmt(data['total'])}\n\n"
                                      f"⭐ *Please rate (1-5):*\n\n{dishes}\n\nReply: 5,4,5 _(per dish+overall)_")
                            ok = send_telegram(data["chat_id"], fb_msg) if data["chat_id"] else False
                            st.success(f"✅ Table {tn} closed" + (" & feedback sent" if ok else "")); st.rerun()
                        except Exception as ex: st.error(f"{ex}")
        else: st.info("📭 No active tables")
    except Exception as ex: st.error(f"{ex}")

# ── TAB 4: Menu Manager ────────────────────────────────────────────────────────

with tab4:
    st.header("🍽️ Menu Manager")
    st.caption("Changes take effect immediately.")
    st.markdown("---")
    try:
        menu_items = supabase.table("menu_items").select("id,content").eq("restaurant_id",cur_rid).execute().data or []
    except Exception as ex:
        st.error(f"{ex}"); menu_items = []

    with st.expander("➕ Add New Item", expanded=False):
        with st.form("add_item", clear_on_submit=True):
            c1,c2 = st.columns(2)
            n_cat=c1.text_input("Category *",placeholder="Starters"); n_name=c2.text_input("Item Name *",placeholder="Burger")
            d1,d2 = st.columns(2)
            n_price=d1.text_input("Price *",placeholder="$18"); n_desc=d2.text_input("Description")
            if st.form_submit_button("➕ Add", type="primary", use_container_width=True):
                if not all([n_cat.strip(),n_name.strip(),n_price.strip()]): st.error("Category, Name, Price required.")
                else:
                    pstr = n_price.strip() if n_price.strip().startswith("$") else f"${n_price.strip()}"
                    try:
                        supabase.table("menu_items").insert({"restaurant_id":cur_rid,"content":build_menu_content(n_cat,n_name,pstr,n_desc)}).execute()
                        st.success(f"✅ Added"); st.rerun()
                    except Exception as ex: st.error(f"{ex}")

    st.markdown("---"); st.subheader(f"📋 Menu ({len(menu_items)} items)")
    if not menu_items: st.info("No items yet.")
    else:
        grouped = {}
        for row in menu_items:
            p = parse_menu_content(row["content"]); cat = p["category"] or "Uncategorised"
            grouped.setdefault(cat,[]).append({"id":row["id"],"p":p})
        for cat, items in sorted(grouped.items()):
            st.markdown(f"#### {cat.upper()}")
            for entry in items:
                rid_row=entry["id"]; p=entry["p"]; ekey=f"edit_{rid_row}"
                if ekey not in st.session_state: st.session_state[ekey]=False
                with st.container(border=True):
                    dc1,dc2,dc3,dc4 = st.columns([3,1.5,1,1])
                    dc1.write(f"**{p['item']}**")
                    if p["description"]: dc1.caption(p["description"])
                    dc2.write(f"💰 {p['price']}")
                    if dc3.button("✏️",key=f"eb_{rid_row}",use_container_width=True): st.session_state[ekey]=not st.session_state[ekey]
                    if dc4.button("🗑️",key=f"db_{rid_row}",use_container_width=True):
                        try: supabase.table("menu_items").delete().eq("id",rid_row).execute(); st.rerun()
                        except Exception as ex: st.error(f"{ex}")
                    if st.session_state.get(ekey):
                        with st.form(f"ef_{rid_row}"):
                            ec1,ec2=st.columns(2)
                            e_cat=ec1.text_input("Category",value=p["category"]); e_name=ec2.text_input("Name",value=p["item"])
                            fc1,fc2=st.columns(2)
                            e_price=fc1.text_input("Price",value=p["price"]); e_desc=fc2.text_input("Desc",value=p["description"])
                            if st.form_submit_button("💾 Save",type="primary",use_container_width=True):
                                pv = e_price.strip() if e_price.strip().startswith("$") else f"${e_price.strip()}"
                                try:
                                    supabase.table("menu_items").update({"content":build_menu_content(e_cat,e_name,pv,e_desc)}).eq("id",rid_row).execute()
                                    st.session_state[ekey]=False; st.rerun()
                                except Exception as ex: st.error(f"{ex}")
                            st.form_submit_button("✖ Cancel",use_container_width=True)
            st.markdown("---")

    with st.expander("📥 Bulk Import"):
        bulk = st.text_area("One block per item, blank line between:",height=200)
        if st.button("📥 Import"):
            blocks = re.split(r"\n\s*\n", bulk.strip()); done=fail=0
            for block in blocks:
                if not block.strip(): continue
                try: supabase.table("menu_items").insert({"restaurant_id":cur_rid,"content":block.strip()}).execute(); done+=1
                except Exception as ex: fail+=1; st.warning(f"{ex}")
            if done: st.success(f"✅ {done} imported"); st.rerun()
            if fail: st.error(f"❌ {fail} failed")

# ── TAB 5: Policies ────────────────────────────────────────────────────────────

with tab5:
    st.header("ℹ️ Policies & AI Context")
    st.caption("Injected into AI system prompt in General Mode. Changes take effect immediately.")
    st.markdown("---")
    existing = ""; pol_id = None
    try:
        pol = supabase.table("restaurant_policies").select("id,policy_text").eq("restaurant_id",cur_rid).limit(1).execute()
        if pol.data: existing=pol.data[0].get("policy_text",""); pol_id=pol.data[0].get("id")
    except Exception as ex: st.warning(f"Could not load policy: {ex}")

    col_main, col_tips = st.columns([2,1])
    with col_main:
        st.subheader(f"📝 {sel_name} — Policy Text")
        draft = st.text_area("Restaurant info & policies:", value=existing, height=350,
            placeholder="WiFi: TechBites2025\nParking: Free on-site\nHours: 8am–11pm\nWheelchair: Yes\nVegan: Yes",
            key="policy_editor")
        sv,cl = st.columns([3,1])
        with sv:
            if st.button("💾 Save Policy",type="primary",use_container_width=True):
                try:
                    supabase.table("restaurant_policies").upsert({"restaurant_id":cur_rid,"policy_text":draft.strip(),
                        "updated_at":datetime.now(DUBAI_TZ).isoformat()},on_conflict="restaurant_id").execute()
                    st.success("✅ Policy saved!"); st.rerun()
                except Exception as ex: st.error(f"{ex}")
        with cl:
            if st.button("🗑️ Clear",use_container_width=True):
                try:
                    supabase.table("restaurant_policies").upsert({"restaurant_id":cur_rid,"policy_text":"",
                        "updated_at":datetime.now(DUBAI_TZ).isoformat()},on_conflict="restaurant_id").execute()
                    st.success("Cleared"); st.rerun()
                except Exception as ex: st.error(f"{ex}")
    with col_tips:
        st.subheader("💡 What to include")
        st.markdown("🔑 WiFi\n🅿️ Parking\n♿ Accessibility\n🕐 Hours\n🌿 Dietary\n💳 Payments\n📞 Contact")
        if existing: st.metric("Words",len(existing.split()))
    if existing:
        with st.expander("🔍 Preview AI Injection"):
            st.code(f"RESTAURANT INFO:\n{existing}", language=None)

# ── TAB 6: Customer Insights (NEW) ────────────────────────────────────────────

with tab6:
    st.header("👥 Customer Insights")
    st.caption("CRM data — tags computed live from visit_count, total_spend, and last_visit.")
    st.markdown("---")

    tag_filter = st.selectbox("Filter by tag:",
        ["All","Frequent Diner","Big Spender","VIP","Churn Risk","New / No Data"],
        key="tag_filter")

    try:
        users_res = supabase.table("users").select(
            "id,username,full_name,visit_count,total_spend,last_visit,preferences"
        ).execute()
        all_users = users_res.data or []
    except Exception as ex:
        st.error(f"Error loading users: {ex}"); all_users = []

    # Attach computed tags to each user row
    enriched = []
    for u in all_users:
        tags = compute_tags(u)
        enriched.append({**u, "tags": tags})

    # Summary metrics
    total_users  = len(enriched)
    churn_count  = sum(1 for u in enriched if "Churn Risk" in u["tags"])
    vip_count    = sum(1 for u in enriched if "VIP" in u["tags"])
    avg_spend    = (sum(float(u.get("total_spend") or 0) for u in enriched) / total_users
                   ) if total_users else 0

    m1,m2,m3,m4 = st.columns(4)
    m1.metric("👤 Total Customers", total_users)
    m2.metric("👑 VIP Guests",       vip_count)
    m3.metric("⚠️ Churn Risk",       churn_count)
    m4.metric("💰 Avg Spend",        fmt(avg_spend))
    st.markdown("---")

    # Apply filter
    if tag_filter == "All":
        display = enriched
    elif tag_filter == "New / No Data":
        display = [u for u in enriched if not u["tags"] and int(u.get("visit_count") or 0) == 0]
    else:
        display = [u for u in enriched if tag_filter in u["tags"]]

    st.write(f"**Showing {len(display)} customer(s)**")

    if not display:
        st.info("No customers match this filter.")
    else:
        for u in display:
            tags      = u["tags"]
            vc        = int(u.get("visit_count") or 0)
            ts        = float(u.get("total_spend") or 0.0)
            lv        = u.get("last_visit")
            prefs     = u.get("preferences") or ""
            name      = u.get("full_name") or u.get("username") or "Unknown"
            username  = u.get("username","")

            # Compute days since last visit
            days_since = None
            if lv:
                try:
                    lv_dt = datetime.fromisoformat(str(lv).replace("Z","+00:00"))
                    days_since = (datetime.now(timezone.utc) - lv_dt).days
                except Exception: pass

            tag_badges = "  ".join(f"`{t}`" for t in tags) if tags else "`New`"
            risk_icon  = "🔴" if "Churn Risk" in tags else ("👑" if "VIP" in tags else
                          ("🌟" if "Big Spender" in tags else ("😊" if "Frequent Diner" in tags else "⚪")))

            with st.container(border=True):
                h1, h2, h3 = st.columns([3, 2, 2])
                h1.markdown(f"{risk_icon} **{name}** (@{username})")
                h1.markdown(tag_badges)
                h2.metric("Visits",  vc)
                h3.metric("Total Spend", fmt(ts))

                detail_cols = st.columns([2, 2, 3])
                detail_cols[0].caption(
                    f"Last visit: {f'{days_since}d ago' if days_since is not None else 'Never'}"
                )
                detail_cols[1].caption(f"User ID: `{u['id'][:12]}...`")
                if prefs:
                    detail_cols[2].info(f"📋 Preferences: _{prefs}_")

    st.markdown("---")
    # Export hint
    st.caption("Tip: Use Supabase's built-in CSV export on the `users` table for a full data dump.")

# ── TAB 7: Table Inventory (NEW) ───────────────────────────────────────────────

with tab7:
    st.header("🪑 Table Inventory")
    st.caption("Define your physical table stock per restaurant. Used by the Smart Availability algorithm.")
    st.markdown("---")

    try:
        inv_data = supabase.table("tables_inventory").select("id,capacity,table_count") \
            .eq("restaurant_id", cur_rid).order("capacity").execute().data or []
    except Exception as ex:
        st.error(f"Error loading inventory: {ex}"); inv_data = []

    # Summary
    if inv_data:
        total_tables = sum(r["table_count"] for r in inv_data)
        total_seats  = sum(r["capacity"] * r["table_count"] for r in inv_data)
        ic1, ic2 = st.columns(2)
        ic1.metric("Total Tables", total_tables)
        ic2.metric("Total Seats",  total_seats)
        st.markdown("---")

    # Current inventory table
    st.subheader("Current Inventory")
    if not inv_data:
        st.info("No inventory configured yet. Add table types below.")
    else:
        for row in inv_data:
            c1,c2,c3,c4 = st.columns([2,2,2,1])
            c1.write(f"**{row['capacity']}-seater tables**")
            c2.write(f"Count: **{row['table_count']}**")
            c3.write(f"Seats: **{row['capacity'] * row['table_count']}**")
            if c4.button("🗑️", key=f"inv_del_{row['id']}", use_container_width=True):
                try:
                    supabase.table("tables_inventory").delete().eq("id", row["id"]).execute()
                    st.success("Deleted"); st.rerun()
                except Exception as ex: st.error(f"{ex}")
        st.markdown("---")

    # Add / update table type
    st.subheader("➕ Add / Update Table Type")
    st.caption("If a table type with this capacity already exists, its count will be updated.")
    with st.form("add_inv", clear_on_submit=True):
        ai1, ai2 = st.columns(2)
        new_cap   = ai1.number_input("Capacity (seats per table)", min_value=1, max_value=20, value=4, step=1)
        new_count = ai2.number_input("Number of tables of this type", min_value=1, max_value=50, value=3, step=1)
        if st.form_submit_button("💾 Save", type="primary", use_container_width=True):
            try:
                supabase.table("tables_inventory").upsert({
                    "restaurant_id": cur_rid,
                    "capacity":      int(new_cap),
                    "table_count":   int(new_count),
                }, on_conflict="restaurant_id,capacity").execute()
                st.success(f"✅ Saved: {int(new_count)}x {int(new_cap)}-seater tables"); st.rerun()
            except Exception as ex: st.error(f"Error: {ex}")

    # Algorithm preview
    if inv_data:
        with st.expander("🔍 Inventory Preview"):
            st.caption("How the bot's Smart Availability algorithm sees your tables:")
            preview_lines = []
            for row in inv_data:
                preview_lines.append(f"  {row['table_count']}x {row['capacity']}-seater")
            st.code("\n".join(preview_lines), language=None)
            st.caption("Party sizing example: a party of 6 could use one 4-top + one 2-top, or three 2-tops.")

# ── Footer ─────────────────────────────────────────────────────────────────────

st.markdown("---")
st.caption(f"🔄 Auto-refresh active • {get_ts()}")