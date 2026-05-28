import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

# --- 1. SECURE CREDENTIALS & AUTHENTICATION ---
# Pull the secrets securely from Streamlit's vault
gsheet_creds = st.secrets["gsheets"]

try:
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(gsheet_creds, scopes=scopes)
    client = gspread.authorize(creds)
    SHEET_ID = "1ZTI3G97SSOcowXJyHpncFFSlGyS5VSLJublqLpAxVIk"
    sh = client.open_by_key(SHEET_ID)
except Exception as e:
    st.error(f"Authentication Failed: {e}")
    st.stop()


# --- 2. FETCH DATA FROM GOOGLE SHEETS ---
@st.cache_data(ttl=60)  
def load_data():
    try:
        raw_materials_sheet = sh.worksheet("Raw_Materials")
        transport_sheet = sh.worksheet("Transport_Rates")
        hardware_sheet = sh.worksheet("Hardware_Rates")
        exchange_sheet = sh.worksheet("Exchange_Rates")

        rm_df = pd.DataFrame(raw_materials_sheet.get_all_records())
        tr_df = pd.DataFrame(transport_sheet.get_all_records())
        hw_df = pd.DataFrame(hardware_sheet.get_all_records())
        ex_df = pd.DataFrame(exchange_sheet.get_all_records())

        for df in [rm_df, tr_df, hw_df, ex_df]:
            df.columns = df.columns.str.strip()

        return rm_df, tr_df, hw_df, ex_df
    except Exception as e:
        st.error(f"Failed to load data: {e}")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

# Load datasets
rm_df, tr_df, hw_df, ex_df = load_data()


# --- 3. HELPER FUNCTIONS ---
def get_latest_rate(df, key_col, value_col, key_val):
    if df.empty or key_col not in df.columns or value_col not in df.columns:
        return 0.0
    match = df[df[key_col].astype(str).str.strip().str.lower() == str(key_val).strip().lower()]
    return float(match.iloc[-1][value_col]) if not match.empty else 0.0

def format_inr(val):
    return f"₹{val:,.2f}"

def format_npr(val):
    return f"रू{val:,.2f}"


# --- 4. STREAMLIT UI ---
st.title("🏭 Material Costing & Purchase Ledger")

tab1, tab2, tab3 = st.tabs(["🧮 Material Costing Calculator", "📦 Record Purchase (Ledger)", "📊 View Ledger & Live Stock"])

# ==========================================
# TAB 1: MATERIAL COSTING CALCULATOR
# ==========================================
with tab1:
    st.header("Calculate Landed Cost")

    # Inputs Layout
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("1. Material Details")
        material_options = rm_df['Material_Name'].dropna().unique().tolist() if not rm_df.empty else ["No materials found"]
        material = st.selectbox("Select Material", material_options)

        purchase_rate_inr = st.number_input("Purchase Rate (INR)", min_value=0.0, format="%.2f")
        quantity = st.number_input("Quantity (MT)", min_value=0.01, value=1.0, step=0.1)

    with col2:
        st.subheader("2. Transport & Duty Options")
        
        transport_options = tr_df['Route'].dropna().unique().tolist() if not tr_df.empty else ["No routes found"]
        route = st.selectbox("Select Transport Route", transport_options)
        
        transport_rate_inr = get_latest_rate(tr_df, 'Route', 'Rate_INR', route)
        st.info(f"Transport Rate: **{format_inr(transport_rate_inr)} / MT**")

        hardware_options = hw_df['Supplier'].dropna().unique().tolist() if not hw_df.empty else ["No suppliers found"]
        hardware = st.selectbox("Select Hardware Rates", hardware_options)
        
        clearing_charge_inr = get_latest_rate(hw_df, 'Supplier', 'Clearing_Charge_INR', hardware)
        custom_duty_pct = get_latest_rate(hw_df, 'Supplier', 'Custom_Duty_Pct', hardware)
        loading_unloading_inr = get_latest_rate(hw_df, 'Supplier', 'Loading_Unloading_INR', hardware)
        
        st.caption(f"Clearing: {format_inr(clearing_charge_inr)}/MT | Duty: {custom_duty_pct}% | Load/Unload: {format_inr(loading_unloading_inr)}/MT")

        if not ex_df.empty and 'Exchange_Rate' in ex_df.columns:
            exchange_rate = float(ex_df['Exchange_Rate'].iloc[-1])
        else:
            exchange_rate = 1.6  # Default fallback
        st.write(f"💱 **Current Exchange Rate:** 1 INR = **{exchange_rate} NPR**")

    st.divider()

    # Calculation Execution
    if st.button("Calculate Landed Cost", type="primary"):
        if purchase_rate_inr <= 0:
            st.warning("Please enter a valid Purchase Rate.")
        else:
            # Step 1: Base Costs (INR)
            base_cost_inr = purchase_rate_inr * quantity
            total_transport_inr = transport_rate_inr * quantity
            total_clearing_inr = clearing_charge_inr * quantity
            total_loading_inr = loading_unloading_inr * quantity
            
            # Step 2: Convert to NPR BEFORE Duty
            base_cost_npr = base_cost_inr * exchange_rate
            total_transport_npr = total_transport_inr * exchange_rate
            total_clearing_npr = total_clearing_inr * exchange_rate
            total_loading_npr = total_loading_inr * exchange_rate
            
            # Assessable Value for Custom Duty (Material + Transport in NPR)
            assessable_value_npr = base_cost_npr + total_transport_npr
            
            # Step 3: Custom Duty Calculation (NPR)
            custom_duty_npr = assessable_value_npr * (custom_duty_pct / 100)
            
            # Step 4: Total Landed Cost (NPR)
            total_landed_cost_npr = (
                base_cost_npr + 
                total_transport_npr + 
                total_clearing_npr + 
                custom_duty_npr + 
                total_loading_npr
            )
            
            landed_cost_per_mt_npr = total_landed_cost_npr / quantity
            landed_cost_per_kg_npr = landed_cost_per_mt_npr / 1000

            # Store result in session state to pass to Tab 2
            st.session_state['last_calc'] = {
                'Material': material,
                'Quantity': quantity,
                'Landed_Cost_Per_MT': landed_cost_per_mt_npr,
                'Landed_Cost_Per_KG': landed_cost_per_kg_npr,
                'Total_Landed_Cost': total_landed_cost_npr
            }

            # Results Display
            st.subheader("🧾 Cost Breakdown")
            
            res_col1, res_col2, res_col3 = st.columns(3)
            
            with res_col1:
                st.write("**Base Costs (INR)**")
                st.write(f"Material: {format_inr(base_cost_inr)}")
                st.write(f"Transport: {format_inr(total_transport_inr)}")
                st.write(f"Clearing: {format_inr(total_clearing_inr)}")
                st.write(f"Load/Unload: {format_inr(total_loading_inr)}")
                st.write(f"**Total INR:** {format_inr(base_cost_inr + total_transport_inr + total_clearing_inr + total_loading_inr)}")

            with res_col2:
                st.write("**Converted Costs (NPR)**")
                st.write(f"Material: {format_npr(base_cost_npr)}")
                st.write(f"Transport: {format_npr(total_transport_npr)}")
                st.write(f"Assessable Value: {format_npr(assessable_value_npr)}")
                st.write(f"Custom Duty ({custom_duty_pct}%): {format_npr(custom_duty_npr)}")
                st.write(f"Other Charges: {format_npr(total_clearing_npr + total_loading_npr)}")

            with res_col3:
                st.success("### Final Landed Cost")
                st.write(f"Total Cost: **{format_npr(total_landed_cost_npr)}**")
                st.write(f"Cost per MT: **{format_npr(landed_cost_per_mt_npr)}**")
                st.write(f"Cost per KG: **{format_npr(landed_cost_per_kg_npr)}**")
                
            st.info("💡 Go to the **'Record Purchase'** tab to save this calculation to the ledger.")


# ==========================================
# TAB 2: RECORD PURCHASE (LEDGER)
# ==========================================
with tab2:
    st.header("Save Purchase to Ledger")
    
    if 'last_calc' not in st.session_state:
        st.warning("⚠️ No recent calculation found. Please run a calculation in the first tab.")
    else:
        calc = st.session_state['last_calc']
        st.success(f"Ready to record: **{calc['Quantity']} MT** of **{calc['Material']}** at **{format_npr(calc['Landed_Cost_Per_KG'])}/KG**")
        
        with st.form("ledger_form"):
            date = st.date_input("Purchase Date")
            supplier = st.text_input("Supplier Name")
            invoice_no = st.text_input("Invoice / LC Number")
            
            submitted = st.form_submit_button("Save to Ledger")
            
            if submitted:
                if not supplier or not invoice_no:
                    st.error("Please fill in Supplier Name and Invoice Number.")
                else:
                    try:
                        purchases_sheet = sh.worksheet("Purchases")
                        
                        # 1. GET EXISTING DATA
                        existing_data = purchases_sheet.get_all_records()
                        df_existing = pd.DataFrame(existing_data)
                        
                        # Fix column names to match exactly
                        expected_columns = ["Date", "Invoice_No", "Supplier", "Material", "Quantity_MT", "Landed_Cost_Per_KG", "Total_Cost_NPR", "Is_Blended"]
                        
                        if not df_existing.empty:
                            df_existing = df_existing[[col for col in expected_columns if col in df_existing.columns]]
                            
                            # Safely convert Quantity_MT to numeric, coercing errors to 0
                            df_existing['Quantity_MT'] = pd.to_numeric(df_existing['Quantity_MT'], errors='coerce').fillna(0)
                        
                        # Extract parameters for the NEW purchase
                        new_qty = float(calc['Quantity'])
                        new_cost_per_kg = float(calc['Landed_Cost_Per_KG'])
                        new_total = new_qty * 1000 * new_cost_per_kg
                        
                        # Create the new standard row
                        new_row = pd.DataFrame([{
                            "Date": date.strftime("%Y-%m-%d"),
                            "Invoice_No": invoice_no,
                            "Supplier": supplier,
                            "Material": calc['Material'],
                            "Quantity_MT": new_qty,
                            "Landed_Cost_Per_KG": new_cost_per_kg,
                            "Total_Cost_NPR": new_total,
                            "Is_Blended": "No"
                        }])
                        
                        rows_to_add = [new_row]
                        
                        # --- BLENDED COST LOGIC ---
                        if not df_existing.empty:
                            # Filter for the exact same material
                            mat_history = df_existing[df_existing['Material'] == calc['Material']]
                            
                            if not mat_history.empty:
                                # Get the absolute latest entry for this material (which represents current stock)
                                latest_entry = mat_history.iloc[-1]
                                old_qty = float(latest_entry.get('Quantity_MT', 0))
                                
                                # If there is remaining stock, blend it!
                                if old_qty > 0:
                                    old_cost_per_kg = float(latest_entry.get('Landed_Cost_Per_KG', 0))
                                    old_total = old_qty * 1000 * old_cost_per_kg
                                    
                                    # Blend Math
                                    blended_qty = old_qty + new_qty
                                    blended_total = old_total + new_total
                                    blended_cost_per_kg = blended_total / (blended_qty * 1000)
                                    
                                    blended_row = pd.DataFrame([{
                                        "Date": date.strftime("%Y-%m-%d"),
                                        "Invoice_No": "BLENDED-STOCK",
                                        "Supplier": "System Generated",
                                        "Material": calc['Material'],
                                        "Quantity_MT": round(blended_qty, 3),
                                        "Landed_Cost_Per_KG": round(blended_cost_per_kg, 2),
                                        "Total_Cost_NPR": round(blended_total, 2),
                                        "Is_Blended": "Yes"
                                    }])
                                    
                                    rows_to_add.append(blended_row)
                        
                        # 2. COMBINE DATA
                        df_to_append = pd.concat(rows_to_add, ignore_index=True)
                        
                        if not df_existing.empty:
                            df_combined = pd.concat([df_existing, df_to_append], ignore_index=True)
                        else:
                            df_combined = df_to_append
                            
                        # 3. CLEAN & SORT
                        df_combined['Date'] = pd.to_datetime(df_combined['Date'])
                        df_combined = df_combined.sort_values(by=['Date', 'Is_Blended'], ascending=[True, True])
                        df_combined['Date'] = df_combined['Date'].dt.strftime('%Y-%m-%d')
                        
                        df_combined_clean = df_combined.fillna("")
                        
                        # 4. WRITE TO GOOGLE SHEETS
                        data_to_write = [df_combined_clean.columns.values.tolist()] + df_combined_clean.values.tolist()
                        purchases_sheet.clear()
                        purchases_sheet.update(values=data_to_write, range_name="A1")
                        
                        st.success(f"✅ Purchase Recorded Successfully! Added {len(rows_to_add)} rows (including blending if applicable).")
                        del st.session_state['last_calc']  # Clear session state
                        st.cache_data.clear()  # Clear cache to refresh Ledger tab
                        
                    except Exception as e:
                        st.error(f"Failed to save to Google Sheets: {e}")


# ==========================================
# TAB 3: VIEW LEDGER & LIVE STOCK
# ==========================================
with tab3:
    st.header("Ledger & Live Stock Status")
    
    col_a, col_b = st.columns(2)
    
    with col_a:
        st.subheader("📉 Record Usage / Sales")
        st.write("Deduct material from your inventory when used or sold.")
        
        try:
            p_sheet = sh.worksheet("Purchases")
            df_ledger = pd.DataFrame(p_sheet.get_all_records())
            
            if not df_ledger.empty:
                df_ledger['Quantity_MT'] = pd.to_numeric(df_ledger['Quantity_MT'], errors='coerce').fillna(0)
                
                with st.form("deduct_form"):
                    deduct_date = st.date_input("Date of Usage")
                    deduct_mat = st.selectbox("Material Used", df_ledger['Material'].unique())
                    deduct_qty = st.number_input("Quantity Used (MT)", min_value=0.001, step=0.1)
                    deduct_reason = st.text_input("Reason / Work Order / Invoice")
                    
                    if st.form_submit_button("Deduct Stock", type="primary"):
                        if not deduct_reason:
                            st.error("Please provide a Reason or Work Order.")
                        else:
                            # 1. Find Current Stock & Blended Cost
                            mat_history = df_ledger[df_ledger['Material'] == deduct_mat]
                            latest_entry = mat_history.iloc[-1]
                            current_qty = float(latest_entry.get('Quantity_MT', 0))
                            current_cost_per_kg = float(latest_entry.get('Landed_Cost_Per_KG', 0))
                            
                            if deduct_qty > current_qty:
                                st.error(f"Insufficient Stock! You only have {current_qty} MT available.")
                            else:
                                # 2. Calculate New Stock
                                new_qty = current_qty - deduct_qty
                                new_total = new_qty * 1000 * current_cost_per_kg
                                
                                # 3. Create Deduction Row (For Tracking)
                                deduct_row = pd.DataFrame([{
                                    "Date": deduct_date.strftime("%Y-%m-%d"),
                                    "Invoice_No": f"USED: {deduct_reason}",
                                    "Supplier": "INTERNAL USAGE",
                                    "Material": deduct_mat,
                                    "Quantity_MT": -deduct_qty, # Negative to show deduction
                                    "Landed_Cost_Per_KG": current_cost_per_kg,
                                    "Total_Cost_NPR": -(deduct_qty * 1000 * current_cost_per_kg),
                                    "Is_Blended": "Usage"
                                }])
                                
                                # 4. Create New Balance Row
                                balance_row = pd.DataFrame([{
                                    "Date": deduct_date.strftime("%Y-%m-%d"),
                                    "Invoice_No": "BALANCE-FORWARD",
                                    "Supplier": "System Generated",
                                    "Material": deduct_mat,
                                    "Quantity_MT": round(new_qty, 3),
                                    "Landed_Cost_Per_KG": current_cost_per_kg,
                                    "Total_Cost_NPR": round(new_total, 2),
                                    "Is_Blended": "Yes"
                                }])
                                
                                # 5. Save Back to Sheets
                                df_combined = pd.concat([df_ledger, deduct_row, balance_row], ignore_index=True)
                                df_combined_clean = df_combined.fillna("")
                                data_to_write = [df_combined_clean.columns.values.tolist()] + df_combined_clean.values.tolist()
                                
                                p_sheet.clear()
                                p_sheet.update(values=data_to_write, range_name="A1")
                                
                                st.success(f"✅ Deducted {deduct_qty} MT! Remaining Stock: {new_qty} MT.")
                                st.cache_data.clear()
                                st.rerun()
            else:
                st.info("No ledger data available.")
        except Exception as e:
            st.error(f"Error loading ledger: {e}")

    with col_b:
        st.subheader("🏢 Current Live Stock")
        if not df_ledger.empty:
            # Group by material and get the LAST entry for each (which is the Blended/Balance row)
            live_stock = df_ledger.drop_duplicates(subset=['Material'], keep='last').copy()
            
            # Format for display
            live_stock = live_stock[['Material', 'Quantity_MT', 'Landed_Cost_Per_KG', 'Total_Cost_NPR']]
            live_stock['Landed_Cost_Per_KG'] = live_stock['Landed_Cost_Per_KG'].apply(lambda x: format_npr(x))
            live_stock['Total_Cost_NPR'] = live_stock['Total_Cost_NPR'].apply(lambda x: format_npr(x))
            
            # Filter out zero stock
            live_stock = live_stock[live_stock['Quantity_MT'] > 0]
            
            st.dataframe(live_stock, use_container_width=True, hide_index=True)
            
    st.divider()
    
    st.subheader("📚 Complete Transaction Ledger")
    if not df_ledger.empty:
        # Sort descending to show newest first
        df_ledger['Date'] = pd.to_datetime(df_ledger['Date'])
        display_ledger = df_ledger.sort_values(by='Date', ascending=False)
        display_ledger['Date'] = display_ledger['Date'].dt.strftime('%Y-%m-%d')
        
        st.dataframe(display_ledger, use_container_width=True, hide_index=True)

st.divider()
st.caption("Upload multiple bill entries in bulk via the Bulk Upload feature (coming soon).")

# --- BULK UPLOAD SALES ---
st.header("Bulk Upload Sales / Usage (Coming Soon / Existing Feature Integration)")
uploaded_file = st.file_uploader("Upload Bulk Sales File (CSV/Excel)", type=['csv', 'xlsx'])

if uploaded_file is not None:
    try:
        if uploaded_file.name.endswith('.csv'):
            df_bill = pd.read_csv(uploaded_file)
        else:
            df_bill = pd.read_excel(uploaded_file)
            
        st.write("Preview of Uploaded File:")
        st.dataframe(df_bill.head())
        
        if st.button("Process Bulk Upload"):
            # Ensure columns exist, rename/map as needed based on your bulk template
            if 'Material' not in df_bill.columns or 'Quantity_MT' not in df_bill.columns:
                st.error("Uploaded file must contain 'Material' and 'Quantity_MT' columns.")
            else:
                # Add default ledger tracking columns if missing
                if 'Date' not in df_bill.columns:
                    df_bill['Date'] = datetime.now().strftime('%Y-%m-%d')
                if 'Invoice_No' not in df_bill.columns:
                    df_bill['Invoice_No'] = "BULK-UPLOAD"
                if 'Supplier' not in df_bill.columns:
                    df_bill['Supplier'] = "BULK-USAGE"
                if 'Is_Blended' not in df_bill.columns:
                    df_bill['Is_Blended'] = "Usage"
                
                # Fetch existing ledger to get current costs
                purchases_sheet = sh.worksheet("Purchases")
                df_existing = pd.DataFrame(purchases_sheet.get_all_records())
                
                processed_rows = []
                for _, row in df_bill.iterrows():
                    mat = row['Material']
                    qty = float(row['Quantity_MT'])
                    
                    if not df_existing.empty:
                        mat_history = df_existing[df_existing['Material'] == mat]
                        if not mat_history.empty:
                            latest_entry = mat_history.iloc[-1]
                            current_cost = float(latest_entry.get('Landed_Cost_Per_KG', 0))
                            
                            # Usage row (Negative)
                            row['Quantity_MT'] = -abs(qty)
                            row['Landed_Cost_Per_KG'] = current_cost
                            row['Total_Cost_NPR'] = -(abs(qty) * 1000 * current_cost)
                            processed_rows.append(row.to_dict())
                            
                            # Balance Forward row
                            current_qty = float(latest_entry.get('Quantity_MT', 0))
                            new_qty = current_qty - abs(qty)
                            processed_rows.append({
                                "Date": row['Date'],
                                "Invoice_No": "BALANCE-FORWARD",
                                "Supplier": "System Generated",
                                "Material": mat,
                                "Quantity_MT": round(new_qty, 3),
                                "Landed_Cost_Per_KG": current_cost,
                                "Total_Cost_NPR": round(new_qty * 1000 * current_cost, 2),
                                "Is_Blended": "Yes"
                            })
                        else:
                            st.warning(f"Material {mat} not found in inventory. Skipped.")
                    else:
                        st.error("Ledger is empty.")
                        break

                if processed_rows:
                    df_processed = pd.DataFrame(processed_rows)
                    
                    # Merge with existing
                    cols_to_drop = ['New_Qty', 'Supplier_Qty', 'Supplier_Total', 'Old_Qty', 'Old_Total', 'Is_Blended']
                    df_processed = df_processed.drop(columns=[c for c in cols_to_drop if c in df_processed.columns])
                    
                    df_combined = pd.concat([df_existing, df_processed], ignore_index=True)
                    df_combined['Date'] = pd.to_datetime(df_combined['Date'])
                    df_combined = df_combined.sort_values(by=['Date', 'Is_Blended'], ascending=[True, True])
                    df_combined['Date'] = df_combined['Date'].dt.strftime('%Y-%m-%d')
                    
                    df_combined_clean = df_combined.fillna("")
                    data_to_write = [df_combined_clean.columns.values.tolist()] + df_combined_clean.values.tolist()
                    
                    purchases_sheet.clear() 
                    purchases_sheet.update(values=data_to_write, range_name="A1")
                    
                    st.success("✅ Bulk usage processed and ledger updated.")
                    st.cache_data.clear()
                    
    except Exception as e:
        st.error(f"Error processing file: {e}")
