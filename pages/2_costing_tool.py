import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

# --- 1. SECURITY BOUNCER ---
# If the memory was wiped (refresh) or they bypassed the login, stop the page from crashing.
if "sh" not in st.session_state:
    st.warning("🔒 Connection lost or not logged in.")
    st.info("Please click the Main Portal page in your sidebar to log in and reconnect to the database.")
    st.stop() # This halts the script here so it doesn't crash on the next lines!

# --- 2. SECURE DATA LOADERS (USING SESSION STATE) ---
@st.cache_data(ttl=60)
def load_products():
    try:
        sh = st.session_state.sh
        worksheet = sh.worksheet("Product_Master")
        df = pd.DataFrame(worksheet.get_all_records())
        df.columns = df.columns.str.strip()
        return df
    except Exception as e:
        st.error(f"Failed to load Product Master: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=60)
def load_purchases_data():
    try:
        sh = st.session_state.sh
        purchases_sheet = sh.worksheet("Purchases")
        return pd.DataFrame(purchases_sheet.get_all_records())
    except Exception:
        return pd.DataFrame() 

df_master = load_products()
df_purchases = load_purchases_data()


# Extract unique sellers
if not df_purchases.empty and 'Seller' in df_purchases.columns:
    existing_sellers = sorted([str(s).strip() for s in df_purchases['Seller'].dropna().unique() if str(s).strip() != ""])
else:
    existing_sellers = []

# Initialize Session State
if 'bill_items' not in st.session_state:
    st.session_state.bill_items = []

st.title("🏗️ Material & Inventory Ledger")


# --- 3. QUICK COSTING SEARCH ---
st.header("🔍 Quick Costing Search")
with st.expander("Search Master Database", expanded=False): 
    if not df_purchases.empty and 'Material' in df_purchases.columns:
        search_materials = sorted(df_purchases['Material'].dropna().unique().tolist())
        
        # SMART PLACEHOLDER SEARCH
        search_selection = st.selectbox(
            "Search Database", 
            options=search_materials, 
            index=None, 
            placeholder="Type or click here to search for a material..."
        )
        
        if search_selection:
            item_data = df_purchases[df_purchases['Material'] == search_selection].iloc[-1]
            
            st.info(f"**Supplier:** {item_data.get('Seller', 'N/A')} | **Bill No:** {item_data.get('Bill_No', 'N/A')} | **Date:** {item_data.get('Date', 'N/A')}")
            
            # Math & Parsing
            landed_rate = float(item_data.get('Landed_Rate_Purchase', 0))
            true_pre_tax_purchase = landed_rate / 1.13
            cost_pc = float(item_data.get('Cost_Pc', 0))
            
            purch_unit = str(item_data.get('Unit_Purchase', '')).strip()
            sales_unit = str(item_data.get('Unit_Sales', '')).strip()
            qty_p = float(item_data.get('Qty_Purchase', 0))
            qty_s = float(item_data.get('Qty_Sales', 1)) 
            
            is_pcs = sales_unit.lower() in ['pcs', 'pc', 'piece', 'pieces']
            is_kg = purch_unit.lower() in ['kg', 'kgs', 'kilogram', 'kilograms']
            
            # Row 1
            r1_c1, r1_c2, r1_c3 = st.columns(3)
            r1_c1.metric("Landed Cost (Purchase Unit)", f"{landed_rate:.2f} / {purch_unit}")
            r1_c2.metric("Cost per Sales Unit", f"{cost_pc:.2f} / {sales_unit}")
            r1_c3.metric("Last Qty Bought", f"{qty_p} {purch_unit}")
            
            st.write("") 
            
            # Row 2
            row_2_metrics = []
            row_2_metrics.append(("Pre-Tax (Purchase Unit)", f"{true_pre_tax_purchase:.2f}"))
            
            if is_pcs:
                pre_tax_pc = cost_pc / 1.13
                row_2_metrics.append(("Pre-Tax (Sales Unit)", f"{pre_tax_pc:.2f} / {sales_unit}"))
                
            if is_pcs and is_kg and qty_s > 0:
                weight_per_pc = qty_p / qty_s
                row_2_metrics.append(("Weight per Piece", f"{weight_per_pc:.3f} {purch_unit}"))
                
            r2_cols = st.columns(len(row_2_metrics))
            for idx, (label, value) in enumerate(row_2_metrics):
                r2_cols[idx].metric(label, value)
                
    else:
        st.write("No costings saved yet. Add a bill below to start building your database!")

st.divider()

# --- 3.5 EDIT OR DELETE OLD BILLS ---
st.header("✏️ Edit Old Bills")
with st.expander("Modify or Delete an existing bill", expanded=False):
    if not df_purchases.empty and all(col in df_purchases.columns for col in ['Bill_No', 'Seller', 'Date']):
        
        # 1. Create a temporary unique ID combining all three details
        df_purchases['Unique_Bill_ID'] = df_purchases['Seller'].astype(str) + " | Bill: " + df_purchases['Bill_No'].astype(str) + " | Date: " + df_purchases['Date'].astype(str)
        
        # 2. Extract just the unique combinations for the dropdown
        unique_bill_options = sorted(df_purchases['Unique_Bill_ID'].unique().tolist())
        
        edit_selection = st.selectbox(
            "Search for Bill to Edit (Matches Seller + Bill No + Date)", 
            options=unique_bill_options, 
            index=None, 
            placeholder="Type Seller name, Bill No, or Date..."
        )
        
        if edit_selection:
            # 3. Filter the master database for JUST this exact combination
            bill_mask = df_purchases['Unique_Bill_ID'] == edit_selection
            
            # 4. Drop the temporary ID column before showing it to the user
            bill_data = df_purchases[bill_mask].drop(columns=['Unique_Bill_ID']).copy()
            
            st.info("Edit the numerical values directly in the table below. The app will automatically recalculate Landed Rates and Totals when you save.")
            
            # Show the interactive data editor
            edited_df = st.data_editor(
                bill_data, 
                hide_index=True, 
                use_container_width=True,
                disabled=["Seller", "Bill_No", "Date", "Group", "Sub-Group", "Material", "Unit_Purchase", "Unit_Sales"] 
            )
            
            c1, c2 = st.columns(2)
            
            # --- SAVE EDITS BUTTON ---
            if c1.button("💾 Save Changes to Bill"):
                try:
                    # Recalculate the Math
                    base_rate = edited_df['Rate_Purchase'].astype(float) + edited_df['Excise_Kg'].astype(float) + edited_df['Transport_Kg'].astype(float) + edited_df['Labour_Kg'].astype(float)
                    edited_df['Landed_Rate_Purchase'] = round(base_rate * 1.13, 2)
                    edited_df['Total_Item_Cost'] = round(edited_df['Landed_Rate_Purchase'] * edited_df['Qty_Purchase'].astype(float), 2)
                    
                    def calc_cost_pc(row):
                        return round(row['Total_Item_Cost'] / row['Qty_Sales'], 2) if float(row['Qty_Sales']) > 0 else 0
                        
                    edited_df['Cost_Pc'] = edited_df.apply(calc_cost_pc, axis=1)
                    
                    # Remove the temporary column from the master sheet before combining
                    df_purchases_clean = df_purchases.drop(columns=['Unique_Bill_ID'])
                    
                    # Remove the old bill rows and swap in the newly edited rows
                    df_purchases_untouched = df_purchases_clean[~bill_mask].copy()
                    df_combined = pd.concat([df_purchases_untouched, edited_df], ignore_index=True)
                    
                    # Sort chronologically and upload to Google Sheets
                    purchases_sheet = sh.worksheet("Purchases")
                    df_combined['Date'] = pd.to_datetime(df_combined['Date'])
                    df_combined = df_combined.sort_values(by='Date', ascending=True)
                    df_combined['Date'] = df_combined['Date'].dt.strftime('%Y-%m-%d')
                    
                    df_clean = df_combined.fillna("")
                    data_to_write = [df_clean.columns.values.tolist()] + df_clean.values.tolist()
                    
                    purchases_sheet.clear()
                    purchases_sheet.update(values=data_to_write, range_name="A1")
                    
                    st.success(f"✅ Bill updated successfully!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Save failed: {e}")
                    
            # --- DELETE BILL BUTTON ---        
            if c2.button("🗑️ Delete Entire Bill", type="primary"):
                try:
                    # Remove the temporary column from the master sheet
                    df_purchases_clean = df_purchases.drop(columns=['Unique_Bill_ID'])
                    
                    # Keep everything EXCEPT the selected bill
                    df_combined = df_purchases_clean[~bill_mask].copy()
                    
                    purchases_sheet = sh.worksheet("Purchases")
                    
                    if not df_combined.empty:
                        df_combined['Date'] = pd.to_datetime(df_combined['Date'])
                        df_combined = df_combined.sort_values(by='Date', ascending=True)
                        df_combined['Date'] = df_combined['Date'].dt.strftime('%Y-%m-%d')
                        df_clean = df_combined.fillna("")
                        data_to_write = [df_clean.columns.values.tolist()] + df_clean.values.tolist()
                    else:
                        data_to_write = [df_purchases_clean.columns.values.tolist()] 
                    
                    purchases_sheet.clear()
                    purchases_sheet.update(values=data_to_write, range_name="A1")
                    
                    st.success(f"🗑️ Bill deleted from database!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Delete failed: {e}")

# --- 4. BILL HEADER & DUPLICATE CHECK ---
st.header("1. Bill Details")
with st.container(border=True):
    c1, c2, c3 = st.columns(3)
    
    seller_options = ["➕ Add New Seller..."] + existing_sellers
    
    # SMART PLACEHOLDER SEARCH
    selected_seller = c1.selectbox(
        "Seller Company Name", 
        options=seller_options, 
        index=None, 
        placeholder="Select a Seller..."
    )
    
    if selected_seller == "➕ Add New Seller...":
        seller_name = c1.text_input("New Seller Name", placeholder="Type New Seller Name Here")
    else:
        seller_name = selected_seller
        
    bill_no = c2.text_input("Bill No.")
    purchase_date = c3.date_input("Purchase Date")

# Duplicate Bill Check Logic
is_duplicate_bill = False
existing_items_in_bill = [] # Create an empty list to track items

if not df_purchases.empty and seller_name and bill_no:
    clean_seller = str(seller_name).strip().lower()
    clean_bill = str(bill_no).strip().lower()
    
    mask_seller = df_purchases['Seller'].astype(str).str.strip().str.lower() == clean_seller
    mask_bill = df_purchases['Bill_No'].astype(str).str.strip().str.lower() == clean_bill
    
    if (mask_seller & mask_bill).any():
        st.warning(f"⚠️ **Bill Found:** Bill No. '{bill_no}' from '{seller_name}' is already in the database.")
        
        # Fetch the data for this specific bill
        bill_data = df_purchases[mask_seller & mask_bill]
        existing_items_in_bill = bill_data['Material'].tolist() # Save the items to check later
        
        append_mode = st.checkbox("Unlock entry to add missing items to this existing bill")
        
        if not append_mode:
            is_duplicate_bill = True
            st.error("🛑 Entry locked to prevent accidental duplicates. Check the box above to unlock.")
        else:
            is_duplicate_bill = False
            st.info("🔓 Unlocked! Ensure your 'Purchase Date' matches the original bill.")
            
            # --- NEW: DISPLAY EXISTING ITEMS ---
            st.write("**Items already recorded on this bill:**")
            st.dataframe(bill_data[['Material', 'Qty_Purchase', 'Unit_Purchase', 'Landed_Rate_Purchase', 'Total_Item_Cost']], hide_index=True)


# --- 5. ITEM ENTRY ---
st.header("2. Add Material")
with st.container(border=True):
    if not df_master.empty and 'Item_Name' in df_master.columns:
        product_list = sorted(df_master['Item_Name'].unique())
    else:
        product_list = []

    selected_product = st.selectbox(
        "Select Product", 
        options=product_list, 
        index=None, 
        placeholder="Type or click to find a product..."
    )

    if selected_product:
        if selected_product in existing_items_in_bill:
            st.error(f"🚨 **Heads Up!** '{selected_product}' is already on this bill! Adding it again will overwrite your previous entry.")
            
        item_info = df_master[df_master['Item_Name'] == selected_product].iloc[0]
        group = item_info['Group']
        sub_group = item_info['Sub-Group']
        p_unit = item_info['Purchase_Unit']
        s_unit = item_info['Sales_Unit']
        
        try:
            conv_fact = float(item_info['Conversion_Factor'])
        except (ValueError, TypeError):
            conv_fact = 1.0

        st.write(f"**Classification:** {group} > {sub_group}")
        st.info(f"**Unit Logic:** Purchased in {p_unit} | Sales tracked in {s_unit}")

        costing_strategy = "Override with New Costing"
        recent_purchase = None
        
        if not df_purchases.empty and selected_product in df_purchases['Material'].values:
            item_history = df_purchases[df_purchases['Material'] == selected_product].copy()
            item_history['Date'] = pd.to_datetime(item_history['Date'])
            item_history = item_history.sort_values(by='Date')
            recent_purchase = item_history.iloc[-1]
            
            days_since = (pd.to_datetime(purchase_date) - recent_purchase['Date']).days
            
            old_date = recent_purchase['Date'].strftime('%Y-%m-%d')
            old_rate = float(recent_purchase.get('Rate_Purchase', 0))
            old_landed = float(recent_purchase.get('Landed_Rate_Purchase', 0))
            old_seller = str(recent_purchase.get('Seller', 'Unknown'))
            
            st.success(f"📜 **Last Purchase Details:** You bought this **{days_since} days ago** ({old_date}) from {old_seller}. \n\n **Old Base Rate:** {old_rate:,.2f} / {p_unit} &nbsp;&nbsp;|&nbsp;&nbsp; **Old Landed Rate:** {old_landed:,.2f} / {p_unit}")
            
            if 0 <= days_since <= 15:
                st.warning(f"🕒 **High-Frequency Purchase:** Because this was bought within 15 days, you can choose to blend the inventory costs.")
                costing_strategy = st.radio(
                    "Price Fluctuation Strategy:",
                    options=["Override with New Costing", "Weighted Average (Blend Old + New)"]
                )

        i1, i2, i3 = st.columns(3)
        qty_p = i1.number_input(f"Total Quantity ({p_unit})", min_value=0.0, step=0.1)
        rate_p = i2.number_input(f"Purchase Rate (per {p_unit})", min_value=0.0)
        qty_s = i3.number_input(f"Calculated Qty ({s_unit})", value=float(qty_p * conv_fact))

        st.write("---")
        st.caption("Additional Costs & Discounts (Calculated per Purchase Unit)")
        f1, f2, f3 = st.columns(3)
        excise = f1.number_input("Excise Duty", min_value=0.0)
        trans = f2.number_input("Transport Cost", min_value=0.0)
        labour = f3.number_input("Labour Cost", min_value=0.0)
        
        d1, d2 = st.columns(2)
        d_type = d1.selectbox("Discount Type", ["None", "Per Unit", "Percentage (%)"])
        d_val = d2.number_input("Discount Value", min_value=0.0)

        if st.button("➕ Add Item to Bill", disabled=is_duplicate_bill):
            base_rate = rate_p + excise + trans + labour
            
            if d_type == "Per Unit":
                taxable = base_rate - d_val
            elif d_type == "Percentage (%)":
                taxable = base_rate * (1 - (d_val/100))
            else:
                taxable = base_rate
            
            landed_rate_p = taxable * 1.13 
            total_item_val = landed_rate_p * qty_p
            cost_per_s_unit = total_item_val / qty_s if qty_s > 0 else 0

            # --- NEW: ISOLATE TODAY'S INVOICE FROM THE DATABASE MATH ---
            supplier_qty = qty_p
            supplier_total = total_item_val
            old_qty_val = 0
            old_total_val = 0
            is_blended = "No"

            if costing_strategy == "Weighted Average (Blend Old + New)" and recent_purchase is not None:
                old_qty_val = float(recent_purchase.get('Qty_Purchase', 0))
                old_qty_s = float(recent_purchase.get('Qty_Sales', 0))
                old_total_val = float(recent_purchase.get('Total_Item_Cost', 0))
                
                new_qty_p = old_qty_val + qty_p
                new_qty_s = old_qty_s + qty_s
                new_total_cost = old_total_val + total_item_val
                
                landed_rate_p = new_total_cost / new_qty_p if new_qty_p > 0 else 0
                cost_per_s_unit = new_total_cost / new_qty_s if new_qty_s > 0 else 0
                
                rate_p = round((float(recent_purchase.get('Rate_Purchase', 0)) + rate_p) / 2, 2)
                excise = round((float(recent_purchase.get('Excise_Kg', 0)) + excise) / 2, 2)
                trans = round((float(recent_purchase.get('Transport_Kg', 0)) + trans) / 2, 2)
                labour = round((float(recent_purchase.get('Labour_Kg', 0)) + labour) / 2, 2)
                
                qty_p = new_qty_p
                qty_s = new_qty_s
                total_item_val = new_total_cost
                is_blended = "Yes"
                st.toast("✅ Applied Weighted Average pricing logic.")

            existing_item_index = None
            for i, item in enumerate(st.session_state.bill_items):
                if item["Material"] == selected_product:
                    existing_item_index = i
                    break

            # Create the payload dictionary
            new_entry = {
                "Seller": seller_name,
                "Bill_No": bill_no,
                "Date": str(purchase_date),
                "Group": group,
                "Sub-Group": sub_group,
                "Material": selected_product,
                "Qty_Purchase": qty_p,
                "Unit_Purchase": p_unit,
                "Qty_Sales": qty_s,
                "Unit_Sales": s_unit,
                "Rate_Purchase": rate_p,
                "Excise_Kg": excise,
                "Transport_Kg": trans,
                "Labour_Kg": labour,
                "Landed_Rate_Purchase": round(landed_rate_p, 2),
                "Cost_Pc": round(cost_per_s_unit, 2),
                "Total_Item_Cost": round(total_item_val, 2),
                # Hidden Trackers for Review Screen
                "Supplier_Qty": supplier_qty,
                "Supplier_Total": round(supplier_total, 2),
                "Old_Qty": old_qty_val,
                "Old_Total": old_total_val,
                "Is_Blended": is_blended
            }

            if existing_item_index is not None:
                st.session_state.bill_items.pop(existing_item_index)
                st.session_state.bill_items.append(new_entry)
                st.success(f"🔄 Merged {selected_product} with previous entry.")
            else:
                st.session_state.bill_items.append(new_entry)
                st.success(f"➕ Added {selected_product} to bill.")
                

# --- 6. REVIEW AND SAVE ---
if st.session_state.bill_items:
    st.header("3. Bill Review")
    
    df_bill = pd.DataFrame(st.session_state.bill_items)
    
    # --- VISUAL BREAKDOWN FOR BLENDED ITEMS ---
    blended_mask = df_bill['Is_Blended'] == 'Yes'
    if blended_mask.any():
        st.subheader("⚖️ 15-Day Blended Costing Breakdown")
        st.info("You chose to blend these new purchases with your inventory from the last 15 days. The 'Database' column shows the new weighted average.")
        
        compare_df = df_bill[blended_mask]
        for idx, row in compare_df.iterrows():
            st.markdown(f"**{row['Material']}**")
            b1, b2, b3 = st.columns(3)
            b1.metric("Old Inventory (Last 15 Days)", f"{row['Old_Total']:,.2f}", f"{row['Old_Qty']} {row['Unit_Purchase']}")
            b2.metric("Today's Invoice (New Addition)", f"{row['Supplier_Total']:,.2f}", f"{row['Supplier_Qty']} {row['Unit_Purchase']}")
            b3.metric("Final Blended Value (Database)", f"{row['Total_Item_Cost']:,.2f}", f"{row['Qty_Purchase']} {row['Unit_Purchase']}")
            st.write("---")

    # --- TODAY'S PHYSICAL INVOICE ---
    st.subheader("🧾 Today's Physical Invoice")
    
    # We display ONLY the 'Supplier' data here so the screen matches the paper bill exactly
    invoice_df = df_bill[['Material', 'Supplier_Qty', 'Unit_Purchase', 'Supplier_Total']].copy()
    invoice_df.columns = ['Material', 'Qty Bought Today', 'Unit', 'Total Cost Today']
    st.dataframe(invoice_df, hide_index=True)
        
    # Totals are calculated strictly on Today's money
    total_bill_new_session = df_bill['Supplier_Total'].astype(float).sum()
    deductions = (df_bill['Transport_Kg'].astype(float) + df_bill['Labour_Kg'].astype(float)) * df_bill['Supplier_Qty'].astype(float) * 1.13
    total_supplier_only = total_bill_new_session - deductions.sum()
    
    t1, t2 = st.columns(2)
    t1.metric("Total Landed Bill (Today's Money)", f"{total_bill_new_session:,.2f}")
    t2.metric("Supplier Invoice (Excl. Transport/Labour)", f"{total_supplier_only:,.2f}")

    # --- FINAL SAVE LOGIC ---
    if st.button("💾 Save Final Bill & Update Costings"):
        try:
            # 1. Clean up the dataframe to remove our hidden trackers before uploading to Sheets
            cols_to_drop = ['Supplier_Qty', 'Supplier_Total', 'Old_Qty', 'Old_Total', 'Is_Blended']
            df_new_clean = df_bill.drop(columns=[col for col in cols_to_drop if col in df_bill.columns])
            
            purchases_sheet = sh.worksheet("Purchases")
            existing_data = purchases_sheet.get_all_records()
            df_existing = pd.DataFrame(existing_data)
            
            if not df_existing.empty:
                df_combined = pd.concat([df_existing, df_new_clean], ignore_index=True)
                df_combined['Date'] = pd.to_datetime(df_combined['Date'])
                df_combined = df_combined.sort_values(by='Date', ascending=True)
                df_combined = df_combined.drop_duplicates(subset=['Material'], keep='last')
                df_combined['Date'] = df_combined['Date'].dt.strftime('%Y-%m-%d')
            else:
                df_combined = df_new_clean
                
            df_combined_clean = df_combined.fillna("")
            data_to_write = [df_combined_clean.columns.values.tolist()] + df_combined_clean.values.tolist()
            
            purchases_sheet.clear() 
            purchases_sheet.update(values=data_to_write, range_name="A1")
            
            st.success("✅ Sheet updated! Blended costings were prioritized and saved.")
            st.balloons()
            st.session_state.bill_items = [] 
            st.rerun()
            
        except Exception as e:
            st.error(f"Save failed: {e}")
