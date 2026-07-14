import re

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Stores", layout="wide")

from src.database import log_action, safe_query, session_scope, stores_for_select
from src.exports import download_table, excel_bytes
from src.imports import clean_store_number, import_stores, sample_store_template
from src.manager_rollup import manager_rollup_query
from src.models import Store
from src.smart_import import (
    REQUIRED_FIELDS,
    display_field,
    load_saved_mappings,
    mapped_dataframe,
    mapping_summary,
    preview_summary,
    review_table,
    save_mapping_pattern,
    scan_issue_rows,
    scan_workbook,
)
from src.utils import apply_theme, df_search, ensure_database_or_stop, metric_help_card, page_header, sidebar_nav


LOCAL_STORE_CSV = "data/stores.csv"

EXACT_ASSIGNMENT_HEADERS = {
    "assigned_pmt": ["pmt", "assigned pmt", "pmt technician", "assigned pmt technician"],
    "assigned_brand": ["assigned brand", "brand enhancement", "brand team", "brand technician"],
    "assigned_calibration": ["calibration", "assigned calibration", "calibration technician"],
}


def normalized_upload_header(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def find_exact_upload_column(columns, candidates):
    lookup = {normalized_upload_header(column): column for column in columns}
    for candidate in candidates:
        column = lookup.get(normalized_upload_header(candidate))
        if column:
            return column
    return ""


def force_exact_assignment_columns(mapped, incoming):
    corrected = mapped.copy()
    forced = {}
    for field, headers in EXACT_ASSIGNMENT_HEADERS.items():
        exact_column = find_exact_upload_column(incoming.columns, headers)
        if not exact_column:
            continue
        raw_values = incoming[exact_column].fillna("").astype(str).str.strip()
        raw_unique = raw_values[raw_values.ne("")].nunique()
        mapped_unique = 0
        if field in corrected.columns:
            mapped_values = corrected[field].fillna("").astype(str).str.strip()
            mapped_unique = mapped_values[mapped_values.ne("")].nunique()
        if raw_unique or field not in corrected.columns or mapped_unique <= 1:
            corrected[field] = raw_values
            forced[field] = {"column": exact_column, "unique": int(raw_unique)}
    return corrected, forced


def render_store_import_summary(summary):
    errors = summary.get("errors") or []
    review = summary.get("review") or []
    created = int(summary.get("created", 0) or 0)
    updated = int(summary.get("updated", 0) or 0)
    skipped = int(summary.get("skipped", 0) or 0)
    duplicates = int(summary.get("duplicates", 0) or 0)
    if errors:
        st.warning("Import finished with row errors. Review the list below.")
    elif review:
        st.info("Import finished with review notes. No rows crashed the import.")
    else:
        st.success("Import completed. No errors found.")
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Created", created)
    s2.metric("Updated", updated)
    with s3:
        metric_help_card("Skipped", skipped, "Store rows skipped during import because they were blank, invalid, duplicate, or could not be safely matched.")
    with s4:
        metric_help_card("Duplicates", duplicates, "Duplicate store numbers found during import. The app merges/updates by store number instead of creating duplicate stores.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Coordinates From Upload", summary.get("coordinates_from_upload", 0))
    c2.metric("Coordinates Geocoded", summary.get("coordinates_geocoded", 0))
    with c3:
        metric_help_card("Coordinates Still Missing", summary.get("coordinates_still_missing", 0), "Stores still missing latitude/longitude after upload/geocoding. These will not plot or route until fixed.")
    a1, a2 = st.columns(2)
    a1.metric("Addresses From Coordinates", summary.get("addresses_from_coordinates", 0))
    with a2:
        metric_help_card("Addresses Still Missing", summary.get("addresses_still_missing", 0), "Stores still missing usable address data after import. Upload a corrected file or fill the store details.")
    pmt_assignments = summary.get("pmt_assignments") or {}
    if pmt_assignments:
        st.subheader("PMT Assignments Imported")
        pmt_rows = [{"PMT": name, "Stores": count} for name, count in sorted(pmt_assignments.items(), key=lambda item: (-item[1], item[0]))]
        st.dataframe(pd.DataFrame(pmt_rows), use_container_width=True, hide_index=True)
    if errors:
        st.dataframe(pd.DataFrame({"Error": errors}), use_container_width=True, hide_index=True)
    if review:
        st.dataframe(pd.DataFrame({"Review Item": review}), use_container_width=True, hide_index=True)
    with st.expander("Import details", expanded=False):
        st.json(summary)


apply_theme()
sidebar_nav()

if st.session_state.get("account_role") == "Manager" and st.session_state.get("manager_rollup_active"):
    page_header("Stores", "Manager roll-up view of stores across all managed areas.")
    st.info("Read-only All Managed Users view. Select one managed person from the sidebar Viewing Workspace dropdown to edit that person's stores.")
    stores_rollup = manager_rollup_query(
        st.session_state.get("user_id"),
        """
        select s.store_number, s.store_name, s.address, s.city, s.state, s.zip, s.latitude, s.longitude,
               p.full_name as assigned_pmt, b.full_name as assigned_brand, c.full_name as assigned_calibration,
               pt.team_name as pmt_team, bt.team_name as brand_team, ct.team_name as calibration_team,
               s.store_status, s.priority, s.notes
        from stores s
        left join employees p on p.id = s.assigned_pmt_employee_id
        left join employees b on b.id = s.assigned_brand_employee_id
        left join employees c on c.id = s.assigned_calibration_employee_id
        left join teams pt on pt.id = s.assigned_pmt_team_id
        left join teams bt on bt.id = s.assigned_brand_team_id
        left join teams ct on ct.id = s.assigned_calibration_team_id
        where s.active = 1
        order by s.store_number
        """,
    )
    if stores_rollup.empty:
        st.warning("No managed stores were found. Claim or assign users under this manager first.")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Managed Areas", stores_rollup["Managed Area"].nunique())
        c2.metric("Active Stores", len(stores_rollup))
        with c3:
            metric_help_card("Missing Coordinates", int(((stores_rollup["latitude"].isna()) | (stores_rollup["longitude"].isna())).sum()), "Managed active stores missing latitude/longitude. These cannot plot or route correctly.")
        f1, f2, f3, f4 = st.columns(4)
        owner_filter = f1.selectbox("Owner / Managed User", ["All"] + sorted(stores_rollup["Managed Area"].dropna().unique().tolist()))
        city_filter = f2.selectbox("City", ["All"] + sorted([value for value in stores_rollup["city"].fillna("").astype(str).unique().tolist() if value.strip()]))
        state_filter = f3.selectbox("State", ["All"] + sorted([value for value in stores_rollup["state"].fillna("").astype(str).unique().tolist() if value.strip()]))
        setup_filter = f4.selectbox("Setup", ["All", "Missing Coordinates", "No Assignment", "Assigned"])
        filtered_rollup = stores_rollup.copy()
        if owner_filter != "All":
            filtered_rollup = filtered_rollup[filtered_rollup["Managed Area"] == owner_filter]
        if city_filter != "All":
            filtered_rollup = filtered_rollup[filtered_rollup["city"].fillna("").astype(str) == city_filter]
        if state_filter != "All":
            filtered_rollup = filtered_rollup[filtered_rollup["state"].fillna("").astype(str) == state_filter]
        if setup_filter == "Missing Coordinates":
            filtered_rollup = filtered_rollup[filtered_rollup["latitude"].isna() | filtered_rollup["longitude"].isna()]
        elif setup_filter == "No Assignment":
            assignment_cols = ["assigned_pmt", "assigned_brand", "assigned_calibration", "pmt_team", "brand_team", "calibration_team"]
            filtered_rollup = filtered_rollup[filtered_rollup[assignment_cols].fillna("").astype(str).apply(lambda row: not any(value.strip() for value in row), axis=1)]
        elif setup_filter == "Assigned":
            assignment_cols = ["assigned_pmt", "assigned_brand", "assigned_calibration", "pmt_team", "brand_team", "calibration_team"]
            filtered_rollup = filtered_rollup[filtered_rollup[assignment_cols].fillna("").astype(str).apply(lambda row: any(value.strip() for value in row), axis=1)]
        search_store = st.text_input("Search store number")
        if search_store.strip():
            filtered_rollup = filtered_rollup[filtered_rollup["store_number"].astype(str).str.contains(search_store.strip(), case=False, na=False)]
        filtered_rollup = df_search(filtered_rollup)
        st.dataframe(filtered_rollup, use_container_width=True, hide_index=True)
        download_table(filtered_rollup, "manager_rollup_stores")
    st.stop()

ensure_database_or_stop()
page_header("Stores", "Master store database, uploads, store details, location data, and city management.")

store_sections = ["Store List", "Upload Stores", "Store Details", "City Manager"]
try:
    requested_section = st.query_params.get("stores_section")
    if isinstance(requested_section, list):
        requested_section = requested_section[0] if requested_section else None
    if requested_section in store_sections:
        st.session_state["stores_section"] = requested_section
except Exception:
    pass
if st.session_state.get("stores_section") not in store_sections:
    st.session_state["stores_section"] = "Store List"
selected_section = st.radio(
    "Stores section",
    store_sections,
    horizontal=True,
    key="stores_section",
    label_visibility="collapsed",
)


def active_store_rows():
    return safe_query(
        """
        select s.id, s.store_number, s.store_name, s.address, s.city, s.state, s.zip, s.latitude, s.longitude,
               p.full_name as assigned_pmt, b.full_name as assigned_brand, c.full_name as assigned_calibration,
               pt.team_name as pmt_team, bt.team_name as brand_team, ct.team_name as calibration_team,
               s.store_status, s.priority, s.notes
        from stores s
        left join employees p on p.id = s.assigned_pmt_employee_id
        left join employees b on b.id = s.assigned_brand_employee_id
        left join employees c on c.id = s.assigned_calibration_employee_id
        left join teams pt on pt.id = s.assigned_pmt_team_id
        left join teams bt on bt.id = s.assigned_brand_team_id
        left join teams ct on ct.id = s.assigned_calibration_team_id
        where s.active = true
        order by s.store_number
        """
    )


def active_store_city_summary():
    return safe_query(
        """
        select trim(city) as city, count(*) as store_count
        from stores
        where active = true and nullif(trim(coalesce(city,'')), '') is not null
        group by trim(city)
        order by trim(city)
        """
    )


def missing_city_total():
    result = safe_query(
        "select count(*) as missing_city_count from stores where active = true and nullif(trim(coalesce(city,'')), '') is null"
    )
    return int(result.iloc[0]["missing_city_count"]) if not result.empty else 0

if selected_section == "Store List":
    stores = active_store_rows()
    city_summary = active_store_city_summary()
    missing_city_count = missing_city_total()
    if stores.empty:
        st.info("No stores found. Upload stores first, or select a different workspace from the sidebar.")
    c1, c2 = st.columns(2)
    city_options = ["All"]
    if missing_city_count:
        city_options.append("Missing City")
    if not city_summary.empty:
        city_options.extend(city_summary["city"].tolist())
    city = c1.selectbox("City", city_options, key="store_list_city_filter")
    status = c2.selectbox("Status", ["All"] + sorted(stores["store_status"].dropna().unique().tolist()) if not stores.empty else ["All"])
    filtered = stores.copy()
    if city == "Missing City":
        filtered = filtered[filtered["city"].fillna("").astype(str).str.strip() == ""]
    elif city != "All":
        filtered = filtered[filtered["city"].fillna("").astype(str).str.strip() == city]
    for field, value in [("store_status", status)]:
        if value != "All":
            filtered = filtered[filtered[field] == value]
    filtered = df_search(filtered)
    st.dataframe(filtered, use_container_width=True, hide_index=True)
    download_table(filtered, "store_list")

if selected_section == "Upload Stores":
    st.info("Required column: store_number. Address, city, state, zip, latitude, and longitude are imported when present.")
    with st.expander("Recommended store upload layout", expanded=True):
        st.warning(
            "For the smoothest import, include one header row with these columns: "
            "Store Number, Address, City, State, ZIP, Latitude, Longitude. "
            "Store Number must be the actual 4-6 digit store/site number, not latitude, ZIP, WO number, or row number."
        )
        st.markdown(
            """
Good column names the app understands:

- Store Number: `Store Number`, `Store #`, `Site Number`, `Site #`, `Location ID`
- Latitude / Longitude: `Latitude`, `Lat`, `Longitude`, `Lon`, `Lng`
- Address: `Address`, `Street Address`, `Store Address`, `Location Address`
- City / State / ZIP: `City`, `State`, `ST`, `ZIP`, `Zip Code`

Avoid merged title rows, blank header rows, pivot tables, hidden-only sheets, and putting `Lat` or `Lon` in the store number field.
            """
        )
    st.download_button("Download store template", data=excel_bytes(sample_store_template()), file_name="store_template.xlsx")
    if st.session_state.get("store_import_summary"):
        st.subheader("Last Store Import")
        render_store_import_summary(st.session_state["store_import_summary"])
    if st.button("Import bundled data/stores.csv"):
        bundled = pd.read_csv(LOCAL_STORE_CSV, dtype=str).fillna("")
        st.session_state["store_import_summary"] = import_stores(bundled)
        st.rerun()
    upload = st.file_uploader("Upload stores Excel/CSV", type=["xlsx", "xls", "xlsm", "csv"])
    if upload:
        try:
            scans = scan_workbook(upload, "stores")
        except Exception as exc:
            st.error("The app could not read this upload. Check that the file is a normal Excel/CSV file and try again.")
            if st.session_state.get("account_role") == "Admin":
                with st.expander("Admin debug details", expanded=False):
                    st.code(str(exc))
            st.stop()
        scan_issues = scan_issue_rows(scans)
        if not scan_issues.empty:
            with st.expander("Upload scan warnings", expanded=False):
                st.dataframe(scan_issues, use_container_width=True, hide_index=True)
                if st.session_state.get("account_role") == "Admin":
                    technical = [item.get("technical_detail") for item in scans if item.get("technical_detail")]
                    if technical:
                        st.caption("Admin debug details")
                        st.code("\n\n".join(technical))
        if not scans or all(item["df"].empty for item in scans):
            st.error("No usable rows were found in this upload. Check that the workbook has a visible sheet with store data.")
            st.stop()
        best = scans[0]
        sheet_names = [item["sheet"] for item in scans]
        selected_sheet = st.selectbox("Detected store sheet", sheet_names, index=0)
        best = next(item for item in scans if item["sheet"] == selected_sheet)
        incoming = best["df"]
        auto_mapping = {field: match.column for field, match in best["mapping"].items()}
        for field, headers in EXACT_ASSIGNMENT_HEADERS.items():
            exact_column = find_exact_upload_column(incoming.columns, headers)
            if exact_column:
                auto_mapping[field] = exact_column
        saved_patterns = load_saved_mappings().get("stores", {})
        saved_pattern_applied = False
        if saved_patterns:
            pattern_choice = st.selectbox("Saved mapping pattern", ["Auto-detect"] + sorted(saved_patterns))
            if pattern_choice != "Auto-detect":
                saved_pattern_applied = True
                auto_mapping.update({field: column for field, column in saved_patterns[pattern_choice].items() if column in incoming.columns})
        assignment_fields = ["assigned_pmt", "assigned_brand", "assigned_calibration"]
        for field in assignment_fields:
            match = best["mapping"].get(field)
            if field in auto_mapping and not saved_pattern_applied and match and match.reason == "data pattern":
                auto_mapping.pop(field, None)
        low_confidence = [
            field for field, match in best["mapping"].items()
            if field in REQUIRED_FIELDS["stores"] and match.confidence < 75
        ]
        missing_required = [field for field in REQUIRED_FIELDS["stores"] if field not in auto_mapping]
        assignment_mapping_missing = any(field not in auto_mapping for field in assignment_fields)
        needs_mapping = bool(missing_required or low_confidence or best["ambiguous"] or assignment_mapping_missing)
        st.caption(
            f"Header row detected: {best['header_row'] + 1}. "
            f"Rows detected: {best['rows']:,}. Columns detected: {best['columns']:,}."
        )
        st.dataframe(mapping_summary(best["mapping"], REQUIRED_FIELDS["stores"]), use_container_width=True, hide_index=True)
        active_store_count_df = safe_query("select count(*) as active_store_count from stores where active = true")
        active_store_count = int(active_store_count_df.iloc[0]["active_store_count"]) if not active_store_count_df.empty else 0
        mapping_options = [""] + incoming.columns.tolist()
        selected_mapping = auto_mapping.copy()
        with st.expander("Advanced Mapping", expanded=needs_mapping):
            if needs_mapping:
                st.warning("Review the fields below before importing. The app could not confidently map every required field.")
            st.caption("Store Number should be the store/site ID column only. Assignment fields should be mapped only when your store file really has PMT/Brand/Calibration assignment columns.")
            fields = [
                "store_number",
                "latitude",
                "longitude",
                "address",
                "city",
                "state",
                "zip",
                "assigned_pmt",
                "assigned_brand",
                "assigned_calibration",
                "market",
                "zone",
                "area",
                "active",
            ]
            for start in range(0, len(fields), 3):
                cols = st.columns(3)
                for col, field in zip(cols, fields[start:start + 3]):
                    default = selected_mapping.get(field, "")
                    selected_mapping[field] = col.selectbox(
                        display_field(field),
                        mapping_options,
                        index=mapping_options.index(default) if default in mapping_options else 0,
                        key=f"store_smart_map_{field}",
                    )
        try:
            mapped = mapped_dataframe(incoming, selected_mapping)
            mapped, forced_assignment_columns = force_exact_assignment_columns(mapped, incoming)
            for field, details in forced_assignment_columns.items():
                selected_mapping[field] = details["column"]
            review = review_table(mapped, "stores")
        except Exception as exc:
            st.error("The app could not build an import preview for this file. Use Advanced Mapping to pick the Store Number, Latitude, Longitude, and address columns.")
            if st.session_state.get("account_role") == "Admin":
                with st.expander("Admin debug details", expanded=False):
                    st.code(str(exc))
            st.stop()
        if "store_number" not in mapped.columns:
            st.error("The app could not identify a store number column. Open Advanced Mapping and select the Store Number / Site Number column.")
            st.stop()
        summary = preview_summary(mapped, review)
        incoming_store_numbers = set(mapped["store_number"].apply(clean_store_number)) if "store_number" in mapped.columns else set()
        incoming_store_numbers.discard("")
        upload_count_cols = st.columns(4)
        upload_count_cols[0].metric("Rows in Upload", f"{summary['rows']:,}")
        upload_count_cols[1].metric("Ready to Import", f"{summary['ready']:,}")
        upload_count_cols[2].metric("Active Stores Now", active_store_count)
        with upload_count_cols[3]:
            metric_help_card("Needs Review", f"{summary['needs_review']:,}", "Rows with warnings or mapping/data issues that should be checked before import.")
        st.subheader("Import Preview")
        preview_cols = [col for col in ["store_number", "address", "city", "state", "zip", "latitude", "longitude", "assigned_pmt", "assigned_brand", "assigned_calibration"] if col in mapped.columns]
        st.dataframe(mapped[preview_cols].head(50) if preview_cols else mapped.head(50), use_container_width=True, hide_index=True)
        if "assigned_pmt" in mapped.columns:
            pmt_values = mapped["assigned_pmt"].fillna("").astype(str).str.strip()
            pmt_values = pmt_values[pmt_values.ne("")]
            unique_pmts = sorted(pmt_values.unique().tolist())
            st.caption(f"Assigned PMT values detected: {len(unique_pmts)} unique PMT(s).")
            if unique_pmts:
                st.write(", ".join(unique_pmts[:20]) + (" ..." if len(unique_pmts) > 20 else ""))
            if len(mapped) > 10 and len(unique_pmts) == 1:
                st.warning(
                    f"This upload maps every store with a PMT value to `{unique_pmts[0]}`. "
                    "If that is not correct, open Advanced Mapping and choose the correct Assigned PMT column or clear it before importing."
                )
        if forced_assignment_columns:
            forced_labels = [
                f"{display_field(field)} from `{details['column']}` ({details['unique']} unique value{'s' if details['unique'] != 1 else ''})"
                for field, details in forced_assignment_columns.items()
            ]
            st.info("Assignment columns locked from exact upload headers: " + "; ".join(forced_labels))
        if not review.empty:
            st.subheader("Rows Needing Review")
            st.dataframe(review, use_container_width=True, hide_index=True)
            download_table(review, "store_import_review")
        replace_active = st.checkbox(
            "Replace active store list with this upload",
            value=True,
            help="Use this for your master store file. Stores not in this upload will be deactivated so old stores stop counting.",
        )
        if replace_active:
            st.warning("This will deactivate active stores that are not in this upload. Existing stores that are still in the upload will keep their map/team assignments unless your file has assignment columns.")
        update_mode_label = st.selectbox("Update Mode", ["Fill missing fields only", "Update existing fields with uploaded values"])
        update_mode = "overwrite" if update_mode_label.startswith("Update") else "fill_missing"
        geocode_missing = st.checkbox(
            "Fill missing coordinates/address during import",
            value=False,
            help="Leave this off for fast imports. Turn it on only when your file is missing latitude/longitude or address fields; it may be slow on Streamlit Cloud.",
        )
        if geocode_missing:
            st.warning("This can be slow because each missing location may require an external address lookup.")
        else:
            st.info("Fast import mode is on. Uploaded latitude/longitude and address fields will be saved, but missing location data will not be looked up during import.")
        create_missing_assignment_employees = st.checkbox(
            "Create missing PMT/assignment employees from this store file",
            value=bool(selected_mapping.get("assigned_pmt") or selected_mapping.get("assigned_calibration") or selected_mapping.get("assigned_brand")),
            help="Use this after wiping/retesting or when the employee list is not loaded yet. Existing employees are matched first.",
        )
        assignment_employee_creation_required = any(
            column in mapped.columns and mapped[column].fillna("").astype(str).str.strip().ne("").any()
            for column in ["assigned_pmt", "assigned_brand", "assigned_calibration"]
        )
        if assignment_employee_creation_required and not create_missing_assignment_employees:
            st.warning("This file has assignment names. Leave employee creation enabled unless you already imported every matching employee name.")
        save_pattern = st.checkbox("Save this mapping pattern for future uploads", value=False)
        pattern_name = st.text_input("Mapping pattern name", value=f"{upload.name} store format", disabled=not save_pattern)
        if st.button("Import Stores", disabled="store_number" not in selected_mapping or not selected_mapping.get("store_number")):
            try:
                import_payload, _ = force_exact_assignment_columns(mapped, incoming)
                st.session_state["store_import_summary"] = import_stores(
                    import_payload,
                    replace_active=replace_active,
                    update_mode=update_mode,
                    geocode_missing=geocode_missing,
                    create_missing_assignment_employees=create_missing_assignment_employees or assignment_employee_creation_required,
                )
                if save_pattern:
                    save_mapping_pattern("stores", pattern_name, selected_mapping)
            except Exception as exc:
                st.session_state["store_import_summary"] = {
                    "created": 0,
                    "updated": 0,
                    "deactivated": 0,
                    "skipped": len(mapped),
                    "errors": [f"Import failed before saving all rows: {exc}"],
                }
                st.error("Import failed safely. No app-wide crash occurred; review the error below and adjust the mapping or file.")
                if st.session_state.get("account_role") == "Admin":
                    with st.expander("Admin debug details", expanded=False):
                        st.code(str(exc))
                st.stop()
            st.rerun()

if selected_section == "Store Details":
    options = stores_for_select()
    if options.empty:
        st.info("No store records are available yet. Upload stores before opening store details.")
    selected = st.selectbox("Store", options["id"].tolist() if not options.empty else [], format_func=lambda x: f"{options.set_index('id').loc[x, 'store_number']} - {options.set_index('id').loc[x, 'city']}" if not options.empty else "")
    if selected:
        detail = safe_query(
            """
            select s.id, s.store_number, s.store_name, s.address, s.city, s.state, s.zip, s.latitude, s.longitude,
                   p.full_name as assigned_pmt, b.full_name as assigned_brand, c.full_name as assigned_calibration,
                   pt.team_name as pmt_team, bt.team_name as brand_team, ct.team_name as calibration_team,
                   s.store_status, s.priority, s.notes
            from stores s
            left join employees p on p.id = s.assigned_pmt_employee_id
            left join employees b on b.id = s.assigned_brand_employee_id
            left join employees c on c.id = s.assigned_calibration_employee_id
            left join teams pt on pt.id = s.assigned_pmt_team_id
            left join teams bt on bt.id = s.assigned_brand_team_id
            left join teams ct on ct.id = s.assigned_calibration_team_id
            where s.id = :id
            """,
            {"id": int(selected)},
        )
        st.dataframe(detail, use_container_width=True, hide_index=True)
        st.subheader("Open Follow-Ups")
        st.dataframe(safe_query("select * from followups where store_id = :id and status not in ('Completed','Cancelled')", {"id": int(selected)}), use_container_width=True, hide_index=True)
        st.subheader("Deferred Work Orders")
        st.dataframe(safe_query("select * from deferred_work_orders where store_id = :id", {"id": int(selected)}), use_container_width=True, hide_index=True)
        st.subheader("Schedule History")
        st.dataframe(safe_query("select * from schedule_items where store_id = :id order by schedule_date desc", {"id": int(selected)}), use_container_width=True, hide_index=True)
        st.subheader("Uploaded Files")
        st.dataframe(safe_query("select * from uploaded_files where related_table = 'stores' and related_id = :id", {"id": int(selected)}), use_container_width=True, hide_index=True)

if selected_section == "City Manager":
    city_summary = active_store_city_summary()
    missing_city_count = missing_city_total()
    st.subheader("Cities in Store Database")
    store_count = int(city_summary["store_count"].sum()) if not city_summary.empty else 0
    if store_count == 0 and missing_city_count == 0:
        st.info("No stores found. Upload stores with city/state values to use City Manager.")
    m1, m2, m3 = st.columns(3)
    m1.metric("Cities Listed", len(city_summary))
    m2.metric("Stores With City", store_count)
    with m3:
        metric_help_card("Stores Missing City", missing_city_count, "Active stores with blank city. This affects filtering, reports, and assignment cleanup.")
    st.dataframe(city_summary, use_container_width=True, hide_index=True, height=360)

    missing_city_stores = safe_query(
        """
        select id, store_number, store_name, address, city, state, zip
        from stores
        where active = true and nullif(trim(coalesce(city,'')), '') is null
        order by store_number
        """
    )
    st.subheader("Stores Missing City")
    st.dataframe(missing_city_stores, use_container_width=True, hide_index=True, height=360)

    if not missing_city_stores.empty:
        st.caption("Update city/state for one store at a time here, or upload a corrected store file with city columns.")
        store_lookup = missing_city_stores.set_index("id")
        missing_store_id = st.selectbox(
            "Store missing city",
            missing_city_stores["id"].tolist(),
            format_func=lambda x: f"{store_lookup.loc[x, 'store_number']} - {store_lookup.loc[x, 'address'] or 'No address'}",
            key="missing_city_store",
        )
        c1, c2 = st.columns(2)
        new_city = c1.text_input("City", key="missing_city_new_city")
        new_state = c2.text_input("State", value=str(store_lookup.loc[missing_store_id, "state"] or ""), key="missing_city_new_state")
        if st.button("Update Store City", disabled=not new_city.strip(), key="missing_city_update"):
            with session_scope() as session:
                store = session.get(Store, int(missing_store_id))
                store.city = new_city.strip()
                store.state = new_state.strip()
            log_action("store city updated", "stores", int(missing_store_id), f"{new_city.strip()}, {new_state.strip()}")
            st.success("Store city updated.")
            st.rerun()
