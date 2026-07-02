import streamlit as st
import re
import io
import zipfile
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, Alignment

# --- 1. SECURITY BOUNCER ---
if "password_correct" not in st.session_state or not st.session_state["password_correct"]:
    st.warning("🔒 Connection lost or not logged in.")
    st.info("Please click the Main Portal page in your sidebar to log in and reconnect to the database.")
    st.stop()

st.title("🧾 Debtor Statement Generator")
st.write(
    "Upload your full Debtors Ledger export. This tool will clean it up, drop any "
    "debtor with a nil balance, and produce one formatted statement per remaining "
    "debtor — packaged as a single zip file for download."
)

# --- 2. FORMATTING CONSTANTS (matches the R R Metal reference format) ---
ACCT_FMT = '_(* #,##0.00_);_(* \\(#,##0.00\\);_(* "-"??_);_(@_)'
DATE_FMT = 'mm-dd-yy'


def clean_name(raw):
    """Strip the leading 'CODE - ' prefix from a debtor name."""
    raw = str(raw).strip()
    if ' - ' in raw:
        return raw.split(' - ', 1)[1].strip()
    return raw


def sanitize_filename(name):
    """Make a debtor name safe to use as a filename."""
    name = re.sub(r'[\\/*?:"<>|]', '', name).strip()
    name = re.sub(r'\s+', ' ', name)
    return name[:150] if name else "Unnamed"


def parse_debtors(file_obj):
    """Parse the Tally-style debtors ledger into a list of per-debtor blocks."""
    wb = load_workbook(file_obj, data_only=False)
    ws = wb[wb.sheetnames[0]]

    blocks = []
    current = None

    for r in range(2, ws.max_row + 1):
        a = ws.cell(r, 1).value
        b = ws.cell(r, 2).value
        c = ws.cell(r, 3).value
        d = ws.cell(r, 4).value
        e = ws.cell(r, 5).value

        # New debtor starts wherever column A says "Customer:"
        if a is not None and str(a).strip() == 'Customer:':
            if current:
                blocks.append(current)
            current = {
                'raw_name': str(b).strip() if b is not None else 'Unnamed',
                'opening': None,
                'txns': [],
                'final_balance_text': None,
            }
            continue

        if current is None:
            continue

        b_str = str(b).strip() if b is not None else ''

        if b_str == 'Opening Balance...':
            current['opening'] = c if c is not None else 0
            continue

        if b_str == 'Party Total =>>':
            current['final_balance_text'] = str(e) if e is not None else ''
            continue

        if a is not None or b is not None:
            current['txns'].append({
                'date': a, 'particulars': b, 'debit': c, 'credit': d, 'bal_text': e
            })

    if current:
        blocks.append(current)

    return blocks


def is_nil(block):
    """A debtor is nil if their final recorded balance says 'Nil'."""
    text = block['final_balance_text']
    if text is None and block['txns']:
        text = block['txns'][-1]['bal_text']
    if text is None:
        return True
    return 'nil' in str(text).lower()


def style_cell(cell, bold=False, color=None, align=None, numfmt=None):
    cell.font = Font(name='Arial', size=10, bold=bold, color=color)
    if align:
        cell.alignment = Alignment(horizontal=align)
    if numfmt:
        cell.number_format = numfmt


def build_workbook(block):
    """Build a single-debtor statement workbook, formatted like the R R Metal reference file."""
    title = clean_name(block['raw_name'])

    wb = Workbook()
    ws = wb.active
    ws.title = 'Sheet1'

    ws.column_dimensions['A'].width = 10.78
    ws.column_dimensions['B'].width = 38.78
    ws.column_dimensions['C'].width = 13.11
    ws.column_dimensions['D'].width = 11.44
    ws.column_dimensions['E'].width = 14.0

    ws.merge_cells('A1:D1')
    ws['A1'] = title
    style_cell(ws['A1'], bold=True, color='FF0000FF', align='center')
    ws['E1'] = 0
    style_cell(ws['E1'], align='right', numfmt=ACCT_FMT)

    row = 2
    if block['opening'] is not None:
        ws.cell(row, 2, 'Opening Balance...')
        ws.cell(row, 3, block['opening'])
        ws.cell(row, 5, f'=E{row-1}+C{row}-D{row}')
        style_cell(ws.cell(row, 2))
        style_cell(ws.cell(row, 3), align='right', numfmt=ACCT_FMT)
        style_cell(ws.cell(row, 5), align='right', numfmt=ACCT_FMT)
        row += 1

    for txn in block['txns']:
        ws.cell(row, 1, txn['date'])
        style_cell(ws.cell(row, 1), numfmt=DATE_FMT)

        ws.cell(row, 2, txn['particulars'])
        style_cell(ws.cell(row, 2))

        if txn['debit']:
            ws.cell(row, 3, txn['debit'])
        style_cell(ws.cell(row, 3), align='right', numfmt=ACCT_FMT)

        if txn['credit']:
            ws.cell(row, 4, txn['credit'])
        style_cell(ws.cell(row, 4), align='right', numfmt=ACCT_FMT)

        ws.cell(row, 5, f'=E{row-1}+C{row}-D{row}')
        style_cell(ws.cell(row, 5), align='right', numfmt=ACCT_FMT)
        row += 1

    last_row = row - 1
    if last_row >= 2:
        style_cell(ws.cell(last_row, 5), bold=True, align='right', numfmt=ACCT_FMT)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf, title


# --- 3. UI: UPLOAD & PROCESS ---
uploaded_file = st.file_uploader("Upload Debtors Ledger (.xlsx)", type=["xlsx"])

if uploaded_file is not None:
    if st.button("⚙️ Process Ledger & Generate Statements"):
        with st.spinner("Reading and parsing the ledger..."):
            try:
                blocks = parse_debtors(uploaded_file)
            except Exception as e:
                st.error(f"Failed to read the file: {e}")
                st.stop()

        total = len(blocks)
        kept_blocks = [b for b in blocks if not is_nil(b)]
        nil_count = total - len(kept_blocks)

        st.success(
            f"✅ Parsed {total} debtors — {nil_count} had a nil balance and were removed, "
            f"{len(kept_blocks)} remain."
        )

        if not kept_blocks:
            st.warning("No debtors with an outstanding balance were found.")
            st.stop()

        with st.spinner(f"Generating {len(kept_blocks)} formatted statements..."):
            zip_buf = io.BytesIO()
            used_names = {}
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                progress = st.progress(0)
                for i, block in enumerate(kept_blocks):
                    file_buf, title = build_workbook(block)
                    fname = sanitize_filename(title)

                    # Guard against duplicate debtor names colliding in the zip
                    if fname in used_names:
                        used_names[fname] += 1
                        fname = f"{fname} ({used_names[fname]})"
                    else:
                        used_names[fname] = 0

                    zf.writestr(f"{fname}.xlsx", file_buf.getvalue())
                    progress.progress((i + 1) / len(kept_blocks))
            zip_buf.seek(0)

        st.success("🎉 All statements generated!")
        st.download_button(
            label="⬇️ Download All Statements (.zip)",
            data=zip_buf,
            file_name="Debtor_Statements.zip",
            mime="application/zip",
        )
else:
    st.info("Upload your Debtors Ledger export to get started.")
