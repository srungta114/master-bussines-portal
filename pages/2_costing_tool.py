import streamlit as st
import pandas as pd

# --- 1. SECURITY BOUNCER ---
# If the memory was wiped (refresh) or they bypassed the login, stop the page from crashing.
if "db" not in st.session_state:
    st.warning("🔒 Connection lost or not logged in.")
    st.info("Please click the Main Portal page in your sidebar to log in and reconnect to the database.")
    st.stop() # This halts the script here so it doesn't crash on the next lines!

# Page-level access check. Hiding this page from the sidebar nav (done in
# app.py) only controls what's SHOWN - it doesn't stop someone who already
# has or guesses this page's direct URL. This is the actual enforcement.
if "costing" not in st.session_state.get("allowed_pages", []):
    st.error("🔒 You don't have access to this page. Contact an administrator if you need it.")
    st.stop()


def compact_search_key(text):
    """Turns '1 1/2" SQUARE PIPE 12 GAUGE' into '112sq12': strips all
    punctuation/spaces, drops pure unit/filler words (pipe, gauge), and
    abbreviates common shape words (square->sq, rectangle->rect, round->rnd).
    Lets users type a quick shorthand code instead of the full item name.
    Kept identical to the same function in the Hardware Inventory page so
    shorthand codes behave the same way in both tools."""
    stopwords = {'pipe', 'gauge'}
    abbreviations = {'square': 'sq', 'rectangle': 'rect', 'round': 'rnd'}
    t = str(text).lower()
    for ch in ['"', "'", '/', '-', '.']:
        t = t.replace(ch, ' ')
    tokens = t.split()
    out = []
    for tok in tokens:
        if tok in stopwords:
            continue
        out.append(abbreviations.get(tok, tok))
    return ''.join(out)


def smart_item_search(label, items, key, placeholder="Type or click to find an item..."):
    """A selectbox with an added shorthand-search layer above it: typing a
    compact code like '112sq12' filters the dropdown down to matching items,
    on top of Streamlit's normal built-in search on the full item name."""
    items = list(items)
    query = st.text_input(
        "🔍 Quick code (optional)",
        key=f"{key}_quick_search",
        placeholder='e.g. 112sq12 for 1 1/2" SQUARE PIPE 12 GAUGE',
    )
    options = items
    if query.strip():
        q = compact_search_key(query)
        filtered = [it for it in items if q in compact_search_key(it)]
        if filtered:
            options = filtered
        else:
            st.caption("No items match that shorthand — showing the full list instead.")
    return st.selectbox(label, options=options, index=None, key=key, placeholder=placeholder)


# --- 2. SECURE DATA LOADERS (FIRESTORE) ---
import re as _re

MAX_RECENT_COSTINGS = 5


def sanitize_doc_id(name):
    """Firestore document IDs can't contain '/' and shouldn't be empty."""
    doc_id = _re.sub(r'[/\\]', '_', str(name).strip())
    doc_id = doc_id[:1500]
    return doc_id if doc_id else "unnamed"


@st.cache_data(ttl=60)
def load_products():
    try:
        db = st.session_state.db
        docs = db.collection("product_master").stream()
        df = pd.DataFrame([doc.to_dict() for doc in docs])
        return df
    except Exception as e:
        st.error(f"Failed to load Product Master: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_materials_costing():
    """One row per material, each with its own 'recent_costings' list (up to
    5 most recent entries, newest first) embedded in the Recent_Costings column."""
    try:
        db = st.session_state.db
        docs = db.collection("materials_costing").stream()
        df = pd.DataFrame([doc.to_dict() for doc in docs])
        return df
    except Exception:
        return pd.DataFrame()


def save_costing_entry(material, group, sub_group, unit_purchase, unit_sales, entry):
    """Saves one new costing entry for a material: prepends it to that
    material's recent-costings history (newest first) and trims to the last
    MAX_RECENT_COSTINGS. This is a single-document read+write, regardless of
    how much history exists - unlike the old Sheets version, which rewrote
    the ENTIRE Purchases table on every single save."""
    db = st.session_state.db
    doc_id = sanitize_doc_id(material)
    doc_ref = db.collection("materials_costing").document(doc_id)

    existing = doc_ref.get()
    existing_history = existing.to_dict().get("recent_costings", []) if existing.exists else []

    updated_history = [entry] + existing_history
    updated_history = updated_history[:MAX_RECENT_COSTINGS]

    doc_ref.set({
        "Material": material,
        "Group": group,
        "Sub-Group": sub_group,
        "Unit_Purchase": unit_purchase,
        "Unit_Sales": unit_sales,
        **entry,  # denormalized "latest" fields at top level for quick lookups
        "recent_costings": updated_history,
    })


def get_flat_costing_history(df_materials_costing):
    """Explodes each material's capped recent_costings array into one row
    per historical entry, across all materials - gives the same 'many rows,
    one per past purchase' shape the old flat Purchases sheet had, so
    duplicate-bill detection and seller lists can search across full
    (capped) history rather than just each material's single latest entry."""
    if df_materials_costing.empty or 'recent_costings' not in df_materials_costing.columns:
        return pd.DataFrame()
    rows = []
    for _, mat_row in df_materials_costing.iterrows():
        material = mat_row.get('Material')
        group = mat_row.get('Group')
        sub_group = mat_row.get('Sub-Group')
        unit_purchase = mat_row.get('Unit_Purchase')
        unit_sales = mat_row.get('Unit_Sales')
        for entry in (mat_row.get('recent_costings') or []):
            rows.append({
                **entry, 'Material': material, 'Group': group, 'Sub-Group': sub_group,
                'Unit_Purchase': unit_purchase, 'Unit_Sales': unit_sales,
            })
    return pd.DataFrame(rows)

df_master = load_products()
df_materials_costing = load_materials_costing()
df_purchases = get_flat_costing_history(df_materials_costing)


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
    if not df_materials_costing.empty and 'Material' in df_materials_costing.columns:
        search_materials = sorted(df_materials_costing['Material'].dropna().unique().tolist())
        
        # SMART PLACEHOLDER SEARCH
        search_selection = smart_item_search(
            "Search Database",
            search_materials,
            key="costing_search_db",
            placeholder="Type or click here to search for a material...",
        )
        
        if search_selection:
            # Direct single-document lookup - the material's own doc already
            # carries its latest costing denormalized at the top level.
            item_data = df_materials_costing[df_materials_costing['Material'] == search_selection].iloc[0]
            
            st.info(f"**Supplier:** {item_data.get('Seller', 'N/A')} | **Bill No:** {item_data.get('Bill_No', 'N/A')} | **Date:** {item_data.get('Date', 'N/A')}")
            
            # Math & Parsing
            landed_rate = float(item_data.get('Landed_Rate_Purchase', 0) or 0)
            true_pre_tax_purchase = landed_rate / 1.13
            cost_pc = float(item_data.get('Cost_Pc', 0) or 0)
            
            purch_unit = str(item_data.get('Unit_Purchase', '')).strip()
            sales_unit = str(item_data.get('Unit_Sales', '')).strip()
            qty_p = float(item_data.get('Qty_Purchase', 0) or 0)
            qty_s = float(item_data.get('Qty_Sales', 1) or 1) 
            
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

            # Last 5 costings, newest first
            st.write("")
            st.subheader("📜 Last 5 Costings")
            history = item_data.get('recent_costings') or []
            if history:
                hist_df = pd.DataFrame(history)

                # Landed price without VAT (Landed_Rate_Purchase already includes 13% VAT)
                if 'Landed_Rate_Purchase' in hist_df.columns:
                    hist_df['Landed_Price_ExVAT'] = pd.to_numeric(
                        hist_df['Landed_Rate_Purchase'], errors='coerce'
                    ) / 1.13

                display_cols = [c for c in [
                    'Date', 'Seller', 'Bill_No', 'Landed_Price_ExVAT',
                    'Landed_Rate_Purchase', 'Cost_Pc', 'Qty_Purchase'
                ] if c in hist_df.columns]

                st.dataframe(
                    hist_df[display_cols].rename(columns={
                        'Landed_Price_ExVAT': 'Landed Price (Ex-VAT)',
                        'Landed_Rate_Purchase': 'Landed Price (Incl. VAT)',
                    }),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.caption("No costing history recorded for this material yet.")
                
    else:
        st.write("No costings saved yet. Add a bill below to start building your database!")

st.divider()

# --- 3.5 EDIT OR DELETE OLD BILLS ---
st.header("✏️ Edit Old Bills")
with st.expander("Modify or Delete an existing bill", expanded=False):
    st.info(
        "⏳ **Coming soon.** This section is being redesigned for the new "
        "per-material costing history model - in the old Sheets-based "
        "system, a 'bill' was a group of rows that could be edited or "
        "deleted together. Now that each material keeps its own capped "
        "history independently, editing a past bill means finding and "
        "updating the matching entry inside *each* affected material's "
        "history - a different, smaller operation than a single sheet "
        "edit. This will be added back once that design is confirmed, "
        "rather than shipping a version that might edit the wrong entries."
    )

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

    selected_product = smart_item_search(
        "Select Product",
        product_list,
        key="costing_select_product",
        placeholder="Type or click to find a product...",
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
            # 1. Clean up the dataframe to remove our hidden trackers
            cols_to_drop = ['Supplier_Qty', 'Supplier_Total', 'Old_Qty', 'Old_Total', 'Is_Blended']
            df_new_clean = df_bill.drop(columns=[col for col in cols_to_drop if col in df_bill.columns])
            df_new_clean = df_new_clean.fillna("")

            # 2. Save each material as its own document update - prepends
            # this new costing to that material's history and trims to the
            # last 5, rather than rewriting the entire database on every
            # single save (the old Sheets version rewrote every row, every
            # time, regardless of how many materials were actually touched).
            for _, row in df_new_clean.iterrows():
                material = str(row.get('Material', '')).strip()
                if not material:
                    continue

                entry = {
                    "Seller": row.get("Seller", ""),
                    "Bill_No": row.get("Bill_No", ""),
                    "Date": str(row.get("Date", "")),
                    "Rate_Purchase": float(row.get("Rate_Purchase", 0) or 0),
                    "Excise_Kg": float(row.get("Excise_Kg", 0) or 0),
                    "Transport_Kg": float(row.get("Transport_Kg", 0) or 0),
                    "Labour_Kg": float(row.get("Labour_Kg", 0) or 0),
                    "Landed_Rate_Purchase": float(row.get("Landed_Rate_Purchase", 0) or 0),
                    "Qty_Purchase": float(row.get("Qty_Purchase", 0) or 0),
                    "Qty_Sales": float(row.get("Qty_Sales", 0) or 0),
                    "Cost_Pc": float(row.get("Cost_Pc", 0) or 0),
                    "Total_Item_Cost": float(row.get("Total_Item_Cost", 0) or 0),
                }

                save_costing_entry(
                    material=material,
                    group=row.get("Group", ""),
                    sub_group=row.get("Sub-Group", ""),
                    unit_purchase=row.get("Unit_Purchase", ""),
                    unit_sales=row.get("Unit_Sales", ""),
                    entry=entry,
                )

            st.cache_data.clear()
            st.success("✅ Database updated! Blended costings were prioritized and saved.")
            st.balloons()
            st.session_state.bill_items = [] 
            st.rerun()
            
        except Exception as e:
            st.error(f"Save failed: {e}")
