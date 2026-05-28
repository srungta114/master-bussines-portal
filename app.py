import streamlit as st

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
            # Now it checks the secret vault instead of hardcoded text!
            if pwd == st.secrets["master_password"]: 
                st.session_state["password_correct"] = True
                st.rerun()
            else:
                st.error("❌ Incorrect Password")
        st.stop()

# 3. If password is correct, setup the Navigation Menu
st.title("🏢 Master Business Portal")
st.write("Welcome! Please select a tool from the sidebar menu.")

# Define the pages
inventory_page = st.Page("pages/1_hardware_inventory.py", title="Hardware Inventory", icon="📦")
costing_page = st.Page("pages/2_costing_tool.py", title="Costing Tool", icon="💰")

# Run the navigation
pg = st.navigation([inventory_page, costing_page])
pg.run()
