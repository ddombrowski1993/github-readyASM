from datetime import date, datetime, timedelta
import io
import re

import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="Reports", layout="wide")

from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from sqlalchemy import select

from src.database import active_employees, log_action, safe_query, session_scope, teams
from src.geo_coverage import geographic_coverage_summary
from src.imports import normalize_columns
from src.manager_rollup import manager_rollup_dataframe, manager_rollup_query, manager_rollup_totals
from src.models import PMCompletionReportRow
from src.pdf_reports import fit_pdf_dataframe, fit_pdf_table
from src.utils import apply_theme, ensure_database_or_stop, is_all_managed_view, page_header, sidebar_nav


apply_theme()
sidebar_nav()
ensure_database_or_stop()

REPORT_ORDER = [
    "Executive Summary",
    "Exception / Problem Report",
    "Individual Performance",
    "Team Performance",
    "Schedule Completion",
    "PM Completion",
    "Follow-Up Report",
    "Deferred Work Order Report",
    "Workload / Store Assignment",
    "Geographic Coverage Report",
    "Weather Impact",
    "Data Quality",
    "Manager Roll-Up",
]
REPORT_PURPOSES = {
    "Executive Summary": "Use this report to prepare a high-level leadership update.",
    "Exception / Problem Report": "Use this report to find what needs attention now.",
    "Individual Performance": "Use this report to review one employee's workload, completion, open work, and issues.",
    "Team Performance": "Use this report to compare workload and completion across teams or employees.",
    "Schedule Completion": "Use this report to analyze scheduled work performance instead of just viewing a schedule.",
    "PM Completion": "Use this report to analyze uploaded PM completion data, aging, categories, and technician pace.",
    "Follow-Up Report": "Use this report to identify overdue follow-ups and high-priority tasks.",
    "Deferred Work Order Report": "Use this report to analyze deferred WOs by status, priority, owner, due date, and aging.",
    "Workload / Store Assignment": "Use this report to find workload balance gaps and missing assignment layers.",
    "Geographic Coverage Report": "Use this report to compare how many square miles each team or technician covers and who likely has more drive time.",
    "Weather Impact": "Use this report to review Brand Enhancement schedule risk from weather flags and rain delays.",
    "Data Quality": "Use this report to find missing or bad data before it breaks scheduling.",
    "Manager Roll-Up": "Use this report to compare managed user workspaces and identify areas that need support.",
}
REPORT_GUIDE = {
    "Executive Summary": {
        "answers": "What is the overall health of my operation right now?",
        "use": "Best for a quick leadership update or weekly check-in.",
        "includes": "Totals, open issues, completed work, overdue items, and recommended follow-ups.",
    },
    "Exception / Problem Report": {
        "answers": "What needs attention first?",
        "use": "Use when you want a punch list of blockers, overdue work, missing data, and problem records.",
        "includes": "Missing coordinates, unassigned stores, overdue follow-ups, deferred WOs, and schedule exceptions.",
    },
    "Individual Performance": {
        "answers": "How is one person doing?",
        "use": "Use for a one-on-one review or to understand an employee's workload and completion.",
        "includes": "Assigned work, completed work, open follow-ups, schedule items, and exceptions for one employee.",
    },
    "Team Performance": {
        "answers": "Which teams are overloaded or falling behind?",
        "use": "Use when comparing areas, crews, or technicians against each other.",
        "includes": "Workload by team, schedule status, completion counts, deferred work, and team-level detail.",
    },
    "Schedule Completion": {
        "answers": "Did scheduled work actually get done?",
        "use": "Use after a week or cycle to review completed, missed, delayed, or rescheduled stops.",
        "includes": "Completion rate, not completed work, schedule detail, and status breakdowns.",
    },
    "PM Completion": {
        "answers": "What does the uploaded PM completion report say?",
        "use": "Use after uploading the weekly PM report from the company file.",
        "includes": "PM rows matched to your team, aging, categories, technician counts, and open/completed status.",
    },
    "Follow-Up Report": {
        "answers": "Which follow-ups are open, overdue, or high priority?",
        "use": "Use to manage calls, vendor issues, store problems, and internal tasks.",
        "includes": "Follow-up aging, priority, status, assigned person, due dates, and detail rows.",
    },
    "Deferred Work Order Report": {
        "answers": "What deferred work is still open and who owns it?",
        "use": "Use to review deferred WOs by type, priority, due date, team, and completion status.",
        "includes": "Open/completed deferred WOs, overdue items, priority groups, team ownership, and detail rows.",
    },
    "Workload / Store Assignment": {
        "answers": "Are stores assigned correctly and evenly?",
        "use": "Use after importing stores or assignments to find gaps and workload imbalance.",
        "includes": "Assigned/unassigned stores, workload by Brand/PMT/Calibration, and missing assignment layers.",
    },
    "Geographic Coverage Report": {
        "answers": "Who has the largest territory or drive-time risk?",
        "use": "Use when balancing areas or reviewing whether assignments are geographically reasonable.",
        "includes": "Approximate coverage square miles, assigned stores, and geographic spread by team or technician.",
    },
    "Weather Impact": {
        "answers": "Which scheduled work is at risk because of weather?",
        "use": "Use before or during a Brand Enhancement schedule week.",
        "includes": "Weather risk, rain delays, impacted schedule items, and area/team weather context.",
    },
    "Data Quality": {
        "answers": "What bad or missing data could break scheduling and maps?",
        "use": "Use before scheduling, after imports, or when maps/assignments look wrong.",
        "includes": "Missing coordinates, missing assignments, invalid records, inactive/mismatched data, and cleanup items.",
    },
    "Manager Roll-Up": {
        "answers": "How are all managed workspaces doing?",
        "use": "Use as a manager/admin to compare claimed users and spot which workspace needs help.",
        "includes": "Counts by managed user, stores, schedules, follow-ups, deferred WOs, and problem summaries.",
    },
}
TEMPLATES = {
    "Leadership Weekly Summary": ("Executive Summary", "This Week"),
    "My Open Issues": ("Exception / Problem Report", "This Week"),
    "Team Performance Review": ("Team Performance", "This Month"),
    "PM Weekly Completion": ("PM Completion", "This Week"),
    "Follow-Up Aging Report": ("Follow-Up Report", "This Month"),
    "Deferred WO Aging Report": ("Deferred Work Order Report", "This Month"),
    "Data Cleanup Report": ("Data Quality", "Year to Date"),
    "Manager Roll-Up Summary": ("Manager Roll-Up", "This Month"),
    "Schedule Health Report": ("Schedule Completion", "This Week"),
    "Geographic Coverage": ("Geographic Coverage Report", "This Month"),
    "Weather Risk Report": ("Weather Impact", "This Week"),
}
WORK_GROUPS = ["All Work Groups", "Brand Enhancement", "PMT", "Calibration", "Follow-Ups", "Deferred Work Orders"]


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


def clean_text(value):
    return str(value or "").strip()


def parse_date(value):
    if value in ("", None):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def date_before_today(series, today=None):
    if series is None:
        return pd.Series(dtype=bool)
    today_ts = pd.Timestamp(today or date.today()).normalize()
    values = pd.to_datetime(series, errors="coerce").dt.normalize()
    return values.notna() & (values < today_ts)


def parse_int(value):
    try:
        if value in ("", None):
            return None
        return int(float(str(value).strip()))
    except ValueError:
        return None


def name_key(value):
    return re.sub(r"[^a-z0-9]", "", clean_text(value).lower())


def date_range_for(label):
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    if label == "Today":
        return today, today
    if label == "This Week":
        return week_start, week_start + timedelta(days=6)
    if label == "Last Week":
        start = week_start - timedelta(days=7)
        return start, start + timedelta(days=6)
    if label == "This Month":
        start = date(today.year, today.month, 1)
        next_month = date(today.year + (today.month == 12), 1 if today.month == 12 else today.month + 1, 1)
        return start, next_month - timedelta(days=1)
    if label == "Last Month":
        first_this = date(today.year, today.month, 1)
        last_prev = first_this - timedelta(days=1)
        return date(last_prev.year, last_prev.month, 1), last_prev
    if label == "This Quarter":
        quarter_month = ((today.month - 1) // 3) * 3 + 1
        start = date(today.year, quarter_month, 1)
        end_month = quarter_month + 2
        next_month = date(today.year + (end_month == 12), 1 if end_month == 12 else end_month + 1, 1)
        return start, next_month - timedelta(days=1)
    if label == "Year to Date":
        return date(today.year, 1, 1), today
    return today, today


def run_query(sql, params=None, rollup=False):
    if rollup:
        return manager_rollup_query(st.session_state.get("user_id"), sql, params=params)
    return safe_query(sql, params=params)


def csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8")


def safe_sheet_name(name):
    clean = re.sub(r"[\[\]\:\*\?\/\\]", " ", str(name))[:31].strip()
    return clean or "Report"


def formatted_excel_bytes(report):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary_rows = [{"Metric": key, "Value": value} for key, value in report["summary"].items()]
        summary_df = pd.DataFrame(summary_rows)
        summary_df.to_excel(writer, index=False, sheet_name="Summary")
        pd.DataFrame({"Key Findings": report["insights"] or ["No key findings."]}).to_excel(writer, index=False, sheet_name="Key Findings")
        pd.DataFrame({"Action Items": report["actions"] or ["No action items."]}).to_excel(writer, index=False, sheet_name="Actions")
        for name, df in report["tables"].items():
            table_df = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
            table_df.to_excel(writer, index=False, sheet_name=safe_sheet_name(name))
        workbook = writer.book
        from openpyxl.styles import Font, PatternFill
        header_font = Font(bold=True)
        header_fill = PatternFill(fill_type="solid", fgColor="D9EAF7")
        for sheet in workbook.worksheets:
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for cell in sheet[1]:
                cell.font = header_font
                cell.fill = header_fill
            for column_cells in sheet.columns:
                values = [str(cell.value or "") for cell in column_cells]
                width = min(max(max(len(value) for value in values) + 2, 12), 42)
                sheet.column_dimensions[column_cells[0].column_letter].width = width
    return buffer.getvalue()


def pdf_report_bytes(report):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), rightMargin=32, leftMargin=32, topMargin=28, bottomMargin=28)
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    h2 = ParagraphStyle("ReportH2", parent=styles["Heading2"], spaceBefore=10, spaceAfter=6, keepWithNext=True)
    normal = styles["Normal"]
    story = [
        Paragraph("Field Planner", title_style),
        Paragraph(report["title"], h2),
        Paragraph(f"Scope: {report['scope']} | Date Range: {report['date_range']} | Generated: {report['generated_at']} | By: {report['generated_by']}", normal),
        Spacer(1, 10),
        Paragraph(report["purpose"], normal),
        Spacer(1, 10),
        Paragraph("Summary", h2),
    ]
    summary_rows = [["Metric", "Value"]] + [[key, str(value)] for key, value in report["summary"].items()]
    summary_table = fit_pdf_table(summary_rows, doc.width, font_size=8)
    story.extend([summary_table, Spacer(1, 10), Paragraph("Key Findings", h2)])
    for item in report["insights"] or ["No key findings."]:
        story.append(Paragraph(f"- {item}", normal))
    story.extend([Spacer(1, 8), Paragraph("Recommended Follow-Up Actions", h2)])
    for item in report["actions"] or ["No action items."]:
        story.append(Paragraph(f"- {item}", normal))
    for name, df in report["tables"].items():
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        if report["title"] == "Manager Roll-Up" and "Roll-Up Manager" in df.columns:
            for manager_name, manager_df in df.groupby("Roll-Up Manager", dropna=False):
                story.extend([Spacer(1, 10), Paragraph(f"{name} - {manager_name}", h2)])
                story.append(fit_pdf_dataframe(manager_df, doc.width, max_columns=8, max_rows=30, font_size=6, header_color="#334155"))
        else:
            story.extend([Spacer(1, 10), Paragraph(name, h2)])
            story.append(fit_pdf_dataframe(df, doc.width, max_columns=8, max_rows=40, font_size=6, header_color="#334155"))
    doc.build(story)
    log_action("analysis report exported", "reports", description=report["title"])
    return buffer.getvalue()


def normalize_pm_columns(df):
    df = normalize_columns(df)
    aliases = {
        "technician": "technician", "tech": "technician", "tech_name": "technician", "technician_name": "technician",
        "assigned_to": "technician", "assigned_tech": "technician", "employee": "technician", "employee_name": "technician", "name": "technician",
        "first_name": "first_name", "last_name": "last_name", "employee_number": "employee_number", "employee_id": "employee_number",
        "wo": "work_order_number", "wo_#": "work_order_number", "wo_number": "work_order_number", "work_order": "work_order_number",
        "work_order_number": "work_order_number", "ticket_number": "work_order_number",
        "store": "store_number", "site_id": "store_number", "location_id": "store_number", "store_#": "store_number", "store_number": "store_number",
        "category": "category", "asset_category": "category", "trade": "category", "problem_type": "category", "equipment": "category",
        "wo_description": "category", "work_order_description": "category",
        "status": "status", "wo_status": "status", "work_order_status": "status",
        "date_opened": "date_opened", "opened_date": "date_opened", "created_date": "date_opened",
        "completed_date": "completed_date", "completion_date": "completed_date", "closed_date": "completed_date",
        "days_open": "days_open", "age": "days_open", "aging": "days_open",
        "notes": "notes", "description": "notes",
    }
    df = df.rename(columns={k: v for k, v in aliases.items() if k in df.columns})
    if df.columns.duplicated().any():
        collapsed = pd.DataFrame(index=df.index)
        for column in dict.fromkeys(df.columns):
            matches = df.loc[:, df.columns == column]
            collapsed[column] = matches.replace("", pd.NA).bfill(axis=1).iloc[:, 0].fillna("")
        df = collapsed
    if "technician" not in df.columns and {"first_name", "last_name"}.issubset(df.columns):
        df["technician"] = (df["first_name"].astype(str).str.strip() + " " + df["last_name"].astype(str).str.strip()).str.strip()
    return df.fillna("")


def read_pm_upload(uploaded_file):
    name = uploaded_file.name.lower()
    if name.endswith(".csv"):
        return pd.read_csv(uploaded_file, dtype=str).fillna("")
    workbook = pd.ExcelFile(uploaded_file)
    sheet_name = "DATA" if "DATA" in workbook.sheet_names else workbook.sheet_names[0]
    return pd.read_excel(workbook, sheet_name=sheet_name, dtype=str).fillna("")


def simplify_pm_category(value):
    text = clean_text(value)
    if not text:
        return "Uncategorized"
    lowered = text.lower()
    for match_text, label in [
        ("cappuccino", "Cappuccino Equipment"), ("cold beverage", "Cold Beverage"), ("refrigeration", "Refrigeration"),
        ("hot food", "Hot Food"), ("coffee", "Coffee"), ("ice machine", "Ice Machine"), ("roller grill", "Roller Grill"),
        ("freezer", "Freezer"), ("cooler", "Cooler"), ("fountain", "Fountain"), ("oven", "Oven"),
    ]:
        if match_text in lowered:
            return label
    parts = [part.strip() for part in text.split(" - ") if part.strip()]
    return (parts[-1].replace(" PM", "").strip() if len(parts) >= 2 else text) or "Uncategorized"


def pm_default_column(original_columns, target):
    for original in original_columns:
        test = normalize_pm_columns(pd.DataFrame(columns=[original]))
        if target in test.columns:
            return original
    return ""


def employee_lookup():
    employees = active_employees()
    lookup = {}
    if employees.empty:
        return employees, lookup
    for row in employees.to_dict("records"):
        full_name = clean_text(row["full_name"])
        lookup[name_key(full_name)] = row
        parts = full_name.split()
        if len(parts) >= 2:
            lookup[name_key(f"{parts[-1]} {' '.join(parts[:-1])}")] = row
            lookup[name_key(f"{parts[-1]}, {' '.join(parts[:-1])}")] = row
        emp_number = clean_text(row.get("employee_number", ""))
        if emp_number:
            lookup[name_key(emp_number)] = row
    return employees, lookup


def match_employee(row, lookup):
    for value in (clean_text(row.get("employee_number", "")), clean_text(row.get("technician", ""))):
        if name_key(value) in lookup:
            return lookup[name_key(value)]
    tech_key = name_key(row.get("technician", ""))
    if tech_key:
        for key, employee in lookup.items():
            if tech_key in key or key in tech_key:
                return employee
    return None


def is_completed_status(status, completed_date):
    return bool(completed_date) or any(word in clean_text(status).lower() for word in ("complete", "completed", "closed", "done"))


def import_pm_report(df, report_week):
    normalized = normalize_pm_columns(df)
    employees, lookup = employee_lookup()
    summary = {"rows_in_upload": len(normalized), "matched": 0, "created": 0, "updated": 0, "skipped": 0, "errors": []}
    if employees.empty:
        summary["errors"].append("No active employees found in the app.")
        return summary
    required = {"technician", "work_order_number"}
    missing = required - set(normalized.columns)
    if missing:
        summary["skipped"] = len(normalized)
        summary["errors"].append("Missing required columns: " + ", ".join(sorted(missing)))
        return summary
    with session_scope() as session:
        for index, row in normalized.iterrows():
            employee = match_employee(row, lookup)
            if employee is None:
                summary["skipped"] += 1
                continue
            work_order_number = clean_text(row.get("work_order_number", ""))
            if not work_order_number:
                summary["skipped"] += 1
                if len(summary["errors"]) < 20:
                    summary["errors"].append(f"Row {index + 2}: missing work order number")
                continue
            completed_date = parse_date(row.get("completed_date", ""))
            date_opened = parse_date(row.get("date_opened", ""))
            raw_status = clean_text(row.get("status", ""))
            status = "Completed" if is_completed_status(raw_status, completed_date) else "Open"
            days_open = parse_int(row.get("days_open", ""))
            if days_open is None and date_opened:
                days_open = max(((completed_date or report_week) - date_opened).days, 0)
            existing = session.scalar(
                select(PMCompletionReportRow).where(
                    PMCompletionReportRow.report_week == report_week,
                    PMCompletionReportRow.work_order_number == work_order_number,
                    PMCompletionReportRow.employee_id == int(employee["id"]),
                )
            )
            created = existing is None
            record = existing or PMCompletionReportRow(report_week=report_week, work_order_number=work_order_number, employee_id=int(employee["id"]))
            if created:
                session.add(record)
            record.technician_name = clean_text(employee["full_name"])
            record.employee_number = clean_text(row.get("employee_number", ""))
            record.store_number = clean_text(row.get("store_number", ""))
            record.category = simplify_pm_category(row.get("category", "") or row.get("notes", ""))
            record.status = status
            record.raw_status = raw_status
            record.date_opened = date_opened
            record.completed_date = completed_date
            record.days_open = days_open
            record.notes = clean_text(row.get("notes", ""))
            summary["matched"] += 1
            summary["created" if created else "updated"] += 1
    log_action("pm completion report imported", "pm_completion_report_rows", description=str(summary))
    return summary


def pm_rows(rollup=False):
    return run_query(
        """
        select report_week, technician_name, employee_id, work_order_number, store_number,
               category, status, raw_status, date_opened, completed_date, days_open, notes
        from pm_completion_report_rows
        order by report_week desc, technician_name, work_order_number
        """,
        rollup=rollup,
    )


def pmt_backlog_df(rollup=False):
    return run_query(
        """
        select e.full_name as technician, s.store_number, s.city, b.status, b.reason,
               b.cycles_missed, b.priority_score, b.last_scheduled_month, b.last_completed_date,
               b.cycle_start, b.cycle_end, b.notes
        from pmt_schedule_backlog b
        left join employees e on e.id = b.employee_id
        left join stores s on s.id = b.store_id
        where b.status in ('Not Scheduled','Not Completed','Carryover','Overdue','Skipped')
        order by e.full_name, b.priority_score desc, b.cycles_missed desc, s.store_number
        """,
        rollup=rollup,
    )


def schedule_df(start, end, rollup=False):
    return run_query(
        """
        select si.schedule_date, si.sequence_number, coalesce(t.team_name,'') as team_name,
               coalesce(e.full_name,'') as employee, s.store_number, s.city, s.address,
               si.work_type, si.status, si.weather_notes, si.completion_notes
        from schedule_items si
        left join stores s on s.id = si.store_id
        left join employees e on e.id = si.employee_id
        left join teams t on t.id = si.team_id
        where si.schedule_date between :start and :end
        order by si.schedule_date, si.work_type, t.team_name, e.full_name, si.sequence_number
        """,
        {"start": start, "end": end},
        rollup=rollup,
    )


def stores_df(rollup=False):
    return run_query(
        """
        select s.id, s.store_number, s.store_name, s.address, s.city, s.state, s.zip, s.latitude, s.longitude,
               bt.team_name as brand_team, pt.team_name as pmt_team, ct.team_name as calibration_team,
               p.full_name as pmt_technician, b.full_name as brand_technician, c.full_name as calibration_technician,
               s.store_status, s.priority, s.active
        from stores s
        left join teams bt on bt.id = s.assigned_brand_team_id
        left join teams pt on pt.id = s.assigned_pmt_team_id
        left join teams ct on ct.id = s.assigned_calibration_team_id
        left join employees p on p.id = s.assigned_pmt_employee_id
        left join employees b on b.id = s.assigned_brand_employee_id
        left join employees c on c.id = s.assigned_calibration_employee_id
        where s.active = true
        order by s.store_number
        """,
        rollup=rollup,
    )


def employees_df(rollup=False):
    return run_query(
        """
        select e.id, e.full_name, e.employee_number, e.role, t.team_name, e.phone, e.email,
               e.home_address, e.home_city, e.home_state, e.home_zip, e.home_latitude, e.home_longitude,
               e.monthly_pmt_store_target, e.active
        from employees e
        left join teams t on t.id = e.team_id
        order by e.active desc, e.full_name
        """,
        rollup=rollup,
    )


def followups_df(start=None, end=None, rollup=False):
    date_filter = "and coalesce(f.due_date, f.next_followup_date, f.date_opened) between :start and :end" if start and end else ""
    return run_query(
        f"""
        select f.id, f.priority, f.category, f.vendor, f.status, f.issue_title, f.date_opened,
               f.next_followup_date, f.due_date, f.completed_date, coalesce(e.full_name, f.related_person, '') as assigned_to,
               s.store_number, s.city
        from followups f
        left join stores s on s.id = f.store_id
        left join employees e on e.id = f.assigned_employee_id
        where 1=1 {date_filter}
        order by (f.due_date is null), f.due_date, f.priority
        """,
        {"start": start, "end": end},
        rollup=rollup,
    )


def deferred_df(start=None, end=None, rollup=False):
    date_filter = "and coalesce(d.completed_date, d.due_date, d.date_created) between :start and :end" if start and end else ""
    type_select = "'Other' as work_order_type" if rollup else "coalesce(d.work_order_type, 'Other') as work_order_type"
    completed_team_select = "'' as completed_team" if rollup else "ct.team_name as completed_team"
    completed_team_join = "" if rollup else "left join teams ct on ct.id = d.completed_team_id"
    return run_query(
        f"""
        select d.id, d.work_order_number, d.title, {type_select},
               d.priority, d.status, d.date_created, d.due_date,
               d.completed_date, coalesce(e.full_name, at.team_name, '') as assigned_to,
               at.team_name as assigned_team, {completed_team_select},
               s.store_number, s.city
        from deferred_work_orders d
        left join stores s on s.id = d.store_id
        left join employees e on e.id = d.assigned_employee_id
        left join teams at on at.id = d.assigned_team_id
        {completed_team_join}
        where 1=1 {date_filter}
        order by d.status, (d.due_date is null), d.due_date
        """,
        {"start": start, "end": end},
        rollup=rollup,
    )


def pto_df(start, end, rollup=False):
    return run_query(
        """
        select e.full_name, c.event_type, c.event_date, c.end_date, c.status, c.notes
        from calloff_pto c
        join employees e on e.id = c.employee_id
        where c.event_date <= :end and coalesce(c.end_date, c.event_date) >= :start
        order by c.event_date, e.full_name
        """,
        {"start": start, "end": end},
        rollup=rollup,
    )


def metric_value(df, condition=None):
    if df.empty:
        return 0
    if condition is None:
        return len(df)
    try:
        return int(condition(df).sum())
    except Exception:
        return 0


def build_report(report_type, start, end, rollup=False, filters=None):
    filters = filters or {}
    generated_by = st.session_state.get("user_email") or st.session_state.get("username", "")
    scope = "All Managed Users" if rollup else st.session_state.get("active_account_label", "My Workspace")
    sched = schedule_df(start, end, rollup=rollup)
    stores = stores_df(rollup=rollup)
    emps = employees_df(rollup=rollup)
    fups = followups_df(start, end, rollup=rollup)
    dwos = deferred_df(start, end, rollup=rollup)
    pto = pto_df(start, end, rollup=rollup)
    pm = pm_rows(rollup=rollup)
    pmt_backlog = pmt_backlog_df(rollup=rollup)
    today = date.today()

    work_group = filters.get("work_group", "All Work Groups")
    if work_group not in ("All Work Groups", "Follow-Ups", "Deferred Work Orders") and not sched.empty:
        sched = sched[sched["work_type"].fillna("").astype(str) == work_group]
    employee_filter = filters.get("employee", "All")
    team_filter = filters.get("team", "All")
    city_filter = filters.get("city", "All")
    status_filter = filters.get("status", "All")
    priority_filter = filters.get("priority", "All")
    if employee_filter != "All" and not sched.empty:
        sched = sched[sched["employee"].fillna("").astype(str) == employee_filter]
    if team_filter != "All" and not sched.empty:
        sched = sched[sched["team_name"].fillna("").astype(str) == team_filter]
    if city_filter != "All":
        if not sched.empty and "city" in sched.columns:
            sched = sched[sched["city"].fillna("").astype(str) == city_filter]
        if not stores.empty and "city" in stores.columns:
            stores = stores[stores["city"].fillna("").astype(str) == city_filter]
        if not dwos.empty and "city" in dwos.columns:
            dwos = dwos[dwos["city"].fillna("").astype(str) == city_filter]
    if status_filter != "All":
        if not sched.empty:
            sched = sched[sched["status"].fillna("").astype(str) == status_filter]
        if not dwos.empty:
            if status_filter == "Open":
                dwos = dwos[~dwos["status"].isin(["Completed", "Cancelled"])]
            else:
                dwos = dwos[dwos["status"].fillna("").astype(str) == status_filter]
    if priority_filter != "All" and not dwos.empty and "priority" in dwos.columns:
        dwos = dwos[dwos["priority"].fillna("").astype(str) == priority_filter]

    completed = metric_value(sched, lambda df: df["status"].eq("Completed"))
    scheduled = len(sched)
    completion_rate = round((completed / scheduled) * 100, 1) if scheduled else 0
    open_followups = metric_value(fups, lambda df: ~df["status"].isin(["Completed", "Cancelled"]))
    overdue_followups = metric_value(fups, lambda df: (~df["status"].isin(["Completed", "Cancelled"])) & date_before_today(df["due_date"], today))
    open_dwos = metric_value(dwos, lambda df: df["status"].isin(["Available", "Assigned", "In Progress"]))
    total_dwos = len(dwos)
    completed_dwos = metric_value(dwos, lambda df: df["status"].eq("Completed"))
    dwo_completion_rate = round((completed_dwos / total_dwos) * 100, 1) if total_dwos else 0
    missing_coords = metric_value(stores, lambda df: df["latitude"].isna() | df["longitude"].isna())
    unassigned = metric_value(stores, lambda df: df[["brand_team", "pmt_team", "calibration_team", "pmt_technician", "brand_technician", "calibration_technician"]].fillna("").astype(str).apply(lambda row: not any(v.strip() for v in row), axis=1))

    schedule_by_work_group = sched.groupby(["work_type", "status"], dropna=False).size().reset_index(name="Count") if not sched.empty else pd.DataFrame()
    schedule_by_owner = sched.groupby(["team_name", "employee", "status"], dropna=False).size().reset_index(name="Count") if not sched.empty else pd.DataFrame()
    followup_priority = fups.groupby(["priority", "status"], dropna=False).size().reset_index(name="Count") if not fups.empty else pd.DataFrame()
    dwo_priority = dwos.groupby(["priority", "status"], dropna=False).size().reset_index(name="Count") if not dwos.empty else pd.DataFrame()
    dwo_type = dwos.groupby(["work_order_type", "status"], dropna=False).size().reset_index(name="Count") if not dwos.empty and "work_order_type" in dwos.columns else pd.DataFrame()
    dwo_team_type = (
        dwos.assign(
            assigned_team=dwos["assigned_team"].fillna("Unassigned"),
            completed_team=dwos["completed_team"].fillna("Not completed"),
        )
        .groupby(["assigned_team", "completed_team", "work_order_type", "status"], dropna=False)
        .size()
        .reset_index(name="Count")
        if not dwos.empty and {"assigned_team", "completed_team", "work_order_type"}.issubset(dwos.columns)
        else pd.DataFrame()
    )
    workload_team = stores.groupby(["brand_team", "pmt_team", "calibration_team"], dropna=False).size().reset_index(name="Store Count") if not stores.empty else pd.DataFrame()
    coverage_group = work_group if work_group in ("Brand Enhancement", "PMT", "Calibration") else "All Work Groups"
    geographic_coverage = geographic_coverage_summary(stores, coverage_group)
    pm_summary = pd.DataFrame()
    if not pm.empty:
        pm["days_open"] = pd.to_numeric(pm["days_open"], errors="coerce")
        pm_summary = pm.groupby(["technician_name", "status"], dropna=False).agg(WOs=("work_order_number", "count"), Avg_Days_Open=("days_open", "mean")).reset_index()
        pm_summary["Avg Days Open"] = pm_summary["Avg_Days_Open"].fillna(0).round(1)
        pm_summary = pm_summary.drop(columns=["Avg_Days_Open"])
    pmt_backlog_summary = (
        pmt_backlog.groupby(["technician", "status"], dropna=False)
        .agg(Stores=("store_number", "count"), Max_Cycles_Missed=("cycles_missed", "max"), Avg_Priority=("priority_score", "mean"))
        .reset_index()
        if not pmt_backlog.empty
        else pd.DataFrame()
    )
    if not pmt_backlog_summary.empty:
        pmt_backlog_summary["Avg_Priority"] = pmt_backlog_summary["Avg_Priority"].fillna(0).round(1)

    exceptions = {
        "Missing Coordinates": stores[stores["latitude"].isna() | stores["longitude"].isna()] if not stores.empty else pd.DataFrame(),
        "Unassigned Stores": stores[stores[["brand_team", "pmt_team", "calibration_team", "pmt_technician", "brand_technician", "calibration_technician"]].fillna("").astype(str).apply(lambda row: not any(v.strip() for v in row), axis=1)] if not stores.empty else pd.DataFrame(),
        "Overdue Follow-Ups": fups[(~fups["status"].isin(["Completed", "Cancelled"])) & date_before_today(fups["due_date"], today)] if not fups.empty and "due_date" in fups.columns else pd.DataFrame(),
        "Deferred WOs Past Due": dwos[(~dwos["status"].isin(["Completed", "Cancelled"])) & date_before_today(dwos["due_date"], today)] if not dwos.empty and "due_date" in dwos.columns else pd.DataFrame(),
        "Needs Rescheduled": sched[sched["status"].isin(["Needs Rescheduled", "Rain Delay", "Not Completed"])] if not sched.empty else pd.DataFrame(),
        "Employees Missing Home Coordinates": emps[(emps["home_latitude"].isna() | emps["home_longitude"].isna()) & emps["active"].astype(str).isin(["1", "True", "true"])] if not emps.empty else pd.DataFrame(),
    }
    exception_count = sum(len(df) for df in exceptions.values())

    summary = {
        "Active Stores": len(stores),
        "Active Employees": metric_value(emps, lambda df: df["active"].astype(str).isin(["1", "True", "true"])),
        "Scheduled Items": scheduled,
        "Completed Items": completed,
        "Completion Rate": f"{completion_rate}%",
        "Open Follow-Ups": open_followups,
        "Overdue Follow-Ups": overdue_followups,
        "Open Deferred WOs": open_dwos,
        "Deferred WOs Total": total_dwos,
        "Deferred WOs Completed": completed_dwos,
        "Deferred WO Completion Rate": f"{dwo_completion_rate}%",
        "Missing Coordinates": missing_coords,
        "Unassigned Stores": unassigned,
        "Exception Items": exception_count,
    }
    insights = []
    if completion_rate < 80 and scheduled:
        insights.append(f"Schedule completion is {completion_rate}%, below the 80% review threshold.")
    if overdue_followups:
        insights.append(f"{overdue_followups} follow-up(s) are overdue.")
    if missing_coords:
        insights.append(f"{missing_coords} store(s) are missing coordinates and may not map or schedule correctly.")
    if unassigned:
        insights.append(f"{unassigned} active store(s) have no assignment layer.")
    if open_dwos:
        insights.append(f"{open_dwos} deferred work order(s) are still open.")
    if not insights:
        insights.append("No major issues found for the selected report filters.")
    actions = []
    if missing_coords:
        actions.append("Fix missing store coordinates from Stores or Areas and Maps.")
    if unassigned:
        actions.append("Assign stores to Brand Enhancement, PMT, and Calibration layers in Areas and Maps.")
    if overdue_followups:
        actions.append("Review overdue follow-ups and assign next action owners.")
    if open_dwos:
        actions.append("Use deferred WOs on weather days or assign them to technicians.")
    if not actions:
        actions.append("Continue monitoring completion and open issue trends.")

    tables = {
        "Schedule Detail": sched,
        "Schedule By Work Group": schedule_by_work_group,
        "Schedule By Owner": schedule_by_owner,
        "Follow-Up Priority": followup_priority,
        "Deferred WO Priority": dwo_priority,
        "Deferred WO By Type": dwo_type,
        "Deferred WO By Team And Type": dwo_team_type,
        "Workload By Team": workload_team,
        "Geographic Coverage Ranking": geographic_coverage,
        "PM Technician Summary": pm_summary,
        "PMT Carryover / Not Scheduled": pmt_backlog,
        "PMT Backlog Summary": pmt_backlog_summary,
    }
    if report_type in ("Exception / Problem Report", "Data Quality"):
        tables = {**exceptions, **tables}
    elif report_type == "Individual Performance":
        tables = {"Schedule Detail": sched, "Follow-Ups": fups, "Deferred WOs": dwos, "PM Rows": pm}
    elif report_type == "Team Performance":
        tables = {"Schedule By Owner": schedule_by_owner, "Workload By Team": workload_team, "Schedule Detail": sched}
    elif report_type == "Schedule Completion":
        tables = {"Schedule By Work Group": schedule_by_work_group, "Schedule By Owner": schedule_by_owner, "Schedule Detail": sched, "Exceptions": exceptions["Needs Rescheduled"]}
    elif report_type == "PM Completion":
        tables = {"PM Technician Summary": pm_summary, "PM Detail": pm, "PMT Backlog Summary": pmt_backlog_summary, "PMT Carryover / Not Scheduled": pmt_backlog}
        if not pm.empty:
            summary.update({"PM Rows": len(pm), "PM Open": int((pm["status"] != "Completed").sum()), "PM Completed": int((pm["status"] == "Completed").sum())})
        if not pmt_backlog.empty:
            summary.update({
                "PMT Carryover / Not Scheduled": len(pmt_backlog),
                "PMT Stores Not Scheduled": int((pmt_backlog["status"] == "Not Scheduled").sum()),
                "PMT Overdue Stores": int((pmt_backlog["status"] == "Overdue").sum()),
            })
    elif report_type == "Follow-Up Report":
        tables = {"Follow-Up Priority": followup_priority, "Follow-Up Detail": fups, "Overdue Follow-Ups": exceptions["Overdue Follow-Ups"]}
    elif report_type == "Deferred Work Order Report":
        tables = {"Deferred WO By Team And Type": dwo_team_type, "Deferred WO By Type": dwo_type, "Deferred WO Priority": dwo_priority, "Deferred WO Detail": dwos, "Past Due Deferred WOs": exceptions["Deferred WOs Past Due"]}
        summary["Completion Rate"] = f"{dwo_completion_rate}%"
        summary["Completed Items"] = completed_dwos
        summary["Scheduled Items"] = total_dwos
    elif report_type == "Workload / Store Assignment":
        tables = {"Workload By Team": workload_team, "Geographic Coverage Ranking": geographic_coverage, "Stores": stores, "Unassigned Stores": exceptions["Unassigned Stores"]}
    elif report_type == "Geographic Coverage Report":
        high_risk = geographic_coverage[geographic_coverage["Drive Time Risk"] == "High"] if not geographic_coverage.empty else pd.DataFrame()
        tables = {"Geographic Coverage Ranking": geographic_coverage, "High Drive Time Risk": high_risk}
        largest_coverage = geographic_coverage["Coverage Sq Miles"].max() if not geographic_coverage.empty else 0
        widest_spread = geographic_coverage["Max Spread Miles"].max() if not geographic_coverage.empty else 0
        summary = {
            "Coverage Groups": len(geographic_coverage),
            "High Drive Time Risk": len(high_risk),
            "Largest Coverage Sq Miles": largest_coverage,
            "Widest Spread Miles": widest_spread,
        }
        insights = []
        if geographic_coverage.empty:
            insights.append("No assigned stores with coordinates were found for the selected scope and work group.")
        elif not high_risk.empty:
            insights.append(f"{len(high_risk)} assignment group(s) have high geographic coverage or spread and likely higher drive time.")
        else:
            insights.append("No high geographic coverage risk was found for the selected scope and work group.")
        actions = [
            "Review the highest square-mile and widest-spread assignments for possible workload balancing.",
            "Use Areas and Maps to adjust assignments if one team or technician covers too much geography.",
        ]
    elif report_type == "Weather Impact":
        weather_rows = sched[sched["status"].isin(["Rain Delay", "Needs Rescheduled"]) | sched["weather_notes"].fillna("").astype(str).str.strip().ne("")] if not sched.empty else pd.DataFrame()
        tables = {"Weather Impact Schedule Items": weather_rows, "Schedule Detail": sched}
        summary["Weather Impact Items"] = len(weather_rows)
    elif report_type == "Manager Roll-Up":
        rollup_df = manager_rollup_dataframe(st.session_state.get("user_id"), include_self=False)
        totals = manager_rollup_totals(rollup_df) if not rollup_df.empty else {}
        summary.update(totals)
        deferred_by_manager = (
            dwos.groupby(["Roll-Up Manager", "Managed Area", "status"], dropna=False)
            .size()
            .reset_index(name="Count")
            if not dwos.empty and {"Roll-Up Manager", "Managed Area", "status"}.issubset(dwos.columns)
            else pd.DataFrame()
        )
        deferred_type_by_manager = (
            dwos.groupby(["Roll-Up Manager", "Managed Area", "work_order_type", "status"], dropna=False)
            .size()
            .reset_index(name="Count")
            if not dwos.empty and {"Roll-Up Manager", "Managed Area", "work_order_type", "status"}.issubset(dwos.columns)
            else pd.DataFrame()
        )
        followups_by_manager = (
            fups.groupby(["Roll-Up Manager", "Managed Area", "status", "priority"], dropna=False)
            .size()
            .reset_index(name="Count")
            if not fups.empty and {"Roll-Up Manager", "Managed Area", "status", "priority"}.issubset(fups.columns)
            else pd.DataFrame()
        )
        schedules_by_manager = (
            sched.groupby(["Roll-Up Manager", "Managed Area", "work_type", "status"], dropna=False)
            .size()
            .reset_index(name="Count")
            if not sched.empty and {"Roll-Up Manager", "Managed Area", "work_type", "status"}.issubset(sched.columns)
            else pd.DataFrame()
        )
        stores_by_manager = (
            stores.groupby(["Roll-Up Manager", "Managed Area"], dropna=False)
            .size()
            .reset_index(name="Store Count")
            if not stores.empty and {"Roll-Up Manager", "Managed Area"}.issubset(stores.columns)
            else pd.DataFrame()
        )
        summary.update(
            {
                "Deferred WOs Total": len(dwos),
                "Deferred WOs Completed": int((dwos["status"] == "Completed").sum()) if not dwos.empty and "status" in dwos.columns else 0,
                "Deferred WOs Open": int((~dwos["status"].isin(["Completed", "Cancelled"])).sum()) if not dwos.empty and "status" in dwos.columns else 0,
                "Follow-Ups Open": int((~fups["status"].isin(["Completed", "Cancelled"])).sum()) if not fups.empty and "status" in fups.columns else 0,
                "Schedule Items Completed": int((sched["status"] == "Completed").sum()) if not sched.empty and "status" in sched.columns else 0,
            }
        )
        tables = {
            "Manager Summary": rollup_df,
            "Stores By Manager": stores_by_manager,
            "Schedules By Manager": schedules_by_manager,
            "Deferred WOs By Manager": deferred_by_manager,
            "Deferred WOs By Manager And Type": deferred_type_by_manager,
            "Follow-Ups By Manager": followups_by_manager,
            "Deferred WO Detail": dwos,
            "Follow-Up Detail": fups,
        }

    return {
        "title": report_type,
        "purpose": REPORT_PURPOSES.get(report_type, ""),
        "scope": scope,
        "date_range": f"{start} to {end}",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "generated_by": generated_by,
        "summary": summary,
        "insights": insights,
        "actions": actions,
        "tables": tables,
    }


def report_filename(report, ext):
    base = re.sub(r"[^A-Za-z0-9]+", "_", f"Field_Planner_{report['title']}_{date.today().isoformat()}").strip("_")
    return f"{base}.{ext}"


page_header("Reports", "Analysis reports for performance, workload, problems, follow-ups, schedules, and manager updates.")
st.info("Other pages export records. The Reports page explains what the records mean and creates shareable PDF/Excel reports.")

with st.expander("Upload Weekly PM Report", expanded=False):
    st.caption("This keeps the existing PM completion import flow. Imported rows feed the PM Completion report.")
    week_end = st.date_input("Week ending", value=date.today(), key="pm_report_week")
    upload = st.file_uploader("Upload weekly PM report", type=["xlsx", "csv"], key="pm_report_upload")
    if upload:
        incoming = read_pm_upload(upload)
        normalized = normalize_pm_columns(incoming)
        useful = [col for col in ["technician", "employee_number", "work_order_number", "store_number", "category", "status", "date_opened", "completed_date", "days_open", "notes"] if col in normalized.columns]
        c1, c2, c3 = st.columns(3)
        c1.metric("Rows in File", len(normalized))
        c2.metric("Useful Columns Found", len(useful))
        c3.metric("Active Employees", len(active_employees()))
        st.dataframe(normalized[useful].head(50) if useful else normalized.head(50), use_container_width=True, hide_index=True)
        original_columns = incoming.columns.tolist()
        mapping_options = [""] + original_columns
        cols = st.columns(3)
        mapping_targets = [
            ("technician", "Technician"), ("work_order_number", "Work Order #"), ("store_number", "Store #"),
            ("category", "Category / Description"), ("status", "Status"), ("days_open", "Days Open"),
            ("date_opened", "Date Opened"), ("completed_date", "Completed Date"), ("notes", "Notes"),
        ]
        selected_mapping = {}
        for index, (target, label) in enumerate(mapping_targets):
            default = pm_default_column(original_columns, target)
            selected_mapping[target] = cols[index % 3].selectbox(label, mapping_options, index=mapping_options.index(default) if default in mapping_options else 0, key=f"pm_map_{target}")
        if st.button("Import PM Report", type="primary"):
            mapped = normalized.copy()
            for target, source in selected_mapping.items():
                if source:
                    mapped[target] = incoming[source]
            st.session_state["pm_report_import_summary"] = import_pm_report(mapped, week_end)
            st.rerun()
    if st.session_state.get("pm_report_import_summary"):
        summary = st.session_state["pm_report_import_summary"]
        st.subheader("Last PM Import")
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Rows in Company File", int(summary.get("rows_in_upload", 0)))
        s2.metric("Rows Kept for Your Team", int(summary.get("matched", 0)))
        s3.metric("Created", int(summary.get("created", 0)))
        s4.metric("Updated", int(summary.get("updated", 0)))
        if summary.get("errors"):
            st.error("Import needs attention.")
            st.write(summary["errors"])
        else:
            st.success("PM import completed.")

with st.container(border=True):
    step_header(1, "Choose Report Type", "Pick the decision-focused report you want to generate.")
    t1, t2 = st.columns([0.35, 0.65])
    with t1:
        with st.container(height=260, border=True):
            template = st.radio(
                "Report template",
                ["Custom"] + list(TEMPLATES.keys()),
                key="report_template_choice",
            )
    default_report, default_range = TEMPLATES.get(template, ("Executive Summary", "This Week"))
    report_type = t2.selectbox("Report type", REPORT_ORDER, index=REPORT_ORDER.index(default_report))
    guide = REPORT_GUIDE.get(report_type, {})
    with t2.container(border=True):
        st.markdown(f"**What it answers:** {guide.get('answers', REPORT_PURPOSES[report_type])}")
        st.markdown(f"**When to use it:** {guide.get('use', REPORT_PURPOSES[report_type])}")
        st.markdown(f"**What you get:** {guide.get('includes', 'Summary, findings, action items, and detail tables.')}")
        if template != "Custom":
            st.caption(f"Template selected: {template}. Suggested date range: {default_range}.")
    generated = st.session_state.get("generated_analysis_report")
    if generated and generated.get("title") != report_type:
        st.session_state.pop("generated_analysis_report", None)

with st.container(border=True):
    step_header(2, "Choose Scope", "Choose which workspace, team, employee, work group, or city this report covers.")
    rollup_available = st.session_state.get("account_role") in ("Manager", "Admin")
    scope_options = ["My Workspace"] + (["All Managed Users"] if rollup_available else [])
    scope = st.selectbox("Scope", scope_options, index=1 if is_all_managed_view() and "All Managed Users" in scope_options else 0)
    rollup = scope == "All Managed Users"
    if report_type == "Manager Roll-Up" and rollup_available:
        scope = "All Managed Users"
        rollup = True
        st.info("Manager Roll-Up reports always use All Managed Users so claimed users and nested claimed users are included.")
    employees = employees_df(rollup=rollup)
    team_data = teams(active_only=False)
    stores = stores_df(rollup=rollup)
    c1, c2, c3, c4 = st.columns(4)
    work_group = c1.selectbox("Work group", WORK_GROUPS)
    employee_options = ["All"] + sorted([value for value in employees.get("full_name", pd.Series(dtype=str)).dropna().astype(str).unique().tolist() if value])
    team_options = ["All"] + sorted([value for value in team_data.get("team_name", pd.Series(dtype=str)).dropna().astype(str).unique().tolist() if value])
    city_options = ["All"] + sorted([value for value in stores.get("city", pd.Series(dtype=str)).fillna("").astype(str).unique().tolist() if value.strip()])
    employee_filter = c2.selectbox("Employee", employee_options)
    team_filter = c3.selectbox("Team", team_options)
    city_filter = c4.selectbox("City / Area", city_options)

with st.container(border=True):
    step_header(3, "Choose Date Range", "Pick the report period. Data Quality uses current records but still accepts a generated date range.")
    range_options = ["Today", "This Week", "Last Week", "This Month", "Last Month", "This Quarter", "Year to Date", "Custom Date Range"]
    range_label = st.selectbox("Date range", range_options, index=range_options.index(default_range) if default_range in range_options else 1)
    if range_label == "Custom Date Range":
        d1, d2 = st.columns(2)
        start = d1.date_input("Start", value=date.today() - timedelta(days=30))
        end = d2.date_input("End", value=date.today())
    else:
        start, end = date_range_for(range_label)
        st.caption(f"Selected range: {start} to {end}")

with st.container(border=True):
    step_header(4, "Choose Filters", "Only relevant filters are applied to the generated report.")
    f1, f2, f3 = st.columns(3)
    status_filter = f1.selectbox("Status", ["All", "Open", "Scheduled", "Completed", "Needs Rescheduled", "Rain Delay", "Not Completed", "Cancelled", "Skipped"])
    priority_filter = f2.selectbox("Priority", ["All", "Critical", "High", "Medium", "Low"])
    exceptions_only = f3.checkbox("Exceptions only", value=report_type in ("Exception / Problem Report", "Data Quality"))

with st.container(border=True):
    step_header(5, "Generate Report", "Validate inputs and generate the selected analysis report.")
    missing = []
    if report_type == "Individual Performance" and employee_filter == "All":
        missing.append("Select an employee for the Individual Performance report.")
    if start > end:
        missing.append("Start date must be before or equal to end date.")
    for item in missing:
        st.warning(item)
    if st.button("Generate Report", type="primary", disabled=bool(missing)):
        st.session_state["generated_analysis_report"] = build_report(
            report_type,
            start,
            end,
            rollup=rollup,
            filters={
                "work_group": work_group,
                "employee": employee_filter,
                "team": team_filter,
                "city": city_filter,
                "status": status_filter,
                "priority": priority_filter,
                "exceptions_only": exceptions_only,
            },
        )
        st.rerun()

report = st.session_state.get("generated_analysis_report")
if report:
    with st.container(border=True):
        step_header(6, "Review Report", "Review summary cards, key findings, action items, charts, and report tables.")
        st.subheader(report["title"])
        st.caption(f"{report['purpose']} Scope: {report['scope']} | Date range: {report['date_range']} | Generated {report['generated_at']}")
        summary_items = list(report["summary"].items())
        for start_index in range(0, len(summary_items), 4):
            cols = st.columns(4)
            for col, (label, value) in zip(cols, summary_items[start_index:start_index + 4]):
                col.metric(label, value)
        k1, k2 = st.columns(2)
        with k1:
            st.subheader("Key Insights")
            for insight in report["insights"]:
                st.info(insight)
        with k2:
            st.subheader("Recommended Follow-Up")
            for action in report["actions"]:
                st.warning(action)
        chart_candidates = {name: df for name, df in report["tables"].items() if isinstance(df, pd.DataFrame) and not df.empty and "Count" in df.columns}
        if chart_candidates:
            chart_name = next(iter(chart_candidates))
            chart_df = chart_candidates[chart_name]
            x_col = chart_df.columns[0]
            color_col = chart_df.columns[1] if len(chart_df.columns) > 2 else None
            fig = px.bar(chart_df, x=x_col, y="Count", color=color_col, title=chart_name, barmode="group")
            fig.update_layout(barmode="group")
            st.plotly_chart(fig, use_container_width=True)
        for name, df in report["tables"].items():
            if isinstance(df, pd.DataFrame):
                with st.expander(name, expanded=name in ("Missing Coordinates", "Unassigned Stores", "Overdue Follow-Ups", "Schedule Detail")):
                    if df.empty:
                        st.info("No records matched this section.")
                    else:
                        st.dataframe(df.head(500), use_container_width=True, hide_index=True)

    with st.container(border=True):
        step_header(7, "Export PDF / Excel", "Download a polished PDF for sharing or a structured Excel workbook for deeper review.")
        e1, e2, e3 = st.columns(3)
        e1.download_button(
            "Export to PDF",
            data=pdf_report_bytes(report),
            file_name=report_filename(report, "pdf"),
            mime="application/pdf",
        )
        e2.download_button(
            "Export to Excel",
            data=formatted_excel_bytes(report),
            file_name=report_filename(report, "xlsx"),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        first_table = next((df for df in report["tables"].values() if isinstance(df, pd.DataFrame) and not df.empty), pd.DataFrame())
        e3.download_button("Export Detail CSV", data=csv_bytes(first_table), file_name=report_filename(report, "csv"), mime="text/csv", disabled=first_table.empty)
