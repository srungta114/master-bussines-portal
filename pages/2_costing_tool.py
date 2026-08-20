import streamlit as st
import pandas as pd
from datetime import datetime, timezone

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


def render_add_new_sku_form(df_master, key_prefix, select_after_create_key=None):
    """Drop-in 'create a new SKU on the fly' expander - for whenever the
    material someone's trying to cost isn't in Product Master yet, so they
    don't have to abandon the bill and go find an admin tool. Writes
    straight to product_master (the SAME collection Hardware Inventory
    reads from/writes to), so the new SKU is usable there immediately too.

    Deliberately NOT wrapped in st.form: the Group/Unit dropdowns need to
    dynamically reveal a "type a new one" text box the moment "Add New..."
    is picked, and st.form batches all widget interactions until submit -
    it wouldn't react to that selection until the whole form was already
    submitted. Using plain widgets means a few extra script reruns while
    filling this in, but nothing here touches Firestore until the actual
    Create SKU click, so that cost is negligible.

    If select_after_create_key is given, the newly created item is
    pre-selected in the smart_item_search box with that key on the rerun
    that follows.
    """
    existing_groups = sorted(df_master['Group'].dropna().unique().tolist()) if 'Group' in df_master.columns else []
    existing_p_units = sorted(df_master['Purchase_Unit'].dropna().unique().tolist()) if 'Purchase_Unit' in df_master.columns else []
    existing_s_units = sorted(df_master['Sales_Unit'].dropna().unique().tolist()) if 'Sales_Unit' in df_master.columns else []
    existing_names_lower = set(df_master['Item_Name'].dropna().str.strip().str.lower()) if 'Item_Name' in df_master.columns else set()

    ADD_NEW = "➕ Add New..."
    field_keys = [
        f"{key_prefix}_new_item_name", f"{key_prefix}_new_group_choice", f"{key_prefix}_new_group_typed",
        f"{key_prefix}_new_subgroup", f"{key_prefix}_new_p_unit_choice", f"{key_prefix}_new_p_unit_typed",
        f"{key_prefix}_new_s_unit_choice", f"{key_prefix}_new_s_unit_typed", f"{key_prefix}_new_conv_factor",
    ]

    with st.expander("➕ Can't find the material? Add a new SKU"):
        new_item_name = st.text_input("Item Name*", key=f"{key_prefix}_new_item_name")

        gc1, gc2 = st.columns(2)
        group_choice = gc1.selectbox(
            "Group*", options=existing_groups + [ADD_NEW],
            index=None, key=f"{key_prefix}_new_group_choice",
        )
        new_group_typed = ""
        if group_choice == ADD_NEW:
            new_group_typed = gc2.text_input("New Group Name", key=f"{key_prefix}_new_group_typed")

        new_sub_group = st.text_input("Sub-Group (optional)", key=f"{key_prefix}_new_subgroup")

        uc1, uc2 = st.columns(2)
        with uc1:
            p_unit_choice = st.selectbox(
                "Purchase Unit*", options=existing_p_units + [ADD_NEW],
                index=None, key=f"{key_prefix}_new_p_unit_choice",
            )
            new_p_unit_typed = ""
            if p_unit_choice == ADD_NEW:
                new_p_unit_typed = st.text_input("New Purchase Unit", key=f"{key_prefix}_new_p_unit_typed")
        with uc2:
            s_unit_choice = st.selectbox(
                "Sales Unit*", options=existing_s_units + [ADD_NEW],
                index=None, key=f"{key_prefix}_new_s_unit_choice",
            )
            new_s_unit_typed = ""
            if s_unit_choice == ADD_NEW:
                new_s_unit_typed = st.text_input("New Sales Unit", key=f"{key_prefix}_new_s_unit_typed")

        new_conv_factor = st.number_input(
            "Conversion Factor (Purchase Unit → Sales Unit)",
            min_value=0.0001, value=1.0, step=0.1, key=f"{key_prefix}_new_conv_factor",
            help="How many Sales Units make up one Purchase Unit. Leave at 1.0 if Purchase and Sales units are the same.",
        )

        if st.button("✅ Create SKU", key=f"{key_prefix}_create_sku_btn", type="primary"):
            group_val = new_group_typed if group_choice == ADD_NEW else group_choice
            p_unit_val = new_p_unit_typed if p_unit_choice == ADD_NEW else p_unit_choice
            s_unit_val = new_s_unit_typed if s_unit_choice == ADD_NEW else s_unit_choice

            if not new_item_name.strip():
                st.error("Item Name is required.")
            elif new_item_name.strip().lower() in existing_names_lower:
                st.error(f"'{new_item_name.strip()}' already exists in Product Master - search for it above instead.")
            elif not group_val or not str(group_val).strip():
                st.error("Group is required.")
            elif not p_unit_val or not str(p_unit_val).strip():
                st.error("Purchase Unit is required.")
            elif not s_unit_val or not str(s_unit_val).strip():
                st.error("Sales Unit is required.")
            else:
                status = save_new_product(new_item_name, group_val, new_sub_group or "", p_unit_val, s_unit_val, new_conv_factor)
                if status == "duplicate":
                    st.error(f"'{new_item_name.strip()}' already exists in Product Master - search for it above instead.")
                else:
                    st.cache_data.clear()
                    for k in field_keys:
                        st.session_state.pop(k, None)
                    if select_after_create_key:
                        st.session_state[select_after_create_key] = new_item_name.strip()
                    st.success(f"✅ Created new SKU: {new_item_name.strip()}")
                    st.rerun()


# --- 2. SECURE DATA LOADERS (FIRESTORE) ---
import re as _re

MAX_RECENT_COSTINGS = 5


def sanitize_doc_id(name):
    """Firestore document IDs can't contain '/' and shouldn't be empty."""
    doc_id = _re.sub(r'[/\\]', '_', str(name).strip())
    doc_id = doc_id[:1500]
    return doc_id if doc_id else "unnamed"


@st.cache_data(ttl=3600)
def load_products():
    try:
        db = st.session_state.db
        docs = db.collection("product_master").stream()
        df = pd.DataFrame([doc.to_dict() for doc in docs])
        return df
    except Exception as e:
        st.error(f"Failed to load Product Master: {e}")
        return pd.DataFrame()


def save_new_product(item_name, group, sub_group, purchase_unit, sales_unit, conversion_factor):
    """Creates a new SKU in product_master - the SAME collection Hardware
    Inventory reads from/writes to, so a SKU added here is immediately
    usable there too (and vice versa). Doc ID is the sanitized item name,
    matching the convention used everywhere else this collection is
    written. Returns "ok" or "duplicate" (an item with this name already
    exists - overwriting it here would silently wipe out whatever
    Group/Units/Conversion Factor it already had)."""
    db = st.session_state.db
    doc_id = sanitize_doc_id(item_name)
    doc_ref = db.collection("product_master").document(doc_id)
    if doc_ref.get().exists:
        return "duplicate"
    doc_ref.set({
        "Item_Name": item_name.strip(),
        "Group": group.strip(),
        "Sub-Group": sub_group.strip(),
        "Purchase_Unit": purchase_unit.strip(),
        "Sales_Unit": sales_unit.strip(),
        "Conversion_Factor": conversion_factor,
    })
    return "ok"


@st.cache_data(ttl=3600)
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


def find_bill_entries(df_materials_costing, seller, bill_no, date=None):
    """Searches every material's (capped, last-5) recent_costings history
    for entries matching a given Seller + Bill No (+ optionally Date, to
    disambiguate a supplier reusing the same bill number across different
    years), keeping track of exactly where each match sits (material + its
    position in that material's array) so it can be edited or deleted in
    place afterward.

    Runs entirely in memory over the already-loaded/cached
    df_materials_costing - no Firestore reads here. Because each material
    only keeps its 5 most recent costings, a bill that's since been
    superseded by 5+ newer purchases for the same material will no longer
    appear here - that's a real limitation of the capped-history model, not
    a bug, and the caller should say so if no matches turn up."""
    clean_seller = str(seller).strip().lower()
    clean_bill = str(bill_no).strip().lower()
    clean_date = str(date).strip() if date else None
    matches = []
    if df_materials_costing.empty:
        return matches

    for _, mat_row in df_materials_costing.iterrows():
        material = mat_row.get('Material')
        history = mat_row.get('recent_costings') or []
        for idx, entry in enumerate(history):
            if (str(entry.get('Seller', '')).strip().lower() != clean_seller
                    or str(entry.get('Bill_No', '')).strip().lower() != clean_bill):
                continue
            if clean_date and str(entry.get('Date', '')).strip() != clean_date:
                continue
            matches.append({
                'material': material,
                'entry_index': idx,
                'is_latest': idx == 0,
                **entry,
            })
    return matches


def _entry_still_matches(stored_entry, expected):
    """Sanity check before writing: confirms the entry sitting at
    entry_index right now is still the same one we found earlier, in case
    someone else edited that material's history in the meantime (the
    cached df_materials_costing can be up to 60 seconds stale). Compares
    Bill_No, Seller, and Date - if any differ, the index has shifted and
    it's not safe to blindly overwrite/delete."""
    for field in ("Bill_No", "Seller", "Date"):
        if str(stored_entry.get(field, "")).strip().lower() != str(expected.get(field, "")).strip().lower():
            return False
    return True


def update_costing_entry(material, entry_index, expected_entry, updated_fields):
    """Overwrites one entry inside a material's recent_costings array in
    place (same position, doesn't re-sort). If that entry happens to be the
    material's latest (index 0), also refreshes the denormalized top-level
    fields on the doc so Quick Costing Search's headline numbers stay in
    sync with the edit.

    Returns "ok", "stale", or "missing" - callers should tell the person to
    refresh and retry on anything other than "ok" rather than assuming it
    worked."""
    db = st.session_state.db
    doc_ref = db.collection("materials_costing").document(sanitize_doc_id(material))
    snap = doc_ref.get()
    if not snap.exists:
        return "missing"

    data = snap.to_dict()
    history = data.get("recent_costings", [])
    if entry_index >= len(history) or not _entry_still_matches(history[entry_index], expected_entry):
        return "stale"

    history[entry_index] = {**history[entry_index], **updated_fields}
    update_payload = {"recent_costings": history}
    if entry_index == 0:
        update_payload.update(updated_fields)
    doc_ref.update(update_payload)
    return "ok"


def delete_costing_entry(material, entry_index, expected_entry):
    """Removes one entry from a material's recent_costings array. If the
    removed entry was the latest (index 0) and older history still exists,
    the next-most-recent entry gets promoted into the denormalized
    top-level fields so Quick Costing Search doesn't keep showing a deleted
    entry's numbers. If no history is left at all, the stale top-level
    fields are left as-is (nothing better to fall back to).

    Returns "ok", "stale", or "missing", same convention as
    update_costing_entry()."""
    db = st.session_state.db
    doc_ref = db.collection("materials_costing").document(sanitize_doc_id(material))
    snap = doc_ref.get()
    if not snap.exists:
        return "missing"

    data = snap.to_dict()
    history = data.get("recent_costings", [])
    if entry_index >= len(history) or not _entry_still_matches(history[entry_index], expected_entry):
        return "stale"

    was_latest = entry_index == 0
    del history[entry_index]
    update_payload = {"recent_costings": history}
    if was_latest and history:
        update_payload.update(history[0])
    doc_ref.update(update_payload)
    return "ok"


def get_last_entered_bill(df_materials_costing):
    """Finds the most recently SAVED bill (by Entered_At save timestamp,
    NOT the purchase Date typed on the bill - those can be backdated) across
    every material's history, and returns every line item that was part of
    that same save.

    Only entries saved after this feature shipped carry an Entered_At
    timestamp, so bills entered before that won't show up here - that's
    expected, not missing data."""
    if df_materials_costing.empty or 'recent_costings' not in df_materials_costing.columns:
        return None, pd.DataFrame()

    best_entered_at = None
    best_key = None
    for _, mat_row in df_materials_costing.iterrows():
        for entry in (mat_row.get('recent_costings') or []):
            ts = entry.get('Entered_At')
            if not ts:
                continue
            if best_entered_at is None or ts > best_entered_at:
                best_entered_at = ts
                best_key = (
                    str(entry.get('Seller', '')).strip().lower(),
                    str(entry.get('Bill_No', '')).strip().lower(),
                )

    if best_entered_at is None:
        return None, pd.DataFrame()

    rows = []
    for _, mat_row in df_materials_costing.iterrows():
        material = mat_row.get('Material')
        for entry in (mat_row.get('recent_costings') or []):
            key = (
                str(entry.get('Seller', '')).strip().lower(),
                str(entry.get('Bill_No', '')).strip().lower(),
            )
            if key == best_key and entry.get('Entered_At') == best_entered_at:
                rows.append({**entry, 'Material': material})

    return best_entered_at, pd.DataFrame(rows)

def lazy_load(session_key, loader_fn, label):
    """Only calls loader_fn() (a live Firestore read) when the person
    explicitly clicks a button - nothing is fetched just because the tool
    was opened. Once loaded, the result lives in st.session_state (not just
    st.cache_data, whose short TTL can still refire on its own on a later
    rerun) until the person clicks Refresh, or a write action clears it via
    clear_lazy_cache() so the next view is guaranteed fresh.

    Returns None if not yet loaded - virtually every section below already
    treats an empty/missing df_materials_costing gracefully (it was written
    defensively for brand-new deployments), so callers can just fall back
    to an empty DataFrame rather than needing special-case branches."""
    is_loaded = session_key in st.session_state
    btn_label = f"🔄 Refresh {label}" if is_loaded else f"📥 Load {label}"
    if st.button(btn_label, key=f"lazy_btn_{session_key}"):
        with st.spinner(f"Fetching {label}..."):
            st.session_state[session_key] = loader_fn()
        st.rerun()
    if not is_loaded:
        st.caption(f"Not fetched yet - click \"Load {label}\" above to search or edit existing costing history.")
        return None
    return st.session_state[session_key]


def clear_lazy_cache():
    """Call this alongside st.cache_data.clear() after any write, so the
    next view is forced to fetch fresh data instead of continuing to show
    whatever was loaded before the edit."""
    st.session_state.pop("sd_materials_costing", None)


df_master = load_products()

st.title("🏗️ Material & Inventory Ledger")

st.subheader("📦 Costing Details")
st.caption(
    "The full costing history (every material's recent purchase records) isn't "
    "fetched automatically when you open this tool - only Product Master (the "
    "item catalog, needed for search boxes below) loads right away. Click below "
    "to fetch costing history when you actually need to search, review, or edit it."
)
df_materials_costing = lazy_load("sd_materials_costing", load_materials_costing, "Costing Details")
if df_materials_costing is None:
    df_materials_costing = pd.DataFrame()
df_purchases = get_flat_costing_history(df_materials_costing)


# Extract unique sellers
if not df_purchases.empty and 'Seller' in df_purchases.columns:
    existing_sellers = sorted([str(s).strip() for s in df_purchases['Seller'].dropna().unique() if str(s).strip() != ""])
else:
    existing_sellers = []

# Initialize Session State
if 'bill_items' not in st.session_state:
    st.session_state.bill_items = []


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

# --- 3.4 LAST BILL ENTERED ---
st.header("🕐 Last Bill Entered")
with st.expander("Check the most recent bill saved to the system", expanded=False):
    if st.button("🔍 Check Last Bill Entered"):
        last_entered_at, last_bill_df = get_last_entered_bill(df_materials_costing)

        if last_entered_at is None:
            st.info(
                "No timestamped entries found yet. Only bills saved after this feature "
                "shipped carry a save timestamp, so this will show your most recent "
                "save once you save a new bill below."
            )
        else:
            try:
                local_dt = datetime.fromisoformat(last_entered_at)
                when_str = local_dt.strftime('%Y-%m-%d %H:%M UTC')
            except ValueError:
                when_str = last_entered_at

            first_row = last_bill_df.iloc[0]
            st.success(
                f"**Last bill entered:** {first_row.get('Bill_No', 'N/A')} from "
                f"{first_row.get('Seller', 'N/A')}, dated {first_row.get('Date', 'N/A')} "
                f"— saved {when_str}"
            )
            display_cols = [c for c in [
                'Material', 'Qty_Purchase', 'Unit_Purchase', 'Rate_Purchase',
                'Landed_Rate_Purchase', 'Cost_Pc', 'Total_Item_Cost',
            ] if c in last_bill_df.columns]
            st.dataframe(last_bill_df[display_cols], use_container_width=True, hide_index=True)

st.divider()

# --- 3.5 EDIT OR DELETE OLD BILLS ---
st.header("✏️ Edit Old Bills")
with st.expander("Modify or Delete an existing bill", expanded=False):
    st.caption(
        "A 'bill' here means every material line that shares the same Seller + Bill No. "
        "Since each material only keeps its 5 most recent costings, a bill that's since "
        "been superseded by 5+ newer purchases for a given material will no longer show "
        "up for that material - that's a limit of the capped-history model, not a bug."
    )

    ec1, ec2 = st.columns(2)
    edit_seller = ec1.selectbox(
        "Seller Company Name", options=existing_sellers, index=None,
        placeholder="Select a Seller...", key="edit_bill_seller",
    )
    edit_bill_no = ec2.text_input("Bill No.", key="edit_bill_no")

    use_date_filter = st.checkbox(
        "Also match by Date (recommended if this Seller might have reused the same Bill No. across different years)",
        key="edit_bill_use_date",
    )
    edit_date = st.date_input("Purchase Date", key="edit_bill_date") if use_date_filter else None

    if edit_seller and edit_bill_no:
        matches = find_bill_entries(
            df_materials_costing, edit_seller, edit_bill_no,
            date=str(edit_date) if edit_date else None,
        )

        if not matches:
            date_note = f" on {edit_date}" if edit_date else ""
            st.warning(
                f"No entries found for Bill No. '{edit_bill_no}' from '{edit_seller}'{date_note} in "
                "any material's current history. It may have aged out of the 5-entry cap, "
                "or the Seller/Bill No.(/Date) may not match exactly."
            )
        else:
            st.write(f"**Found {len(matches)} material line(s) on this bill:**")

            match_df = pd.DataFrame(matches)
            match_df.insert(0, "Select", False)

            editable_cols = [
                'Select', 'material', 'Date', 'Qty_Purchase', 'Rate_Purchase',
                'Excise_Kg', 'Transport_Kg', 'Labour_Kg', 'Landed_Rate_Purchase',
                'Cost_Pc', 'Total_Item_Cost', 'Qty_Sales',
            ]
            editable_cols = [c for c in editable_cols if c in match_df.columns]
            display_df = match_df[editable_cols].rename(columns={'material': 'Material'})

            st.caption(
                "Edit values directly in the table below, then Save. Fields like Landed "
                "Rate and Cost/Pc are NOT auto-recalculated from the others (the original "
                "discount applied isn't stored), so update every affected field yourself."
            )

            edited_df = st.data_editor(
                display_df,
                use_container_width=True,
                hide_index=True,
                disabled=['Material', 'Date'],
                column_config={
                    "Select": st.column_config.CheckboxColumn("Select", help="Check to delete this line"),
                },
                key="edit_bill_editor",
            )

            save_col, del_col = st.columns(2)

            with save_col:
                if st.button("💾 Save Edited Values"):
                    results = {"ok": 0, "stale": 0, "missing": 0}
                    for i, edited_row in edited_df.iterrows():
                        original = matches[i]
                        updated_fields = {
                            k: edited_row[k] for k in [
                                'Rate_Purchase', 'Excise_Kg', 'Transport_Kg', 'Labour_Kg',
                                'Landed_Rate_Purchase', 'Cost_Pc', 'Total_Item_Cost',
                                'Qty_Purchase', 'Qty_Sales',
                            ] if k in edited_row
                        }
                        for k in updated_fields:
                            updated_fields[k] = float(updated_fields[k] or 0)

                        status = update_costing_entry(
                            material=original['material'],
                            entry_index=original['entry_index'],
                            expected_entry=original,
                            updated_fields=updated_fields,
                        )
                        results[status] = results.get(status, 0) + 1

                    st.cache_data.clear()
                    clear_lazy_cache()
                    if results.get("stale") or results.get("missing"):
                        st.warning(
                            f"Saved {results['ok']} line(s), but {results.get('stale', 0) + results.get('missing', 0)} "
                            "had changed since you loaded this page and were skipped. Refresh and try those again."
                        )
                    else:
                        st.success(f"✅ Saved {results['ok']} line(s).")
                    st.rerun()

            with del_col:
                selected_rows = edited_df[edited_df["Select"]]
                if not selected_rows.empty:
                    confirm_delete = st.checkbox(
                        f"I understand this will permanently delete {len(selected_rows)} line(s).",
                        key="edit_bill_delete_confirm",
                    )
                    if st.button("🗑️ Delete Selected Lines", type="primary", disabled=not confirm_delete):
                        results = {"ok": 0, "stale": 0, "missing": 0}
                        for i in selected_rows.index:
                            original = matches[i]
                            status = delete_costing_entry(
                                material=original['material'],
                                entry_index=original['entry_index'],
                                expected_entry=original,
                            )
                            results[status] = results.get(status, 0) + 1

                        st.cache_data.clear()
                        clear_lazy_cache()
                        if results.get("stale") or results.get("missing"):
                            st.warning(
                                f"Deleted {results['ok']} line(s), but {results.get('stale', 0) + results.get('missing', 0)} "
                                "had changed since you loaded this page and were skipped. Refresh and try those again."
                            )
                        else:
                            st.success(f"✅ Deleted {results['ok']} line(s).")
                        st.rerun()

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

    render_add_new_sku_form(df_master, key_prefix="costing", select_after_create_key="costing_select_product")

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
            #
            # One shared timestamp for every material in THIS bill (rather
            # than a fresh one per iteration) is what lets get_last_entered_bill()
            # and the Edit Old Bills search group them back together as
            # "one bill" later, even though each material is stored as a
            # separate document.
            entered_at = datetime.now(timezone.utc).isoformat()

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
                    "Entered_At": entered_at,
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
            clear_lazy_cache()
            st.success("✅ Database updated! Blended costings were prioritized and saved.")
            st.balloons()
            st.session_state.bill_items = [] 
            st.rerun()
            
        except Exception as e:
            st.error(f"Save failed: {e}")
