import streamlit as st
import pandas as pd
import io

# --- 1. SECURITY BOUNCER ---
# Same pattern as every other tool page: "db" is the shared Firestore client
# cached by app.py at login, and page-level access is enforced here directly
# (not just by hiding the nav link) since a hidden link alone doesn't stop
# someone who already has or guesses this page's direct URL.
if "db" not in st.session_state:
    st.warning("🔒 Connection lost or not logged in.")
    st.info("Please click the Main Portal page in your sidebar to log in and reconnect to the database.")
    st.stop()

if "data_cleanup" not in st.session_state.get("allowed_pages", []):
    st.error("🔒 You don't have access to this page. Contact an administrator if you need it.")
    st.stop()

st.title("🧹 Stock Data Cleanup")
st.caption(
    "Upload a Tally 'Product Group Summary' CSV export. This removes dead rows "
    "(zero Balance, In, and Out Qty) and gives you back a cleaned file."
)


def load_csv(uploaded_file):
    return pd.read_csv(uploaded_file, low_memory=False)


def clean_data(df):
    log = {}

    for col in ["Balance Qty", "In Qty", "Out Qty"]:
        if col not in df.columns:
            st.error(f"Expected column '{col}' not found in this file - is this a Product Group Summary export?")
            st.stop()

    zero_mask = (
        pd.to_numeric(df["Balance Qty"], errors="coerce").fillna(0).eq(0)
        & pd.to_numeric(df["In Qty"], errors="coerce").fillna(0).eq(0)
        & pd.to_numeric(df["Out Qty"], errors="coerce").fillna(0).eq(0)
    )
    log["dead_rows_removed"] = int(zero_mask.sum())
    cleaned = df[~zero_mask].copy()
    log["final_row_count"] = len(cleaned)
    return cleaned, log


uploaded_file = st.file_uploader("Upload Product Group Summary CSV", type=["csv"])

if uploaded_file:
    df_raw = load_csv(uploaded_file)
    st.write(f"**Loaded {len(df_raw)} rows, {len(df_raw.columns)} columns.**")

    if st.button("🧹 Clean Data", type="primary"):
        with st.spinner("Processing..."):
            cleaned_df, log = clean_data(df_raw)
        st.session_state["cleaned_data_result"] = (cleaned_df, log, uploaded_file.name)

    if "cleaned_data_result" in st.session_state:
        cleaned_df, log, source_name = st.session_state["cleaned_data_result"]

        st.divider()
        c1, c2 = st.columns(2)
        c1.metric("Rows removed (all-zero)", log["dead_rows_removed"])
        c2.metric("Final row count", log["final_row_count"])

        st.subheader("Preview")
        st.dataframe(cleaned_df, use_container_width=True, hide_index=True)

        csv_bytes = cleaned_df.to_csv(index=False).encode("utf-8-sig")
        out_name = source_name.rsplit(".", 1)[0] + "_cleaned.csv"
        st.download_button(
            "⬇️ Download Cleaned CSV",
            data=csv_bytes,
            file_name=out_name,
            mime="text/csv",
            type="primary",
        )
