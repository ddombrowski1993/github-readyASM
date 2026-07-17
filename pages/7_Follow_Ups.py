from datetime import date, timedelta
import re

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Follow-Ups", layout="wide")

from src.database import active_employees, log_action, safe_query, session_scope, stores_for_select
from src.exports import download_table
from src.manager_rollup import manager_rollup_query
from src.maps import render_plain_table
from src.models import Followup, FollowupOption, UploadedFile
from src.pdf_reports import build_pdf_report, pdf_bytes
from src.utils import apply_theme, effective_rollup_user_id, ensure_database_or_stop, is_all_managed_view, page_header, save_upload, sidebar_nav


apply_theme()
sidebar_nav()

DEFAULT_FOLLOWUP_TYPES = [
    "Store / National Account",
    "Personnel / Employee",
    "Leadership / Boss",
    "HR / Recruiting",
    "External Contact",
    "Other / Custom",
]

DEFAULT_FOLLOWUP_CATEGORIES = [
    "Vendor",
    "Store issue",
    "Maintenance",
    "Landscaping",
    "Pest",
    "HR",
    "Personal",
    "Operations",
    "PMT",
    "Brand Enhancement",
    "Calibration",
    "Invoice",
    "Work Order",
    "Other",
]

FOLLOWUP_OPTION_LABELS = {
    "followup_type": "Follow-up Type",
    "category": "Category",
    "vendor": "Vendor / Person / Company",
}


def step_header(number, title, text):
    st.markdown(
        f"""
        <div style="border-left:7px solid #2563eb;background:#f8fbff;border-radius:8px;
        padding:0.55rem 0.75rem;margin-bottom:0.65rem;">
        <div style="font-weight:850;color:#0f172a;">STEP {number} - {title}</div>
        <div style="color:#475569;font-size:0.95rem;">{text}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def option_key(value):
    return key(value)


def default_followup_options(option_type):
    if option_type == "followup_type":
        return DEFAULT_FOLLOWUP_TYPES
    if option_type == "category":
        return DEFAULT_FOLLOWUP_CATEGORIES
    return []


def followup_existing_values(column_name):
    if column_name not in {"followup_type", "category", "vendor"}:
        return []
    df = safe_query(
        f"""
        select distinct {column_name} as value
        from followups
        where coalesce({column_name}, '') <> ''
        order by {column_name}
        """
    )
    if df.empty:
        return []
    return [clean_text(value) for value in df["value"].tolist() if clean_text(value)]


def followup_custom_options(option_type, active_only=True):
    expected_columns = ["id", "option_type", "option_value", "active", "notes"]
    status_filter = "and active is true" if active_only else ""
    df = safe_query(
        f"""
        select id, option_type, option_value, active, notes
        from followup_options
        where option_type = :option_type {status_filter}
        order by option_value
        """,
        {"option_type": option_type},
    )
    for column in expected_columns:
        if column not in df.columns:
            df[column] = pd.Series(dtype="object")
    return df[expected_columns]


def followup_dropdown_options(option_type, include_blank=False):
    options = []
    for source in [
        default_followup_options(option_type),
        followup_custom_options(option_type, active_only=True)["option_value"].tolist(),
        followup_existing_values(option_type),
    ]:
        for value in source:
            text = clean_text(value)
            if text and option_key(text) not in {option_key(item) for item in options}:
                options.append(text)
    if include_blank:
        return [""] + options
    return options


def save_followup_option(option_type, option_value, notes=""):
    value = clean_text(option_value)
    if not value:
        return False, "Enter a value before saving."
    with session_scope() as session:
        existing_options = session.query(FollowupOption).filter(FollowupOption.option_type == option_type).all()
        for existing in existing_options:
            if option_key(existing.option_value) == option_key(value):
                existing.option_value = value
                existing.active = True
                existing.notes = clean_text(notes)
                return True, f"{FOLLOWUP_OPTION_LABELS.get(option_type, option_type)} option reactivated."
        session.add(FollowupOption(option_type=option_type, option_value=value, active=True, notes=clean_text(notes)))
    return True, f"{FOLLOWUP_OPTION_LABELS.get(option_type, option_type)} option added."


def deactivate_followup_option(option_id):
    with session_scope() as session:
        option = session.get(FollowupOption, int(option_id))
        if not option:
            return False, "Option was not found."
        option.active = False
    return True, "Option deactivated."


def render_followup_dropdown_manager():
    with st.expander("Manage Follow-Up Dropdown Lists", expanded=False):
        st.caption("These options are saved in this workspace. Built-in defaults and values already used on follow-ups stay available.")
        option_type = st.selectbox(
            "Dropdown to manage",
            ["followup_type", "category", "vendor"],
            format_func=lambda value: FOLLOWUP_OPTION_LABELS.get(value, value),
            key="followup_option_type_manager",
        )
        c1, c2 = st.columns([0.65, 0.35])
        new_value = c1.text_input(f"Add {FOLLOWUP_OPTION_LABELS.get(option_type, option_type)}", key=f"new_followup_option_{option_type}")
        notes = c2.text_input("Notes optional", key=f"new_followup_option_notes_{option_type}")
        if st.button("Add Dropdown Option", type="primary", key=f"add_followup_option_{option_type}"):
            ok, message = save_followup_option(option_type, new_value, notes)
            if ok:
                log_action("followup dropdown option added", "followup_options", description=f"{option_type}: {new_value}")
                st.success(message)
                st.rerun()
            else:
                st.warning(message)
        current_options = followup_custom_options(option_type, active_only=False)
        if current_options.empty:
            st.info("No custom options have been added for this dropdown yet.")
        else:
            render_plain_table(current_options[["id", "option_value", "active", "notes"]], max_rows=200)
            active_options = current_options[current_options["active"] == True]
            if not active_options.empty:
                active_index = active_options.set_index("id")
                remove_id = st.selectbox(
                    "Deactivate custom option",
                    active_options["id"].tolist(),
                    format_func=lambda value: active_index.loc[value, "option_value"],
                    key=f"deactivate_followup_option_{option_type}",
                )
                if st.button("Deactivate Selected Option", type="secondary", key=f"deactivate_followup_option_button_{option_type}"):
                    ok, message = deactivate_followup_option(remove_id)
                    if ok:
                        log_action("followup dropdown option deactivated", "followup_options", int(remove_id))
                        st.success(message)
                        st.rerun()
                    else:
                        st.error(message)


def clean_text(value):
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value or "").strip())


def key(value):
    return re.sub(r"[^a-z0-9]+", " ", clean_text(value).lower()).strip()


def followup_base_query():
    return """
    select f.id, coalesce(f.followup_type,'Store / National Account') as type, f.priority, s.store_number,
           f.issue_title, f.category, e.full_name as owner,
           coalesce(f.related_person, f.external_contact, '') as person,
           coalesce(f.organization, f.vendor, '') as organization,
           f.vendor, f.date_opened, f.next_followup_date, f.due_date, f.completed_date, f.status
    from followups f
    left join stores s on s.id = f.store_id
    left join employees e on e.id = f.assigned_employee_id
    """


def apply_filters(df, manager=False):
    if df.empty:
        return df
    cols = st.columns(4)
    if manager and "Managed Area" in df.columns:
        owner = cols[0].selectbox("Managed User / Owner", ["All"] + sorted(df["Managed Area"].dropna().astype(str).unique().tolist()))
        if owner != "All":
            df = df[df["Managed Area"] == owner]
    status = cols[1 if manager else 0].selectbox("Status", ["All"] + sorted(df["status"].dropna().astype(str).unique().tolist()))
    priority = cols[2 if manager else 1].selectbox("Priority", ["All"] + sorted(df["priority"].dropna().astype(str).unique().tolist()))
    category = cols[3 if manager else 2].selectbox("Category", ["All"] + sorted(df["category"].fillna("").astype(str).unique().tolist()))
    if status != "All":
        df = df[df["status"] == status]
    if priority != "All":
        df = df[df["priority"] == priority]
    if category != "All":
        df = df[df["category"].fillna("").astype(str) == category]
    f1, f2, f3, f4 = st.columns(4)
    vendor = f1.selectbox("Vendor", ["All"] + sorted([v for v in df["vendor"].fillna("").astype(str).unique().tolist() if v]))
    assigned = f2.selectbox("Assigned to", ["All"] + sorted([v for v in df["owner"].fillna("").astype(str).unique().tolist() if v]))
    store_search = f3.text_input("Store number")
    overdue_only = f4.checkbox("Overdue only")
    if vendor != "All":
        df = df[df["vendor"].fillna("").astype(str) == vendor]
    if assigned != "All":
        df = df[df["owner"].fillna("").astype(str) == assigned]
    if store_search.strip():
        df = df[df["store_number"].fillna("").astype(str).str.contains(store_search.strip(), case=False, na=False)]
    if overdue_only:
        due_dates = pd.to_datetime(df["due_date"], errors="coerce").dt.date
        df = df[(~df["status"].isin(["Completed", "Cancelled"])) & (due_dates < date.today())]
    return df


page_header("Follow-Ups", "Add, review, and complete follow-up tasks from manual entries.")

if is_all_managed_view():
    with st.container(border=True):
        step_header(2, "Review Managed Follow-Ups", "Read-only manager roll-up across all managed users.")
        followups_rollup = manager_rollup_query(effective_rollup_user_id(), followup_base_query() + " order by f.due_date, f.priority desc")
        if followups_rollup.empty:
            st.warning("No managed follow-ups were found.")
        else:
            open_mask = ~followups_rollup["status"].isin(["Completed", "Cancelled"])
            due_dates = pd.to_datetime(followups_rollup["due_date"], errors="coerce").dt.date
            overdue_mask = open_mask & (due_dates < date.today())
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Managed Areas", followups_rollup["Managed Area"].nunique())
            c2.metric("Open Follow-Ups", int(open_mask.sum()))
            c3.metric("Overdue Follow-Ups", int(overdue_mask.sum()))
            c4.metric("Completed", int((followups_rollup["status"] == "Completed").sum()))
            filtered_rollup = apply_filters(followups_rollup, manager=True)
            render_plain_table(filtered_rollup, max_rows=300)
            download_table(filtered_rollup, "manager_rollup_followups")
    st.stop()

ensure_database_or_stop()
stores = stores_for_select()
employees = active_employees()

with st.container(border=True):
    step_header(1, "Add Follow-Up", "Create follow-up items manually and manage the dropdown lists used by this page.")
    render_followup_dropdown_manager()
    with st.form("followup"):
        c1, c2, c3 = st.columns(3)
        followup_type = c1.selectbox("Follow-up type", followup_dropdown_options("followup_type"))
        category = c2.selectbox("Category", followup_dropdown_options("category"))
        priority = c3.selectbox("Priority", ["Low", "Normal", "High", "Urgent"])
        c4, c5 = st.columns(2)
        store_id = c4.selectbox("Store", [None] + stores["id"].tolist() if not stores.empty else [None], format_func=lambda x: "No specific store" if x is None else f"{stores.set_index('id').loc[x, 'store_number']} - {stores.set_index('id').loc[x, 'city']}")
        vendor_options = followup_dropdown_options("vendor", include_blank=True)
        vendor = c5.selectbox(
            "Vendor / person / company",
            vendor_options,
            format_func=lambda value: "No vendor/person/company" if not value else value,
        )
        c6, c7 = st.columns(2)
        assigned = c6.selectbox("Owner / responsible employee", [None] + employees["id"].tolist() if not employees.empty else [None], format_func=lambda x: "Unassigned" if x is None else employees.set_index("id").loc[x, "full_name"])
        fm = c7.text_input("FM / Facilities Manager")
        title = st.text_input("Follow-up title")
        desc = st.text_area("Comments / details")
        c8, c9, c10 = st.columns(3)
        opened = c8.date_input("Date submitted/opened", value=date.today())
        next_follow = c9.date_input("Next follow-up", value=date.today() + timedelta(days=2))
        due = c10.date_input("Due date", value=date.today() + timedelta(days=7))
        status = st.selectbox("Status", ["Open", "Waiting on Vendor", "Waiting on Store", "Waiting on Internal Team", "Scheduled", "Completed", "Cancelled"])
        notes = st.text_area("Internal notes")
        attachment = st.file_uploader("Attach photos/PDFs/screenshots/Excel", type=["png", "jpg", "jpeg", "pdf", "xlsx", "csv"])
        submitted = st.form_submit_button("Add Follow-Up")
    if submitted and not title:
        st.warning("Add a follow-up title before saving.")
    if submitted and title:
        with session_scope() as session:
            follow = Followup(
                followup_type=followup_type, store_id=store_id, issue_title=title, issue_description=desc,
                category=category, priority=priority, assigned_employee_id=assigned, organization=fm,
                vendor=vendor, status=status, date_opened=opened, next_followup_date=next_follow,
                due_date=due, internal_notes=notes,
            )
            session.add(follow)
            session.flush()
            follow_id = follow.id
            if attachment:
                path = save_upload(attachment)
                session.add(UploadedFile(related_table="followups", related_id=follow_id, file_name=attachment.name, file_type=attachment.type, file_path_or_url=str(path)))
        log_action("followup added", "followups", follow_id, title)
        st.success("Follow-up saved.")

open_sql = followup_base_query()
with st.container(border=True):
    step_header(2, "Review Open Follow-Ups", "Active follow-ups that are not completed or cancelled.")
    open_df = safe_query(open_sql + " where f.status not in ('Completed','Cancelled') order by (f.due_date is null), f.due_date")
    filtered_open = apply_filters(open_df)
    render_plain_table(filtered_open, max_rows=300)
    download_table(filtered_open, "open_followups")

with st.container(border=True):
    step_header(3, "Overdue / Needs Attention", "Follow-ups past due or marked high importance.")
    attention_df = safe_query(
        open_sql + " where f.status not in ('Completed','Cancelled') and (coalesce(f.due_date,f.next_followup_date) < :today or f.priority in ('High','Urgent','Critical')) order by coalesce(f.due_date,f.next_followup_date)",
        {"today": date.today()},
    )
    render_plain_table(attention_df, max_rows=300)
    download_table(attention_df, "followups_needing_attention")

with st.container(border=True):
    step_header(4, "Completed Follow-Ups", "Completed items no longer count as open.")
    completed_df = safe_query(open_sql + " where f.status = 'Completed' order by (f.completed_date is null), f.completed_date desc")
    render_plain_table(completed_df, max_rows=300)
    download_table(completed_df, "completed_followups")

with st.container(border=True):
    step_header(5, "Attachment History / Reports", "Attachments and generated report exports for follow-up tracking.")
    files = safe_query("select * from uploaded_files where related_table = 'followups' order by uploaded_at desc")
    render_plain_table(files, max_rows=200)
    report = safe_query(open_sql + " order by f.status, (f.due_date is null), f.due_date")
    download_table(report, "followups_report")
    if st.button("Generate Follow-Up PDF"):
        path = build_pdf_report("Follow-Up Report", report, "followups_report.pdf")
        st.download_button("Download PDF", data=pdf_bytes(path), file_name="followups_report.pdf")
