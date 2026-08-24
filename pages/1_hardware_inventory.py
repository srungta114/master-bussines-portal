import streamlit as st
import pandas as pd
from datetime import datetime
from difflib import get_close_matches
from google.cloud import firestore
import re
import io

# --- 1. SECURITY BOUNCER ---
# If the memory was wiped (refresh) or they bypassed the login, stop the page from crashing.
# Mirrors the Costing Tool's bouncer: "db" is the single shared Firestore
# client cached by app.py at login, so both tools now check for the same key
# instead of this page authenticating its own separate Sheets connection.
if "db" not in st.session_state:
    st.warning("🔒 Connection lost or not logged in.")
    st.info("Please click the Main Portal page in your sidebar to log in and reconnect to the database.")
    st.stop()  # This halts the script here so it doesn't crash on the next lines!

# Page-level access check. Hiding this page from the sidebar nav (done in
# app.py) only controls what's SHOWN - it doesn't stop someone who already
# has or guesses this page's direct URL. This is the actual enforcement.
if "inventory" not in st.session_state.get("allowed_pages", []):
    st.error("🔒 You don't have access to this page. Contact an administrator if you need it.")
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
import re as _re


def _sanitize_doc_id(name):
    """Firestore document IDs can't contain '/' and shouldn't be empty."""
    doc_id = _re.sub(r'[/\\]', '_', str(name).strip())
    doc_id = doc_id[:1500]  # Firestore's document ID length limit
    return doc_id if doc_id else "unnamed"


@st.cache_data(ttl=3600)
def get_product_master():
    # Shared with the Costing Tool - already migrated to Firestore there,
    # so this just reads the same collection rather than re-migrating it.
    try:
        db = st.session_state.db
        docs = db.collection("product_master").stream()
        return pd.DataFrame([d.to_dict() for d in docs])
    except Exception:
        return pd.DataFrame(columns=["Item_Name", "Purchase_Unit", "Sales_Unit", "Group"])


def save_new_product(item_name, group, sub_group, purchase_unit, sales_unit):
    """Creates a new SKU in product_master - the SAME collection the
    Costing Tool reads from, so a SKU added here is immediately usable
    there too. Doc ID is the sanitized item name, matching the convention
    used everywhere else this collection is written (migration script,
    Costing Tool). Returns "ok" or "duplicate" (an item with this name
    already exists - overwriting it here would silently wipe out whatever
    Group/Units it already had)."""
    db = st.session_state.db
    doc_id = _sanitize_doc_id(item_name)
    doc_ref = db.collection("product_master").document(doc_id)
    if doc_ref.get().exists:
        return "duplicate"
    doc_ref.set({
        "Item_Name": item_name.strip(),
        "Group": group.strip(),
        "Sub-Group": sub_group.strip(),
        "Purchase_Unit": purchase_unit.strip(),
        "Sales_Unit": sales_unit.strip(),
    })
    return "ok"

@st.cache_data(ttl=3600)
def get_purchases():
    # Every fiscal year's transactions live in their own subcollection at
    # purchases_fy/{fy}/entries. A collection_group query reads across every
    # one of those subcollections in a single call, replacing the old
    # "loop every FY worksheet" pattern.
    #
    # _doc_id/_fy_doc_id are captured (and NOT written back to Firestore -
    # they're stripped before any save) so that editing or deleting a
    # transaction later can target that exact document directly, instead of
    # having to clear and rewrite everything in that fiscal year just to
    # touch one row.
    try:
        db = st.session_state.db
        docs = db.collection_group("entries").stream()
        records = []
        for d in docs:
            row = d.to_dict()
            row['_doc_id'] = d.id
            row['_fy_doc_id'] = d.reference.parent.parent.id
            records.append(row)
        return pd.DataFrame(records) if records else pd.DataFrame()
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=3600)
def get_code_mapping():
    try:
        db = st.session_state.db
        docs = db.collection("code_mapping").stream()
        return pd.DataFrame([d.to_dict() for d in docs])
    except Exception:
        return pd.DataFrame(columns=["Item Code", "Item Name", "Units"])

@st.cache_data(ttl=3600)
def get_learned_mappings():
    try:
        db = st.session_state.db
        docs = db.collection("learned_mappings").stream()
        return pd.DataFrame([d.to_dict() for d in docs])
    except Exception:
        return pd.DataFrame(columns=["Billed_Description", "Matched_Item_Name"])

@st.cache_data(ttl=3600)
def get_stock_levels():
    """The running-stock aggregate - one small document per item, kept in
    sync by append_purchases()/overwrite_purchases() via _apply_stock_deltas().
    This is what View Inventory's Stock Summary reads from, instead of
    summing every purchase transaction ever recorded on every page load."""
    try:
        db = st.session_state.db
        docs = db.collection("stock_levels").stream()
        return pd.DataFrame([d.to_dict() for d in docs])
    except Exception:
        return pd.DataFrame(columns=["Item_Name", "Net_Qty"])

def _prepare_for_save(df_to_save):
    """Shared date/fiscal-year prep used by both the append and overwrite paths."""
    df_save = df_to_save.copy()
    if 'Date' in df_save.columns:
        df_save['Date'] = pd.to_datetime(df_save['Date'], dayfirst=True, errors='coerce').dt.strftime('%d/%m/%Y')
        calculated_fy = df_save['Date'].apply(get_nepali_fiscal_year)

        if 'Fiscal Year' in df_save.columns:
            # Keep manual FY if provided, otherwise auto-calculate
            df_save['Fiscal Year'] = [
                fy if str(fy).strip() not in ["", "nan", "None", "Unknown"] else calc
                for fy, calc in zip(df_save['Fiscal Year'], calculated_fy)
            ]
        else:
            df_save['Fiscal Year'] = calculated_fy
    return df_save


def _fy_to_doc_id(fy):
    """Same naming convention as the old worksheet-name helper, now used as
    the Firestore document ID under purchases_fy/ instead of a tab name."""
    doc_id = str(fy).replace("/", "-") if pd.notna(fy) and str(fy).strip() != "Unknown" else "FY Unknown"
    if not doc_id.startswith("FY "):
        doc_id = "FY " + doc_id
    return doc_id


def _commit_in_batches(db, write_fn, items):
    """Runs write_fn(batch, item) over items, committing every 400 writes
    (Firestore batches cap at 500)."""
    batch = db.batch()
    count = 0
    for item in items:
        write_fn(batch, item)
        count += 1
        if count % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()


def _apply_stock_deltas(db, df, sign=1):
    """Keeps the stock_levels/{item} aggregate collection in sync using
    atomic Firestore increments - no read-before-write needed, so this is
    cheap (a handful of writes, zero extra reads) no matter how large the
    underlying purchases history is. sign=+1 adds the rows' contribution,
    sign=-1 backs it out (used when overwrite_purchases deletes rows before
    rewriting them).

    Also stamps each item's Group/Stock Unit onto its aggregate doc (last
    non-blank value seen wins) purely so View Inventory's Stock Summary can
    display those columns without needing to touch purchases_fy at all.

    Writes a doc for every item touched by `df`, even ones whose net delta
    in THIS batch is exactly zero (e.g. a purchase and a full return
    entered together) - Firestore's Increment() creates the field starting
    from 0 if it doesn't already exist, so this still correctly produces a
    Net_Qty: 0 doc rather than silently omitting the item from the
    aggregate (and therefore from the Stock Summary) just because nothing
    changed in this particular batch.

    View Inventory's Stock Summary reads THIS collection (one small doc per
    item) instead of summing every purchase transaction ever recorded.
    """
    if df is None or df.empty or 'Item_Name' not in df.columns or 'Stock Qty Added' not in df.columns:
        return

    work = df.copy()
    work['_qty'] = pd.to_numeric(work['Stock Qty Added'], errors='coerce').fillna(0)

    def first_non_blank(series):
        for v in series:
            if pd.notna(v) and str(v).strip() != "":
                return v
        return None

    agg_spec = {'_qty': 'sum'}
    if 'Group' in work.columns:
        agg_spec['Group'] = first_non_blank
    if 'Stock Unit' in work.columns:
        agg_spec['Stock Unit'] = first_non_blank

    grouped = work.groupby('Item_Name').agg(agg_spec)
    if grouped.empty:
        return

    coll_ref = db.collection("stock_levels")

    def write_delta(batch, item):
        item_name, row = item
        if not item_name or pd.isna(item_name):
            return
        doc_data = {
            "Item_Name": item_name,
            "Net_Qty": firestore.Increment(sign * float(row['_qty'])),
        }
        if 'Group' in row and pd.notna(row.get('Group')):
            doc_data["Group"] = row['Group']
        if 'Stock Unit' in row and pd.notna(row.get('Stock Unit')):
            doc_data["Stock_Unit"] = row['Stock Unit']
        doc_ref = coll_ref.document(_sanitize_doc_id(item_name))
        batch.set(doc_ref, doc_data, merge=True)

    _commit_in_batches(db, write_delta, list(grouped.iterrows()))


def append_purchases(new_rows_df):
    """Append-only save: adds new rows as new documents in the correct FY's
    entries subcollection WITHOUT reading or rewriting any existing data
    there.

    This is the default path for anything that is genuinely new data
    (single-entry commits, bulk-upload commits with no bill being
    replaced). It's both far faster (no need to transmit and rewrite tens
    of thousands of existing documents just to add a handful of new ones)
    and safe under concurrent use: two people saving at the same moment
    simply both get their own new documents, instead of one person's
    full-collection rewrite silently erasing the other's just-saved
    transaction.
    """
    if new_rows_df.empty:
        return

    df_save = _prepare_for_save(new_rows_df)
    if 'Fiscal Year' not in df_save.columns:
        return

    db = st.session_state.db

    for fy, group_df in df_save.groupby('Fiscal Year'):
        fy_id = _fy_to_doc_id(fy)
        entries_ref = db.collection("purchases_fy").document(fy_id).collection("entries")
        group_df = group_df.fillna("")

        rows = [row.to_dict() for _, row in group_df.iterrows()]
        _commit_in_batches(
            db,
            lambda batch, row: batch.set(entries_ref.document(), row),
            rows,
        )

    # New rows are purely additive here (nothing was deleted), so just add
    # their contribution to the running stock aggregate.
    _apply_stock_deltas(db, df_save, sign=1)


def overwrite_purchases(df_to_save, only_fys=None):
    """Full clear-and-rewrite of one or more fiscal years' worth of
    transactions.

    NOT USED BY ANY UI PATH ANYMORE - editing a transaction, deleting bills,
    and overriding a duplicate bill during bulk upload all now use the
    targeted update_purchase_entry()/delete_purchase_entries() functions
    below instead, which touch only the specific document(s) actually
    changing rather than reading, deleting, and rewriting every transaction
    in the affected fiscal year(s). On a database with thousands of
    transactions per year, that used to mean thousands of reads/deletes/
    writes for a single-bill edit or a handful of deletions.

    Left here, unused, as a documented fallback in case a genuine
    full-fiscal-year rebuild is ever needed (e.g. a one-off data-repair
    script) - it is not wired into any button in this file.
    """
    if df_to_save.empty and not only_fys:
        return

    df_save = _prepare_for_save(df_to_save) if not df_to_save.empty else df_to_save
    db = st.session_state.db

    if only_fys is not None:
        if isinstance(only_fys, str):
            only_fys = [only_fys]
        if 'Fiscal Year' in df_save.columns:
            df_save = df_save[df_save['Fiscal Year'].isin(only_fys)]
        target_fys = only_fys
    else:
        target_fys = df_save['Fiscal Year'].dropna().unique().tolist() if 'Fiscal Year' in df_save.columns else []

    for fy in target_fys:
        fy_id = _fy_to_doc_id(fy)
        entries_ref = db.collection("purchases_fy").document(fy_id).collection("entries")

        # Read what's there BEFORE deleting, so we can back its contribution
        # out of the stock aggregate below - the docs are already being
        # fetched for the delete itself, so this doesn't cost any extra
        # Firestore reads beyond what overwrite_purchases already needed.
        existing_docs = list(entries_ref.stream())
        old_rows_df = pd.DataFrame([d.to_dict() for d in existing_docs]) if existing_docs else pd.DataFrame()
        existing_refs = [d.reference for d in existing_docs]
        _commit_in_batches(db, lambda batch, ref: batch.delete(ref), existing_refs)

        # ...then write the fresh set for this FY.
        group_df = df_save[df_save['Fiscal Year'] == fy].fillna("") if 'Fiscal Year' in df_save.columns else pd.DataFrame()
        rows = [row.to_dict() for _, row in group_df.iterrows()]
        _commit_in_batches(
            db,
            lambda batch, row: batch.set(entries_ref.document(), row),
            rows,
        )

        # Back out what was deleted, then add back whatever the fresh set
        # actually contains - covers edits, deletes, and duplicate-bill
        # overrides all with the same two calls.
        _apply_stock_deltas(db, old_rows_df, sign=-1)
        _apply_stock_deltas(db, group_df, sign=1)


def update_purchase_entry(fy_doc_id, doc_id, updated_fields):
    """Targeted single-document update - touches exactly ONE transaction
    document by its real Firestore ID, not the rest of that fiscal year's
    history. This is what the single-bill editor uses now, in place of
    overwrite_purchases()'s clear-and-rewrite-the-whole-FY approach - for a
    fiscal year with thousands of transactions, editing one bill used to
    cost thousands of reads/deletes/writes for a one-row change; this costs
    exactly one read (to compute the stock delta) and one write.

    Returns "ok" or "missing" (doc no longer exists - e.g. deleted by
    someone else since the page was loaded)."""
    db = st.session_state.db
    doc_ref = db.collection("purchases_fy").document(fy_doc_id).collection("entries").document(doc_id)
    snap = doc_ref.get()
    if not snap.exists:
        return "missing"

    old_row = snap.to_dict()
    doc_ref.update(updated_fields)

    # Adjust the stock aggregate by just the delta this one edit causes,
    # rather than backing out and re-adding an entire fiscal year.
    _apply_stock_deltas(db, pd.DataFrame([old_row]), sign=-1)
    _apply_stock_deltas(db, pd.DataFrame([{**old_row, **updated_fields}]), sign=1)
    return "ok"


def delete_purchase_entries(entries):
    """Targeted deletes - entries is a list of dicts, each with at least
    fy_doc_id, doc_id, Item_Name, and Stock Qty Added (i.e. rows pulled
    straight from get_purchases(), which already carries _doc_id/_fy_doc_id
    for exactly this purpose). Only the specific documents being deleted
    are touched - the rest of that fiscal year's history is left alone,
    unlike the old approach of clearing and rewriting the entire FY just to
    remove a handful of bills."""
    db = st.session_state.db
    if not entries:
        return

    def do_delete(batch, entry):
        doc_ref = (
            db.collection("purchases_fy").document(entry['fy_doc_id'])
            .collection("entries").document(entry['doc_id'])
        )
        batch.delete(doc_ref)

    _commit_in_batches(db, do_delete, entries)

    # Back out exactly what was deleted from the stock aggregate.
    _apply_stock_deltas(db, pd.DataFrame(entries), sign=-1)


def save_learned_mappings(df_to_save):
    # Learned_Mappings is small (~400 rows), so - like the old sheet-clear
    # approach - this fully replaces the collection each save rather than
    # diffing it. Doc IDs are the (sanitized) billed description, so
    # re-running with the same description overwrites in place.
    db = st.session_state.db
    coll_ref = db.collection("learned_mappings")

    existing_refs = [d.reference for d in coll_ref.stream()]
    _commit_in_batches(db, lambda batch, ref: batch.delete(ref), existing_refs)

    df_to_save = df_to_save.fillna("")
    rows = []
    for _, row in df_to_save.iterrows():
        billed = str(row.get("Billed_Description", "")).strip()
        doc_ref = coll_ref.document(_sanitize_doc_id(billed)) if billed else coll_ref.document()
        rows.append((doc_ref, row.to_dict()))
    _commit_in_batches(db, lambda batch, item: batch.set(item[0], item[1]), rows)

# Initialization
products_df = get_product_master()

# NOTE: purchases_df is deliberately NOT loaded here. get_purchases() reads
# every purchase transaction ever recorded via a Firestore collection_group
# query (one billed read per document - tens of thousands of them). Loading
# it unconditionally on every script rerun, regardless of which tab is even
# being used, is what caused the Firestore daily quota (ResourceExhausted)
# to blow out. It's now fetched lazily, once, inside each tab body that
# actually needs it (and only after that tab's permission check passes) via
# the local load_purchases() helper below - so a user who only has, say,
# Masters & AI Memory access never triggers it at all.

def load_purchases():
    """On-demand purchases loader - call this from inside a tab body right
    before the data is actually needed, not at module level. Cached by
    get_purchases() itself, so calling this more than once in the same tab
    or across tabs within the same rerun is free."""
    df = get_purchases()
    if not df.empty and 'Date' in df.columns:
        df['Fiscal Year'] = df['Date'].apply(get_nepali_fiscal_year)
    return df


def lazy_load(session_key, loader_fn, label, widget_key=None):
    """Only calls loader_fn() (a live Firestore read) when the person
    explicitly clicks a button - nothing is fetched just because the app
    opened or the script reran for an unrelated reason. This matters
    because Streamlit reruns the ENTIRE script on every interaction
    anywhere in the app, so without this, a tab's data would silently
    refetch on every click in a completely different tab too.

    Once loaded, the result lives in st.session_state (not just
    st.cache_data, whose short TTL can still refire on its own on a later
    rerun) until the person clicks Refresh, or a write action clears it via
    clear_lazy_cache() so the next view is guaranteed fresh rather than
    silently stale.

    session_key is the DATA cache key - deliberately the same across
    multiple tabs that share the same underlying data (e.g. Tabs 1, 2, and
    5 all use "sd_purchases_df"), so loading once in one tab makes it
    available in the others too without re-fetching.

    widget_key is the BUTTON's key and must be unique per call site, since
    all tab bodies render every rerun regardless of which tab is visible -
    two buttons sharing a key (e.g. from two tabs both defaulting to
    f"lazy_btn_{session_key}") raises StreamlitDuplicateElementKey. Pass a
    distinct widget_key (e.g. the tab name) whenever the same session_key
    is used in more than one place.

    Returns None if not yet loaded - callers should skip the rest of that
    tab's body in that case (see usage in each tab below).
    """
    widget_key = widget_key or session_key
    is_loaded = session_key in st.session_state
    btn_label = f"🔄 Refresh {label}" if is_loaded else f"📥 Load {label}"
    if st.button(btn_label, key=f"lazy_btn_{widget_key}"):
        with st.spinner(f"Fetching {label}..."):
            st.session_state[session_key] = loader_fn()
        st.rerun()
    if not is_loaded:
        st.caption(f"Not fetched yet - click \"Load {label}\" above when you're ready to view it.")
        return None
    return st.session_state[session_key]


def clear_lazy_cache():
    """Call this alongside st.cache_data.clear() after any write, so the
    next view of a lazily-loaded tab is forced to fetch fresh data instead
    of continuing to show whatever was loaded before the edit."""
    for k in ("sd_purchases_df", "sd_stock_df"):
        st.session_state.pop(k, None)


def normalize_code(x):
    """Normalizes an item code for matching, regardless of whether it came
    in as a clean string or as something pandas silently turned into a
    number. A code column with even one blank cell gets upcast to float64
    by pandas, turning "5001" into 5001.0 - str(row[4]) then reads back as
    "5001.0" and will never match a code_dict key of "5001". This strips
    that artifact. It also guards against the same thing happening to
    codes read out of Firestore/mapping_df."""
    if pd.isna(x):
        return ""
    s = str(x).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s

mapping_df = get_code_mapping()
learned_df = get_learned_mappings()

# 1. Create the Product Code & Unit translation dictionaries
code_dict = {}
unit_dict = {}
if not mapping_df.empty:
    if len(mapping_df.columns) >= 2:
        code_dict = dict(zip(mapping_df.iloc[:, 0].apply(normalize_code), mapping_df.iloc[:, 1].astype(str).str.strip()))
    if len(mapping_df.columns) >= 3:
        unit_dict = dict(zip(mapping_df.iloc[:, 0].apply(normalize_code), mapping_df.iloc[:, 2].astype(str).str.strip()))

# 2. Create the AI Memory dictionary
memory_dict = {}
memory_dict_normalized = {}
if not learned_df.empty and 'Billed_Description' in learned_df.columns and 'Matched_Item_Name' in learned_df.columns:
    memory_dict = dict(zip(learned_df['Billed_Description'].astype(str).str.strip().str.upper(), learned_df['Matched_Item_Name'].astype(str).str.strip()))

# Pre-process stock items for fuzzy matching
stock_items = products_df['Item_Name'].dropna().unique().tolist()
stock_items_lower = {str(item).lower(): item for item in stock_items}

# --- UNIVERSAL NORMALIZATION ENGINE ---
def normalize_text(text):
    t = str(text).strip().lower()

    # Finding 1: strip Excel-formula artifacts (e.g. "=+SCREW", "=+ BIT").
    # These are almost always a stray '=' or '=+' left over from a copy/paste
    # or accidental formula entry, not part of the real item description.
    # NOT anchored to the start of the string - the merged description often
    # has a category prefix before it (e.g. "HARDWARE GOODS - =+SCREW"), so
    # the artifact can appear mid-string.
    t = re.sub(r'=\+?', ' ', t)
    
    # 0. Common Master DB Typos & Hardware Abbreviations
    #
    # Split into two groups on purpose:
    #  - word_typo_fixes: plain alphabetic words -> matched with \b word
    #    boundaries, so fixing one typo can't be re-mangled by a later rule
    #    matching a substring of its own output (e.g. 'jagdamba' -> 'jagadamba'
    #    then 'jagadamb' matching the tail of that already-fixed word and
    #    adding an extra 'a' -> 'jagadambaa', which then silently broke the
    #    \bjagadamba\b isolation lock later on).
    #  - punctuation_fixes: forms like 'h.r.' that end in a non-word character
    #    (a period). \b doesn't reliably match right after punctuation
    #    followed by whitespace, so these intentionally stay as plain
    #    substring replacement.
    word_typo_fixes = {
        'sqaure': 'square', 'squre': 'square', 'rect': 'rectangle',
        'guage': 'gauge',  # confirmed real typo variant present in Product_Master itself (e.g. "12 GUAGE")
        'jagdamba': 'jagadamba', 'jgadamba': 'jagadamba', 'jagamba': 'jagadamba', 'jagadamb': 'jagadamba',
        'chnnel': 'channel',  # confirmed real typo in sales data ("CHNNEL 5\"")
        'been': 'beam',  # confirmed real typo in sales data ("6\" I BEEN") - generic fuzzy
                          # matching doesn't catch this (string similarity 0.5, below the 0.6 cutoff)
    }
    punctuation_fixes = {
        'h.r.': 'hr', 'h.r': 'hr',
        'c.r.': 'cr', 'c.r': 'cr',
        'g.i.': 'gi', 'g.i': 'gi',
        'c.g.i.': 'cgi', 'c.g.i': 'cgi',
        'c.c.': 'cc', 'c.c': 'cc'
    }
    for k, v in word_typo_fixes.items():
        t = re.sub(rf'\b{re.escape(k)}\b', v, t)
    for k, v in punctuation_fixes.items():
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


# Build the memory bank's normalized-text lookup AFTER normalize_text is
# defined, so a repeat billed description that differs only by whitespace,
# punctuation, or a known typo (e.g. "guage" vs "gauge") still reuses a
# manual correction the AI already learned, instead of asking the human to
# re-confirm the identical match every time it appears with tiny variations.
if memory_dict:
    for billed_desc, matched_item in memory_dict.items():
        memory_dict_normalized[normalize_text(billed_desc)] = matched_item

# Precompute the normalized master-item candidate list ONCE at startup,
# rather than rebuilding it (via normalize_text on every master item) inside
# find_best_match on every single call. On a bulk upload of hundreds of
# rows against a ~500-item master list, this was redoing tens of thousands
# of redundant normalize_text calls whose result never changes within a run.
#
# Also attach each item's Group and Sub-Group (e.g. PIPE/ROUND vs
# PIPE/SQUARE, or HULAS SHEET/COLOR JASTA vs HULAS SHEET/SADA JASTA) so the
# matcher can use your own master-data categories as a filter, instead of
# relying only on hand-written regex keyword locks.
_group_lookup = {}
_subgroup_lookup = {}
if 'Group' in products_df.columns:
    _group_lookup = dict(zip(products_df['Item_Name'], products_df['Group']))
if 'Sub-Group' in products_df.columns:
    _subgroup_lookup = dict(zip(products_df['Item_Name'], products_df['Sub-Group']))

BASE_CANDIDATES = [
    {
        "original_key": k,
        "norm_key": normalize_text(k),
        "val": v,
        "group": _group_lookup.get(v),
        "sub_group": _subgroup_lookup.get(v) if _subgroup_lookup.get(v) not in (None, "") else None,
    }
    for k, v in stock_items_lower.items()
]

# For each Group, what Sub-Group keywords actually exist (normalized)?
# e.g. {"PIPE": {"round", "square", "rectangle"}, "ROD": {"plain", "square", "tmt"}, ...}
# Groups with no Sub-Group data at all (e.g. ANGLE, FLAT) simply won't appear
# here, so they're never affected by this filter.
_GROUP_SUBGROUP_VOCAB = {}
for _c in BASE_CANDIDATES:
    if _c["group"] and _c["sub_group"]:
        _GROUP_SUBGROUP_VOCAB.setdefault(_c["group"], {})[normalize_text(_c["sub_group"])] = _c["sub_group"]


# --- REWORKED AI LOGIC WITH DUAL-NORMALIZATION ---
def find_best_match(description, mapped_keywords=""):
    debug_log = {"Original": str(description)}
    
    # 1. AI Memory Bank Check - exact (case/whitespace-insensitive) match first
    orig_upper = str(description).strip().upper()
    if orig_upper in memory_dict:
        debug_log["Status"] = "Memory Match"
        return memory_dict[orig_upper], debug_log

    # 1b. Fuzzy-tolerant memory check: same normalization pipeline used for
    # matching, so a previously-corrected description that reappears with a
    # stray space, different punctuation, or a known typo variant still
    # hits memory instead of falling through to fuzzy scoring again.
    desc_clean_for_memory = normalize_text(description)
    if desc_clean_for_memory in memory_dict_normalized:
        debug_log["Status"] = "Memory Match (normalized)"
        return memory_dict_normalized[desc_clean_for_memory], debug_log

    # 2. Normalize the Uploaded Description
    desc_clean = desc_clean_for_memory
    debug_log["Cleaned"] = desc_clean
    
    # 3. Start from the precomputed normalized master list
    candidates = list(BASE_CANDIDATES)

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

    # 4A-2. SUB-GROUP LOCKS (data-driven, from Product_Master's Sub-Group column)
    # Covers categories the hand-written isolation_keywords list above never
    # touched at all - e.g. HULAS SHEET "COLOR JASTA" vs "SADA JASTA" vs
    # "DECKING", or ROD "PLAIN" vs "SQUARE" vs "TMT". For each candidate's
    # own Group, check whether the input text mentions any of that group's
    # known Sub-Group keywords; if so, keep only candidates whose Sub-Group
    # matches. Candidates whose Group has no Sub-Group data at all (e.g.
    # ANGLE, FLAT) are never filtered by this - there's nothing to check.
    subgroup_filtered = []
    subgroup_lock_notes = []
    for c in candidates:
        vocab = _GROUP_SUBGROUP_VOCAB.get(c["group"])
        if not vocab:
            subgroup_filtered.append(c)
            continue

        mentioned = [norm_sg for norm_sg in vocab if re.search(rf'\b{re.escape(norm_sg)}\b', desc_clean)]
        if not mentioned:
            # No sub-group signal in the input for this candidate's group -
            # can't rule it out, so keep it (avoids over-filtering on silence).
            subgroup_filtered.append(c)
        elif c["sub_group"] and normalize_text(c["sub_group"]) in mentioned:
            subgroup_filtered.append(c)
            subgroup_lock_notes.append(f"SUBGROUP:{c['group']}={mentioned[0]}")

    if subgroup_filtered:
        candidates = subgroup_filtered
        triggered_locks.extend(sorted(set(subgroup_lock_notes)))

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
    
    # A token like "3.2mm" or "16gauge" or "1.5" - all digits (with an
    # optional decimal) followed only by optional unit letters, nothing else.
    _NUMERIC_TOKEN_RE = re.compile(r'^\d+(\.\d+)?[a-z]*$')

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
            elif _NUMERIC_TOKEN_RE.match(d_word):
                # Finding 3: measurements never get fuzzy credit, only exact
                # matches. Generic string similarity thinks "3.2mm" and
                # "12mm" are ~0.6 similar (same suffix, similar length) and
                # was handing out 0.8 credit for what are actually two
                # completely different diameters - confirmed against real
                # data where this caused "WELDING ROD 3.2MM" to tie against
                # "TMT ROD 12MM" and "TMT ROD 20MM" instead of clearly
                # winning on "WELDING ROD" alone.
                continue
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
        top_ratio = scoring_details[0]['Ratio'] if scoring_details else highest_score
        second_ratio = scoring_details[1]['Ratio'] if len(scoring_details) > 1 else 0.0
        gap = round(top_ratio - second_ratio, 2)

        # Only auto-accept if there's a clear winner. A high top score with
        # an almost-as-high runner-up (e.g. two dimension variants of the
        # same product line) used to get silently auto-picked with a coin-flip
        # outcome; now it's routed to manual review instead, unless the top
        # score is near-exact (>=0.9) where a close runner-up is expected and
        # safe (e.g. the true match plus a near-duplicate item name).
        if top_ratio >= 0.9 or gap >= 0.15 or len(scoring_details) <= 1:
            debug_log["Status"] = "Fuzzy Passed"
            return best_match, debug_log

        debug_log["Status"] = f"Ambiguous (Top {top_ratio} vs 2nd {second_ratio}, gap {gap} < 0.15)"
        return None, debug_log

    debug_log["Status"] = "Failed (Score < 0.3)"
    return None, debug_log

def format_debug_string(log):
    if log.get("Status") in ["Memory Match", "Memory Match (normalized)", "Exact Match"]:
        return f"🟢 {log['Status']}"

    if str(log.get("Status", "")).startswith("Ambiguous"):
        locks = log.get('Strict_Locks', [])
        lock_str = f"🔒 Locks: {locks}" if locks else "🔓 Locks: None"
        return f"🟡 {log['Status']} | {lock_str}"

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

def build_records_from_auto_matched(edited_auto_df, auto_matched, bulk_type):
    """Shared by both the 'unmatched items present' and 'all auto-matched'
    commit paths, so a fix here only needs to be made once instead of
    kept in sync across two nearly-identical copies of this loop."""
    final_records = []
    new_learned_rules = []

    qty_multiplier = -1 if bulk_type in ["Sales", "Purchase Returns"] else 1

    if edited_auto_df is not None and not edited_auto_df.empty:
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

            final_records.append({
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

    return final_records, new_learned_rules, qty_multiplier


def compact_search_key(text):
    """Turns '1 1/2" SQUARE PIPE 12 GAUGE' into '112sq12': strips all
    punctuation/spaces, drops pure unit/filler words (pipe, gauge), and
    abbreviates common shape words (square->sq, rectangle->rect, round->rnd).
    Lets users type a quick shorthand code instead of the full item name."""
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


def smart_item_search(label, items, key, placeholder="Click here to type..."):
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


def render_add_new_sku_form(products_df, key_prefix, select_after_create_key=None):
    """Drop-in 'create a new SKU on the fly' expander - for whenever the
    item someone's trying to enter data for isn't in Product Master yet, so
    they don't have to abandon what they're doing and go find an admin
    tool. Writes straight to product_master (same collection the Costing
    Tool reads from), so the new SKU is usable there immediately too.

    Deliberately NOT wrapped in st.form: the Group/Unit dropdowns need to
    dynamically reveal a "type a new one" text box the moment "Add New..."
    is picked, and st.form batches all widget interactions until submit -
    it wouldn't react to that selection until the whole form was already
    submitted. Using plain widgets means a few extra script reruns while
    filling this in, but nothing here touches Firestore until the actual
    Create SKU click, so that cost is negligible.

    If select_after_create_key is given, the newly created item is
    pre-selected in the smart_item_search box with that key on the rerun
    that follows (so the person lands right back on the item they just
    created, instead of having to search for it again).
    """
    existing_groups = sorted(products_df['Group'].dropna().unique().tolist()) if 'Group' in products_df.columns else []
    existing_p_units = sorted(products_df['Purchase_Unit'].dropna().unique().tolist()) if 'Purchase_Unit' in products_df.columns else []
    existing_s_units = sorted(products_df['Sales_Unit'].dropna().unique().tolist()) if 'Sales_Unit' in products_df.columns else []
    existing_names_lower = set(products_df['Item_Name'].dropna().str.strip().str.lower()) if 'Item_Name' in products_df.columns else set()

    ADD_NEW = "➕ Add New..."
    field_keys = [
        f"{key_prefix}_new_item_name", f"{key_prefix}_new_group_choice", f"{key_prefix}_new_group_typed",
        f"{key_prefix}_new_subgroup", f"{key_prefix}_new_p_unit_choice", f"{key_prefix}_new_p_unit_typed",
        f"{key_prefix}_new_s_unit_choice", f"{key_prefix}_new_s_unit_typed",
    ]

    with st.expander("➕ Can't find the item? Add a new SKU"):
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
                status = save_new_product(new_item_name, group_val, new_sub_group or "", p_unit_val, s_unit_val)
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


st.title("📦 Hardware Inventory Management")
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "🛒 Single Entry", "📤 Bulk Uploads", "📊 View Inventory", "📋 Masters & AI Memory", "📝 Edit Transactions"
])

# --- TAB 1: SINGLE TRANSACTION ENTRY (WITH CART) ---
with tab1:
    st.header("Single Transaction Entry")
    if "inventory_single_entry" not in st.session_state.get("user_permissions", []):
        st.info("🔒 Adding new transactions requires a permission an administrator hasn't granted your account yet.")
    else:
        purchases_df = lazy_load("sd_purchases_df", load_purchases, "Transaction Data", widget_key="purchases_tab1")

        if purchases_df is None:
            pass  # lazy_load already showed the Load button + caption above
        else:
            trans_type = st.radio(
                "Select Transaction Type", 
                ["Purchase", "Sales", "Purchase Return", "Sales Return", "Opening Stock", "Stock Adjustment"], 
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
                    opening_fy_input = None
                elif trans_type == "Opening Stock":
                    bill_number = "OPENING-STOCK"
                    st.info("Transaction will be securely recorded as OPENING-STOCK.")
                    opening_fy_input = st.text_input("Target Fiscal Year", value="FY 2082-83", help="Specify which fiscal year this stock belongs to.")
                else:
                    bill_number = st.text_input("Bill / Invoice Number", placeholder="Enter Bill Number...")
                    opening_fy_input = None
            
                bill_exists = False
                override_duplicate = True
                if bill_number and trans_type not in ["Stock Adjustment", "Opening Stock"] and not purchases_df.empty and str(bill_number).strip() in purchases_df['Bill Number'].astype(str).str.strip().values:
                    bill_exists = True
                    st.error(f"🚨 Bill Number '{bill_number}' already exists.")
                    override_duplicate = st.checkbox("I intentionally want to APPEND to this existing bill.")

            st.divider()

            items = products_df['Item_Name'].dropna().unique().tolist()
            selected_item = smart_item_search("Search and Select Item to Add", items, key="tab1_item_search")

            render_add_new_sku_form(products_df, key_prefix="tab1", select_after_create_key="tab1_item_search")

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
                
                    elif trans_type == "Opening Stock":
                        stock_qty = st.number_input(f"Opening Stock Quantity ({s_unit})", min_value=0.01, step=1.0, value=None)
                        billed_qty = 0
            
                    if st.form_submit_button("➕ Add Item to Cart", type="primary"):
                        if trans_type == "Stock Adjustment" and (stock_qty is None or stock_qty == 0):
                            st.error("Please enter a non-zero adjustment quantity.")
                        elif trans_type == "Opening Stock" and not stock_qty:
                            st.error("Please enter the Opening Stock quantity.")
                        elif trans_type not in ["Stock Adjustment", "Opening Stock"] and not billed_qty:
                            st.error("Please enter the Billed Quantity.")
                        elif trans_type in ["Purchase", "Purchase Return"] and p_unit != s_unit and not stock_qty:
                            st.error(f"Please enter the Actual Stock Impact in {s_unit}.")
                        else:
                            if trans_type in ["Sales", "Purchase Return"]:
                                final_stock = -abs(stock_qty)
                            elif trans_type in ["Purchase", "Sales Return", "Opening Stock"]:
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
                        if trans_type not in ["Stock Adjustment", "Opening Stock"] and not bill_number:
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
                                    "Fiscal Year": opening_fy_input if trans_type == "Opening Stock" else get_nepali_fiscal_year(entry_date.strftime("%d/%m/%Y")),
                                    "Bill Number": bill_number,
                                    "Group": row["Group"],
                                    "Item_Name": row["Item_Name"],
                                    "Purchase Qty": row["Purchase Qty"],
                                    "Purchase Unit": row["Purchase Unit"],
                                    "Stock Qty Added": row["Stock Qty Added"],
                                    "Stock Unit": row["Stock Unit"]
                                })
                    
                            new_records_df = pd.DataFrame(records_to_save)
                            append_purchases(new_records_df)
                            st.session_state.single_entry_cart = []
                            st.cache_data.clear()
                            clear_lazy_cache()
                            st.success(f"✅ Successfully saved {len(records_to_save)} items under Ref/Bill: {bill_number}")
                            st.rerun()
                with c_clr:
                    if st.button("🗑️ Clear Entire Bill", use_container_width=True):
                        st.session_state.single_entry_cart = []
                        st.rerun()

# --- TAB 2: BULK UPLOADS ---
with tab2:
    st.header("Bulk Upload Transactions")
    if "inventory_bulk_upload" not in st.session_state.get("user_permissions", []):
        st.info("🔒 Bulk uploading transactions requires a permission an administrator hasn't granted your account yet.")
    else:
        purchases_df = lazy_load("sd_purchases_df", load_purchases, "Transaction Data", widget_key="purchases_tab2")

        if purchases_df is None:
            pass  # lazy_load already showed the Load button + caption above
        else:
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
                                # dtype={4: str} on the item-code column is deliberate: without
                                # it, pandas infers the column's type from the data, and a
                                # single blank cell anywhere in that column is enough to upcast
                                # the WHOLE column to float64 (so "5001" silently becomes
                                # 5001.0), or leading zeros get eaten (so "007" becomes 7).
                                # Either way the code_dict lookup below then never matches, and
                                # the raw code shows through instead of the product name.
                                df_upload = pd.read_csv(uploaded_file, header=None, dtype={4: str})
                            else:
                                df_upload = pd.read_excel(uploaded_file, header=None, dtype={4: str})
                    
                            df_upload[1] = df_upload[1].astype(str).str.strip()
                            uploaded_bills_count = df_upload.groupby(1).size().to_dict()
                    
                            db_bills_count = {}
                            if not purchases_df.empty and 'Bill Number' in purchases_df.columns:
                                db_bills_count = purchases_df['Bill Number'].astype(str).str.strip().value_counts().to_dict()
                    
                            duplicate_bills = []
                            for b_no, count in uploaded_bills_count.items():
                                # Flag ANY existing bill number, regardless of whether the
                                # line-item count happens to match. A corrected invoice
                                # re-uploaded with a different number of lines used to slip
                                # through here silently and double-import - now any match
                                # at all routes to the Skip/Override/Add-Duplicate resolution
                                # step so a human decides instead of the tool assuming.
                                if b_no in db_bills_count:
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
                            qty_val = pd.to_numeric(str(row[2]).replace(",", "").strip(), errors="coerce") if pd.notna(row[2]) else 0.0
                            qty_val = 0.0 if pd.isna(qty_val) else float(qty_val)
                    
                            sales_unit = str(row[9]).strip() if len(row) > 9 and pd.notna(row[9]) else ""
                            raw_item_code = normalize_code(row[4]) if len(row) > 4 else ""
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
                    
                            # DIRECT CODE MATCH (runs BEFORE fuzzy matching, takes priority
                            # over it): if this row's item code has a known mapping in
                            # Firestore's code_mapping collection, and that mapped name is a
                            # real Product Master entry, trust it outright instead of handing
                            # it to the fuzzy engine. Without this, a perfectly good code ->
                            # name mapping from the database could still get missed or
                            # second-guessed by fuzzy matching (wrong threshold, a slightly
                            # different phrasing, etc). A known code is a known code - it
                            # shouldn't need to be "found" again.
                            matched_item = None
                            debug_str = ""
                            if raw_item_code and raw_item_code in code_dict:
                                candidate_name = code_dict[raw_item_code].strip()
                                if candidate_name and candidate_name.lower() != 'nan' and candidate_name in products_df['Item_Name'].values:
                                    matched_item = candidate_name
                                    debug_str = f"🟢 Direct Code Match ({raw_item_code} → {candidate_name}, from Firestore code_mapping)"

                            if matched_item is None:
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

                            bills_to_delete = st.session_state.get("bills_to_delete", [])

                            if bills_to_delete and not purchases_df.empty and 'Bill Number' in purchases_df.columns:
                                # "Override" means replacing an existing bill's rows with the
                                # fresh ones about to be appended below. This now deletes only
                                # the specific documents for those bill numbers - using the
                                # real Firestore doc IDs already carried on purchases_df -
                                # instead of rewriting every other transaction in that fiscal
                                # year just to remove a few rows.
                                affected_mask = purchases_df['Bill Number'].astype(str).str.strip().isin(bills_to_delete)
                                rows_to_delete = purchases_df[affected_mask]
                                if '_doc_id' in rows_to_delete.columns and '_fy_doc_id' in rows_to_delete.columns:
                                    delete_purchase_entries(rows_to_delete.to_dict('records'))

                            if not new_records_df.empty:
                                append_purchases(new_records_df)

                            if new_learned:
                                new_rules_df = pd.DataFrame(new_learned)
                                updated_learnings = pd.concat([learned_df, new_rules_df], ignore_index=True)
                                updated_learnings['Billed_Description'] = updated_learnings['Billed_Description'].astype(str).str.strip().str.upper()
                                updated_learnings = updated_learnings.drop_duplicates(subset=["Billed_Description"], keep="last")
                                save_learned_mappings(updated_learnings)
                    
                            st.cache_data.clear()
                            clear_lazy_cache()
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

                            # Placed OUTSIDE the manual-matching form below (not inside
                            # it) for the same reactivity reason as Tab 1's version -
                            # forms batch every widget until submit, so the Group/Unit
                            # "Add New..." toggle wouldn't react live inside one. Works
                            # the same way regardless of bulk_type (Sales, Purchases,
                            # Purchase Returns, Sales Returns all share this one flow) -
                            # any SKU created here immediately appears as a selectable
                            # option in the "Match to Master Product" dropdowns below.
                            render_add_new_sku_form(products_df, key_prefix="tab2", select_after_create_key=None)

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
                                    final_records_to_commit, new_learned_rules, qty_multiplier = build_records_from_auto_matched(
                                        edited_auto_df, auto_matched, bulk_type
                                    )

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
                                final_records_to_commit, new_learned_rules, qty_multiplier = build_records_from_auto_matched(
                                    edited_auto_df, auto_matched, bulk_type
                                )

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
    if "inventory_view" not in st.session_state.get("user_permissions", []):
        st.info("🔒 Viewing stock levels and the ledger requires a permission an administrator hasn't granted your account yet.")
    else:
        st.subheader("Stock Summary Report")
        summary_items = st.multiselect("Select Item(s) to view (Leave blank for all)", options=products_df['Item_Name'].dropna().unique())

        # Reads the small stock_levels aggregate (one doc per item that has
        # ever moved) instead of summing every purchase transaction ever
        # recorded - this is the whole point of maintaining that aggregate.
        stock_df = lazy_load("sd_stock_df", get_stock_levels, "Stock Levels")

        if stock_df is None:
            pass  # lazy_load already showed the Load button + caption above
        elif not stock_df.empty:
            inventory_summary = stock_df.rename(columns={"Net_Qty": "Total Stock on Hand", "Stock_Unit": "Stock Unit"})
            cols_order = [c for c in ["Group", "Item_Name", "Stock Unit", "Total Stock on Hand"] if c in inventory_summary.columns]
            inventory_summary = inventory_summary[cols_order]

            if summary_items:
                inventory_summary = inventory_summary[inventory_summary['Item_Name'].isin(summary_items)]

            st.dataframe(inventory_summary, use_container_width=True, hide_index=True)

            st.divider()
            st.subheader("Item Stock Ledger")

            c1, c2 = st.columns(2)
            with c1:
                ledger_item = smart_item_search(
                    "Select a particular item to view its ledger",
                    products_df['Item_Name'].dropna().unique().tolist(),
                    key="ledger_item_search",
                )

            if ledger_item:
                # Targeted query for just this item's transactions, instead
                # of loading the entire purchases history and filtering in
                # memory - costs roughly one Firestore read per transaction
                # THIS item has, not one per transaction across every item.
                db = st.session_state.db
                item_docs = db.collection_group("entries").where("Item_Name", "==", ledger_item).stream()
                ledger = pd.DataFrame([d.to_dict() for d in item_docs])

                if ledger.empty:
                    st.info(f"No transactions found for {ledger_item}.")
                else:
                    ledger['Date_Parsed'] = pd.to_datetime(ledger['Date'], dayfirst=True, errors='coerce')
                    ledger = ledger.sort_values('Date_Parsed')

                    # Run the global cumulative sum FIRST for accounting accuracy
                    ledger['Running Balance'] = pd.to_numeric(ledger['Stock Qty Added'], errors='coerce').fillna(0).cumsum()

                    with c2:
                        fy_options = (
                            ["All"] + sorted(ledger['Fiscal Year'].dropna().unique().tolist(), reverse=True)
                            if 'Fiscal Year' in ledger.columns else ["All"]
                        )
                        selected_fy = st.selectbox("Filter Ledger by Fiscal Year", options=fy_options)

                    # Then filter the view by Fiscal Year if requested
                    if selected_fy != "All":
                        ledger = ledger[ledger['Fiscal Year'] == selected_fy]

                    st.dataframe(ledger[['Fiscal Year', 'Date', 'Bill Number', 'Purchase Qty', 'Stock Qty Added', 'Running Balance']], use_container_width=True, hide_index=True)
        else:
            st.write("No inventory data found.")

# --- TAB 4: PRODUCT MASTER & AI MEMORY ---
with tab4:
    st.header("Database & AI Memory")
    if "inventory_masters" not in st.session_state.get("user_permissions", []):
        st.info("🔒 Viewing/managing the Product Master and AI Memory requires a permission an administrator hasn't granted your account yet.")
    else:
    
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
                clear_lazy_cache()
                st.success("✅ AI Memory Optimized! All duplicate formatting variations have been removed.")
                st.rerun()
        else:
            st.info("The AI Memory is currently empty. It will learn when you manually map unmatched items!")

# --- TAB 5: UNIFIED EDIT TRANSACTIONS ---
with tab5:
    st.header("Edit Database Transactions")

    _has_edit_perm = "inventory_edit_transactions" in st.session_state.get("user_permissions", [])
    _has_delete_perm = "inventory_bulk_delete" in st.session_state.get("user_permissions", [])

    if not _has_edit_perm and not _has_delete_perm:
        st.info("🔒 Editing or deleting transactions requires a permission an administrator hasn't granted your account yet.")
    else:
        purchases_df = lazy_load("sd_purchases_df", load_purchases, "Transaction Data", widget_key="purchases_tab5")

        if purchases_df is None:
            pass  # lazy_load already showed the Load button + caption above
        elif purchases_df.empty:
            st.info("No records found in the database.")
        else:
            df_filtered = purchases_df.copy()
            df_filtered['Date_Str'] = pd.to_datetime(df_filtered['Date'], dayfirst=True, errors='coerce').dt.strftime('%d/%m/%Y')
            
            # Add FY to the dropdown label so it's easier to find specific bills
            fy_col = df_filtered['Fiscal Year'].astype(str) if 'Fiscal Year' in df_filtered.columns else "FY Unknown"
            df_filtered['Bill_Label'] = fy_col + " | " + df_filtered['Bill Number'].astype(str).str.strip() + " (Date: " + df_filtered['Date_Str'].astype(str) + ")"
            
            if not _has_edit_perm:
                st.info("🔒 Editing transactions requires a permission an administrator hasn't granted your account yet.")
            else:
                bill_list = sorted(df_filtered['Bill_Label'].dropna().unique())
                selected_label = st.selectbox(f"Search & Select Reference / Bill to Edit", options=bill_list, index=None)

                if selected_label:
                    bill_data = df_filtered[df_filtered['Bill_Label'] == selected_label].copy()
                    original_indices = bill_data.index
            
                    # Drop purely visual/temporary columns AND the internal
                    # _doc_id/_fy_doc_id plumbing fields before showing this to
                    # the person - they're what makes the targeted save below
                    # possible, but aren't meant to be seen or edited directly.
                    cols_to_drop = ['Bill_Label', 'Date_Str', '_doc_id', '_fy_doc_id']
                    if 'Date_Parsed' in bill_data.columns: cols_to_drop.append('Date_Parsed')
                    display_df = bill_data.drop(columns=[c for c in cols_to_drop if c in bill_data.columns])
            
                    st.write("✏️ **Edit quantities or details below:**")
                    edited_df = st.data_editor(display_df, use_container_width=True)

                    if st.button("💾 Save Transaction Changes", type="primary"):
                        # Targeted per-document updates - each row is written back
                        # to its own real Firestore document by _doc_id, not a
                        # full-FY clear-and-rewrite. On a database with thousands
                        # of transactions per fiscal year, that used to mean
                        # rewriting everything just to change one bill; this now
                        # costs one read + one write per line actually edited.
                        results = {"ok": 0, "moved": 0, "missing": 0}

                        for idx in original_indices:
                            original_row = bill_data.loc[idx]
                            edited_row = edited_df.loc[idx]

                            fy_doc_id = original_row.get('_fy_doc_id')
                            doc_id = original_row.get('_doc_id')
                            if not fy_doc_id or not doc_id:
                                results["missing"] += 1
                                continue

                            new_fy = get_nepali_fiscal_year(edited_row.get('Date')) if 'Date' in edited_df.columns else original_row.get('Fiscal Year')
                            new_fy_doc_id = _fy_to_doc_id(new_fy)

                            updated_fields = {k: edited_row[k] for k in edited_row.index}
                            updated_fields['Fiscal Year'] = new_fy

                            if new_fy_doc_id == fy_doc_id:
                                # Same fiscal year - a plain targeted field update.
                                status = update_purchase_entry(fy_doc_id, doc_id, updated_fields)
                                results[status] = results.get(status, 0) + 1
                            else:
                                # A date edit moved this transaction into a
                                # different fiscal year. Firestore can't "move" a
                                # document between subcollections, so this deletes
                                # the old one and appends a fresh one in the new
                                # FY's subcollection - still just one document
                                # touched on each side, not a full-FY rewrite.
                                delete_purchase_entries([dict(original_row)])
                                append_purchases(pd.DataFrame([updated_fields]))
                                results["moved"] += 1

                        st.cache_data.clear()
                        clear_lazy_cache()
                        if results.get("missing"):
                            st.warning(
                                f"Saved {results['ok'] + results['moved']} line(s), but {results['missing']} "
                                "couldn't be matched to a real record and were skipped."
                            )
                        else:
                            st.success(f"✅ Transaction updated successfully! ({results['ok']} updated, {results['moved']} moved fiscal year)")
                        st.rerun()

            st.divider()
            st.header("🗑️ Bulk Delete Bills")

            # Feature-level gate: someone can have the Hardware Inventory page
            # but not this specific action, if an admin hasn't granted the
            # "inventory_bulk_delete" permission for their account. Checked the
            # same way app.py's has_permission() does (against
            # st.session_state.user_permissions), inline rather than imported,
            # since pages don't share app.py's function definitions directly.
            if "inventory_bulk_delete" not in st.session_state.get("user_permissions", []):
                st.info("🔒 Bulk deleting bills requires a permission an administrator hasn't granted your account yet.")
            else:
                st.caption("Select any combination of purchase and sales bills below, then delete them all in one go.")

                search_term = st.text_input(
                    "Filter list (bill number, item, or date)",
                    key="bulk_delete_search",
                    placeholder="Type to narrow the list...",
                )

                bill_summary = (
                    df_filtered.groupby(['Bill_Label', 'Bill Number', 'Fiscal Year', 'Date_Str'], dropna=False)
                    .agg(
                        Items=('Item_Name', 'count'),
                        Net_Qty=('Purchase Qty', lambda s: pd.to_numeric(s, errors='coerce').sum()),
                    )
                    .reset_index()
                    .sort_values('Date_Str', ascending=False)
                )
                bill_summary.rename(columns={'Date_Str': 'Date', 'Net_Qty': 'Net Purchase Qty'}, inplace=True)

                if search_term.strip():
                    mask = bill_summary.apply(
                        lambda r: search_term.strip().lower() in " ".join(str(v) for v in r.values).lower(),
                        axis=1,
                    )
                    bill_summary = bill_summary[mask]

                if bill_summary.empty:
                    st.info("No bills match that filter.")
                else:
                    bill_summary.insert(0, "Select", False)

                    edited_summary = st.data_editor(
                        bill_summary,
                        use_container_width=True,
                        hide_index=True,
                        disabled=[c for c in bill_summary.columns if c != "Select"],
                        column_config={
                            "Select": st.column_config.CheckboxColumn("Select", help="Check the bills you want to delete"),
                        },
                        key="bulk_delete_editor",
                    )

                    selected_bills = edited_summary[edited_summary["Select"]]

                    if not selected_bills.empty:
                        total_items = int(selected_bills["Items"].sum())
                        st.warning(
                            f"⚠️ {len(selected_bills)} bill(s) selected, covering {total_items} line item(s) total. "
                            "This cannot be undone."
                        )

                        confirm = st.checkbox(
                            f"I understand this will permanently delete these {len(selected_bills)} bill(s).",
                            key="bulk_delete_confirm",
                        )

                        if st.button("🗑️ Delete Selected Bills", type="primary", disabled=not confirm):
                            labels_to_delete = set(selected_bills['Bill_Label'])
                            rows_to_delete = df_filtered[df_filtered['Bill_Label'].isin(labels_to_delete)]

                            # Targeted deletes by real Firestore doc ID - only the
                            # documents for these specific bills are touched, not
                            # a clear-and-rewrite of every other transaction in
                            # the fiscal year(s) they happen to live in.
                            delete_purchase_entries(rows_to_delete.to_dict('records'))

                            st.cache_data.clear()
                            clear_lazy_cache()
                            st.success(f"✅ Deleted {len(selected_bills)} bill(s) ({len(rows_to_delete)} line items).")
                            st.rerun()
