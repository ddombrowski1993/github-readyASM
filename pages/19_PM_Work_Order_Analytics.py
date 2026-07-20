import hashlib
import re
from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="PM Work Order Analytics", layout="wide")

from src.exports import excel_bytes
from src.pm_work_order_analytics import (
    FIELD_LABELS,
    NORMALIZED_FIELDS,
    REQUIRED_FIELDS,
    apply_duration_rules,
    detect_column_mapping,
    empty_validation_result,
    ensure_analytics_ready,
    import_and_compare,
    load_duration_rules,
    normalize_records,
    query_events_df,
    query_snapshot_df,
    read_upload_dataframe,
    read_workbook_sheets,
    save_duration_rule,
    summary_counts,
    upload_runs_df,
    validate_normalized,
    workspace_key,
)
from src.utils import apply_theme, ensure_database_or_stop, metric_help_card, page_header, section_header, sidebar_nav


apply_theme()
sidebar_nav()
ensure_database_or_stop()
try:
    ensure_analytics_ready()
except Exception as exc:
    page_header("PM Work Order Analytics", "Database setup required")
    st.error("PM Work Order Analytics database tables have not been initialized for this workspace.")
    st.info("Open the app after the latest deployment finishes, or have an administrator run the normal database initialization.")
    st.code(str(exc))
    st.stop()

page_header(
    "PM Work Order Analytics",
    "Upload daily PM work-order exports, compare changes between uploads, and review technician, category, cancellation, and duration trends.",
)

st.info(
    "This page is isolated from the main Reports page. The first upload creates a baseline; later uploads show status changes first observed between uploads."
)


def display_df(df, columns=None, max_rows=100):
    if df.empty:
        st.info("No records found for the selected view.")
        return
    shown = df.copy()
    if columns:
        shown = shown[[col for col in columns if col in shown.columns]]
    st.dataframe(shown.head(max_rows), use_container_width=True, hide_index=True)
    if len(shown) > max_rows:
        st.caption(f"Showing first {max_rows:,} of {len(shown):,} rows. Use filters or export for more detail.")


def download_df(df, label, filename, key):
    st.download_button(
        label,
        data=excel_bytes(df),
        file_name=filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        disabled=df.empty,
        key=key,
    )


def safe_key_part(value):
    return re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "")).strip("_")[:80]


def stable_validation_result(validation):
    result = empty_validation_result()
    result.update(validation or {})
    return result


def filtered_snapshot(df):
    filtered = df.copy()
    if filtered.empty:
        return filtered
    c1, c2, c3, c4 = st.columns(4)
    status = c1.multiselect("Normalized Status", sorted(filtered["normalized_status"].dropna().unique().tolist()))
    technician = c2.multiselect("PM Technician", sorted(filtered["pm_technician"].fillna("").replace("", "Unassigned").unique().tolist()))
    work_type = c3.multiselect("Work Type", sorted(filtered["work_type"].fillna("").replace("", "Blank").unique().tolist()))
    duration = c4.multiselect("Duration Status", sorted(filtered["duration_status"].fillna("").replace("", "Blank").unique().tolist()))
    c5, c6, c7 = st.columns(3)
    store_search = c5.text_input("Store search")
    wo_search = c6.text_input("Work-order search")
    text_search = c7.text_input("Description / notes search")
    if status:
        filtered = filtered[filtered["normalized_status"].isin(status)]
    if technician:
        compare = filtered["pm_technician"].fillna("").replace("", "Unassigned")
        filtered = filtered[compare.isin(technician)]
    if work_type:
        compare = filtered["work_type"].fillna("").replace("", "Blank")
        filtered = filtered[compare.isin(work_type)]
    if duration:
        compare = filtered["duration_status"].fillna("").replace("", "Blank")
        filtered = filtered[compare.isin(duration)]
    if store_search:
        filtered = filtered[filtered["store_number"].fillna("").astype(str).str.contains(store_search, case=False, na=False)]
    if wo_search:
        filtered = filtered[filtered["work_order_id"].fillna("").astype(str).str.contains(wo_search, case=False, na=False)]
    if text_search:
        haystack = (
            filtered["short_description"].fillna("").astype(str)
            + " "
            + filtered["work_notes"].fillna("").astype(str)
            + " "
            + filtered["additional_comments"].fillna("").astype(str)
        )
        filtered = filtered[haystack.str.contains(text_search, case=False, na=False)]
    return filtered


with st.expander("Upload Current Work Order Report", expanded=True):
    st.caption("Use the current Excel export. Column order can change; mapping is based on headers.")
    upload = st.file_uploader("Upload .xlsx work-order export", type=["xlsx"], key="pm_wo_upload")
    if upload:
        file_bytes, sheet_names = read_workbook_sheets(upload)
        file_hash = hashlib.sha256(file_bytes).hexdigest()[:16]
        sheet_name = st.selectbox("Worksheet", sheet_names, key="pm_wo_sheet")
        incoming = read_upload_dataframe(file_bytes, sheet_name)
        detected_mapping, ambiguous = detect_column_mapping(incoming.columns.tolist())
        st.write(f"Rows detected: **{len(incoming):,}** | Columns detected: **{len(incoming.columns):,}**")
        st.caption(f"Current workspace: {workspace_key()}")
        st.subheader("Column Mapping")
        options = [""] + incoming.columns.tolist()
        manual_mapping = {}
        cols = st.columns(3)
        for index, field in enumerate(NORMALIZED_FIELDS):
            default = detected_mapping.get(field, "")
            label = FIELD_LABELS.get(field, field)
            mapping_key = f"pm_wo_map_{safe_key_part(workspace_key())}_{file_hash}_{safe_key_part(sheet_name)}_{field}"
            manual_mapping[field] = cols[index % 3].selectbox(
                label,
                options,
                index=options.index(default) if default in options else 0,
                key=mapping_key,
            )
        normalized = normalize_records(incoming, manual_mapping)
        normalized = apply_duration_rules(normalized)
        validation = stable_validation_result(validate_normalized(normalized, manual_mapping, ambiguous))
        v1, v2, v3, v4, v5, v6 = st.columns(6)
        v1.metric("Rows", f"{int(validation.get('rows') or 0):,}")
        v2.metric("Unique WOs", f"{int(validation.get('unique_work_orders') or 0):,}")
        v3.metric("Duplicate Rows", int(validation.get("duplicate_row_count") or validation.get("duplicate_work_order_ids") or 0))
        v4.metric("Duplicate WO IDs", int(validation.get("duplicate_unique_work_order_ids") or 0))
        v5.metric("Missing WO IDs", int(validation.get("missing_work_order_ids") or 0))
        v6.metric("Invalid Durations", int(validation.get("missing_or_invalid_durations") or 0))
        if validation.get("missing_required"):
            st.error("Missing required mappings: " + ", ".join(FIELD_LABELS[field] for field in validation["missing_required"]))
        if validation.get("mapping_errors"):
            for error in validation["mapping_errors"]:
                st.error(error)
        if validation.get("unrecognized_statuses"):
            st.warning("Unrecognized statuses will be grouped as Other: " + ", ".join(validation["unrecognized_statuses"]))
        if ambiguous:
            with st.expander("Column Mapping Recommendations", expanded=False):
                for field, details in ambiguous.items():
                    st.markdown(f"**{FIELD_LABELS.get(field, field)}**")
                    st.caption(f"Recommended: {details.get('recommended', '')}")
                    others = details.get("other_candidates") or []
                    if others:
                        st.caption("Other possible columns: " + ", ".join(others))
                    st.caption(f"{details.get('recommended', '')} was selected because it is the strongest header match. Review the dropdown before importing.")
        st.subheader("Validation Preview")
        preview_cols = [
            "work_order_id",
            "record_number",
            "store_number",
            "created_at",
            "status",
            "normalized_status",
            "category",
            "subcategory",
            "line_of_service",
            "work_type",
            "pm_technician",
            "closed_by",
            "actual_work_duration_minutes",
            "duration_status",
        ]
        display_df(normalized, preview_cols, max_rows=50)
        if int(validation.get("duplicate_row_count") or validation.get("duplicate_work_order_ids") or 0):
            duplicate_preview = normalized[
                normalized["work_order_id"].replace("", pd.NA).notna()
                & normalized["work_order_id"].duplicated(keep=False)
            ].copy()
            duplicate_cols = [
                "source_row_number",
                "work_order_id",
                "record_number",
                "store_number",
                "status",
                "short_description",
                "pm_technician",
            ]
            with st.expander("Duplicate Work Order Preview", expanded=False):
                display_df(duplicate_preview, duplicate_cols, max_rows=100)
                download_df(duplicate_preview[duplicate_cols], "Download Duplicate Preview", "pm_work_order_duplicate_preview.xlsx", "pm_wo_duplicate_preview")
        import_disabled = not validation["can_import"]
        if st.button("Import and Compare", type="primary", disabled=import_disabled):
            summary = import_and_compare(normalized, upload.name, file_bytes, sheet_name)
            st.session_state["pm_wo_import_summary"] = summary
            st.cache_data.clear()
            st.rerun()

if st.session_state.get("pm_wo_import_summary"):
    summary = st.session_state["pm_wo_import_summary"]
    if summary.get("baseline"):
        st.success("Baseline created. Daily work-order changes will be available after the next upload.")
    else:
        st.success(
            f"Import complete. {summary['changed_count']:,} meaningful change event(s), "
            f"{summary['newly_closed_count']:,} newly completed, {summary['newly_canceled_count']:,} newly canceled."
        )


@st.cache_data(show_spinner=False)
def cached_snapshot(key):
    return query_snapshot_df()


@st.cache_data(show_spinner=False)
def cached_events(key):
    return query_events_df()


snapshot = cached_snapshot(workspace_key())
events = cached_events(workspace_key())
runs = upload_runs_df()
counts = summary_counts()

if snapshot.empty:
    st.warning("No PM work-order baseline has been uploaded for this workspace yet.")
    st.stop()

overview_tab, daily_tab, tech_tab, category_tab, duration_tab, cancel_tab, explorer_tab, history_tab, settings_tab = st.tabs(
    [
        "Overview",
        "Daily Changes",
        "Technician Performance",
        "Work Categories",
        "Duration Review",
        "Cancellations",
        "Work Order Explorer",
        "Upload History",
        "Settings",
    ]
)

with overview_tab:
    section_header("Overview", "Current work-order snapshot and changes first observed during the latest uploads.", "blue")
    latest_run_id = int(runs.iloc[0]["id"]) if not runs.empty else None
    latest_events = events[events["upload_run_id"] == latest_run_id] if latest_run_id and not events.empty else pd.DataFrame()
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Total Work Orders", f"{counts.get('Total', 0):,}")
    k2.metric("Open", f"{counts.get('Open', 0):,}")
    k3.metric("Completed", f"{counts.get('Completed', 0):,}")
    k4.metric("Canceled", f"{counts.get('Canceled', 0):,}")
    k5.metric("Closed Since Last Upload", int((latest_events["event_type"] == "NEWLY_COMPLETED").sum()) if not latest_events.empty else 0)
    with k6:
        metric_help_card("Duration Flags", counts.get("Duration Flags", 0), "Work orders with missing, invalid, below-range, or above-range work duration.")
    c1, c2 = st.columns(2)
    status_counts = snapshot.groupby("normalized_status", dropna=False).size().reset_index(name="Count")
    c1.plotly_chart(px.pie(status_counts, values="Count", names="normalized_status", title="Status Overview"), use_container_width=True)
    work_type_counts = snapshot.groupby("work_type", dropna=False).size().reset_index(name="Count").sort_values("Count", ascending=False).head(12)
    c2.plotly_chart(px.bar(work_type_counts, x="work_type", y="Count", title="Work Type Breakdown"), use_container_width=True)
    st.subheader("Corrective vs Planned Maintenance")
    cvp = snapshot.assign(work_type_group=snapshot["work_type"].fillna("").str.title())
    cvp_counts = cvp.groupby(["pm_technician", "work_type_group"], dropna=False).size().reset_index(name="Count")
    st.plotly_chart(px.bar(cvp_counts, x="pm_technician", y="Count", color="work_type_group", title="Work Type by PM Technician"), use_container_width=True)

with daily_tab:
    section_header("Daily Changes", "Status transitions are based on comparison between uploads.", "green")
    if events.empty:
        st.info("No change events yet. Upload a second report after the baseline to populate this section.")
    else:
        change_filter = st.multiselect("Event Type", sorted(events["event_type"].unique().tolist()), default=["NEWLY_COMPLETED"] if "NEWLY_COMPLETED" in events["event_type"].unique() else [])
        event_view = events[events["event_type"].isin(change_filter)] if change_filter else events
        c1, c2 = st.columns(2)
        trend = event_view.copy()
        trend["Detected Date"] = pd.to_datetime(trend["detected_at"]).dt.date
        trend_counts = trend.groupby(["Detected Date", "event_type"]).size().reset_index(name="Count")
        c1.plotly_chart(px.line(trend_counts, x="Detected Date", y="Count", color="event_type", markers=True, title="Changes by Upload Date"), use_container_width=True)
        tech_counts = event_view.groupby(["pm_technician", "event_type"], dropna=False).size().reset_index(name="Count").sort_values("Count", ascending=False).head(30)
        c2.plotly_chart(px.bar(tech_counts, x="pm_technician", y="Count", color="event_type", title="Changes by Technician"), use_container_width=True)
        st.caption("When no source closed timestamp exists, these dates mean first observed completed/canceled during upload comparison.")
        display_df(event_view, max_rows=150)
        download_df(event_view, "Export Daily Changes", "pm_work_order_daily_changes.xlsx", "pm_wo_daily_export")

with tech_tab:
    section_header("Technician Performance", "Technician-level workload, outcomes, work type mix, and duration flags.", "blue")
    tech = snapshot.copy()
    tech["pm_technician"] = tech["pm_technician"].fillna("").replace("", "Unassigned")
    tech_summary = tech.groupby("pm_technician").agg(
        total=("work_order_id", "count"),
        open=("normalized_status", lambda s: int((s == "Open").sum())),
        completed=("normalized_status", lambda s: int((s == "Completed").sum())),
        canceled=("normalized_status", lambda s: int((s == "Canceled").sum())),
        corrective=("work_type", lambda s: int(s.fillna("").str.contains("corrective", case=False).sum())),
        planned=("work_type", lambda s: int(s.fillna("").str.contains("planned|maintenance|pm", case=False, regex=True).sum())),
        avg_duration=("actual_work_duration_minutes", "mean"),
        duration_flags=("duration_status", lambda s: int(s.isin(["Below Expected Range", "Above Expected Range", "Missing Duration", "Invalid Duration"]).sum())),
    ).reset_index()
    display_df(tech_summary.sort_values("total", ascending=False), max_rows=200)
    st.plotly_chart(px.bar(tech_summary, x="pm_technician", y=["corrective", "planned"], title="Corrective vs Planned by Technician", barmode="group"), use_container_width=True)
    download_df(tech_summary, "Export Technician Summary", "pm_work_order_technician_summary.xlsx", "pm_wo_tech_export")

with category_tab:
    section_header("Work Categories", "Break down work by category, subcategory, line of service, priority, and work type.", "green")
    left, right = st.columns(2)
    for col_name, holder, title in [
        ("category", left, "Category"),
        ("subcategory", right, "Subcategory"),
    ]:
        data = snapshot.groupby([col_name, "normalized_status"], dropna=False).size().reset_index(name="Count").sort_values("Count", ascending=False).head(40)
        holder.plotly_chart(px.bar(data, x=col_name, y="Count", color="normalized_status", title=title), use_container_width=True)
    los = snapshot.groupby(["line_of_service", "normalized_status"], dropna=False).size().reset_index(name="Count").sort_values("Count", ascending=False).head(40)
    st.plotly_chart(px.bar(los, x="line_of_service", y="Count", color="normalized_status", title="Line of Service"), use_container_width=True)
    category_choice = st.selectbox("Drill into Category", [""] + sorted(snapshot["category"].fillna("").replace("", "Blank").unique().tolist()))
    if category_choice:
        category_records = snapshot[snapshot["category"].fillna("").replace("", "Blank").eq(category_choice)]
        display_df(category_records, max_rows=100)
        download_df(category_records, "Export Category Detail", "pm_work_order_category_detail.xlsx", "pm_wo_category_export")

with duration_tab:
    section_header("Duration Review", "Flags are review indicators, not automatic proof of poor work.", "orange")
    dur_counts = snapshot.groupby("duration_status", dropna=False).size().reset_index(name="Count")
    st.plotly_chart(px.bar(dur_counts, x="duration_status", y="Count", title="Duration Status"), use_container_width=True)
    flagged = snapshot[snapshot["duration_status"].isin(["Below Expected Range", "Above Expected Range", "Missing Duration", "Invalid Duration"])]
    display_df(
        flagged,
        [
            "work_order_id",
            "store_number",
            "pm_technician",
            "category",
            "subcategory",
            "line_of_service",
            "work_type",
            "actual_work_duration_minutes",
            "expected_min_minutes",
            "expected_max_minutes",
            "duration_status",
            "short_description",
        ],
        max_rows=150,
    )
    download_df(flagged, "Export Duration Exceptions", "pm_work_order_duration_exceptions.xlsx", "pm_wo_duration_export")

with cancel_tab:
    section_header("Cancellations", "Review canceled work orders and stores with repeated cancellations.", "red")
    canceled = snapshot[snapshot["normalized_status"].eq("Canceled")]
    threshold = st.number_input("Repeat cancellation threshold", min_value=1, max_value=20, value=3, step=1)
    repeat_stores = canceled.groupby("store_number", dropna=False).size().reset_index(name="Canceled Count").sort_values("Canceled Count", ascending=False)
    repeat_stores = repeat_stores[repeat_stores["Canceled Count"] >= threshold]
    c1, c2 = st.columns(2)
    c1.metric("Canceled Work Orders", len(canceled))
    c2.metric("Repeat Cancellation Stores", len(repeat_stores))
    if not repeat_stores.empty:
        st.plotly_chart(px.bar(repeat_stores.head(30), x="store_number", y="Canceled Count", title="Stores with Repeat Cancellations"), use_container_width=True)
    display_df(canceled, max_rows=150)
    download_df(canceled, "Export Canceled Work Orders", "pm_work_order_canceled.xlsx", "pm_wo_cancel_export")
    download_df(repeat_stores, "Export Repeat Cancellation Stores", "pm_work_order_repeat_cancellations.xlsx", "pm_wo_repeat_cancel_export")

with explorer_tab:
    section_header("Work Order Explorer", "Filter and paginate detailed records without rendering every row at once.", "gray")
    filtered = filtered_snapshot(snapshot)
    page_size = st.selectbox("Rows per page", [50, 100, 250], index=0)
    total_pages = max(1, int((len(filtered) - 1) / page_size) + 1)
    page = st.number_input("Page", min_value=1, max_value=total_pages, value=1, step=1)
    start = (page - 1) * page_size
    end = start + page_size
    st.caption(f"{len(filtered):,} records after filters. Page {page} of {total_pages}.")
    display_df(filtered.iloc[start:end], max_rows=page_size)
    selected_wo = st.selectbox("Open detail for work order", [""] + filtered["work_order_id"].astype(str).head(1000).tolist())
    if selected_wo:
        record = filtered[filtered["work_order_id"].astype(str).eq(selected_wo)].head(1).T.reset_index()
        record.columns = ["Field", "Value"]
        st.dataframe(record, use_container_width=True, hide_index=True)
    download_df(filtered, "Export Filtered Detail", "pm_work_order_filtered_detail.xlsx", "pm_wo_filtered_export")

with history_tab:
    section_header("Upload History", "Upload run summaries are stored; full workbooks are not retained.", "blue")
    display_df(runs, max_rows=100)
    download_df(runs, "Export Upload History", "pm_work_order_upload_history.xlsx", "pm_wo_history_export")

with settings_tab:
    section_header("Settings", "Configure expected duration ranges by category, subcategory, line of service, work type, or description pattern.", "gray")
    rules = pd.DataFrame(load_duration_rules())
    if not rules.empty:
        st.subheader("Active Duration Rules")
        display_df(rules, max_rows=100)
    st.subheader("Add Duration Rule")
    c1, c2, c3 = st.columns(3)
    rule_name = c1.text_input("Rule name")
    category = c2.text_input("Category")
    subcategory = c3.text_input("Subcategory")
    c4, c5, c6 = st.columns(3)
    line_of_service = c4.text_input("Line of Service")
    work_type = c5.text_input("Work Type")
    pattern = c6.text_input("Short-description contains")
    c7, c8 = st.columns(2)
    min_minutes = c7.number_input("Minimum minutes", min_value=0.0, value=0.0, step=5.0)
    max_minutes = c8.number_input("Maximum minutes", min_value=0.0, value=0.0, step=5.0)
    notes = st.text_area("Notes")
    if st.button("Save Duration Rule", type="primary", disabled=not rule_name):
        save_duration_rule(
            {
                "rule_name": rule_name,
                "category": category,
                "subcategory": subcategory,
                "line_of_service": line_of_service,
                "work_type": work_type,
                "short_description_pattern": pattern,
                "min_minutes": min_minutes if min_minutes > 0 else None,
                "max_minutes": max_minutes if max_minutes > 0 else None,
                "notes": notes,
            }
        )
        st.cache_data.clear()
        st.success("Duration rule saved. Re-import the current report to apply it to existing snapshot records.")
