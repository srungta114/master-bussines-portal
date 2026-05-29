import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

# 1. Page Config MUST be the very first Streamlit command
st.set_page_config(page_title="Business Portal", layout="wide", page_icon="🏢")

# 2. Centralized Password Protection
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False

    if not st.session_state["password_correct"]:
        st.title("🔒 Access Restricted")
        st.write("Please enter the master password to access the Business Portal.")
        pwd = st.text_input("Password", type="password")
        
        if st.button("Login"):
            if pwd == st.secrets["master_password"]: 
                st.session_state["password_correct"] = True
                st.rerun()
            else:
                st.error("❌ Incorrect Password")
        st.stop()

# Call the password function to enforce it
check_password()

# --- 3. SECURE MASTER GOOGLE SHEETS CONNECTION ---
# This pulls safely from Streamlit Secrets and runs in the background
if "sh" not in st.session_state:
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        
        # Look how clean this is now! It securely grabs the whole dictionary from the vault.
        creds = Credentials.from_service_account_info(
            st.secrets["gsheets"], 
            scopes=scopes
        )
        client = gspread.authorize(creds)
        
        # The ID of your specific Google Sheet
        SHEET_ID = "1ZTI3G97SSOcowXJyHpncFFSlGyS5VSLJublqLpAxVIk"
        
        # Save the master connection to the session state memory
        st.session_state.sh = client.open_by_key(SHEET_ID)
        
    except Exception as e:
        st.error(f"Google Sheets Authentication Failed: {e}")
        st.stop()

# 4. Setup the Navigation Menu
st.title("🏢 Master Business Portal")
st.write("Welcome! Please select a tool from the sidebar menu.")

# Define the pages
inventory_page = st.Page("pages/1_hardware_inventory.py", title="Hardware Inventory", icon="📦")
costing_page = st.Page("pages/2_costing_tool.py", title="Costing Tool", icon="💰")

# Run the navigation
pg = st.navigation([inventory_page, costing_page])
pg.run()
