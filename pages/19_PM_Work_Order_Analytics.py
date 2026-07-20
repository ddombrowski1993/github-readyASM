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
    normalize_status,
    query_events_df,
    query_snapshot_df,
    read_upload_dataframe,
    read_workbook_sheets,
    save_duration_rule,
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


DETAIL_COLUMNS = [
    "work_order_id",
    "record_number",
    "store_number",
    "pm_technician",
    "status",
    "normalized_status",
    "priority",
    "work_type",
    "work_type_group",
    "category",
    "subcategory",
    "line_of_service",
    "short_description",
    "duration_display",
    "duration_status",
    "closed_by",
    "created_at",
]
REQUIRED_DETAIL_COLUMNS = ["work_order_id", "store_number", "status", "normalized_status"]


def work_type_group(value):
    text = str(value or "").strip().lower()
    if "corrective" in text:
        return "Corrective"
    if "planned" in text or "maintenance" in text or text == "pm":
        return "Planned Maintenance"
    return "Other"


def display_minutes(value):
    if value is None or pd.isna(value):
        return None
    minutes = float(value)
    if minutes > 24 * 60:
        minutes = minutes / 60
    return round(minutes, 2)


def safe_detail_df(df, required_columns=None):
    required_columns = required_columns or REQUIRED_DETAIL_COLUMNS
    missing_required = [col for col in required_columns if col not in df.columns]
    if missing_required:
        st.error("The filtered work-order data is missing required fields: " + ", ".join(missing_required))
        return pd.DataFrame(columns=DETAIL_COLUMNS), missing_required
    return df.reindex(columns=DETAIL_COLUMNS).copy(), []


def format_duration(value):
    minutes = display_minutes(value)
    if minutes is None:
        return ""
    total = int(round(minutes))
    hours, mins = divmod(total, 60)
    if hours:
        return f"{hours} hr {mins} min"
    return f"{mins} min"


def prepare_snapshot(df):
    prepared = df.copy()
    for col in ["store_number", "work_order_id", "record_number", "pm_technician", "work_type", "category", "subcategory", "line_of_service", "priority", "status"]:
        if col in prepared.columns:
            prepared[col] = prepared[col].fillna("").astype(str).str.strip()
    prepared["normalized_status"] = prepared["status"].map(normalize_status)
    prepared["store_number"] = prepared["store_number"].astype(str).str.replace(r"\.0$", "", regex=True)
    prepared["work_type_group"] = prepared["work_type"].map(work_type_group)
    prepared["duration_minutes_display"] = prepared["actual_work_duration_minutes"].map(display_minutes)
    prepared["duration_display"] = prepared["duration_minutes_display"].map(format_duration)
    return prepared


def set_filter(**updates):
    filters = st.session_state.get("pm_wo_filters", {}).copy()
    for key, value in updates.items():
        if value in (None, "", [], "All"):
            filters.pop(key, None)
        else:
            filters[key] = value
    st.session_state["pm_wo_filters"] = filters


def reset_filters():
    st.session_state["pm_wo_filters"] = {}


def apply_saved_filters(df):
    filtered = df.copy()
    filters = st.session_state.get("pm_wo_filters", {})
    if not filters:
        return filtered
    for col, value in filters.items():
        if col not in filtered.columns:
            continue
        if col in {"store_number", "work_order_id", "pm_technician", "short_description", "category", "subcategory", "line_of_service"}:
            filtered = filtered[filtered[col].fillna("").astype(str).str.contains(str(value), case=False, na=False)]
        elif isinstance(value, list):
            filtered = filtered[filtered[col].isin(value)]
        else:
            filtered = filtered[filtered[col].eq(value)]
    return filtered


def render_active_filters():
    filters = st.session_state.get("pm_wo_filters", {})
    if not filters:
        st.caption("Active filters: none")
        return
    parts = [f"{key}: {value}" for key, value in filters.items()]
    st.caption("Active filters: " + " | ".join(parts))
    st.button("Clear All Filters", on_click=reset_filters, key="pm_wo_clear_filters")


def render_schema_diagnostic(df):
    with st.expander("Snapshot Schema Diagnostic", expanded=False):
        missing_required = [col for col in REQUIRED_DETAIL_COLUMNS if col not in df.columns]
        missing_optional = [col for col in DETAIL_COLUMNS if col not in df.columns and col not in REQUIRED_DETAIL_COLUMNS]
        duplicate_columns = df.columns[df.columns.duplicated()].tolist()
        st.write("Rows:", len(df))
        st.write("Available columns:", df.columns.tolist())
        st.write("Missing required detail fields:", missing_required)
        st.write("Missing optional detail fields:", missing_optional)
        st.write("Duplicate column names:", duplicate_columns)
        st.dataframe(pd.DataFrame({"column": df.columns, "dtype": [str(dtype) for dtype in df.dtypes]}), use_container_width=True, hide_index=True)


def filtered_snapshot(df):
    filtered = df.copy()
    if filtered.empty:
        return filtered
    with st.form("pm_wo_explorer_filters"):
        c1, c2, c3, c4 = st.columns(4)
        status = c1.multiselect("Normalized Status", sorted(filtered["normalized_status"].dropna().unique().tolist()))
        technician = c2.text_input("Technician search")
        work_type = c3.multiselect("Work Type", sorted(filtered["work_type_group"].dropna().unique().tolist()))
        priority = c4.multiselect("Priority", sorted(filtered["priority"].fillna("").replace("", "Blank").unique().tolist()))
        c5, c6, c7, c8 = st.columns(4)
        store_search = c5.text_input("Store search")
        wo_search = c6.text_input("Work-order search")
        category_search = c7.text_input("Category search")
        text_search = c8.text_input("Description / notes search")
        apply_clicked = st.form_submit_button("Apply Filters")
    if apply_clicked:
        reset_filters()
        if status:
            set_filter(normalized_status=status)
        if technician:
            set_filter(pm_technician=technician)
        if work_type:
            set_filter(work_type_group=work_type)
        if priority:
            set_filter(priority=priority)
        if store_search:
            set_filter(store_number=store_search)
        if wo_search:
            set_filter(work_order_id=wo_search)
        if category_search:
            set_filter(category=category_search)
        if text_search:
            set_filter(short_description=text_search)
    return apply_saved_filters(filtered)


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

if snapshot.empty:
    st.warning("No PM work-order baseline has been uploaded for this workspace yet.")
    st.stop()

snapshot = prepare_snapshot(snapshot)
filtered_snapshot_global = apply_saved_filters(snapshot)
status_counts_all = snapshot["normalized_status"].value_counts(dropna=False).to_dict()
counts = {
    "Total": int(len(snapshot)),
    "Open": int(status_counts_all.get("Open", 0)),
    "In Progress": int(status_counts_all.get("In Progress", 0)),
    "Completed": int(status_counts_all.get("Completed", 0)),
    "Canceled": int(status_counts_all.get("Canceled", 0)),
    "Other": int(status_counts_all.get("Other", 0)),
    "Duration Flags": int(snapshot["duration_status"].isin(["Below Expected Range", "Above Expected Range"]).sum()),
}

render_active_filters()
render_schema_diagnostic(snapshot)

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
    k1, k2, k3, k4, k5, k6, k7 = st.columns(7)
    k1.metric("Total Work Orders", f"{counts.get('Total', 0):,}")
    k2.metric("Open", f"{counts.get('Open', 0):,}")
    k3.metric("In Progress", f"{counts.get('In Progress', 0):,}")
    k4.metric("Completed", f"{counts.get('Completed', 0):,}")
    k5.metric("Canceled", f"{counts.get('Canceled', 0):,}")
    k6.metric("Closed Since Last Upload", int((latest_events["event_type"] == "NEWLY_COMPLETED").sum()) if not latest_events.empty else 0)
    with k7:
        metric_help_card("Duration Flags", counts.get("Duration Flags", 0), "Work orders below or above a configured expected duration range.")
    status_total = counts.get("Open", 0) + counts.get("In Progress", 0) + counts.get("Completed", 0) + counts.get("Canceled", 0) + counts.get("Other", 0)
    if status_total != counts.get("Total", 0):
        st.warning(f"Status reconciliation mismatch: status groups total {status_total:,}, but total work orders is {counts.get('Total', 0):,}.")
    b1, b2, b3, b4, b5, b6 = st.columns(6)
    if b1.button("View Open Work Orders", key="view_open"):
        set_filter(normalized_status="Open")
        st.rerun()
    if b2.button("View In Progress", key="view_progress"):
        set_filter(normalized_status="In Progress")
        st.rerun()
    if b3.button("View Completed", key="view_completed"):
        set_filter(normalized_status="Completed")
        st.rerun()
    if b4.button("View Canceled", key="view_canceled"):
        set_filter(normalized_status="Canceled")
        st.rerun()
    if b5.button("View Corrective", key="view_corrective"):
        set_filter(work_type_group="Corrective")
        st.rerun()
    if b6.button("View Planned PM", key="view_planned"):
        set_filter(work_type_group="Planned Maintenance")
        st.rerun()
    c1, c2 = st.columns(2)
    status_counts = filtered_snapshot_global.groupby("normalized_status", dropna=False).size().reset_index(name="Count")
    c1.plotly_chart(px.pie(status_counts, values="Count", names="normalized_status", title="Status Overview"), use_container_width=True)
    work_type_counts = filtered_snapshot_global.groupby("work_type_group", dropna=False).size().reset_index(name="Count").sort_values("Count", ascending=False)
    c2.plotly_chart(px.bar(work_type_counts, x="work_type_group", y="Count", title="Work Type Breakdown"), use_container_width=True)
    st.subheader("Corrective vs Planned Maintenance")
    cvp_counts = filtered_snapshot_global.groupby(["pm_technician", "work_type_group"], dropna=False).size().reset_index(name="Count")
    st.plotly_chart(px.bar(cvp_counts, x="pm_technician", y="Count", color="work_type_group", title="Work Type by PM Technician"), use_container_width=True)
    with st.expander("Filtered Work Order Detail", expanded=bool(st.session_state.get("pm_wo_filters"))):
        display_df(filtered_snapshot_global, DETAIL_COLUMNS, max_rows=100)
        overview_export, overview_missing = safe_detail_df(filtered_snapshot_global)
        if not overview_missing:
            download_df(overview_export, "Export Filtered Work Orders", "pm_work_order_filtered_overview.xlsx", "pm_wo_overview_filtered_export")
    with st.expander("Status Diagnostics", expanded=False):
        diagnostics = snapshot.groupby(["status", "normalized_status"], dropna=False).size().reset_index(name="Count").sort_values("Count", ascending=False)
        display_df(diagnostics, max_rows=100)
        download_df(diagnostics, "Export Status Diagnostics", "pm_work_order_status_diagnostics.xlsx", "pm_wo_status_diag_export")

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
    technician_options = ["All Technicians"] + sorted(snapshot["pm_technician"].fillna("").replace("", "Unassigned").unique().tolist())
    selected_technician = st.selectbox("Technician", technician_options, key="pm_wo_tech_select")
    tech = filtered_snapshot_global.copy()
    tech["pm_technician"] = tech["pm_technician"].fillna("").replace("", "Unassigned")
    if selected_technician != "All Technicians":
        tech = tech[tech["pm_technician"].eq(selected_technician)]
    tech_summary = tech.groupby("pm_technician").agg(
        total=("work_order_id", "count"),
        open=("normalized_status", lambda s: int((s == "Open").sum())),
        in_progress=("normalized_status", lambda s: int((s == "In Progress").sum())),
        completed=("normalized_status", lambda s: int((s == "Completed").sum())),
        canceled=("normalized_status", lambda s: int((s == "Canceled").sum())),
        corrective=("work_type_group", lambda s: int((s == "Corrective").sum())),
        planned=("work_type_group", lambda s: int((s == "Planned Maintenance").sum())),
        avg_duration=("duration_minutes_display", "mean"),
        median_duration=("duration_minutes_display", "median"),
        duration_flags=("duration_status", lambda s: int(s.isin(["Below Expected Range", "Above Expected Range"]).sum())),
    ).reset_index()
    if not tech_summary.empty:
        tech_summary["completion_rate"] = (tech_summary["completed"] / tech_summary["total"]).fillna(0).map(lambda v: f"{v:.1%}")
        tech_summary["cancellation_rate"] = (tech_summary["canceled"] / tech_summary["total"]).fillna(0).map(lambda v: f"{v:.1%}")
        tech_summary["average_duration"] = tech_summary["avg_duration"].map(format_duration)
        tech_summary["median_duration_display"] = tech_summary["median_duration"].map(format_duration)
    display_cols = ["pm_technician", "total", "open", "in_progress", "completed", "canceled", "corrective", "planned", "completion_rate", "cancellation_rate", "average_duration", "median_duration_display", "duration_flags"]
    display_df(tech_summary.sort_values("total", ascending=False), display_cols, max_rows=200)
    left, right = st.columns(2)
    left.plotly_chart(px.bar(tech_summary, x="pm_technician", y=["corrective", "planned"], title="Corrective vs Planned by Technician", barmode="group"), use_container_width=True)
    status_tech = tech.groupby(["pm_technician", "normalized_status"], dropna=False).size().reset_index(name="Count")
    right.plotly_chart(px.bar(status_tech, x="pm_technician", y="Count", color="normalized_status", title="Completed vs Canceled by Technician", barmode="stack"), use_container_width=True)
    st.subheader("Technician Work Orders")
    display_df(tech, DETAIL_COLUMNS, max_rows=100)
    download_df(tech_summary, "Export Technician Summary", "pm_work_order_technician_summary.xlsx", "pm_wo_tech_export")

with category_tab:
    section_header("Work Categories", "Break down work by category, subcategory, line of service, priority, and work type.", "green")
    mode = st.radio("Work Type Scope", ["Planned Maintenance Only", "Corrective Only", "All Work"], horizontal=True, key="pm_wo_category_mode")
    top_n = st.selectbox("Show", [10, 20, 50, "All"], index=1, key="pm_wo_category_top")
    category_df = filtered_snapshot_global.copy()
    if mode == "Planned Maintenance Only":
        category_df = category_df[category_df["work_type_group"].eq("Planned Maintenance")]
    elif mode == "Corrective Only":
        category_df = category_df[category_df["work_type_group"].eq("Corrective")]
    category_df["category"] = category_df["category"].replace("", "Blank")
    category_df["subcategory"] = category_df["subcategory"].replace("", "Blank")
    category_df["line_of_service"] = category_df["line_of_service"].replace("", "Blank")
    category_totals = category_df.groupby("category", dropna=False).size().reset_index(name="Total").sort_values("Total", ascending=False)
    selected_categories = category_totals["category"].tolist() if top_n == "All" else category_totals["category"].head(int(top_n)).tolist()
    category_chart = category_df[category_df["category"].isin(selected_categories)].groupby(["category", "normalized_status"], dropna=False).size().reset_index(name="Count")
    st.plotly_chart(px.bar(category_chart, y="category", x="Count", color="normalized_status", title="Category Overview", orientation="h"), use_container_width=True)
    category_choice = st.selectbox("Drill into Category", [""] + category_totals["category"].tolist())
    if category_choice:
        category_records = category_df[category_df["category"].eq(category_choice)]
        sub = category_records.groupby(["subcategory", "normalized_status"], dropna=False).size().reset_index(name="Count")
        st.plotly_chart(px.bar(sub, y="subcategory", x="Count", color="normalized_status", title=f"Subcategories for {category_choice}", orientation="h"), use_container_width=True)
        subcategory_choice = st.selectbox("Optional Subcategory Drill-Down", [""] + sorted(category_records["subcategory"].unique().tolist()))
        los_source = category_records if not subcategory_choice else category_records[category_records["subcategory"].eq(subcategory_choice)]
        los = los_source.groupby(["line_of_service", "normalized_status"], dropna=False).size().reset_index(name="Count")
        st.plotly_chart(px.bar(los, y="line_of_service", x="Count", color="normalized_status", title="Line of Service", orientation="h"), use_container_width=True)
        summary = category_df.groupby(["category", "subcategory", "line_of_service", "work_type_group"], dropna=False).agg(
            total=("work_order_id", "count"),
            completed=("normalized_status", lambda s: int((s == "Completed").sum())),
            canceled=("normalized_status", lambda s: int((s == "Canceled").sum())),
            open=("normalized_status", lambda s: int((s == "Open").sum())),
            in_progress=("normalized_status", lambda s: int((s == "In Progress").sum())),
            technician_count=("pm_technician", "nunique"),
        ).reset_index().sort_values("total", ascending=False)
        display_df(summary, max_rows=100)
        display_df(los_source, DETAIL_COLUMNS, max_rows=100)
        category_export, category_missing = safe_detail_df(category_records)
        if not category_missing:
            download_df(category_export, "Export Category Detail", "pm_work_order_category_detail.xlsx", "pm_wo_category_export")

with duration_tab:
    section_header("Duration Review", "Flags are review indicators, not automatic proof of poor work.", "orange")
    dur_source = filtered_snapshot_global.copy()
    dur_counts = dur_source.groupby("duration_status", dropna=False).size().reset_index(name="Count")
    d1, d2, d3, d4, d5 = st.columns(5)
    d1.metric("Below Expected", int((dur_source["duration_status"] == "Below Expected Range").sum()))
    d2.metric("Within Expected", int((dur_source["duration_status"] == "Within Expected Range").sum()))
    d3.metric("Above Expected", int((dur_source["duration_status"] == "Above Expected Range").sum()))
    d4.metric("Missing Duration", int((dur_source["duration_status"] == "Missing Duration").sum()))
    d5.metric("No Rule", int((dur_source["duration_status"] == "No Rule Configured").sum()))
    st.plotly_chart(px.bar(dur_counts, y="duration_status", x="Count", title="Duration Status", orientation="h"), use_container_width=True)
    flagged = dur_source[dur_source["duration_status"].isin(["Below Expected Range", "Above Expected Range"])]
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
            "duration_display",
            "expected_min_minutes",
            "expected_max_minutes",
            "duration_status",
            "short_description",
        ],
        max_rows=150,
    )
    duration_export, duration_missing = safe_detail_df(flagged)
    if not duration_missing:
        download_df(duration_export, "Export Duration Exceptions", "pm_work_order_duration_exceptions.xlsx", "pm_wo_duration_export")

with cancel_tab:
    section_header("Cancellations", "Review canceled work orders and stores with repeated cancellations.", "red")
    canceled = filtered_snapshot_global[filtered_snapshot_global["normalized_status"].eq("Canceled")].copy()
    with st.form("pm_wo_cancel_filters"):
        c1, c2, c3, c4 = st.columns(4)
        cancel_store = c1.text_input("Store Number")
        cancel_tech = c2.text_input("Technician")
        cancel_wo = c3.text_input("Work Order")
        cancel_text = c4.text_input("Search notes / description")
        threshold = st.number_input("Repeat cancellation threshold", min_value=1, max_value=20, value=3, step=1)
        apply_cancel_filters = st.form_submit_button("Apply Cancellation Filters")
    if apply_cancel_filters:
        if cancel_store:
            set_filter(store_number=cancel_store)
        if cancel_tech:
            set_filter(pm_technician=cancel_tech)
        if cancel_wo:
            set_filter(work_order_id=cancel_wo)
        if cancel_text:
            set_filter(short_description=cancel_text)
        st.rerun()
    repeat_stores = canceled.groupby("store_number", dropna=False).size().reset_index(name="Canceled Count").sort_values("Canceled Count", ascending=False)
    repeat_stores = repeat_stores[repeat_stores["Canceled Count"] >= threshold]
    c1, c2 = st.columns(2)
    c1.metric("Canceled Work Orders", len(canceled))
    c2.metric("Repeat Cancellation Stores", len(repeat_stores))
    if not repeat_stores.empty:
        st.plotly_chart(px.bar(repeat_stores.head(30), y="store_number", x="Canceled Count", title="Stores with Repeat Cancellations", orientation="h"), use_container_width=True)
    cancel_summary = canceled.groupby("store_number", dropna=False).agg(
        canceled_count=("work_order_id", "count"),
        technicians_involved=("pm_technician", lambda s: ", ".join(sorted(set(v for v in s if v))[:5])),
        oldest_created=("created_at", "min"),
        newest_created=("created_at", "max"),
        work_orders=("work_order_id", lambda s: ", ".join(s.astype(str).head(10))),
    ).reset_index().sort_values("canceled_count", ascending=False)
    display_df(cancel_summary, max_rows=100)
    display_df(canceled, DETAIL_COLUMNS, max_rows=150)
    canceled_export, canceled_missing = safe_detail_df(canceled)
    if not canceled_missing:
        download_df(canceled_export, "Export Canceled Work Orders", "pm_work_order_canceled.xlsx", "pm_wo_cancel_export")
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
    display_df(filtered.iloc[start:end], DETAIL_COLUMNS, max_rows=page_size)
    selected_wo = st.selectbox("Open detail for work order", [""] + filtered["work_order_id"].astype(str).head(1000).tolist())
    if selected_wo:
        record = filtered[filtered["work_order_id"].astype(str).eq(selected_wo)].head(1).T.reset_index()
        record.columns = ["Field", "Value"]
        st.dataframe(record, use_container_width=True, hide_index=True)
    filtered_export, filtered_missing = safe_detail_df(filtered)
    if not filtered_missing:
        download_df(filtered_export, "Export Filtered Detail", "pm_work_order_filtered_detail.xlsx", "pm_wo_filtered_export")

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
