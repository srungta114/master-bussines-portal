import streamlit as st
from google.cloud import firestore
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone

# 1. Page Config MUST be the very first Streamlit command
st.set_page_config(page_title="Business Portal", layout="wide", page_icon="🏢")

# --- 2. LOGIN (individual Google accounts, replaces the old shared password) ---
if not st.user.is_logged_in:
    st.title("🔒 A S Concern Business Portal")
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

user_email = (st.user.email or "").strip().lower()
st.session_state.user_email = user_email

# GUARD: user_email is used directly as a Firestore document ID everywhere
# in this app (here, and in Manage Users). An empty string is an INVALID
# Firestore document ID - db.collection("users").document("") raises
# exactly the InvalidArgument seen here, deep inside batch_get_documents,
# with no useful message surfaced to the user. This happens when Google's
# OAuth response doesn't include an email claim for some reason (e.g. the
# account's email scope wasn't granted, or an edge case in st.login()'s
# token handling) - st.user.is_logged_in can be True while st.user.email
# is still empty. Catching it here turns a raw, unreadable stack trace
# into a clear, actionable message instead.
if not user_email or "@" not in user_email:
    st.error(
        "🔒 Couldn't read a valid email address from your Google login. "
        "Please log out and try logging in again - if this keeps happening, "
        "contact an administrator."
    )
    st.stop()

# --- 4. ROLE + PAGE-ACCESS LOOKUP (with bootstrap-admin support for first-ever login) ---
# Canonical page keys - used both for the checkboxes in Manage Users and for
# each tool page's own access check.
ALL_PAGE_KEYS = ["inventory", "costing", "debtors", "data_cleanup"]

# Canonical FEATURE keys - finer-grained than pages. Someone can have access
# to a whole page (e.g. "inventory") but still be blocked from a specific
# action within it, if that key isn't in their granted permissions below.
# Add new keys here as new gated features are built; each tool page checks
# its own keys with require_permission()/has_permission().
ALL_PERMISSION_KEYS = [
    "inventory_single_entry",       # Hardware Inventory: Single Entry tab
    "inventory_bulk_upload",        # Hardware Inventory: Bulk Uploads tab
    "inventory_view",               # Hardware Inventory: View Inventory tab
    "inventory_masters",            # Hardware Inventory: Masters & AI Memory tab
    "inventory_edit_transactions",  # Hardware Inventory: Edit Transactions - edit a bill
    "inventory_bulk_delete",        # Hardware Inventory: Edit Transactions - bulk delete bills
]

if "user_role" not in st.session_state:
    # Wrapped in try/except deliberately: Streamlit Cloud redacts the real
    # message on an UNCAUGHT exception ("original error message is
    # redacted to prevent data leaks"), which is exactly why the previous
    # InvalidArgument traceback showed WHERE this failed but not WHY. By
    # catching it ourselves and displaying it, we bypass that redaction and
    # get the actual reason Firestore rejected the request - needed to fix
    # the real cause instead of guessing at it again.
    try:
        user_ref = db.collection("users").document(user_email)
        user_doc = user_ref.get()
    except Exception as e:
        st.error(
            "🔒 Couldn't look up your account in the database.\n\n"
            f"Debug info - email: `{user_email!r}` (length {len(user_email)})\n\n"
            f"Error: `{type(e).__name__}: {e}`\n\n"
            "Please screenshot this and share it so it can be fixed."
        )
        st.stop()

    if user_doc.exists:
        data = user_doc.to_dict()
        if not data.get("active", True):
            st.error("🔒 Your access has been disabled. Please contact an administrator.")
            st.stop()
        st.session_state.user_role = data.get("role", "staff")
        # Admins implicitly get every page regardless of what's stored;
        # staff only get whatever pages an admin explicitly checked for them.
        st.session_state.allowed_pages = (
            ALL_PAGE_KEYS if st.session_state.user_role == "admin"
            else data.get("pages", [])
        )
        # Same pattern, one level finer-grained: admins implicitly get every
        # gated feature; staff only get whatever an admin explicitly checked.
        st.session_state.user_permissions = (
            ALL_PERMISSION_KEYS if st.session_state.user_role == "admin"
            else data.get("permissions", [])
        )
        user_ref.update({"last_login": datetime.now(timezone.utc)})
    else:
        bootstrap_admins = [e.strip().lower() for e in st.secrets.get("bootstrap_admins", [])]
        if user_email in bootstrap_admins:
            user_ref.set({
                "email": user_email,
                "role": "admin",
                "pages": ALL_PAGE_KEYS,
                "permissions": ALL_PERMISSION_KEYS,
                "active": True,
                "created_at": datetime.now(timezone.utc),
                "last_login": datetime.now(timezone.utc),
            })
            st.session_state.user_role = "admin"
            st.session_state.allowed_pages = ALL_PAGE_KEYS
            st.session_state.user_permissions = ALL_PERMISSION_KEYS
        else:
            st.error(
                "🔒 Your account hasn't been granted access to this portal yet. "
                "Please ask an administrator to add you under Manage Users."
            )
            if st.button("Log out"):
                st.logout()
            st.stop()

user_role = st.session_state.user_role
allowed_pages = st.session_state.allowed_pages
user_permissions = st.session_state.user_permissions
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


def require_page_access(page_key):
    """Call at the top of each tool page with its own key (e.g. 'inventory').
    This is the REAL enforcement point - hiding a page from the sidebar nav
    only affects what's shown, it doesn't stop someone who already knows or
    guesses the direct URL (Streamlit still serves any page in pages/ by
    path regardless of navigation visibility). Every page needs this check
    itself, not just app.py filtering the nav list."""
    if page_key not in st.session_state.get("allowed_pages", []):
        st.error("🔒 You don't have access to this page. Contact an administrator if you need it.")
        st.stop()


def has_permission(permission_key):
    """Non-blocking check - use this to decide whether to even show a
    button/section (e.g. `if has_permission(...): st.button(...)`), as
    opposed to require_permission() which halts the page outright."""
    return permission_key in st.session_state.get("user_permissions", [])


def require_permission(permission_key):
    """Call inside a specific feature/section (not necessarily the whole
    page) that needs finer-grained gating than page-level access - e.g. a
    user might have the 'inventory' page but not the
    'inventory_bulk_delete' feature within it. Unlike require_page_access(),
    this doesn't st.stop() the entire page - only use it after everything
    else on the page that SHOULD still render for this user has already
    been drawn, or wrap the gated section so only that section is skipped."""
    if not has_permission(permission_key):
        st.warning("🔒 You don't have access to this feature. Contact an administrator if you need it.")
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

PAGE_DEFINITIONS = {
    "inventory": st.Page("pages/1_hardware_inventory.py", title="Hardware Inventory", icon="📦"),
    "costing": st.Page("pages/2_costing_tool.py", title="Costing Tool", icon="💰"),
    "debtors": st.Page("pages/3_debtor_statements.py", title="Debtor Statements", icon="🧾"),
    "data_cleanup": st.Page("pages/5_data_cleanup.py", title="Data Cleanup", icon="🧹"),
}

# Only show pages this specific user has been granted. This controls what's
# VISIBLE - the actual blocking of direct-URL access happens inside each
# page via require_page_access(), since a hidden nav link alone doesn't stop
# someone who already has or guesses the URL.
pages = [PAGE_DEFINITIONS[key] for key in ALL_PAGE_KEYS if key in allowed_pages]

if not pages and user_role != "admin":
    st.warning("You don't currently have access to any tools. Contact an administrator.")

if user_role == "admin":
    pages.append(st.Page("pages/4_manage_users.py", title="Manage Users", icon="👥"))

pg = st.navigation(pages)
pg.run()
