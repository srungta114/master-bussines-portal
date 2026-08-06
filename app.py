import streamlit as st
from google.cloud import firestore
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone

# 1. Page Config MUST be the very first Streamlit command
st.set_page_config(page_title="Business Portal", layout="wide", page_icon="🏢")

# --- 2. LOGIN (individual Google accounts, replaces the old shared password) ---
if not st.user.is_logged_in:
    st.title("🔒 Business Portal")
    st.write("Please log in with your Google account to continue.")
    if st.button("Log in with Google", type="primary"):
        st.login("google")
    st.stop()

# --- 3. CONNECTIONS ---
# Firestore: new, powers login/roles/audit log.
if "db" not in st.session_state:
    try:
        creds_dict = dict(st.secrets["firestore"])
        creds = Credentials.from_service_account_info(creds_dict)
        st.session_state.db = firestore.Client(credentials=creds, project=creds_dict["project_id"])
    except Exception as e:
        st.error("Could not connect to the database. Please contact the administrator.")
        st.stop()

db = st.session_state.db

# Google Sheets: UNCHANGED for now. Your existing Hardware Inventory, Costing
# Tool, and Debtor Statements pages still run on Sheets - this transition only
# replaces the shared password with individual logins + roles. Data migration
# to Firestore happens later, one tool at a time, once this is confirmed
# working. Removing this block would break those three pages immediately.
if "sh" not in st.session_state:
    try:
        import gspread
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        sheets_creds = Credentials.from_service_account_info(st.secrets["gsheets"], scopes=scopes)
        client = gspread.authorize(sheets_creds)
        SHEET_ID = "1ZTI3G97SSOcowXJyHpncFFSlGyS5VSLJublqLpAxVIk"
        st.session_state.sh = client.open_by_key(SHEET_ID)
    except Exception as e:
        st.error(f"Google Sheets Authentication Failed: {e}")
        st.stop()

user_email = st.user.email.strip().lower()
st.session_state.user_email = user_email

# --- 4. ROLE LOOKUP (with bootstrap-admin support for first-ever login) ---
if "user_role" not in st.session_state:
    user_ref = db.collection("users").document(user_email)
    user_doc = user_ref.get()

    if user_doc.exists:
        data = user_doc.to_dict()
        if not data.get("active", True):
            st.error("🔒 Your access has been disabled. Please contact an administrator.")
            st.stop()
        st.session_state.user_role = data.get("role", "staff")
        user_ref.update({"last_login": datetime.now(timezone.utc)})
    else:
        bootstrap_admins = [e.strip().lower() for e in st.secrets.get("bootstrap_admins", [])]
        if user_email in bootstrap_admins:
            user_ref.set({
                "email": user_email,
                "role": "admin",
                "active": True,
                "created_at": datetime.now(timezone.utc),
                "last_login": datetime.now(timezone.utc),
            })
            st.session_state.user_role = "admin"
        else:
            st.error(
                "🔒 Your account hasn't been granted access to this portal yet. "
                "Please ask an administrator to add you under Manage Users."
            )
            if st.button("Log out"):
                st.logout()
            st.stop()

user_role = st.session_state.user_role
# Compatibility shim: the existing Hardware Inventory / Debtor Statements pages
# still check this exact flag from the old shared-password login. Only set
# once we've actually confirmed the user is authorized above - not before.
st.session_state.password_correct = True


# --- 5. SHARED HELPERS - other pages import/paste these for consistent access checks ---
def require_admin():
    """Call at the top of any admin-only section. Stops the page for non-admins."""
    if st.session_state.get("user_role") != "admin":
        st.warning("🔒 This action requires admin access. Contact an administrator if you need this.")
        st.stop()


def log_action(action, details=None):
    """Writes a lightweight audit trail entry. Cheap - one small Firestore write."""
    try:
        db.collection("audit_log").add({
            "email": st.session_state.get("user_email", "unknown"),
            "action": action,
            "details": details or {},
            "timestamp": datetime.now(timezone.utc),
        })
    except Exception:
        pass  # never let logging failures break the actual action


# --- 6. SIDEBAR: identity + logout ---
with st.sidebar:
    st.write(f"👤 **{user_email}**")
    st.write(f"Role: **{user_role}**")
    if st.button("Log out"):
        st.logout()

# --- 7. MAIN NAVIGATION ---
st.title("🏢 Master Business Portal")
st.write("Welcome! Please select a tool from the sidebar menu.")

inventory_page = st.Page("pages/1_hardware_inventory.py", title="Hardware Inventory", icon="📦")
costing_page = st.Page("pages/2_costing_tool.py", title="Costing Tool", icon="💰")
debtors_page = st.Page("pages/3_debtor_statements.py", title="Debtor Statements", icon="🧾")

pages = [inventory_page, costing_page, debtors_page]

if user_role == "admin":
    users_page = st.Page("pages/4_manage_users.py", title="Manage Users", icon="👥")
    pages.append(users_page)

pg = st.navigation(pages)
pg.run()
