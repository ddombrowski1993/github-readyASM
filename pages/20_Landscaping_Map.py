import importlib
import io
import traceback
from html import escape

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

st.set_page_config(page_title="Landscaping Map", layout="wide")

from src.database import log_action
from src.utils import apply_theme, ensure_database_or_stop, metric_help_card, page_header, sidebar_nav

try:
    landscaping = importlib.import_module("src.landscaping")
except Exception as exc:
    st.error(f"Landscaping feature load failed: {exc}")
    st.stop()


def excel_bytes(df):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Report")
    return buffer.getvalue()


def csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8")


def center_for(df):
    valid = df.dropna(subset=["latitude", "longitude"])
    if valid.empty:
        return [41.4993, -81.6944]
    return [float(valid["latitude"].mean()), float(valid["longitude"].mean())]


def current_user_label():
    return st.session_state.get("user_email") or st.session_state.get("username") or st.session_state.get("active_account_label") or ""


def metric_int(value):
    try:
        return f"{int(value):,}"
    except Exception:
        return "0"


def render_preview_metrics(summary):
    cols = st.columns(6)
    cols[0].metric("Rows Found", metric_int(summary.get("rows_found")))
    cols[1].metric("Unique Stores", metric_int(summary.get("unique_stores")))
    cols[2].metric("Matched Stores", metric_int(summary.get("matched_stores")))
    cols[3].metric("Unmatched Stores", metric_int(summary.get("unmatched_stores")))
    cols[4].metric("Landscaping Vendors", metric_int(summary.get("landscaping_vendors")))
    with cols[5]:
        metric_help_card("Blank Vendor", metric_int(summary.get("blank_vendor_rows")), "Blank landscaping cells are ignored in Safe Update mode.")


def render_result_metrics(result):
    cols = st.columns(6)
    cols[0].metric("Matched Stores", metric_int(result.get("matched_stores")))
    cols[1].metric("Updated Assignments", metric_int(result.get("updated_assignments")))
    cols[2].metric("Unchanged", metric_int(result.get("unchanged_assignments")))
    cols[3].metric("New Vendors", metric_int(result.get("new_vendors_created")))
    cols[4].metric("Unmatched", metric_int(result.get("unmatched_stores")))
    cols[5].metric("Blank Ignored", metric_int(result.get("blank_vendor_rows")))


def color_for_row(row):
    color = str(row.get("display_color") or "").strip()
    if color:
        return color
    vendor = str(row.get("landscaping_vendor") or "").strip()
    return landscaping.stable_color(vendor) if vendor else "#9ca3af"


def map_popup(row):
    vendor = str(row.get("landscaping_vendor") or "Unassigned").strip() or "Unassigned"
    schedule = str(row.get("landscaping_schedule") or "").strip()
    lines = [
        f"<b>Store {escape(str(row.get('store_number', '')))}</b>",
        escape(str(row.get("address") or "")),
        f"{escape(str(row.get('city') or ''))}, {escape(str(row.get('state') or ''))} {escape(str(row.get('zip') or ''))}",
        f"<b>Landscaping Vendor:</b> {escape(vendor)}",
    ]
    if schedule:
        lines.append(f"<b>Landscaping Schedule:</b> {escape(schedule)}")
    return "<br>".join(lines)


def render_legend(summary_df, colors):
    if summary_df.empty:
        return
    st.subheader("Landscaping Vendors")
    for _, row in summary_df.iterrows():
        vendor = row["Landscaping Vendor"]
        color = colors.get(vendor, "#9ca3af")
        st.markdown(
            f"""
            <div style="display:flex;align-items:center;gap:.45rem;margin:.2rem 0;">
              <span style="width:14px;height:14px;border-radius:50%;background:{color};border:1px solid #475569;display:inline-block;"></span>
              <span><b>{escape(str(vendor))}</b> — {int(row["Stores"]):,} stores</span>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_landscaping_map(df, show_dots=True, show_territories=True, show_labels=True, key="landscaping_map"):
    mapped = df.copy()
    if mapped.empty:
        st.info("No stores match the current filters.")
        return {}
    mapped["latitude"] = pd.to_numeric(mapped["latitude"], errors="coerce")
    mapped["longitude"] = pd.to_numeric(mapped["longitude"], errors="coerce")
    mapped = mapped.dropna(subset=["latitude", "longitude"])
    if mapped.empty:
        st.warning("No stores with coordinates match the current filters.")
        return {}
    fmap = folium.Map(location=center_for(mapped), zoom_start=7, tiles="OpenStreetMap")
    if show_territories:
        for shape in landscaping.territory_shapes(mapped):
            label = shape["vendor"]
            color = shape["color"]
            if shape["hull"]:
                folium.Polygon(
                    locations=shape["hull"],
                    color=color,
                    weight=2,
                    fill=True,
                    fill_color=color,
                    fill_opacity=0.14,
                    tooltip=f"{label} territory visualization",
                ).add_to(fmap)
            else:
                radius_miles = 18 if len(shape["points"]) == 1 else 28
                folium.Circle(
                    location=shape["center"],
                    radius=radius_miles * 1609.344,
                    color=color,
                    weight=2,
                    fill=True,
                    fill_color=color,
                    fill_opacity=0.12,
                    tooltip=f"{label} local coverage",
                ).add_to(fmap)
            if show_labels and len(shape["points"]) >= 2:
                folium.Marker(
                    shape["center"],
                    icon=folium.DivIcon(
                        html=f"""
                        <div style="background:white;border:2px solid {color};border-radius:6px;color:#111827;
                                    font-weight:800;font-size:12px;padding:3px 6px;white-space:nowrap;
                                    box-shadow:0 1px 4px rgba(0,0,0,.18);">
                            {escape(label)}
                        </div>
                        """
                    ),
                ).add_to(fmap)
    if show_dots:
        for _, row in mapped.iterrows():
            vendor = str(row.get("landscaping_vendor") or "").strip()
            color = color_for_row(row)
            tooltip = f"Store {row.get('store_number', '')} - {vendor or 'Unassigned'}"
            folium.CircleMarker(
                [float(row["latitude"]), float(row["longitude"])],
                radius=6,
                color="#ffffff",
                weight=1,
                fill=True,
                fill_color=color,
                fill_opacity=0.94,
                tooltip=tooltip,
                popup=folium.Popup(map_popup(row), max_width=340),
            ).add_to(fmap)
    return st_folium(fmap, width=None, height=720, key=key)


apply_theme()
sidebar_nav()
ensure_database_or_stop()
page_header("Landscaping Map", "Upload landscaping vendor assignments and view vendor coverage by geography.")
try:
    landscaping.ensure_landscaping_tables()
except Exception as exc:
    st.error("Landscaping database setup failed.")
    st.code(f"{type(exc).__name__}: {exc}", language="text")
    with st.expander("Full setup traceback", expanded=True):
        st.code(traceback.format_exc(), language="text")
    st.stop()

with st.expander("Import / Update Landscaping Assignments", expanded=True):
    upload = st.file_uploader("Upload Landscaping Assignment File", type=["xlsx", "xls", "xlsm", "csv"], key="landscaping_upload")
    if upload:
        scans = landscaping.scan_landscaping_workbook(upload)
        scan_options = [
            item
            for item in scans
            if not item.get("df", pd.DataFrame()).empty
        ]
        if not scan_options:
            st.error("No usable rows were found in this upload.")
            st.stop()
        sheet_names = [item["sheet"] for item in scan_options]
        selected_sheet = st.selectbox("Detected workbook sheet", sheet_names, index=0)
        scan = next(item for item in scan_options if item["sheet"] == selected_sheet)
        incoming = scan["df"]
        auto_mapping = {
            "store_number": scan.get("store_column") or landscaping.find_column(incoming.columns, landscaping.STORE_ALIASES, fallback_index=0),
            "landscaping_vendor": scan.get("landscaping_column") or landscaping.find_column(incoming.columns, landscaping.LANDSCAPING_ALIASES, fallback_index=22 if len(incoming.columns) > 22 else None),
            "landscaping_schedule": scan.get("schedule_column") or landscaping.find_column(incoming.columns, landscaping.SCHEDULE_ALIASES),
        }
        for field, aliases in landscaping.LOCATION_ALIASES.items():
            auto_mapping[field] = landscaping.find_column(incoming.columns, aliases)
        st.caption(
            f"Header row detected: {scan['header_row'] + 1}. Rows detected: {scan['rows']:,}. "
            f"Store Number column detected: {auto_mapping.get('store_number') or 'Not detected'}. "
            f"Landscaping column detected: {auto_mapping.get('landscaping_vendor') or 'Not detected'}."
        )
        options = [""] + incoming.columns.tolist()
        with st.expander("Advanced Mapping", expanded=not auto_mapping.get("store_number") or not auto_mapping.get("landscaping_vendor")):
            for field, label in [
                ("store_number", "Store Number"),
                ("landscaping_vendor", "Landscaping Vendor"),
                ("landscaping_schedule", "Landscaping Schedule"),
                ("address", "Address"),
                ("city", "City"),
                ("state", "State"),
                ("zip", "ZIP"),
            ]:
                default = auto_mapping.get(field, "")
                auto_mapping[field] = st.selectbox(
                    label,
                    options,
                    index=options.index(default) if default in options else 0,
                    key=f"landscaping_map_{field}",
                )
        if not auto_mapping.get("store_number") or not auto_mapping.get("landscaping_vendor"):
            st.error("Choose both Store Number and Landscaping Vendor before previewing this file.")
        else:
            preview, preview_summary = landscaping.build_landscaping_preview(incoming, auto_mapping)
            st.session_state["landscaping_preview"] = preview
            st.session_state["landscaping_preview_summary"] = preview_summary
            st.session_state["landscaping_upload_name"] = upload.name
            st.subheader("Import Preview")
            render_preview_metrics(preview_summary)
            preview_cols = [
                "Store",
                "Landscaping Vendor",
                "Landscaping Schedule",
                "Match Status",
                "Location",
                "Address From Upload",
                "City From Upload",
                "State From Upload",
            ]
            st.dataframe(preview[preview_cols].head(100), use_container_width=True, hide_index=True)
            if preview_summary.get("blank_vendor_rows"):
                st.info("Safe Update is on. Blank landscaping values in the upload will not erase existing assignments.")
            if st.button("Import Landscaping Assignments", type="primary"):
                result = landscaping.apply_landscaping_import(preview, upload.name, imported_by=current_user_label(), safe_update=True)
                st.session_state["landscaping_import_result"] = result
                log_action(
                    "landscaping import",
                    "store_landscaping_assignments",
                    description=f"{upload.name}: {result['updated_assignments']} updated, {result['unmatched_stores']} unmatched.",
                )
                st.success("Import Complete")
                st.rerun()

if st.session_state.get("landscaping_import_result"):
    with st.expander("Last Import Results", expanded=True):
        result = st.session_state["landscaping_import_result"]
        render_result_metrics(result)
        if result.get("unmatched"):
            st.subheader("Unmatched Stores")
            st.dataframe(pd.DataFrame(result["unmatched"]), use_container_width=True, hide_index=True)

data = landscaping.landscaping_assignments_df()
if data.empty:
    st.info("No active stores were found. Upload the master store database before using the Landscaping Map.")
    st.stop()

data["landscaping_vendor"] = data["landscaping_vendor"].fillna("")
data["vendor_label"] = data["landscaping_vendor"].replace("", "Unassigned")
data["dot_color"] = data.apply(color_for_row, axis=1)

summary_df = landscaping.vendor_summary(data)
assigned_count = int(data["landscaping_vendor"].astype(str).str.strip().ne("").sum())
unassigned_count = int(len(data) - assigned_count)
largest = summary_df[summary_df["Landscaping Vendor"] != "Unassigned"].head(1)
largest_vendor = largest.iloc[0]["Landscaping Vendor"] if not largest.empty else "-"
largest_count = int(largest.iloc[0]["Stores"]) if not largest.empty else 0

st.subheader("Landscaping Overview")
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Total Landscaping Stores", metric_int(len(data)))
c2.metric("Landscaping Vendors", metric_int(data["landscaping_vendor"].replace("", pd.NA).dropna().nunique()))
c3.metric("Assigned Stores", metric_int(assigned_count))
c4.metric("Unassigned Stores", metric_int(unassigned_count))
c5.metric("Largest Vendor", largest_vendor)
c6.metric("Largest Count", metric_int(largest_count))
st.dataframe(summary_df, use_container_width=True, hide_index=True)

st.subheader("Filters / Display")
f1, f2, f3, f4 = st.columns(4)
vendor_options = ["All Vendors"] + sorted([value for value in data["vendor_label"].unique().tolist() if str(value).strip()])
vendor_filter = f1.selectbox("Landscaping Vendor", vendor_options)
state_options = ["All"] + sorted([value for value in data["state"].dropna().astype(str).unique().tolist() if value.strip()])
state_filter = f2.selectbox("State", state_options)
market_options = ["All"] + sorted([value for value in data["market"].dropna().astype(str).unique().tolist() if value.strip()])
market_filter = f3.selectbox("Market", market_options)
status_filter = f4.selectbox("Assignment Status", ["All", "Assigned", "Unassigned"])
t1, t2, t3, t4 = st.columns(4)
show_dots = t1.toggle("Store Dots", value=True)
show_territories = t2.toggle("Vendor Territory Shading", value=True)
show_labels = t3.toggle("Vendor Labels", value=True)
show_unassigned = t4.toggle("Unassigned Stores", value=True)

filtered = data.copy()
if vendor_filter != "All Vendors":
    filtered = filtered[filtered["vendor_label"] == vendor_filter]
if state_filter != "All":
    filtered = filtered[filtered["state"].astype(str) == state_filter]
if market_filter != "All":
    filtered = filtered[filtered["market"].astype(str) == market_filter]
if status_filter == "Assigned":
    filtered = filtered[filtered["landscaping_vendor"].astype(str).str.strip().ne("")]
elif status_filter == "Unassigned":
    filtered = filtered[filtered["landscaping_vendor"].astype(str).str.strip().eq("")]
if not show_unassigned:
    filtered = filtered[filtered["landscaping_vendor"].astype(str).str.strip().ne("")]

left, right = st.columns([4, 1])
with left:
    st.subheader("Interactive Landscaping Map")
    map_data = render_landscaping_map(
        filtered,
        show_dots=show_dots,
        show_territories=show_territories,
        show_labels=show_labels,
    )
with right:
    colors = data.groupby("vendor_label")["dot_color"].first().to_dict()
    render_legend(landscaping.vendor_summary(filtered), colors)

export_cols = [
    "store_number",
    "address",
    "city",
    "state",
    "zip",
    "landscaping_vendor",
    "latitude",
    "longitude",
    "landscaping_schedule",
]
export_df = filtered[export_cols].rename(
    columns={
        "store_number": "Store Number",
        "zip": "ZIP",
        "landscaping_vendor": "Landscaping Vendor",
        "landscaping_schedule": "Landscaping Schedule",
        "latitude": "Latitude",
        "longitude": "Longitude",
        "address": "Address",
        "city": "City",
        "state": "State",
    }
)
e1, e2 = st.columns(2)
e1.download_button("Export Landscaping CSV", data=csv_bytes(export_df), file_name="landscaping_assignments.csv", mime="text/csv")
e2.download_button(
    "Export Landscaping Excel",
    data=excel_bytes(export_df),
    file_name="landscaping_assignments.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

with st.expander("Selected Store / Manual Landscaping Correction", expanded=False):
    mapped_options = filtered.dropna(subset=["latitude", "longitude"]).copy()
    if mapped_options.empty:
        st.info("No mapped stores are available for manual correction under the current filters.")
    else:
        store_ids = mapped_options["store_id"].astype(int).tolist()
        indexed = mapped_options.set_index("store_id")
        selected_store_id = st.selectbox(
            "Store",
            store_ids,
            format_func=lambda value: f"Store {indexed.loc[value, 'store_number']} - {indexed.loc[value, 'city']}, {indexed.loc[value, 'state']}",
        )
        selected_row = indexed.loc[selected_store_id]
        vendor_names = sorted([value for value in data["landscaping_vendor"].dropna().unique().tolist() if str(value).strip()])
        current_vendor = str(selected_row.get("landscaping_vendor") or "")
        choices = [""] + vendor_names
        default_index = choices.index(current_vendor) if current_vendor in choices else 0
        selected_vendor = st.selectbox("Landscaping Vendor", choices, index=default_index, format_func=lambda value: value or "Unassigned")
        new_vendor = st.text_input("New vendor name", value="" if selected_vendor else "")
        schedule = st.text_input("Landscaping Schedule", value=str(selected_row.get("landscaping_schedule") or ""))
        final_vendor = new_vendor.strip() or selected_vendor
        if st.button("Save Landscaping Assignment"):
            landscaping.manual_assign_store(int(selected_store_id), final_vendor, schedule=schedule)
            log_action("landscaping assignment manually updated", "store_landscaping_assignments", int(selected_store_id), f"Store {selected_row['store_number']} set to {final_vendor or 'Unassigned'}.")
            st.success("Landscaping assignment saved.")
            st.rerun()

with st.expander("Import Issues / Unmatched Stores", expanded=False):
    result = st.session_state.get("landscaping_import_result") or {}
    unmatched = pd.DataFrame(result.get("unmatched") or [])
    if unmatched.empty:
        st.info("No unmatched stores from the latest landscaping import.")
    else:
        st.dataframe(unmatched, use_container_width=True, hide_index=True)
