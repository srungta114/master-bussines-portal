import streamlit as st
from datetime import datetime, timezone

# --- SECURITY BOUNCER ---
if "user_role" not in st.session_state:
    st.warning("🔒 Connection lost or not logged in.")
    st.info("Please click the Main Portal page in your sidebar to log in.")
    st.stop()

if st.session_state.user_role != "admin":
    st.error("🔒 This page requires admin access.")
    st.stop()

db = st.session_state.db

st.title("👥 Manage Users")
st.write(
    "Add new staff, change roles, or disable access. Changes take effect the "
    "next time that person loads the app."
)

PAGE_LABELS = {
    "inventory": "📦 Hardware Inventory",
    "costing": "💰 Costing Tool",
    "debtors": "🧾 Debtor Statements",
    "data_cleanup": "🧹 Data Cleanup",
}

# Finer-grained than pages: a user can have the page but not a specific
# action within it. Add new entries here whenever a new gated feature is
# built - key must match what the page's has_permission()/
# require_permission() call checks for.
PERMISSION_LABELS = {
    "inventory_single_entry": "🛒 Inventory: Single Entry",
    "inventory_bulk_upload": "📤 Inventory: Bulk Uploads",
    "inventory_view": "📊 Inventory: View Inventory",
    "inventory_masters": "📋 Inventory: Masters & AI Memory",
    "inventory_edit_transactions": "📝 Inventory: Edit a Transaction",
    "inventory_bulk_delete": "🗑️ Inventory: Bulk Delete Bills",
}

# --- ADD A NEW USER ---
st.header("➕ Add a New User")
with st.form("add_user_form", clear_on_submit=True):
    new_email = st.text_input("Email address (must match their Google login)").strip().lower()
    new_role = st.selectbox("Role", options=["staff", "admin"])
    st.caption("Page access (ignored for admins - admins always get every page):")
    new_pages = [
        key for key, label in PAGE_LABELS.items()
        if st.checkbox(label, key=f"new_page_{key}")
    ]
    st.caption("Feature access (ignored for admins - admins always get every feature):")
    new_permissions = [
        key for key, label in PERMISSION_LABELS.items()
        if st.checkbox(label, key=f"new_perm_{key}")
    ]
    submitted = st.form_submit_button("Add User")

    if submitted:
        if not new_email or "@" not in new_email:
            st.error("Please enter a valid email address.")
        else:
            existing = db.collection("users").document(new_email).get()
            if existing.exists:
                st.warning(f"{new_email} already has access. Edit their access in the table below instead.")
            else:
                db.collection("users").document(new_email).set({
                    "email": new_email,
                    "role": new_role,
                    "pages": new_pages,
                    "permissions": new_permissions,
                    "active": True,
                    "created_at": datetime.now(timezone.utc),
                    "last_login": None,
                })
                st.success(f"✅ Added {new_email} as {new_role}. They can log in with Google now.")
                st.rerun()

# --- EXISTING USERS ---
st.header("📋 Current Users")

users_ref = db.collection("users").stream()
users = [{"id": doc.id, **doc.to_dict()} for doc in users_ref]
users.sort(key=lambda u: u.get("email", ""))

if not users:
    st.info("No users found yet.")
else:
    for u in users:
        email = u["id"]
        col1, col2, col3, col4 = st.columns([3, 2, 2, 2])

        with col1:
            st.write(email)
            last_login = u.get("last_login")
            if last_login:
                st.caption(f"Last login: {last_login.strftime('%Y-%m-%d %H:%M')}")
            else:
                st.caption("Never logged in")

        with col2:
            current_role = u.get("role", "staff")
            new_role_val = st.selectbox(
                "Role", options=["staff", "admin"],
                index=["staff", "admin"].index(current_role),
                key=f"role_{email}", label_visibility="collapsed",
            )
            if new_role_val != current_role:
                if email == st.session_state.user_email and new_role_val != "admin":
                    st.error("You can't remove your own admin access.")
                else:
                    db.collection("users").document(email).update({"role": new_role_val})
                    st.rerun()

        with col3:
            is_active = u.get("active", True)
            active_label = "🟢 Active" if is_active else "🔴 Disabled"
            if st.button(active_label, key=f"toggle_{email}"):
                if email == st.session_state.user_email:
                    st.error("You can't disable your own account.")
                else:
                    db.collection("users").document(email).update({"active": not is_active})
                    st.rerun()

        with col4:
            if st.button("🗑️ Remove", key=f"remove_{email}"):
                if email == st.session_state.user_email:
                    st.error("You can't remove your own account.")
                else:
                    db.collection("users").document(email).delete()
                    st.rerun()

        # Page access - only meaningful for staff, since admins always get everything
        if new_role_val == "admin":
            st.caption("Admins have access to every page automatically.")
        else:
            current_pages = set(u.get("pages", []))
            page_cols = st.columns(len(PAGE_LABELS))
            updated_pages = set(current_pages)
            for col, (key, label) in zip(page_cols, PAGE_LABELS.items()):
                with col:
                    checked = st.checkbox(label, value=key in current_pages, key=f"pages_{email}_{key}")
                    if checked:
                        updated_pages.add(key)
                    else:
                        updated_pages.discard(key)
            if updated_pages != current_pages:
                db.collection("users").document(email).update({"pages": sorted(updated_pages)})
                st.rerun()

            # Feature access - finer-grained than pages, same "admins get
            # everything automatically" rule applies. Wrapped into rows of 3
            # so this stays readable as more feature keys get added later.
            st.caption("Feature access:")
            current_permissions = set(u.get("permissions", []))
            updated_permissions = set(current_permissions)
            perm_items = list(PERMISSION_LABELS.items())
            for row_start in range(0, len(perm_items), 3):
                row_items = perm_items[row_start:row_start + 3]
                perm_cols = st.columns(3)
                for col, (key, label) in zip(perm_cols, row_items):
                    with col:
                        checked = st.checkbox(label, value=key in current_permissions, key=f"perm_{email}_{key}")
                        if checked:
                            updated_permissions.add(key)
                        else:
                            updated_permissions.discard(key)
            if updated_permissions != current_permissions:
                db.collection("users").document(email).update({"permissions": sorted(updated_permissions)})
                st.rerun()

        st.divider()

# --- AUDIT LOG PREVIEW ---
st.header("📜 Recent Activity")
log_entries = (
    db.collection("audit_log")
    .order_by("timestamp", direction="DESCENDING")
    .limit(50)
    .stream()
)
log_rows = [doc.to_dict() for doc in log_entries]

if log_rows:
    import pandas as pd
    df = pd.DataFrame(log_rows)
    if "timestamp" in df.columns:
        df["timestamp"] = df["timestamp"].apply(
            lambda t: t.strftime("%Y-%m-%d %H:%M:%S") if t else ""
        )
    st.dataframe(df[["timestamp", "email", "action", "details"]], use_container_width=True, hide_index=True)
else:
    st.caption("No activity logged yet.")
