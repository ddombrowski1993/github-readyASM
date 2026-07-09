import io
from pathlib import Path

import pandas as pd
import streamlit as st


def excel_bytes(df):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Report")
    return buffer.getvalue()


def csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8")


def download_table(df, base_name, key_suffix=None):
    suffix = key_suffix or base_name
    c1, c2 = st.columns(2)
    c1.download_button(
        "Download Excel",
        data=excel_bytes(df),
        file_name=f"{base_name}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        disabled=df.empty,
        key=f"download_excel_{suffix}",
    )
    c2.download_button(
        "Download CSV",
        data=csv_bytes(df),
        file_name=f"{base_name}.csv",
        mime="text/csv",
        disabled=df.empty,
        key=f"download_csv_{suffix}",
    )


def save_excel(df, path):
    path = Path(path)
    path.parent.mkdir(exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Report")
    return path
