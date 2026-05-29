import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from difflib import get_close_matches
import re
import io

# --- 1. SECURITY BOUNCER ---
# If the memory was wiped (refresh) or they bypassed the login, stop the page from crashing.
if "sh" not in st.session_state:
    st.warning("🔒 Connection lost or not logged in.")
    st.info("Please click the Main Portal page in your sidebar to log in and reconnect to the database.")
    st.stop() # This halts the script here so it doesn't crash on the next lines!


# --- SECURE CREDENTIALS & AUTHENTICATION ---
gsheet_creds = st.secrets["gsheets"]

try:
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(gsheet_creds, scopes=scopes)
    client = gspread.authorize(creds)
    
    # Connects exactly to your new Inventory Database
    SHEET_ID = "1I3A79zVuSg4Gy98EgUfktYLEzJtXUc6vynSobGl2rFQ"
    sh = client.open_by_key(SHEET_ID)
except Exception as e:
    st.error(f"Authentication Failed: {e}")
    st.stop()

# --- NEPALI FISCAL YEAR ENGINE (DYNAMIC) ---
def get_nepali_fiscal_year(date_val):
    try:
        if pd.isna(date_val) or str(date_val).strip() == "" or str(date_val).strip().lower() in ['nan', 'nat', 'none']:
            return "Unknown"
            
        if isinstance(date_val, str):
            d = pd.to_datetime(date_val, format="%d/%m/%Y")
        else:
            d = pd.to_datetime(date_val)
            
        if pd.isna(d):
            return "Unknown"
            
        year = d.year
        shrawan_1_dates = {
            2020: 16, 2021: 16, 2022: 17, 2023: 17,
            2024: 16, 2025: 16, 2026: 16, 2027: 17,
            2028: 16, 2029: 16, 2030: 17
        }
        cutoff_day = shrawan_1_dates.get(year, 16)

        if d.month > 7 or (d.month == 7 and d.day >= cutoff_day):
            bs_year = year + 57
        else:
            bs_year = year + 56
        
        return f"FY {bs_year}-{str(bs_year + 1)[-2:]}"
    except:
        return "Unknown"

# --- GOOGLE SHEETS DYNAMIC READ/WRITE FUNCTIONS ---
@st.cache_data(ttl=10)
def get_product_master():
    try:
        records = sh.worksheet("Product_Master").get_all_records()
        return pd.DataFrame(records)
    except Exception:
        return pd.DataFrame(columns=["Item_Name", "Purchase_Unit", "Sales_Unit", "Group"])

@st.cache_data(ttl=10)
def get_purchases():
    try:
        all_records = []
        # Read from all Fiscal Year tabs
        for ws in sh.worksheets():
            if ws.title.startswith("FY "):
                all_records.extend(ws.get_all_records())
                
        # Also read the old "Purchases" tab so you don't lose old data during migration
        try:
            old_ws = sh.worksheet("Purchases")
            all_records.extend(old_ws.get_all_records())
        except Exception:
            pass
            
        return pd.DataFrame(all_records) if all_records else pd.DataFrame()
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=10)
def get_code_mapping():
    try:
        records = sh.worksheet("Code_Mapping").get_all_records()
        return pd.DataFrame(records)
    except Exception:
        return pd.DataFrame(columns=["Item Code", "Item Name", "Units"])

@st.cache_data(ttl=10)
def get_learned_mappings():
    try:
        records = sh.worksheet("Learned_Mappings").get_all_records()
        return pd.DataFrame(records)
    except Exception:
        return pd.DataFrame(columns=["Billed_Description", "Matched_Item_Name"])

def save_purchases(df_to_save):
    if df_to_save.empty:
        return

    df_save = df_to_save.copy()
    if 'Date' in df_save.columns:
        df_save['Date'] = pd.to_datetime(df_save['Date'], dayfirst=True, errors='coerce').dt.strftime('%d/%m/%Y')
        df_save['Fiscal Year'] = df_save['Date'].apply(get_nepali_fiscal_year)
    
    if 'Fiscal Year' in df_save.columns:
        existing_ws = {ws.title: ws for ws in sh.worksheets() if ws.title.startswith("FY ")}
        processed_tabs = set()
        
        # 1. Group data by Fiscal Year and route to different tabs
        for fy, group_df in df_save.groupby('Fiscal Year'):
            ws_name = str(fy).replace("/", "-") if pd.notna(fy) and str(fy).strip() != "Unknown" else "FY Unknown"
            if not ws_name.startswith("FY "): 
                ws_name = "FY " + ws_name
                
            processed_tabs.add(ws_name)
            
            # Create the tab automatically if it doesn't exist
            if ws_name in existing_ws:
                ws = existing_ws[ws_name]
            else:
                ws = sh.add_worksheet(title=ws_name, rows="1000", cols="20")
            
            # Write data to the specific FY tab
            group_df = group_df.fillna("")
            data_to_write = [group_df.columns.values.tolist()] + group_df.values.tolist()
            ws.clear()
            ws.update(values=data_to_write, range_name="A1")
            
        # 2. Clear out the old 'Purchases' tab so it doesn't duplicate data
        try:
            old_ws = sh.worksheet("Purchases")
            old_ws.clear()
        except Exception:
            pass

def save_learned_mappings(df_to_save):
    try:
        ws = sh.worksheet("Learned_Mappings")
    except Exception:
        ws = sh.add_worksheet(title="Learned_Mappings", rows="1000", cols="5")
    
    df_to_save = df_to_save.fillna("")
    data_to_write = [df_to_save.columns.values.tolist()] + df_to_save.values.tolist()
    ws.clear()
    ws.update(values=data_to_write, range_name="A1")

# Initialization
products_df = get_product_master()
products_df = get_product_master()
purchases_df = get_purchases()

# Dynamically apply Fiscal Year to active session data if it's an older database version
if not purchases_df.empty and 'Date' in purchases_df.columns:
    purchases_df['Fiscal Year'] = purchases_df['Date'].apply(get_nepali_fiscal_year)

mapping_df = get_code_mapping()
learned_df = get_learned_mappings()

# 1. Create the Product Code & Unit translation dictionaries
code_dict = {}
unit_dict = {}
if not mapping_df.empty:
    if len(mapping_df.columns) >= 2:
        code_dict = dict(zip(mapping_df.iloc[:, 0].astype(str).str.strip(), mapping_df.iloc[:, 1].astype(str).str.strip()))
    if len(mapping_df.columns) >= 3:
        unit_dict = dict(zip(mapping_df.iloc[:, 0].astype(str).str.strip(), mapping_df.iloc[:, 2].astype(str).str.strip()))

# 2. Create the AI Memory dictionary
memory_dict = {}
if not learned_df.empty and 'Billed_Description' in learned_df.columns and 'Matched_Item_Name' in learned_df.columns:
    memory_dict = dict(zip(learned_df['Billed_Description'].astype(str).str.strip().str.upper(), learned_df['Matched_Item_Name'].astype(str).str.strip()))

# Pre-process stock items for fuzzy matching
stock_items = products_df['Item_Name'].dropna().unique().tolist()
stock_items_lower = {str(item).lower(): item for item in stock_items}

# --- UNIVERSAL NORMALIZATION ENGINE ---
def normalize_text(text):
    t = str(text).strip().lower()
    
    # 0. Common Master DB Typos & Hardware Abbreviations
    replacements = {
        'sqaure': 'square', 'squre': 'square', 'rect': 'rectangle',
        'jagdamba': 'jagadamba', 'jgadamba': 'jagadamba', 'jagamba': 'jagadamba', 'jagadamb': 'jagadamba',
        'h.r.': 'hr', 'h.r': 'hr',
        'c.r.': 'cr', 'c.r': 'cr',
        'g.i.': 'gi', 'g.i': 'gi',
        'c.g.i.': 'cgi', 'c.g.i': 'cgi',
        'c.c.': 'cc', 'c.c': 'cc'
    }
    for k, v in replacements.items():
        t = t.replace(k, v)
        
    # 1. Punctuation Splitting (Fix "SQ.14" -> "SQ 14" safely before decimals)
    t = re.sub(r'([a-zA-Z])\.(\d)', r'\1 \2', t)
    t = re.sub(r'(\d)\.([a-zA-Z])', r'\1 \2', t)
    
    # 2. Leading Zero Decimals (Safely catch .46 -> 0.46)
    t = re.sub(r'(^|\s)\.(\d+)', r'\g<1>0.\2', t)
    
    # 3. Fractions & Symbols
    t = re.sub(r'(\d+)\s+(\d+)/(\d+)', lambda m: str(float(m.group(1)) + float(m.group(2))/float(m.group(3))), t)
    t = re.sub(r'(\d+)/(\d+)', lambda m: str(float(m.group(1))/float(m.group(2))), t)
    t = t.replace('"', ' inch ').replace("'", ' feet ').replace('`', ' feet ')
    
    t = re.sub(r'\b(\d+(?:\.\d+)?)\s*(?:ft|foot)\b', r'\1 feet ', t)
    t = re.sub(r'([\d.]+)\s*#', r'\1gauge ', t)
    t = re.sub(r'\b([\d.]+)\s*g\b', r'\1gauge ', t)
    
    t = t.replace('-', ' ')
    t = re.sub(r'(?<!\d)\.|\.(?!\d)', ' ', t)
    
    # Separate attached letters and numbers generally (exclude 'x' handled below)
    t = re.sub(r'([a-wyzA-WYZ])(\d)', r'\1 \2', t)
    t = re.sub(r'(\d)([a-wyzA-WYZ])', r'\1 \2', t)
    
    # 4. Sheet Dimension Shorthands (4x8x18gauge -> 4 feet x 8 feet 18gauge)
    t = re.sub(r'\b(\d+)\s*[xX*]\s*(\d+)\s*[xX*]\s*([\d.]+)\s*gauge\b', r'\1 feet x \2 feet \3gauge ', t)
    
    # Compress standard dimensions (25 x 25 x 3 -> 25x25x3)
    t = re.sub(r'(?<=\d)\s*[xX*]\s*(?=\d)', 'x', t)
    
    # Equal Angle compression: (25x25x3 -> 25x3)
    t = re.sub(r'\b(\d+(?:\.\d+)?)x\1x([\d.]+)\b', r'\1x\2', t)
    
    # Sheet 2-part shorthand (4x8 -> 4 feet x 8 feet)
    if any(w in t for w in ['sheet', 'plate', 'jasta', 'corrugated']):
        t = re.sub(r'\b(\d+)x(\d+)\b', r'\1 feet x \2 feet', t)
    
    # Re-attach thickness units so there is NO SPACE (14 gauge -> 14gauge)
    t = re.sub(r'([\d.]+)\s+(mm|gauge)\b', r'\1\2', t)
    
    # 5. Smart Checks
    dim_match = re.search(r'([\d.]+)\s*(?:inch|feet|mm|cm)?\s*x\s*([\d.]+)', t)
    if dim_match:
        try:
            if float(dim_match.group(1)) != float(dim_match.group(2)):
                t = re.sub(r'\b(sq|square)\b', 'rectangle', t)
        except ValueError:
            pass
            
    # 6. Parameter Rules
    t = re.sub(r'\bred\b', 'maroon', t)
    t = re.sub(r'\bms[\s]+(sq|square)[\s]+rod\b', 'square rod', t)
    t = re.sub(r'\bms[\s]+plain[\s]+rod\b', 'plain rod', t)
    t = re.sub(r'\bfibre[\s]+corrugated(?:[\s]+sheet)?\b', 'fibre jasta', t)
    
    # 7. Any-Order Combinations
    if re.search(r'\bms\b', t) and re.search(r'\b(sq|square|rectangle)\b', t) and re.search(r'\bpipe\b', t):
        is_rect = bool(re.search(r'\brectangle\b', t))
        t = re.sub(r'\bms\b', '', t)
        t = re.sub(r'\b(sq|square|rectangle)\b', '', t)
        t = re.sub(r'\bpipe\b', '', t)
        t += ' rectangle pipe' if is_rect else ' square pipe'
        
    elif re.search(r'\bms\b', t) and re.search(r'\bround\b', t) and re.search(r'\bpipe\b', t):
        t = re.sub(r'\bms\b', '', t)
        t = re.sub(r'\bround\b', '', t)
        t = re.sub(r'\bpipe\b', '', t)
        t += ' black pipe'
        
    return " ".join(t.split())

# --- REWORKED AI LOGIC WITH DUAL-NORMALIZATION ---
def find_best_match(description, mapped_keywords=""):
    debug_log = {"Original": str(description)}
    
    # 1. AI Memory Bank Check
    orig_upper = str(description).strip().upper()
    if orig_upper in memory_dict:
        debug_log["Status"] = "Memory Match"
        return memory_dict[orig_upper], debug_log

    # 2. Normalize the Uploaded Description
    desc_clean = normalize_text(description)
    debug_log["Cleaned"] = desc_clean
    
    # 3. Normalize the Master List
    candidates = []
    for k, v in stock_items_lower.items():
        candidates.append({
            "original_key": k,
            "norm_key": normalize_text(k),
            "val": v
        })

    triggered_locks = []

    # 4A. ISOLATION LOCKS (Boolean match required)
    isolation_keywords = ["maroon", "square rod", "plain rod", "square pipe", "rectangle pipe", "fibre jasta", "black pipe", "hulas", "jagadamba"]
    for kw in isolation_keywords:
        has_kw = bool(re.search(rf'\b{kw}\b', desc_clean))
        filtered = [c for c in candidates if bool(re.search(rf'\b{kw}\b', c["norm_key"])) == has_kw]
        if filtered:
            candidates = filtered
            if has_kw: triggered_locks.append(f"ISO:{kw}")
        else:
            if has_kw: triggered_locks.append(f"ISO:{kw}(Bypassed)")

    # 4B. POSITIVE LOCKS (If in input, MUST be in candidate)
    positive_locks = []
    
    # Extract dimensions
    dims = re.findall(r'\b\d+(?:\.\d+)?x\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?)?\b', desc_clean)
    positive_locks.extend(dims)
    
    # Extract Feet 
    feet_matches = re.findall(r'\b\d+(?:\.\d+)?\s*feet\b', desc_clean)
    positive_locks.extend(feet_matches)
            
    # Extract Thickness
    mm_matches = re.findall(r'\b\d+(?:\.\d+)?mm\b', desc_clean)
    positive_locks.extend(mm_matches)
    
    gauge_matches = re.findall(r'\b\d+(?:\.\d+)?gauge\b', desc_clean)
    positive_locks.extend(gauge_matches)
    
    # Apply Positive Locks Safely
    for kw in positive_locks:
        filtered = [c for c in candidates if bool(re.search(rf'\b{kw}\b', c["norm_key"]))]
        if filtered:
            candidates = filtered
            triggered_locks.append(f"REQ:{kw}")
        else:
            triggered_locks.append(f"REQ:{kw}(Bypassed)")

    # 4C. DYNAMIC MAPPED KEYWORDS
    if mapped_keywords:
        norm_kw = normalize_text(mapped_keywords)
        kw_list = [w for w in norm_kw.split() if len(w) > 1]
        for kw in kw_list:
            filtered_candidates = []
            for c in candidates:
                c_words = c["norm_key"].split()
                if kw in c_words or get_close_matches(kw, c_words, n=1, cutoff=0.8):
                    filtered_candidates.append(c)
            if filtered_candidates:
                candidates = filtered_candidates
                triggered_locks.append(f"MAP:{kw}")
            else:
                triggered_locks.append(f"MAP:{kw}(Bypassed)")

    debug_log["Strict_Locks"] = triggered_locks

    # 5. Extract words and score
    best_match = None
    highest_score = 0
    desc_words = set(desc_clean.split())
    
    if not desc_words:
        debug_log["Status"] = "Empty String"
        return None, debug_log
    
    scoring_details = []
    
    for c in candidates:
        norm_key = c["norm_key"]
        val = c["val"]
        
        if desc_clean == norm_key: 
            debug_log["Status"] = "Exact Match"
            return val, debug_log
            
        key_words = set(norm_key.split())
        if not key_words: continue
        
        score = 0
        exact_matches = []
        fuzzy_matches = []
        
        for d_word in desc_words:
            if d_word in key_words:
                score += 1 
                exact_matches.append(d_word)
            else:
                fuzzy = get_close_matches(d_word, key_words, n=1, cutoff=0.6)
                if fuzzy:
                    score += 0.8
                    fuzzy_matches.append(f"{d_word}->{fuzzy[0]}")
        
        denominator = max(len(key_words), len(desc_words))
        match_ratio = score / denominator if denominator > 0 else 0
        
        if match_ratio > 0:
            scoring_details.append({
                "Item": val,
                "Ratio": round(match_ratio, 2),
                "Exact": exact_matches,
                "Fuzzy": fuzzy_matches
            })
            
        if match_ratio > highest_score:
            highest_score = match_ratio
            best_match = val
            
    # Sort and store top 3 scores for debugging
    scoring_details = sorted(scoring_details, key=lambda x: x["Ratio"], reverse=True)[:3]
    debug_log["Top_Scorers"] = scoring_details
            
    if highest_score >= 0.3:
        debug_log["Status"] = "Fuzzy Passed"
        return best_match, debug_log
        
    debug_log["Status"] = "Failed (Score < 0.3)"
    return None, debug_log

def format_debug_string(log):
    if log.get("Status") in ["Memory Match", "Exact Match"]:
        return f"🟢 {log['Status']}"
    
    locks = log.get('Strict_Locks', [])
    lock_str = f"🔒 Locks: {locks}" if locks else "🔓 Locks: None"
    
    res = f"Cleaned: '{log.get('Cleaned', '')}' | {lock_str} | "
    scorers = log.get("Top_Scorers", [])
    
    if scorers:
        top = scorers[0]
        res += f"🏆 Best: {top['Item']} ({top['Ratio']}) [E:{top['Exact']}, F:{top['Fuzzy']}]"
        if len(scorers) > 1:
            runner = scorers[1]
            res += f" | 🥈 2nd: {runner['Item']} ({runner['Ratio']})"
    else:
        res += "❌ No similar items found due to strict locks."
        
    return res

st.title("📦 Hardware Inventory Management")
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "🛒 Single Entry", "📤 Bulk Uploads", "📊 View Inventory", "📋 Masters & AI Memory", "📝 Edit Transactions"
])

# --- TAB 1: SINGLE TRANSACTION ENTRY (WITH CART) ---
with tab1:
    st.header("Single Transaction Entry")
    
    trans_type = st.radio(
        "Select Transaction Type", 
        ["Purchase", "Sales", "Purchase Return", "Sales Return", "Stock Adjustment"], 
        horizontal=True
    )
    
    if "single_entry_type" not in st.session_state or st.session_state.single_entry_type != trans_type:
        st.session_state.single_entry_type = trans_type
        st.session_state.single_entry_cart = []
        
    st.divider()

    c_date, c_bill = st.columns(2)
    with c_date:
        entry_date = st.date_input("Date", value=datetime.now(), format="DD/MM/YYYY")
    with c_bill:
        if trans_type == "Stock Adjustment":
            bill_number = st.text_input("Reference / Reason (Optional)", placeholder="E.g., Physical Audit, Damage...")
            if not bill_number:
                bill_number = f"ADJ-{datetime.now().strftime('%Y%m%d%H%M')}"
        else:
            bill_number = st.text_input("Bill / Invoice Number", placeholder="Enter Bill Number...")
            
        bill_exists = False
        override_duplicate = True
        if bill_number and trans_type != "Stock Adjustment" and not purchases_df.empty and str(bill_number).strip() in purchases_df['Bill Number'].astype(str).str.strip().values:
            bill_exists = True
            st.error(f"🚨 Bill Number '{bill_number}' already exists.")
            override_duplicate = st.checkbox("I intentionally want to APPEND to this existing bill.")

    st.divider()

    items = products_df['Item_Name'].dropna().unique()
    selected_item = st.selectbox("Search and Select Item to Add", options=items, index=None, placeholder="Click here to type...")

    if selected_item:
        item_details = products_df[products_df['Item_Name'] == selected_item].iloc[0]
        p_unit = item_details['Purchase_Unit']
        s_unit = item_details['Sales_Unit']
        group = item_details['Group'] 
        
        with st.form("add_item_form", clear_on_submit=True):
            st.subheader(f"Adding: {selected_item}")
            
            billed_qty = None
            stock_qty = None
            
            if trans_type in ["Purchase", "Purchase Return"]:
                c1, c2 = st.columns(2)
                with c1:
                    billed_qty = st.number_input(f"Billed Qty ({p_unit})", min_value=0.01, step=1.0, value=None)
                with c2:
                    if p_unit != s_unit:
                        st.info(f"Conversion: Billed in {p_unit}, Stocked in {s_unit}")
                        stock_qty = st.number_input(f"Actual Stock Impact ({s_unit})", min_value=0.01, step=1.0, value=None)
                    else:
                        stock_qty = billed_qty
                        st.info(f"Units match. Stock impact will be exactly the billed quantity ({s_unit}).")
            
            elif trans_type in ["Sales", "Sales Return"]:
                billed_qty = st.number_input(f"Billed Qty ({s_unit})", min_value=0.01, step=1.0, value=None)
                stock_qty = billed_qty
                
            elif trans_type == "Stock Adjustment":
                stock_qty = st.number_input(f"Stock Adjustment Quantity ({s_unit})", step=1.0, value=None, help="Use positive numbers to ADD stock, negative to DEDUCT stock.")
                billed_qty = 0
            
            if st.form_submit_button("➕ Add Item to Cart", type="primary"):
                if trans_type == "Stock Adjustment" and (stock_qty is None or stock_qty == 0):
                    st.error("Please enter a non-zero adjustment quantity.")
                elif trans_type != "Stock Adjustment" and not billed_qty:
                    st.error("Please enter the Billed Quantity.")
                elif trans_type in ["Purchase", "Purchase Return"] and p_unit != s_unit and not stock_qty:
                    st.error(f"Please enter the Actual Stock Impact in {s_unit}.")
                else:
                    if trans_type in ["Sales", "Purchase Return"]:
                        final_stock = -abs(stock_qty)
                    elif trans_type in ["Purchase", "Sales Return"]:
                        final_stock = abs(stock_qty)
                    elif trans_type == "Stock Adjustment":
                        final_stock = stock_qty  
                        
                    pur_qty_val = billed_qty if trans_type in ["Purchase", "Purchase Return"] else 0
                    pur_unit_val = p_unit if trans_type in ["Purchase", "Purchase Return"] else "-"

                    st.session_state.single_entry_cart.append({
                        "Item_Name": selected_item,
                        "Group": group,
                        "Purchase Qty": pur_qty_val,
                        "Purchase Unit": pur_unit_val,
                        "Stock Qty Added": final_stock, 
                        "Stock Unit": s_unit
                    })
                    st.rerun()
    
    if st.session_state.single_entry_cart:
        st.divider()
        st.subheader(f"🛒 Items in Current Bill ({len(st.session_state.single_entry_cart)})")
        
        cart_df = pd.DataFrame(st.session_state.single_entry_cart)
        edited_cart = st.data_editor(cart_df, num_rows="dynamic", use_container_width=True, key="cart_editor")
        
        c_sub, c_clr = st.columns([2, 1])
        with c_sub:
            if st.button("💾 Commit Transaction to Database", type="primary", use_container_width=True):
                if trans_type != "Stock Adjustment" and not bill_number:
                    st.error("Please enter a Bill Number at the top.")
                elif bill_exists and not override_duplicate:
                    st.error("Duplicate Bill detected. Please check the override box above to proceed.")
                elif edited_cart.empty:
                    st.error("Cart is empty.")
                else:
                    records_to_save = []
                    for _, row in edited_cart.iterrows():
                        records_to_save.append({
                            "Date": entry_date.strftime("%d/%m/%Y"),
                            "Fiscal Year": get_nepali_fiscal_year(entry_date.strftime("%d/%m/%Y")),
                            "Bill Number": bill_number,
                            "Group": row["Group"],
                            "Item_Name": row["Item_Name"],
                            "Purchase Qty": row["Purchase Qty"],
                            "Purchase Unit": row["Purchase Unit"],
                            "Stock Qty Added": row["Stock Qty Added"],
                            "Stock Unit": row["Stock Unit"]
                        })
                    
                    updated_purchases = pd.concat([purchases_df, pd.DataFrame(records_to_save)], ignore_index=True)
                    save_purchases(updated_purchases)
                    st.session_state.single_entry_cart = []
                    st.cache_data.clear()
                    st.success(f"✅ Successfully saved {len(records_to_save)} items under Ref/Bill: {bill_number}")
                    st.rerun()
        with c_clr:
            if st.button("🗑️ Clear Entire Bill", use_container_width=True):
                st.session_state.single_entry_cart = []
                st.rerun()

# --- TAB 2: BULK UPLOADS ---
with tab2:
    st.header("Bulk Upload Transactions")
    
    bulk_type = st.radio(
        "Select Upload Type", 
        ["Sales", "Purchases", "Purchase Returns", "Sales Returns"], 
        horizontal=True
    )
    
    if st.session_state.get("bulk_type") != bulk_type:
        for key in ['auto_matched', 'unmatched', 'processed_file_name', 'raw_upload_data', 'resolving_duplicates', 'df_to_process', 'bills_to_delete', 'committed_file_name']:
            if key in st.session_state: del st.session_state[key]
        st.session_state.bulk_type = bulk_type
        st.rerun()
        
    uploaded_file = st.file_uploader(f"Upload {bulk_type} File (No Headers)", type=['csv', 'xlsx'])
    
    if uploaded_file is not None:
        
        if st.session_state.get("committed_file_name") == uploaded_file.name:
            st.success("🎉 Database updated successfully! Please clear the file above (click the 'X') to upload a new one.")
        else:
            # 1. INITIAL LOAD & DUPLICATE CHECK
            if "processed_file_name" not in st.session_state or st.session_state.processed_file_name != uploaded_file.name:
                try:
                    if uploaded_file.name.endswith('.csv'):
                        df_upload = pd.read_csv(uploaded_file, header=None)
                    else:
                        df_upload = pd.read_excel(uploaded_file, header=None)
                    
                    df_upload[1] = df_upload[1].astype(str).str.strip()
                    uploaded_bills_count = df_upload.groupby(1).size().to_dict()
                    
                    db_bills_count = {}
                    if not purchases_df.empty and 'Bill Number' in purchases_df.columns:
                        db_bills_count = purchases_df['Bill Number'].astype(str).str.strip().value_counts().to_dict()
                    
                    duplicate_bills = []
                    for b_no, count in uploaded_bills_count.items():
                        if b_no in db_bills_count and db_bills_count[b_no] == count:
                            duplicate_bills.append(b_no)
                    
                    st.session_state.raw_upload_data = df_upload
                    st.session_state.processed_file_name = uploaded_file.name
                    st.session_state.bills_to_delete = [] 
                    
                    if duplicate_bills:
                        st.session_state.resolving_duplicates = True
                        st.session_state.duplicate_bills = duplicate_bills
                    else:
                        st.session_state.resolving_duplicates = False
                        st.session_state.df_to_process = df_upload
                    
                    st.rerun()
                except Exception as e:
                    st.error(f"Error reading file: {e}")

            # 2. DUPLICATE RESOLUTION UI
            if st.session_state.get("resolving_duplicates", False):
                st.warning(f"⚠️ Found {len(st.session_state.duplicate_bills)} duplicate bill(s) matching exactly in the database.")
                
                with st.form("resolve_duplicates_form"):
                    resolutions = {}
                    for bill in st.session_state.duplicate_bills:
                        resolutions[bill] = st.radio(
                            f"Bill Number: {bill}",
                            options=["Skip (Do not import)", "Override (Replace old bill)", "Add Duplicate (Keep both)"],
                            key=f"res_{bill}"
                        )
                    
                    if st.form_submit_button("Confirm Resolutions", type="primary"):
                        df_to_process = st.session_state.raw_upload_data.copy()
                        bills_to_delete = []
                        
                        for bill, action in resolutions.items():
                            if "Skip" in action:
                                df_to_process = df_to_process[df_to_process[1] != bill]
                            elif "Override" in action:
                                bills_to_delete.append(bill)
                        
                        st.session_state.bills_to_delete = bills_to_delete
                        st.session_state.df_to_process = df_to_process
                        st.session_state.resolving_duplicates = False
                        st.rerun()

            # 3. FUZZY MATCHING & UNIT COMPARISON
            if not st.session_state.get("resolving_duplicates", False) and "auto_matched" not in st.session_state and "df_to_process" in st.session_state:
                df_to_process = st.session_state.df_to_process
                auto_matched_records = []
                unmatched_raw_records = []
                
                qty_multiplier = 1
                if bulk_type in ["Sales", "Purchase Returns"]:
                    qty_multiplier = -1
                
                for index, row in df_to_process.iterrows():
                    date_val = row[0]
                    bill_val = str(row[1]).strip()
                    qty_val = float(row[2]) if pd.notna(row[2]) else 0.0
                    
                    sales_unit = str(row[9]).strip() if len(row) > 9 and pd.notna(row[9]) else ""
                    raw_item_code = str(row[4]).strip() if len(row) > 4 and pd.notna(row[4]) else ""
                    other_desc = str(row[5]).strip() if len(row) > 5 and pd.notna(row[5]) else ""
                    
                    mapped_name = code_dict.get(raw_item_code, raw_item_code)
                    sku_unit = unit_dict.get(raw_item_code, "")
                    
                    if sales_unit and sku_unit:
                        if sales_unit.lower() == sku_unit.lower():
                            unit_check = f"✅ {sales_unit}"
                        else:
                            unit_check = f"⚠️ File: {sales_unit} | SKU: {sku_unit}"
                    else:
                        unit_check = f"{sales_unit}" if sales_unit else f"{sku_unit}"
                    
                    mapping_kw = ""
                    if mapped_name and mapped_name.lower() != 'nan':
                        merged_description = f"{mapped_name} - {other_desc}".strip(" -")
                        if raw_item_code in code_dict and str(code_dict[raw_item_code]).strip().lower() != 'nan':
                            mapping_kw = str(code_dict[raw_item_code]).strip()
                    else:
                        merged_description = other_desc
                        
                    is_blank_date = pd.isna(date_val) or str(date_val).strip() == "" or str(date_val).strip().lower() in ['nan', 'nat', 'none']
                    if is_blank_date and 'cancel' in merged_description.lower():
                        auto_matched_records.append({
                            "Date": datetime.now().strftime("%d/%m/%Y"), 
                            "Fiscal Year": get_nepali_fiscal_year(datetime.now()),
                            "Bill Number": bill_val, 
                            "Group": "Cancelled", 
                            "Item_Name": "Cancelled Bill",
                            "Purchase Qty": 0, 
                            "Purchase Unit": "-", 
                            "Stock Qty Added": 0, 
                            "Stock Unit": "-", 
                            "Original Billed Data": merged_description,
                            "Unit Check": "🚫", 
                            "AI Reasoning": "🟢 Cancelled Bill Detected", 
                            "Display_Desc": merged_description
                        })
                        continue 
                    
                    matched_item, debug_log = find_best_match(merged_description, mapped_keywords=mapping_kw)
                    debug_str = format_debug_string(debug_log)
                    
                    if matched_item:
                        item_details = products_df[products_df['Item_Name'] == matched_item].iloc[0]
                        
                        pur_qty_val = qty_val if bulk_type in ["Purchases", "Purchase Returns"] else 0
                        pur_unit_val = item_details['Purchase_Unit'] if bulk_type in ["Purchases", "Purchase Returns"] else "-"
                        
                        auto_matched_records.append({
                            "Date": date_val,
                            "Fiscal Year": get_nepali_fiscal_year(date_val),
                            "Bill Number": bill_val, 
                            "Group": item_details['Group'], 
                            "Item_Name": matched_item,
                            "Purchase Qty": pur_qty_val, 
                            "Purchase Unit": pur_unit_val, 
                            "Stock Qty Added": abs(qty_val) * qty_multiplier, 
                            "Stock Unit": item_details['Sales_Unit'], 
                            "Original Billed Data": merged_description,
                            "Unit Check": unit_check, 
                            "AI Reasoning": debug_str, 
                            "Display_Desc": merged_description
                        })
                    else:
                        unmatched_raw_records.append({
                            "Date": date_val, 
                            "Bill Number": bill_val, 
                            "Qty": qty_val, 
                            "Description": merged_description,
                            "Original Billed Data": merged_description,
                            "AI Reasoning": debug_str, 
                            "Unit Check": unit_check 
                        })
                
                st.session_state.auto_matched = auto_matched_records
                st.session_state.unmatched = unmatched_raw_records
                st.rerun()

            # 4. FINAL REVIEW & COMMIT UI
            if "auto_matched" in st.session_state:
                auto_matched = st.session_state.auto_matched
                unmatched = st.session_state.unmatched
                
                def commit_sales_to_db(records_to_save, new_learned=None):
                    clean_records = [{k: v for k, v in r.items() if k not in ['Display_Desc', 'Original Billed Data', 'Unit Check', 'AI Reasoning']} for r in records_to_save]
                    new_records_df = pd.DataFrame(clean_records)
                    
                    current_purchases = purchases_df.copy()
                    bills_to_delete = st.session_state.get("bills_to_delete", [])
                    
                    if bills_to_delete and not current_purchases.empty and 'Bill Number' in current_purchases.columns:
                        current_purchases = current_purchases[~current_purchases['Bill Number'].astype(str).str.strip().isin(bills_to_delete)]
                    
                    if not new_records_df.empty or bills_to_delete:
                        if not new_records_df.empty:
                            updated_purchases = pd.concat([current_purchases, new_records_df], ignore_index=True)
                        else:
                            updated_purchases = current_purchases
                        save_purchases(updated_purchases)
                    
                    if new_learned:
                        new_rules_df = pd.DataFrame(new_learned)
                        updated_learnings = pd.concat([learned_df, new_rules_df], ignore_index=True)
                        updated_learnings['Billed_Description'] = updated_learnings['Billed_Description'].astype(str).str.strip().str.upper()
                        updated_learnings = updated_learnings.drop_duplicates(subset=["Billed_Description"], keep="last")
                        save_learned_mappings(updated_learnings)
                    
                    st.cache_data.clear()
                    st.session_state.committed_file_name = st.session_state.processed_file_name
                    
                    keys_to_clear = ['auto_matched', 'unmatched', 'raw_upload_data', 'resolving_duplicates', 'df_to_process', 'bills_to_delete']
                    for key in keys_to_clear:
                        if key in st.session_state:
                            del st.session_state[key]
                            
                    st.rerun()

                if auto_matched:
                    st.success(f"✅ Automatically matched {len(auto_matched)} items.")
                    display_df = pd.DataFrame(auto_matched).drop(columns=['Display_Desc', 'Group', 'Purchase Qty', 'Purchase Unit', 'Stock Qty Added', 'Stock Unit', 'Fiscal Year'], errors='ignore')
                    
                    buffer = io.BytesIO()
                    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                        display_df.to_excel(writer, index=False, sheet_name='Auto_Matched')
                    buffer.seek(0)

                    st.download_button(
                        label="📥 Download Auto-Matched as Excel",
                        data=buffer,
                        file_name=f"AutoMatched_{datetime.now().strftime('%d-%m-%Y')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )

                    st.write("✏️ **Review and override any incorrect automatic matches below:**")
                    
                    extended_stock_items = ["Cancelled Bill"] + stock_items
                    
                    edited_auto_df = st.data_editor(
                        display_df,
                        column_config={
                            "Item_Name": st.column_config.SelectboxColumn(
                                "Item_Name (Editable)",
                                help="Select the correct master product to override the AI",
                                options=extended_stock_items,
                                required=True
                            ),
                            "AI Reasoning": st.column_config.TextColumn("AI Reasoning", width="large")
                        },
                        use_container_width=True,
                        key="auto_match_editor"
                    )
                else:
                    edited_auto_df = pd.DataFrame()

                if unmatched:
                    st.warning(f"⚠️ {len(unmatched)} items could not be matched automatically.")
                    with st.form("manual_mapping_form"):
                        manual_selections = []
                        h1, h2, h3, h4, h5, h6 = st.columns([1, 1.5, 0.5, 1, 2.5, 2])
                        h1.write("**Bill No**")
                        h2.write("**Billed Description**")
                        h3.write("**Qty**")
                        h4.write("**Unit**")
                        h5.write("**AI Reasoning**")
                        h6.write("**Match to Master Product**")
                        st.divider()
                        
                        extended_stock_items = ["-- Skip / Do Not Import --", "Cancelled Bill"] + stock_items
                        
                        for idx, un_row in enumerate(unmatched):
                            c1, c2, c3, c4, c5, c6 = st.columns([1, 1.5, 0.5, 1, 2.5, 2])
                            with c1: st.write(un_row['Bill Number'])
                            with c2: st.write(un_row['Original Billed Data'])
                            with c3: st.write(un_row['Qty'])
                            with c4: st.write(un_row.get('Unit Check', '-'))
                            with c5: st.caption(un_row.get('AI Reasoning', '-'))
                            with c6:
                                selected = st.selectbox("Match", options=extended_stock_items, key=f"un_{idx}", label_visibility="collapsed")
                            manual_selections.append((un_row, selected))
                            
                        st.write("")
                        if st.form_submit_button("Confirm Manual Matches & Commit ALL Sales", type="primary"):
                            final_records_to_commit = []
                            new_learned_rules = [] 
                            
                            qty_multiplier = 1
                            if bulk_type in ["Sales", "Purchase Returns"]:
                                qty_multiplier = -1
                            
                            if not edited_auto_df.empty:
                                for idx, row in edited_auto_df.iterrows():
                                    orig_row = auto_matched[idx]
                                    current_item = row['Item_Name']
                                    date_to_save = orig_row['Date'] if orig_row['Date'] else datetime.now().strftime("%d/%m/%Y")
                                    
                                    if current_item == "Cancelled Bill":
                                        group_val = "Cancelled"
                                        sales_unit = "-"
                                        stock_qty_added = 0
                                        pur_qty = 0
                                        pur_unit = "-"
                                    else:
                                        item_details = products_df[products_df['Item_Name'] == current_item].iloc[0]
                                        group_val = item_details['Group']
                                        sales_unit = item_details['Sales_Unit']
                                        
                                        original_qty = float(st.session_state.raw_upload_data[st.session_state.raw_upload_data[1].astype(str).str.strip() == orig_row['Bill Number']].iloc[0][2])
                                        stock_qty_added = abs(original_qty) * qty_multiplier
                                        
                                        pur_qty = abs(original_qty) if bulk_type in ["Purchases", "Purchase Returns"] else 0
                                        pur_unit = item_details['Purchase_Unit'] if bulk_type in ["Purchases", "Purchase Returns"] else "-"
                                        
                                    final_records_to_commit.append({
                                        "Date": date_to_save, 
                                        "Fiscal Year": get_nepali_fiscal_year(date_to_save),
                                        "Bill Number": orig_row['Bill Number'], 
                                        "Group": group_val, 
                                        "Item_Name": current_item,
                                        "Purchase Qty": pur_qty, 
                                        "Purchase Unit": pur_unit, 
                                        "Stock Qty Added": stock_qty_added, 
                                        "Stock Unit": sales_unit
                                    })
                                    
                                    if current_item != orig_row['Item_Name'] and current_item != "Cancelled Bill":
                                        new_learned_rules.append({
                                            "Billed_Description": str(orig_row['Display_Desc']).strip().upper(),
                                            "Matched_Item_Name": current_item
                                        })
                            
                            for un_row, selected_item in manual_selections:
                                if selected_item != "-- Skip / Do Not Import --":
                                    date_to_save = un_row['Date'] if un_row['Date'] else datetime.now().strftime("%d/%m/%Y")
                                    if selected_item == "Cancelled Bill":
                                        group_val = "Cancelled"
                                        sales_unit = "-"
                                        stock_qty_added = 0
                                        pur_qty = 0
                                        pur_unit = "-"
                                    else:
                                        item_details = products_df[products_df['Item_Name'] == selected_item].iloc[0]
                                        group_val = item_details['Group']
                                        sales_unit = item_details['Sales_Unit']
                                        stock_qty_added = abs(un_row['Qty']) * qty_multiplier
                                        
                                        pur_qty = abs(un_row['Qty']) if bulk_type in ["Purchases", "Purchase Returns"] else 0
                                        pur_unit = item_details['Purchase_Unit'] if bulk_type in ["Purchases", "Purchase Returns"] else "-"
                                        
                                        new_learned_rules.append({
                                            "Billed_Description": str(un_row['Description']).strip().upper(),
                                            "Matched_Item_Name": selected_item
                                        })
                                        
                                    final_records_to_commit.append({
                                        "Date": date_to_save,
                                        "Fiscal Year": get_nepali_fiscal_year(date_to_save),
                                        "Bill Number": un_row['Bill Number'], 
                                        "Group": group_val, 
                                        "Item_Name": selected_item,
                                        "Purchase Qty": pur_qty, 
                                        "Purchase Unit": pur_unit, 
                                        "Stock Qty Added": stock_qty_added, 
                                        "Stock Unit": sales_unit
                                    })
                            
                            if final_records_to_commit or st.session_state.get("bills_to_delete"):
                                commit_sales_to_db(final_records_to_commit, new_learned_rules)
                else:
                    if st.button("Commit Sales to Database", type="primary"):
                        final_records_to_commit = []
                        new_learned_rules = []
                        
                        qty_multiplier = 1
                        if bulk_type in ["Sales", "Purchase Returns"]:
                            qty_multiplier = -1
                        
                        if not edited_auto_df.empty:
                            for idx, row in edited_auto_df.iterrows():
                                orig_row = auto_matched[idx]
                                current_item = row['Item_Name']
                                date_to_save = orig_row['Date'] if orig_row['Date'] else datetime.now().strftime("%d/%m/%Y")
                                
                                if current_item == "Cancelled Bill":
                                    group_val = "Cancelled"
                                    sales_unit = "-"
                                    stock_qty_added = 0
                                    pur_qty = 0
                                    pur_unit = "-"
                                else:
                                    item_details = products_df[products_df['Item_Name'] == current_item].iloc[0]
                                    group_val = item_details['Group']
                                    sales_unit = item_details['Sales_Unit']
                                    stock_qty_added = abs(orig_row['Stock Qty Added']) * qty_multiplier
                                    
                                    pur_qty = orig_row['Purchase Qty'] if bulk_type in ["Purchases", "Purchase Returns"] else 0
                                    pur_unit = item_details['Purchase_Unit'] if bulk_type in ["Purchases", "Purchase Returns"] else "-"
                                
                                final_records_to_commit.append({
                                    "Date": date_to_save,
                                    "Fiscal Year": get_nepali_fiscal_year(date_to_save),
                                    "Bill Number": orig_row['Bill Number'], 
                                    "Group": group_val, 
                                    "Item_Name": current_item,
                                    "Purchase Qty": pur_qty, 
                                    "Purchase Unit": pur_unit, 
                                    "Stock Qty Added": stock_qty_added, 
                                    "Stock Unit": sales_unit
                                })
                                
                                if current_item != orig_row['Item_Name'] and current_item != "Cancelled Bill":
                                    new_learned_rules.append({
                                        "Billed_Description": str(orig_row['Display_Desc']).strip().upper(),
                                        "Matched_Item_Name": current_item
                                    })
                                    
                        if final_records_to_commit or st.session_state.get("bills_to_delete"):
                            commit_sales_to_db(final_records_to_commit, new_learned_rules)

    else:
        keys_to_clear = ['auto_matched', 'unmatched', 'processed_file_name', 'raw_upload_data', 'resolving_duplicates', 'df_to_process', 'bills_to_delete', 'committed_file_name']
        for key in keys_to_clear:
            if key in st.session_state:
                del st.session_state[key]

# --- TAB 3: VIEW INVENTORY & LEDGER ---
with tab3:
    st.header("Live Stock Levels & Ledger")
    
    st.subheader("Stock Summary Report")
    summary_items = st.multiselect("Select Item(s) to view (Leave blank for all)", options=products_df['Item_Name'].dropna().unique())
    
    if not purchases_df.empty:
        inventory_summary = purchases_df.groupby(['Group', 'Item_Name', 'Stock Unit'])['Stock Qty Added'].sum().reset_index()
        inventory_summary.rename(columns={'Stock Qty Added': 'Total Stock on Hand'}, inplace=True)
        
        if summary_items:
            inventory_summary = inventory_summary[inventory_summary['Item_Name'].isin(summary_items)]
            
        st.dataframe(inventory_summary, use_container_width=True, hide_index=True)
        
        st.divider()
        st.subheader("Item Stock Ledger")
        
        c1, c2 = st.columns(2)
        with c1:
            ledger_item = st.selectbox("Select a particular item to view its ledger", options=products_df['Item_Name'].dropna().unique(), index=None)
        with c2:
            fy_options = ["All"] + sorted(purchases_df['Fiscal Year'].dropna().unique().tolist(), reverse=True) if not purchases_df.empty and 'Fiscal Year' in purchases_df.columns else ["All"]
            selected_fy = st.selectbox("Filter Ledger by Fiscal Year", options=fy_options)
            
        if ledger_item:
            ledger = purchases_df[purchases_df['Item_Name'] == ledger_item].copy()
            ledger['Date_Parsed'] = pd.to_datetime(ledger['Date'], dayfirst=True, errors='coerce')
            ledger = ledger.sort_values('Date_Parsed')
            
            # Run the global cumulative sum FIRST for accounting accuracy
            ledger['Running Balance'] = ledger['Stock Qty Added'].cumsum()
            
            # Then filter the view by Fiscal Year if requested
            if selected_fy != "All":
                ledger = ledger[ledger['Fiscal Year'] == selected_fy]
                
            st.dataframe(ledger[['Fiscal Year', 'Date', 'Bill Number', 'Purchase Qty', 'Stock Qty Added', 'Running Balance']], use_container_width=True, hide_index=True)
    else:
        st.write("No inventory data found.")

# --- TAB 4: PRODUCT MASTER & AI MEMORY ---
with tab4:
    st.header("Database & AI Memory")
    
    st.subheader("1. Base Product Master")
    st.dataframe(products_df, use_container_width=True, hide_index=True)
    
    st.divider()
    
    st.subheader("2. AI Learned Mappings (Memory Bank)")
    st.write("The AI automatically saves rules when you manually match items. It uses these to get smarter over time.")
    
    if not learned_df.empty:
        st.dataframe(learned_df, use_container_width=True, hide_index=True)
        
        if st.button("🧹 Optimize & Clean Duplicates from AI Memory"):
            clean_df = learned_df.copy()
            clean_df['Billed_Description'] = clean_df['Billed_Description'].astype(str).str.strip().str.upper()
            clean_df = clean_df.drop_duplicates(subset=["Billed_Description"], keep="last")
            save_learned_mappings(clean_df)
            st.cache_data.clear()
            st.success("✅ AI Memory Optimized! All duplicate formatting variations have been removed.")
            st.rerun()
    else:
        st.info("The AI Memory is currently empty. It will learn when you manually map unmatched items!")

# --- TAB 5: UNIFIED EDIT TRANSACTIONS ---
with tab5:
    st.header("Edit Database Transactions")
    
    if purchases_df.empty:
        st.info("No records found in the database.")
    else:
        df_filtered = purchases_df.copy()
        df_filtered['Date_Str'] = pd.to_datetime(df_filtered['Date'], dayfirst=True, errors='coerce').dt.strftime('%d/%m/%Y')
        
        # Add FY to the dropdown label so it's easier to find specific bills
        fy_col = df_filtered['Fiscal Year'].astype(str) if 'Fiscal Year' in df_filtered.columns else "FY Unknown"
        df_filtered['Bill_Label'] = fy_col + " | " + df_filtered['Bill Number'].astype(str).str.strip() + " (Date: " + df_filtered['Date_Str'].astype(str) + ")"
        
        bill_list = sorted(df_filtered['Bill_Label'].dropna().unique())
        selected_label = st.selectbox(f"Search & Select Reference / Bill to Edit", options=bill_list, index=None)

        if selected_label:
            bill_data = df_filtered[df_filtered['Bill_Label'] == selected_label].copy()
            original_indices = bill_data.index
            
            # Drop purely visual/temporary columns before passing to the editor
            cols_to_drop = ['Bill_Label', 'Date_Str']
            if 'Date_Parsed' in bill_data.columns: cols_to_drop.append('Date_Parsed')
            display_df = bill_data.drop(columns=cols_to_drop)
            
            st.write("✏️ **Edit quantities or details below:**")
            edited_df = st.data_editor(display_df, use_container_width=True)

            if st.button("💾 Save Transaction Changes", type="primary"):
                final_df = purchases_df.drop(index=original_indices).copy()
                final_df = pd.concat([final_df, edited_df], ignore_index=True)
                save_purchases(final_df)
                st.cache_data.clear()
                st.success("✅ Transaction updated successfully!")
                st.rerun()
