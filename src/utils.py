from datetime import date
from html import escape
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from src.auth import authenticate, authenticate_with_reason, accessible_accounts_for_current_user, can_access_account_slug, create_user, find_user_by_email, init_auth_db, reset_password_with_secret, sign_in, sign_out, user_count


APP_DIR = Path(__file__).resolve().parents[1]
load_dotenv(APP_DIR / ".env")
load_dotenv()

UPLOAD_DIR = Path("uploads")
REPORT_DIR = Path("reports")
UPLOAD_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)


STATUS_COLORS = {
    "Open": "#d97706",
    "Scheduled": "#2563eb",
    "Published": "#2563eb",
    "Completed": "#16a34a",
    "Not Completed": "#dc2626",
    "Rain Delay": "#ea580c",
    "Needs Rescheduled": "#7c3aed",
    "Overdue": "#b91c1c",
    "Available": "#6b7280",
    "Assigned": "#0891b2",
    "In Progress": "#2563eb",
    "Cancelled": "#64748b",
}

SECRET_QUESTIONS = [
    "What city were you born in?",
    "What was the name of your first pet?",
    "What was your childhood nickname?",
    "What is your mother's maiden name?",
    "What was the name of your first school?",
    "What was the make of your first car?",
    "What is the name of the street you grew up on?",
]


def apply_theme():
    st.markdown(
        """
        <style>
        :root {
            --asm-border: #d9e2ec;
            --asm-muted: #52616b;
            --asm-ink: #102a43;
            --asm-soft: #f4f7fb;
            --asm-blue: #1d4ed8;
            --asm-green: #15803d;
            --asm-orange: #ea580c;
            --asm-red: #dc2626;
            --asm-teal: #0f766e;
            --asm-purple: #7c3aed;
        }
        html, body, [class*="css"] {
            font-size: 17px;
        }
        .stApp {
            background:
                linear-gradient(180deg, rgba(29, 78, 216, 0.08), rgba(255, 255, 255, 0) 280px),
                #f4f7fb;
        }
        .block-container {
            padding-top: 1.2rem;
            padding-bottom: 2.5rem;
            max-width: 1500px;
        }
        h1, h2, h3 {color: var(--asm-ink);}
        h1 {font-size: 2.35rem; line-height: 1.12; margin-bottom: 0.2rem; font-weight: 800;}
        h2 {font-size: 1.55rem; font-weight: 800;}
        h3 {font-size: 1.22rem; font-weight: 800;}
        div[data-testid="stSidebar"] {
            background: linear-gradient(180deg, #ffffff 0%, #f1f5f9 100%);
            border-right: 1px solid #cbd5e1;
        }
        div[data-testid="stSidebarNav"] {
            display: none;
        }
        div[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
            color: var(--asm-muted);
        }
        .sidebar-user {
            background: #f8fafc;
            border: 1px solid #cbd5e1;
            border-radius: 10px;
            color: #475569;
            font-size: 0.82rem;
            line-height: 1.35;
            margin-bottom: 0.65rem;
            padding: 0.65rem 0.7rem;
        }
        .sidebar-app-title {
            color: #0f172a;
            font-size: 1.7rem;
            font-weight: 850;
            letter-spacing: 0;
            line-height: 1.05;
            margin: 0.55rem 0 0.35rem 0;
            padding-bottom: 0.6rem;
            border-bottom: 4px solid #2563eb;
        }
        .sidebar-app-subtitle {
            color: #64748b;
            font-size: 0.82rem;
            margin: -0.15rem 0 0.9rem 0;
        }
        .sidebar-group {
            border: 1px solid #cbd5e1;
            border-radius: 12px;
            margin: 0.85rem 0 0.35rem 0;
            padding: 0.58rem 0.65rem;
            box-shadow: 0 4px 12px rgba(15, 23, 42, 0.05);
        }
        .sidebar-group.home {background: #e8f5e9; border-color: #b7dfbd;}
        .sidebar-group.operations {background: #fff3e0; border-color: #ffd8a8;}
        .sidebar-group.admin {background: #e3f2fd; border-color: #b8daf6;}
        .sidebar-section-title {
            color: #334155;
            font-size: 0.74rem;
            font-weight: 850;
            letter-spacing: 0;
            text-transform: uppercase;
        }
        section[data-testid="stSidebar"] div[data-testid="stButton"] > button {
            width: 100%;
            min-height: 36px;
            margin-bottom: 0.35rem;
        }
        section[data-testid="stSidebar"] a {
            border-radius: 8px;
            min-height: 34px;
        }
        div[data-testid="stPageLink"] a {
            background: rgba(255,255,255,0.88);
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            color: var(--asm-ink);
            font-weight: 700;
            justify-content: flex-start;
            min-height: 36px;
            padding: 0.45rem 0.75rem;
            text-decoration: none;
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
        }
        div[data-testid="stPageLink"] a:hover {
            background: #eff6ff;
            border-color: var(--asm-blue);
            color: var(--asm-blue);
            box-shadow: 0 4px 12px rgba(29, 78, 216, 0.14);
        }
        div[data-testid="stPageLink"] a[aria-current="page"],
        div[data-testid="stPageLink"] a[aria-current="true"] {
            background: #dbeafe !important;
            border-color: #93c5fd !important;
            border-left: 5px solid #2563eb !important;
            color: #0f172a !important;
            font-weight: 850 !important;
        }
        div[data-testid="stVerticalBlock"] > div[data-testid="stHorizontalBlock"] {
            gap: 0.75rem;
        }
        .asm-page-header {
            background:
                linear-gradient(90deg, rgba(29, 78, 216, 0.12), rgba(21, 128, 61, 0.08) 42%, rgba(234, 88, 12, 0.10)),
                #ffffff;
            border: 1px solid #c7d7ea;
            border-radius: 8px;
            padding: 1.15rem 1.25rem;
            margin-bottom: 1rem;
            box-shadow: 0 4px 16px rgba(16, 42, 67, 0.08);
        }
        .asm-page-header p, .asm-page-header [data-testid="stCaptionContainer"] {
            color: #334e68;
            font-size: 1rem;
        }
        .asm-panel {
            background: #ffffff;
            border: 1px solid #c7d7ea;
            border-left: 5px solid #64748b;
            border-radius: 10px;
            padding: 1rem 1.1rem;
            margin-bottom: 1rem;
            box-shadow: 0 4px 16px rgba(16, 42, 67, 0.07);
        }
        .asm-section {
            background: #fbfdff;
            border: 1px solid #c7d7ea;
            border-left: 6px solid var(--asm-blue);
            border-radius: 10px;
            padding: 1rem 1.1rem;
            margin: 1rem 0;
            box-shadow: 0 4px 16px rgba(16, 42, 67, 0.08);
        }
        .asm-section.green {border-left-color: var(--asm-green);}
        .asm-section.yellow {border-left-color: var(--asm-orange); background: #fffaf0;}
        .asm-section.red {border-left-color: var(--asm-red); background: #fff7f7;}
        .asm-section.gray {border-left-color: #64748b;}
        .asm-section.focused {
            background: #fff1f2 !important;
            border-left-color: var(--asm-red) !important;
            box-shadow: 0 0 0 4px rgba(220, 38, 38, 0.16), 0 8px 22px rgba(15, 23, 42, 0.10);
        }
        .asm-section-focus-label {
            color: #991b1b;
            font-weight: 850;
            margin-bottom: 0.18rem;
        }
        .asm-section h3 {
            margin-top: 0;
            margin-bottom: 0.25rem;
        }
        .asm-section p {
            color: var(--asm-muted);
            margin: 0;
        }
        .asm-workflow {
            background: #ffffff;
            border: 1px solid #c7d7ea;
            border-radius: 10px;
            padding: 0.95rem 1rem;
            margin: 0.5rem 0 1rem 0;
            box-shadow: 0 8px 22px rgba(16, 42, 67, 0.08);
        }
        .asm-workflow-title {
            color: #0f172a;
            font-size: 1rem;
            font-weight: 850;
            margin: 0 0 0.8rem 0.1rem;
        }
        .asm-progress-track {
            align-items: stretch;
            display: flex;
            gap: 0;
            overflow-x: auto;
            padding: 0.2rem 0.05rem;
        }
        .asm-workflow-link {
            color: inherit;
            display: contents;
            text-decoration: none;
        }
        .asm-step-action {
            align-items: center;
            background: #ffffff;
            border: 1px solid #c7d7ea;
            border-radius: 8px;
            box-shadow: 0 1px 2px rgba(16, 42, 67, 0.05);
            color: #1f2937 !important;
            display: flex;
            font-size: 1rem;
            font-weight: 500;
            justify-content: center;
            min-height: 2.5rem;
            padding: 0.45rem 0.75rem;
            text-align: center;
            text-decoration: none !important;
            width: 100%;
        }
        .asm-step-action:hover {
            border-color: var(--asm-blue);
            color: var(--asm-blue) !important;
        }
        .asm-workflow-step {
            align-items: flex-start;
            background: #f8fafc;
            border: 2px solid #fecaca;
            border-radius: 8px;
            display: flex;
            flex: 1 1 150px;
            gap: 0.65rem;
            min-height: 82px;
            min-width: 145px;
            padding: 0.72rem 0.78rem;
            position: relative;
        }
        .asm-workflow-step:hover {
            border-color: #2563eb;
            box-shadow: 0 8px 20px rgba(37, 99, 235, 0.15);
            transform: translateY(-1px);
        }
        .asm-workflow-step:not(:last-child)::after {
            background: #cbd5e1;
            content: "";
            height: 4px;
            left: calc(100% - 2px);
            position: absolute;
            top: 26px;
            width: 18px;
            z-index: 0;
        }
        .asm-workflow-step.active {
            background: #fff7ed;
            border-color: #fb923c;
            box-shadow: inset 0 0 0 2px rgba(251, 146, 60, 0.10);
        }
        .asm-workflow-step.done {
            background: #f0fdf4;
            border-color: #86efac;
        }
        .asm-workflow-step.done:not(:last-child)::after {background: #22c55e;}
        .asm-workflow-number {
            align-items: center;
            background: #fee2e2;
            border-radius: 999px;
            color: #991b1b;
            display: flex;
            flex: 0 0 28px;
            font-size: 0.9rem;
            font-weight: 900;
            height: 28px;
            justify-content: center;
            width: 28px;
            z-index: 1;
        }
        .asm-workflow-step.active .asm-workflow-number {
            background: #fed7aa;
            color: #9a3412;
        }
        .asm-workflow-step.done .asm-workflow-number {
            background: var(--asm-green);
            color: #ffffff;
        }
        .asm-workflow-body {
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
            min-width: 0;
        }
        .asm-workflow-label {
            color: #0f172a;
            font-size: 0.88rem;
            font-weight: 800;
            line-height: 1.15;
            overflow-wrap: anywhere;
        }
        .asm-workflow-status {
            align-items: center;
            border-radius: 999px;
            display: inline-flex;
            font-size: 0.74rem;
            font-weight: 850;
            gap: 0.25rem;
            line-height: 1;
            margin-top: 0.1rem;
            padding: 0.28rem 0.45rem;
            width: fit-content;
        }
        .asm-workflow-status.complete {
            background: #dcfce7;
            color: #166534;
        }
        .asm-workflow-status.incomplete {
            background: #fee2e2;
            color: #991b1b;
        }
        .asm-workflow-status.current {
            background: #ffedd5;
            color: #9a3412;
        }
        .asm-next-action {
            background: #f8fbff;
            border: 1px solid #bfdbfe;
            border-left: 6px solid var(--asm-blue);
            border-radius: 8px;
            color: #12335a;
            font-weight: 750;
            margin: 0.25rem 0 0.85rem 0;
            padding: 0.7rem 0.85rem;
        }
        .asm-next-action strong {
            color: #0f172a;
        }
        .asm-home-card {
            background: #ffffff;
            border: 1px solid #c7d7ea;
            border-left: 6px solid var(--asm-blue);
            border-radius: 8px;
            padding: 1.1rem;
            min-height: 145px;
            box-shadow: 0 4px 14px rgba(16, 42, 67, 0.08);
        }
        div[data-testid="column"]:nth-of-type(2) .asm-home-card {border-left-color: var(--asm-green);}
        div[data-testid="column"]:nth-of-type(3) .asm-home-card {border-left-color: var(--asm-orange);}
        div[data-testid="column"]:nth-of-type(4) .asm-home-card {border-left-color: var(--asm-red);}
        .asm-home-card h3 {margin: 0 0 0.25rem 0;}
        .asm-home-card p {color: var(--asm-muted); margin: 0 0 0.75rem 0;}
        [data-testid="stMetric"] {
            background: #ffffff;
            border: 1px solid #c7d7ea;
            border-top: 5px solid var(--asm-blue);
            border-radius: 8px;
            padding: 13px 14px;
            box-shadow: 0 4px 14px rgba(16, 42, 67, 0.08);
            min-height: 112px;
        }
        div[data-testid="column"]:nth-of-type(2) [data-testid="stMetric"] {border-top-color: var(--asm-green);}
        div[data-testid="column"]:nth-of-type(3) [data-testid="stMetric"] {border-top-color: var(--asm-orange);}
        div[data-testid="column"]:nth-of-type(4) [data-testid="stMetric"] {border-top-color: var(--asm-red);}
        div[data-testid="column"]:nth-of-type(5) [data-testid="stMetric"] {border-top-color: var(--asm-purple);}
        .asm-metric-card {
            background: #ffffff;
            border: 1px solid #c7d7ea;
            border-radius: 8px;
            border-top: 5px solid var(--asm-blue);
            box-shadow: 0 4px 14px rgba(16, 42, 67, 0.08);
            min-height: 112px;
            padding: 13px 14px;
        }
        .asm-metric-card .asm-metric-label {
            color: var(--asm-muted);
            font-size: 0.875rem;
            font-weight: 700;
            line-height: 1.2;
            margin-bottom: 1.1rem;
        }
        .asm-metric-card .asm-metric-value {
            color: var(--asm-ink);
            font-size: 1.3rem;
            font-weight: 800;
            line-height: 1.15;
        }
        .asm-metric-card .asm-metric-caption {
            color: var(--asm-muted);
            font-size: 0.78rem;
            line-height: 1.25;
            margin-top: 0.55rem;
        }
        [data-testid="stMetricValue"] {
            color: var(--asm-ink);
            font-size: 1.75rem;
            font-weight: 800;
            line-height: 1.15;
            white-space: normal;
        }
        [data-testid="stMetricLabel"] {
            min-height: 2.4rem;
            display: flex;
            align-items: flex-start;
        }
        [data-testid="stMetricLabel"] p {
            color: var(--asm-muted);
            font-size: 0.78rem;
            font-weight: 700;
            line-height: 1.15;
            white-space: normal;
            overflow: visible;
            text-overflow: clip;
            word-break: normal;
            overflow-wrap: anywhere;
        }
        .stTabs [data-baseweb="tab-list"] {
            gap: 0.35rem;
            border-bottom: 1px solid var(--asm-border);
            background: rgba(255,255,255,0.72);
            border-radius: 10px 10px 0 0;
            padding: 0.25rem 0.25rem 0 0.25rem;
        }
        .stTabs [data-baseweb="tab"] {
            border-radius: 8px 8px 0 0;
            padding: 0.65rem 0.95rem;
            font-weight: 700;
            color: #475569;
        }
        .stTabs [aria-selected="true"] {
            background: linear-gradient(180deg, #ffffff, #eef4ff);
            border: 1px solid #9fb7d3;
            border-bottom-color: #ffffff;
            color: var(--asm-blue);
        }
        div[data-testid="stDataFrame"] {
            border: 1px solid var(--asm-border);
            border-radius: 8px;
            overflow: hidden;
        }
        .stButton > button, .stDownloadButton > button {
            border-radius: 8px;
            border: 1px solid #94a3b8;
            background: #ffffff;
            color: #0f172a;
            font-weight: 750;
            min-height: 42px;
            font-size: 1rem;
            box-shadow: 0 2px 6px rgba(15, 23, 42, 0.07);
        }
        .stButton > button[kind="primary"], .stDownloadButton > button[kind="primary"] {
            background: #2563eb;
            border-color: #1d4ed8;
            color: #ffffff;
            box-shadow: 0 6px 16px rgba(37, 99, 235, 0.24);
        }
        .stButton > button:hover, .stDownloadButton > button:hover {
            border-color: var(--asm-blue);
            color: var(--asm-blue);
            box-shadow: 0 4px 12px rgba(29, 78, 216, 0.14);
        }
        .stButton > button[kind="primary"]:hover, .stDownloadButton > button[kind="primary"]:hover {
            background: #1d4ed8;
            color: #ffffff;
        }
        div[data-baseweb="input"] > div,
        div[data-baseweb="select"] > div,
        div[data-baseweb="textarea"] > div,
        div[data-baseweb="base-input"] {
            background: #f1f5f9 !important;
            border-color: #cbd5e1 !important;
            border-radius: 10px !important;
        }
        input, textarea {
            color: #0f172a !important;
            font-size: 0.98rem !important;
        }
        div[data-baseweb="input"]:focus-within > div,
        div[data-baseweb="select"]:focus-within > div,
        div[data-baseweb="textarea"]:focus-within > div {
            background: #ffffff !important;
            border-color: #2563eb !important;
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15) !important;
        }
        div[data-baseweb="popover"] {
            z-index: 999999 !important;
        }
        div[data-baseweb="popover"] [role="listbox"],
        div[data-baseweb="popover"] ul[role="listbox"],
        div[data-baseweb="popover"] div[role="listbox"] {
            max-height: min(360px, 55vh) !important;
            overflow-y: auto !important;
            overflow-x: hidden !important;
            overscroll-behavior: contain;
            scrollbar-width: auto;
            scrollbar-color: #64748b #e2e8f0;
        }
        div[data-baseweb="popover"] [role="option"] {
            min-height: 38px;
        }
        div[data-baseweb="popover"] [role="listbox"]::-webkit-scrollbar,
        div[data-baseweb="popover"] ul[role="listbox"]::-webkit-scrollbar,
        div[data-baseweb="popover"] div[role="listbox"]::-webkit-scrollbar {
            width: 12px;
        }
        div[data-baseweb="popover"] [role="listbox"]::-webkit-scrollbar-track,
        div[data-baseweb="popover"] ul[role="listbox"]::-webkit-scrollbar-track,
        div[data-baseweb="popover"] div[role="listbox"]::-webkit-scrollbar-track {
            background: #e2e8f0;
        }
        div[data-baseweb="popover"] [role="listbox"]::-webkit-scrollbar-thumb,
        div[data-baseweb="popover"] ul[role="listbox"]::-webkit-scrollbar-thumb,
        div[data-baseweb="popover"] div[role="listbox"]::-webkit-scrollbar-thumb {
            background: #64748b;
            border-radius: 999px;
            border: 2px solid #e2e8f0;
        }
        label, [data-testid="stWidgetLabel"] p {
            color: #334155 !important;
            font-weight: 700 !important;
        }
        [data-testid="stFileUploader"] section {
            background: #f1f5f9;
            border: 1px dashed #94a3b8;
            border-radius: 10px;
        }
        [data-testid="stCheckbox"] label,
        [data-testid="stRadio"] label {
            color: #334155 !important;
            font-weight: 650 !important;
        }
        .status-badge {
            display: inline-block;
            border-radius: 999px;
            color: white;
            padding: 2px 9px;
            font-size: 12px;
            font-weight: 700;
        }
        .small-muted {color: #64748b; font-size: 0.9rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def page_header(title, description="", actions=None):
    st.markdown('<div class="asm-page-header">', unsafe_allow_html=True)
    left, right = st.columns([0.72, 0.28])
    with left:
        st.title(title)
        if description:
            st.caption(description)
    with right:
        if actions:
            for label, target in actions:
                st.page_link(target, label=label)
    st.markdown("</div>", unsafe_allow_html=True)


def is_all_managed_view():
    return st.session_state.get("account_role") == "Manager" and st.session_state.get("manager_rollup_active")


def selected_workspace_scope():
    if is_all_managed_view():
        return "all_managed"
    if st.session_state.get("active_account_slug") == st.session_state.get("account_slug"):
        return "my_workspace"
    return "managed_user"


def can_edit_current_scope():
    return not is_all_managed_view()


WORKSPACE_TRANSIENT_KEYS = {
    "store_import_summary",
    "employee_import_summary",
    "dwo_import_summary",
    "pm_report_import_summary",
    "schedule_preview",
    "schedule_preview_signature",
    "pmt_schedule_draft",
    "pmt_schedule_draft_settings",
    "calibration_schedule_preview",
}
WORKSPACE_TRANSIENT_PREFIXES = (
    "auto_assign_",
)


def clear_workspace_transient_state():
    for key in list(st.session_state.keys()):
        if key in WORKSPACE_TRANSIENT_KEYS or any(key.startswith(prefix) for prefix in WORKSPACE_TRANSIENT_PREFIXES):
            st.session_state.pop(key, None)


PAGE_PERMISSIONS = {
    "Settings": {"Admin"},
    "Admin Controls": {"Admin"},
}


def can_access_page(user=None, page_name=""):
    role = (user or {}).get("account_role") if user else st.session_state.get("account_role", "User")
    role = role or "User"
    allowed_roles = PAGE_PERMISSIONS.get(page_name)
    if not allowed_roles:
        return role in {"User", "Manager", "Admin"}
    return role in allowed_roles


def access_denied(page_name="This page"):
    page_header(page_name, "Access Denied")
    st.error("You do not have permission to access this page.")
    st.page_link("app.py", label="Return to Dashboard")


def require_page_access(page_name):
    if can_access_page(page_name=page_name):
        return True
    access_denied(page_name)
    st.stop()


def section_header(title, description="", tone="blue", focus_key=None, focus_value=None):
    focused = bool(focus_key and st.session_state.get(focus_key) == focus_value)
    focus_class = " focused" if focused else ""
    focus_id = f' id="{escape(str(focus_key))}-{escape(str(focus_value))}"' if focus_key and focus_value is not None else ""
    focus_note = '<div class="asm-section-focus-label">Start here</div>' if focused else ""
    st.markdown(
        f'<div{focus_id} class="asm-section {tone}{focus_class}" style="scroll-margin-top:5rem;">'
        f'{focus_note}<h3>{escape(str(title))}</h3><p>{escape(str(description))}</p></div>',
        unsafe_allow_html=True,
    )


def metric_help_card(label, value, explanation, border_color="#dc2626", caption="Hover for why this is showing."):
    st.markdown(
        f"""
        <div class="asm-metric-card" title="{escape(str(explanation))}" style="border-top-color:{escape(str(border_color))};">
            <div class="asm-metric-label">{escape(str(label))}</div>
            <div class="asm-metric-value">{escape(str(value))}</div>
            <div class="asm-metric-caption">{escape(str(caption))}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _workflow_target_page(target):
    if isinstance(target, dict):
        return target.get("page", ""), target.get("state", {})
    route_map = {
        "/": "app.py",
        "/Stores": "pages/3_Stores.py",
        "/Map_Center": "pages/4_Map_Center.py",
        "/Scheduler": "pages/5_Scheduler.py",
        "/View_Schedule": "pages/12_View_Schedule.py",
        "/Reports": "pages/9_Reports.py",
        "/PMT_Monthly_Scheduler": "pages/13_PMT_Monthly_Scheduler.py",
        "/Calibration_Scheduler": "pages/14_Calibration_Scheduler.py",
    }
    return route_map.get(str(target), str(target)), {}


def _open_workflow_target(target):
    _page, state_updates = _workflow_target_page(target)
    for key, value in state_updates.items():
        st.session_state[key] = value


def _mark_workflow_step_reviewed(title, step_index):
    key = f"workflow_reviewed_{title}"
    reviewed = set(st.session_state.get(key, []))
    reviewed.add(int(step_index))
    st.session_state[key] = sorted(reviewed)


def workflow_guide(steps, current_step=1, title="Workflow", next_action="", step_links=None):
    current_step = max(1, int(current_step or 1))
    step_links = step_links or []
    reviewed_steps = set(st.session_state.get(f"workflow_reviewed_{title}", []))
    rendered_steps = []
    for index, label in enumerate(steps, start=1):
        is_complete = index < current_step or index in reviewed_steps
        is_current = index == current_step
        step_class = "done" if is_complete else "active" if is_current else ""
        status_class = "complete" if is_complete else "current" if is_current else "incomplete"
        status_icon = "✓" if is_complete else "✕"
        status_text = "Complete" if is_complete else "Incomplete"
        step_markup = (
            f'<div class="asm-workflow-step {step_class}">'
            f'<div class="asm-workflow-number">{status_icon}</div>'
            f'<div class="asm-workflow-body">'
            f'<div class="asm-workflow-label">Step {index}: {escape(str(label))}</div>'
            f'<div class="asm-workflow-status {status_class}">{status_icon} {status_text}</div>'
            f"</div></div>"
        )
        rendered_steps.append(step_markup)
    st.markdown(
        f'<div class="asm-workflow"><div class="asm-workflow-title">{escape(str(title))}</div>'
        f'<div class="asm-progress-track">{"".join(rendered_steps)}</div></div>',
        unsafe_allow_html=True,
    )
    if step_links:
        cols = st.columns(len(steps))
        for index, (col, label) in enumerate(zip(cols, steps), start=1):
            target = step_links[index - 1] if index <= len(step_links) else None
            if target:
                page, state_updates = _workflow_target_page(target)
                if state_updates:
                    col.button(
                        f"Open Step {index}",
                        key=f"workflow_step_{title}_{index}_{label}",
                        on_click=_open_workflow_target,
                        args=(target,),
                        use_container_width=True,
                    )
                else:
                    href = str(target) if isinstance(target, str) and str(target).startswith("/") else page
                    col.markdown(
                        f'<a class="asm-step-action" href="{escape(href)}" target="_self">Open Step {index}</a>',
                        unsafe_allow_html=True,
                    )
            col.button(
                f"Mark Step {index} Reviewed",
                key=f"workflow_review_{title}_{index}_{label}",
                on_click=_mark_workflow_step_reviewed,
                args=(title, index),
                use_container_width=True,
            )
    if next_action:
        st.markdown(f'<div class="asm-next-action"><strong>Next:</strong> {escape(str(next_action))}</div>', unsafe_allow_html=True)


def step_flow(steps, hint=""):
    """Compact single-line step sequence — no buttons, no state tracking.
    Replaces workflow_guide on tabbed scheduler pages where the tabs already
    provide phase navigation and only a quick orientation strip is needed.
    """
    pills = []
    for i, step in enumerate(steps, 1):
        pills.append(
            f'<span style="display:inline-flex;align-items:center;gap:5px;'
            f'background:#f8fafc;border:1px solid #e2e8f0;border-radius:20px;'
            f'padding:3px 11px 3px 5px;font-size:0.76rem;white-space:nowrap;">'
            f'<span style="background:#475569;color:#fff;border-radius:50%;'
            f'width:17px;height:17px;display:inline-flex;align-items:center;'
            f'justify-content:center;font-size:0.66rem;font-weight:700;'
            f'flex-shrink:0;">{i}</span>'
            f'<span style="color:#1e293b;">{escape(str(step))}</span>'
            f'</span>'
        )
    sep = '&nbsp;<span style="color:#cbd5e1;font-size:0.9rem;">›</span>&nbsp;'
    st.markdown(
        f'<div style="display:flex;flex-wrap:wrap;align-items:center;'
        f'gap:4px;padding:8px 0 2px;">{sep.join(pills)}</div>',
        unsafe_allow_html=True,
    )
    if hint:
        st.caption(f"↳ {hint}")


def sidebar_nav():
    from src.database import latest_undo_snapshot, restore_latest_undo_snapshot

    if st.session_state.get("authenticated"):
        display_name = f"{st.session_state.get('first_name', '')} {st.session_state.get('last_name', '')}".strip()
        st.sidebar.markdown(
            f'<div class="sidebar-user">Signed in as<br><strong>{display_name or st.session_state.get("username", "")}</strong><br>{st.session_state.get("account_role", "User")}</div>',
            unsafe_allow_html=True,
        )
        if st.sidebar.button("Sign out", type="secondary"):
            sign_out()
            st.cache_resource.clear()
            if hasattr(st, "switch_page"):
                st.switch_page("app.py")
            st.rerun()
    else:
        st.sidebar.markdown('<div class="sidebar-app-title">FIELD PLANNER</div>', unsafe_allow_html=True)
        st.sidebar.markdown('<div class="sidebar-app-subtitle">Field work project management</div>', unsafe_allow_html=True)
        return
    st.sidebar.markdown('<div class="sidebar-app-title">FIELD PLANNER</div>', unsafe_allow_html=True)
    st.sidebar.markdown('<div class="sidebar-app-subtitle">Field work project management</div>', unsafe_allow_html=True)
    accounts = accessible_accounts_for_current_user()
    account_role = st.session_state.get("account_role", "User")
    managed_accounts = [
        account for account in accounts
        if account.get("manager_user_id") == st.session_state.get("user_id")
    ]
    if account_role == "Manager" and managed_accounts:
        current_slug = st.session_state.get("active_account_slug") or st.session_state.get("account_slug")
        options = ["__manager_rollup__"] + [account["account_slug"] for account in accounts]
        own_slug = st.session_state.get("account_slug")
        account_labels = {
            "__manager_rollup__": "All Managed Users",
            **{
                account["account_slug"]: "My Workspace" if account["account_slug"] == own_slug else f"{account['first_name']} {account['last_name']}".strip() or account["email"]
                for account in accounts
            },
        }
        current_option = "__manager_rollup__" if st.session_state.get("manager_rollup_active", True) else current_slug
        selected_slug = st.sidebar.selectbox(
            "Viewing Workspace",
            options,
            index=options.index(current_option) if current_option in options else 0,
            format_func=lambda slug: account_labels.get(slug, slug),
            key="sidebar_workspace_selector",
        )
        if selected_slug == "__manager_rollup__":
            if not st.session_state.get("manager_rollup_active"):
                clear_workspace_transient_state()
                st.session_state["manager_rollup_active"] = True
                st.session_state["active_account_slug"] = st.session_state.get("account_slug")
                st.session_state["active_account_label"] = "All Managed Users"
                st.cache_resource.clear()
                st.rerun()
            st.session_state["active_account_slug"] = st.session_state.get("account_slug")
            st.session_state["active_account_label"] = "All Managed Users"
            st.session_state["manager_rollup_active"] = True
        else:
            if selected_slug != current_slug or st.session_state.get("manager_rollup_active"):
                clear_workspace_transient_state()
                st.session_state["manager_rollup_active"] = False
                st.session_state["active_account_slug"] = selected_slug
                st.session_state["active_account_label"] = account_labels.get(selected_slug, selected_slug)
                st.cache_resource.clear()
                st.rerun()
    elif len(accounts) > 1:
        active_slug = st.session_state.get("active_account_slug") or st.session_state.get("account_slug")
        if not can_access_account_slug(active_slug):
            active_slug = st.session_state.get("account_slug")
        account_slugs = [account["account_slug"] for account in accounts]
        account_labels = {
            account["account_slug"]: f"{account['first_name']} {account['last_name']}".strip() or account["email"]
            for account in accounts
        }
        selected_slug = st.sidebar.selectbox(
            "Workspace",
            account_slugs,
            index=account_slugs.index(active_slug) if active_slug in account_slugs else 0,
            format_func=lambda slug: account_labels.get(slug, slug),
            key="sidebar_workspace_selector",
        )
        if selected_slug != st.session_state.get("active_account_slug"):
            clear_workspace_transient_state()
            st.session_state["manager_rollup_active"] = False
            st.session_state["active_account_slug"] = selected_slug
            st.session_state["active_account_label"] = account_labels.get(selected_slug, selected_slug)
            st.cache_resource.clear()
            st.rerun()
    elif accounts:
        if accounts[0]["account_slug"] != st.session_state.get("active_account_slug"):
            clear_workspace_transient_state()
        st.session_state["active_account_slug"] = accounts[0]["account_slug"]
        st.session_state["active_account_label"] = f"{accounts[0]['first_name']} {accounts[0]['last_name']}".strip() or accounts[0]["email"]
        st.session_state["manager_rollup_active"] = False
    st.sidebar.markdown('<div class="sidebar-group home"><div class="sidebar-section-title">Home</div></div>', unsafe_allow_html=True)
    st.sidebar.page_link("app.py", label="Dashboard")
    st.sidebar.markdown('<div class="sidebar-group operations"><div class="sidebar-section-title">Main Operations</div></div>', unsafe_allow_html=True)
    st.sidebar.page_link("pages/3_Stores.py", label="Stores")
    st.sidebar.page_link("pages/4_Map_Center.py", label="Areas and Maps")
    st.sidebar.page_link("pages/5_Scheduler.py", label="Brand Enhancement Scheduler")
    st.sidebar.page_link("pages/13_PMT_Monthly_Scheduler.py", label="PMT Monthly Scheduler")
    st.sidebar.page_link("pages/14_Calibration_Scheduler.py", label="Calibration Scheduler")
    st.sidebar.page_link("pages/12_View_Schedule.py", label="View Schedule")
    st.sidebar.page_link("pages/16_Weather.py", label="Weather")
    st.sidebar.page_link("pages/11_Site_Visits.py", label="Site Visits")
    st.sidebar.page_link("pages/8_Deferred_Work_Orders.py", label="Deferred Work Orders")
    st.sidebar.markdown('<div class="sidebar-group admin"><div class="sidebar-section-title">Admin</div></div>', unsafe_allow_html=True)
    st.sidebar.page_link("pages/2_Employees.py", label="Employees")
    st.sidebar.page_link("pages/6_Call_Off_PTO.py", label="Call Off / PTO")
    st.sidebar.page_link("pages/7_Follow_Ups.py", label="Follow-Ups")
    st.sidebar.page_link("pages/9_Reports.py", label="Reports")
    st.sidebar.page_link("pages/18_My_Profile.py", label="My Profile")
    if account_role == "Admin":
        st.sidebar.page_link("pages/10_Settings.py", label="Settings")
        st.sidebar.page_link("pages/17_Admin_Controls.py", label="Admin Controls")
    st.sidebar.page_link("pages/15_Help_How_It_Works.py", label="Help / How To")
    st.sidebar.divider()
    undo_snapshot = latest_undo_snapshot()
    if undo_snapshot:
        st.sidebar.caption(f"Undo last change: {undo_snapshot.get('action_label') or undo_snapshot.get('table_names', '')}")
    else:
        st.sidebar.caption("No undo point saved yet.")
    if st.sidebar.button("Undo Last Change", disabled=not undo_snapshot, key="global_undo_last_change"):
        ok, message = restore_latest_undo_snapshot()
        if ok:
            st.sidebar.success(message)
            st.cache_resource.clear()
            st.rerun()
        st.sidebar.error(message)


def require_login():
    if st.session_state.get("authenticated") and st.session_state.get("account_slug"):
        return True
    if st.session_state.get("authenticated") and not st.session_state.get("account_slug"):
        sign_out()
    try:
        init_auth_db()
        first_account = user_count() == 0
    except Exception as exc:
        st.markdown(
            """
            <div style="background:#ffffff;border:2px solid #dc2626;border-radius:8px;padding:1rem 1.25rem;margin:1rem 0;color:#111827;max-width:760px;">
              <h2 style="margin:0 0 .5rem 0;font-size:1.35rem;">Login temporarily unavailable</h2>
              <p style="margin:.25rem 0;">The persistent database could not be loaded. Existing information has not been intentionally deleted.</p>
              <p style="margin:.25rem 0;">The app will not open first-account setup unless PostgreSQL storage is available.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.error("The persistent database is currently unavailable. Existing information has not been intentionally deleted.")
        st.info("No new first account can be created until the configured database connection is restored.")
        st.code(str(exc))
        return False
    st.markdown(
        """
        <style>
        .stApp {
            background:
                radial-gradient(circle at 50% 36%, rgba(255, 255, 255, 0.78), rgba(255, 255, 255, 0.28) 34%, rgba(226, 242, 255, 0) 64%),
                linear-gradient(135deg, #dff2ff 0%, #eef8ff 42%, #f8fbff 100%) !important;
            min-height: 100vh;
            overflow-x: hidden;
            overflow-y: auto !important;
        }
        html, body {
            height: auto !important;
            min-height: 100% !important;
            overflow-x: hidden !important;
            overflow-y: auto !important;
        }
        [data-testid="stAppViewContainer"], section.main, [data-testid="stMain"] {
            height: 100vh !important;
            min-height: 100vh !important;
            overflow-x: hidden !important;
            overflow-y: scroll !important;
        }
        [data-testid="stMainBlockContainer"] {
            min-height: 135vh !important;
            padding-bottom: 35vh !important;
        }
        header[data-testid="stHeader"] {
            background: transparent;
        }
        .login-background-scene {
            animation: login-sky-breathe 16s ease-in-out infinite alternate;
            inset: 0;
            overflow: hidden;
            pointer-events: none;
            position: fixed;
            z-index: -1;
        }
        .login-background-scene::after {
            background:
                radial-gradient(circle at 50% 42%, rgba(255, 255, 255, 0.72), rgba(255, 255, 255, 0.42) 28%, rgba(255, 255, 255, 0) 56%),
                linear-gradient(180deg, rgba(255, 255, 255, 0.16), rgba(255, 255, 255, 0.56));
            content: "";
            inset: 0;
            position: absolute;
        }
        .login-background-scene svg {
            display: block;
            height: 100%;
            min-height: 720px;
            width: 100%;
        }
        .block-container {
            background: transparent !important;
            left: 2rem;
            max-width: min(820px, calc(100vw - 4rem));
            min-height: auto;
            padding: 1.25rem 1rem 2rem 1rem;
            position: relative;
            top: 0;
            z-index: 20;
        }
        .login-hero {
            backdrop-filter: blur(16px);
            background: rgba(255, 255, 255, 0.9);
            border: 1px solid rgba(191, 219, 254, 0.9);
            border-radius: 18px;
            box-shadow: 0 18px 48px rgba(15, 23, 42, 0.16);
            margin: 0 0 1rem 0;
            padding: 2.4rem 3rem 1.8rem 3rem;
            position: relative;
            text-align: center;
            z-index: 30;
        }
        .login-hero h1 {
            color: #0f172a;
            font-size: 3.1rem;
            font-weight: 850;
            letter-spacing: 0;
            line-height: 1.08;
            margin: 0;
        }
        .login-accent {
            background: linear-gradient(90deg, #15803d 0%, #22c55e 31%, #f97316 32%, #f97316 62%, #dc2626 63%, #dc2626 100%);
            border-radius: 999px;
            height: 5px;
            margin: 0.75rem auto 0.85rem auto;
            width: 122px;
        }
        .login-hero p {
            color: #475569;
            font-size: 1.1rem;
            margin: 0;
        }
        .login-card {
            backdrop-filter: blur(16px);
            background: rgba(255, 255, 255, 0.92);
            border: 1px solid rgba(191, 219, 254, 0.95);
            border-radius: 18px;
            box-shadow: 0 18px 48px rgba(15, 23, 42, 0.16);
            margin-top: 0.75rem;
            padding: 1.35rem 1.5rem 1.25rem 1.5rem;
            position: relative;
            z-index: 30;
        }
        .login-card h3 {
            color: #1e293b;
            font-size: 1.85rem;
            font-weight: 800;
            margin: 0 0 0.35rem 0;
        }
        .login-card p {
            color: #52616b;
            font-size: 1rem;
            margin: 0;
        }
        .stTabs [data-baseweb="tab-list"] {
            backdrop-filter: blur(16px);
            background: rgba(255, 255, 255, 0.9);
            border: 1px solid rgba(191, 219, 254, 0.95);
            border-radius: 14px;
            box-shadow: 0 10px 26px rgba(15, 23, 42, 0.1);
            gap: 0.45rem;
            margin: 0.75rem 0 0.75rem 0;
            padding: 0.55rem;
            position: relative;
            z-index: 35;
        }
        .stTabs [data-baseweb="tab"] {
            border-radius: 10px;
            color: #64748b;
            font-weight: 750;
            min-width: 150px;
            padding: 0.75rem 1rem;
        }
        .stTabs [aria-selected="true"] {
            background: #dcfce7;
            border: 1px solid #bbf7d0;
            color: #166534;
        }
        div[data-testid="stForm"] {
            backdrop-filter: blur(16px);
            background: rgba(255, 255, 255, 0.94);
            border: 1px solid rgba(203, 213, 225, 0.92);
            border-radius: 18px;
            box-shadow: 0 18px 48px rgba(15, 23, 42, 0.14);
            max-height: min(58vh, 680px);
            overflow-y: auto;
            overscroll-behavior: auto;
            padding: 1.35rem 1.5rem 1.5rem 1.5rem;
            position: relative;
            scrollbar-color: #64748b #e2e8f0;
            scrollbar-gutter: stable;
            scrollbar-width: auto;
            z-index: 35;
        }
        div[data-testid="stForm"]::-webkit-scrollbar {
            width: 12px;
        }
        div[data-testid="stForm"]::-webkit-scrollbar-track {
            background: #e2e8f0;
            border-radius: 999px;
        }
        div[data-testid="stForm"]::-webkit-scrollbar-thumb {
            background: #64748b;
            border: 3px solid #e2e8f0;
            border-radius: 999px;
        }
        div[data-baseweb="input"] > div {
            background: #f8fafc !important;
            border: 1px solid #cbd5e1 !important;
            border-radius: 10px !important;
            min-height: 52px;
        }
        div[data-baseweb="input"]:focus-within > div {
            background: #ffffff !important;
            border-color: #15803d !important;
            box-shadow: 0 0 0 3px rgba(21, 128, 61, 0.16) !important;
        }
        .stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
            background: linear-gradient(90deg, #15803d, #16a34a 46%, #f97316 47%, #dc2626 100%);
            border-color: #166534;
            border-radius: 10px;
            color: #ffffff;
            font-weight: 800;
            min-height: 52px;
            width: 100%;
        }
        .stButton > button[kind="primary"]:hover, .stFormSubmitButton > button[kind="primary"]:hover {
            border-color: #14532d;
            box-shadow: 0 10px 24px rgba(21, 128, 61, 0.22);
            color: #ffffff;
            filter: brightness(0.96);
        }
        .login-note {
            background: #f4f7fb;
            border: 1px solid #d9e2ec;
            border-radius: 8px;
            color: #334e68;
            font-size: 0.95rem;
            margin-top: 0.75rem;
            padding: 0.8rem 0.9rem;
        }
        .login-cloud {
            animation: login-cloud-drift 18s ease-in-out infinite alternate;
        }
        .login-cloud.slow {
            animation-duration: 24s;
        }
        .login-sign-glow {
            animation: login-sign-pulse 3.6s ease-in-out infinite;
        }
        .login-window-glow {
            animation: login-window-shimmer 4.8s ease-in-out infinite;
        }
        @keyframes login-cloud-drift {
            from { transform: translateX(-24px); }
            to { transform: translateX(28px); }
        }
        @keyframes login-sign-pulse {
            0%, 100% { opacity: 0.76; }
            50% { opacity: 1; }
        }
        @keyframes login-window-shimmer {
            0%, 100% { opacity: 0.68; }
            50% { opacity: 0.98; }
        }
        @keyframes login-sky-breathe {
            from { filter: saturate(1) brightness(1); }
            to { filter: saturate(1.08) brightness(1.03); }
        }
        @media (max-width: 760px) {
            .block-container {
                max-width: 94vw;
                padding-left: 1rem;
                padding-right: 1rem;
                padding-top: 1rem;
                padding-bottom: 2rem;
                left: 0;
            }
            .login-hero {
                padding: 1.55rem 1.15rem 1.25rem 1.15rem;
            }
            .login-hero h1 {
                font-size: 2.25rem;
            }
            .login-background-scene svg {
                min-height: 760px;
                transform: translateX(-13%) scale(1.18);
                transform-origin: bottom center;
            }
            .stTabs [data-baseweb="tab"] {
                min-width: 0;
                padding-left: 0.65rem;
                padding-right: 0.65rem;
            }
        }
        @media (max-height: 760px) {
            .block-container {
                padding-top: 1rem;
                padding-bottom: 2rem;
            }
            .login-hero {
                padding: 1.2rem 1.5rem 1rem 1.5rem;
            }
            .login-hero h1 {
                font-size: 2.35rem;
            }
            .login-card {
                padding: 1rem 1.2rem;
            }
            div[data-testid="stForm"] {
                max-height: min(62vh, 640px);
                overflow-y: auto;
                padding: 1rem 1.2rem 1.2rem 1.2rem;
            }
        }
        </style>
        <div class="login-background-scene" aria-hidden="true">
            <svg viewBox="0 0 1440 900" preserveAspectRatio="xMidYMid slice" role="img">
                <defs>
                    <linearGradient id="loginSky" x1="0" x2="0" y1="0" y2="1">
                        <stop offset="0%" stop-color="#bfe6ff"/>
                        <stop offset="52%" stop-color="#e9f7ff"/>
                        <stop offset="100%" stop-color="#f8fbff"/>
                    </linearGradient>
                    <linearGradient id="loginPavement" x1="0" x2="1" y1="0" y2="1">
                        <stop offset="0%" stop-color="#cbd5e1"/>
                        <stop offset="100%" stop-color="#94a3b8"/>
                    </linearGradient>
                    <filter id="loginGlow" x="-30%" y="-30%" width="160%" height="160%">
                        <feGaussianBlur stdDeviation="5" result="blur"/>
                        <feMerge>
                            <feMergeNode in="blur"/>
                            <feMergeNode in="SourceGraphic"/>
                        </feMerge>
                    </filter>
                </defs>
                <rect width="1440" height="900" fill="url(#loginSky)"/>
                <g class="login-cloud slow" opacity="0.62">
                    <ellipse cx="210" cy="128" rx="82" ry="24" fill="#ffffff"/>
                    <ellipse cx="270" cy="119" rx="58" ry="28" fill="#ffffff"/>
                    <ellipse cx="330" cy="133" rx="76" ry="22" fill="#ffffff"/>
                </g>
                <g class="login-cloud" opacity="0.54">
                    <ellipse cx="1010" cy="156" rx="92" ry="26" fill="#ffffff"/>
                    <ellipse cx="1084" cy="144" rx="62" ry="34" fill="#ffffff"/>
                    <ellipse cx="1152" cy="160" rx="82" ry="24" fill="#ffffff"/>
                </g>
                <g class="login-cloud slow" opacity="0.38">
                    <ellipse cx="620" cy="92" rx="72" ry="18" fill="#ffffff"/>
                    <ellipse cx="672" cy="84" rx="46" ry="24" fill="#ffffff"/>
                    <ellipse cx="724" cy="96" rx="64" ry="18" fill="#ffffff"/>
                </g>
                <path d="M0 705 C210 652 356 670 545 700 C758 734 927 675 1146 655 C1267 644 1365 666 1440 700 L1440 900 L0 900 Z" fill="url(#loginPavement)" opacity="0.78"/>
                <path d="M0 772 C304 720 560 742 794 774 C1025 806 1248 786 1440 746 L1440 900 L0 900 Z" fill="#64748b" opacity="0.16"/>
                <g transform="translate(690 390) scale(0.84)">
                    <ellipse cx="470" cy="386" rx="520" ry="52" fill="#0f172a" opacity="0.18"/>
                    <path d="M74 126 L174 38 H776 L892 126 Z" fill="#f8fafc" stroke="#cbd5e1" stroke-width="5"/>
                    <path d="M174 38 H776 L838 86 H130 Z" fill="#ffffff"/>
                    <path d="M774 126 L892 126 L852 340 L734 328 Z" fill="#e2e8f0" stroke="#cbd5e1" stroke-width="4"/>
                    <path d="M86 126 H774 L734 328 H86 Z" fill="#ffffff" stroke="#cbd5e1" stroke-width="4"/>
                    <path d="M104 140 H762 L746 184 H98 Z" fill="#15803d"/>
                    <path d="M98 184 H746 L739 204 H94 Z" fill="#f97316"/>
                    <path d="M94 204 H739 L732 224 H90 Z" fill="#dc2626"/>
                    <path d="M762 140 L872 140 L864 181 L746 184 Z" fill="#116b34"/>
                    <path d="M746 184 L864 181 L860 200 L739 204 Z" fill="#df6414"/>
                    <path d="M739 204 L860 200 L856 220 L732 224 Z" fill="#b91c1c"/>
                    <path d="M48 214 H802 L760 258 H34 Z" fill="#f8fafc" stroke="#cbd5e1" stroke-width="4"/>
                    <path d="M58 226 H780 L760 246 H42 Z" fill="#15803d"/>
                    <path d="M42 246 H760 L752 258 H34 Z" fill="#f97316"/>
                    <rect x="132" y="252" width="170" height="82" rx="8" fill="#dbeafe" stroke="#93c5fd" stroke-width="3"/>
                    <rect x="324" y="252" width="154" height="82" rx="8" fill="#dbeafe" stroke="#93c5fd" stroke-width="3"/>
                    <rect x="500" y="252" width="154" height="82" rx="8" fill="#dbeafe" stroke="#93c5fd" stroke-width="3"/>
                    <path d="M706 242 H782 L764 340 H692 Z" fill="#e2e8f0" stroke="#94a3b8" stroke-width="3"/>
                    <path class="login-window-glow" d="M142 262 H292 V320 H142 Z" fill="#fff7ed" opacity="0.9"/>
                    <rect class="login-window-glow" x="334" y="262" width="134" height="58" rx="6" fill="#fff7ed" opacity="0.8"/>
                    <rect class="login-window-glow" x="510" y="262" width="134" height="58" rx="6" fill="#fff7ed" opacity="0.8"/>
                    <line x1="217" y1="252" x2="217" y2="334" stroke="#93c5fd" stroke-width="3"/>
                    <line x1="401" y1="252" x2="401" y2="334" stroke="#93c5fd" stroke-width="3"/>
                    <line x1="577" y1="252" x2="577" y2="334" stroke="#93c5fd" stroke-width="3"/>
                    <circle cx="756" cy="292" r="4" fill="#475569"/>
                    <rect x="318" y="48" width="304" height="96" rx="18" fill="#ffffff" stroke="#0f766e" stroke-width="6"/>
                    <rect class="login-sign-glow" x="338" y="68" width="264" height="58" rx="12" fill="#ffffff" filter="url(#loginGlow)"/>
                    <rect x="354" y="82" width="30" height="32" rx="4" fill="#15803d"/>
                    <rect x="390" y="82" width="30" height="32" rx="4" fill="#f97316"/>
                    <rect x="426" y="82" width="30" height="32" rx="4" fill="#dc2626"/>
                    <text x="542" y="111" fill="#0f172a" font-family="Arial, sans-serif" font-size="28" font-weight="900" text-anchor="middle">7-Eleven</text>
                    <path d="M74 340 H856" stroke="#94a3b8" stroke-width="6" stroke-linecap="round"/>
                    <path d="M130 374 H320 M402 374 H592 M672 374 H858" stroke="#ffffff" stroke-width="8" stroke-linecap="round" opacity="0.75"/>
                    <path d="M-18 330 L88 330 L50 376 L-58 376 Z" fill="#e2e8f0" opacity="0.9"/>
                    <rect x="-6" y="262" width="44" height="78" rx="6" fill="#ffffff" stroke="#cbd5e1" stroke-width="3"/>
                    <rect x="1" y="270" width="30" height="18" rx="3" fill="#15803d"/>
                    <rect x="1" y="292" width="30" height="10" fill="#f97316"/>
                    <rect x="1" y="305" width="30" height="10" fill="#dc2626"/>
                </g>
                <g opacity="0.4">
                    <circle cx="216" cy="606" r="54" fill="#ffffff"/>
                    <circle cx="1198" cy="628" r="68" fill="#ffffff"/>
                </g>
            </svg>
        </div>
        <div class="login-hero">
            <h1>FIELD PLANNER</h1>
            <div class="login-accent"></div>
            <p>Schedules, stores, assignments, and field operations in one place.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    login_tab, create_tab, reset_tab = st.tabs(["Sign In", "Create Account", "Forgot Password"])
    with login_tab:
        st.markdown('<div class="login-card"><h3>Welcome back</h3><p>Sign in to open your workspace.</p></div>', unsafe_allow_html=True)
        if first_account:
            st.info("No accounts exist yet. Create the first account to start.")
        with st.form("login_form"):
            username = st.text_input("Username or email", key="login_username")
            password = st.text_input("Password", type="password", key="login_password")
            login_submitted = st.form_submit_button("Sign In", type="primary")
        if login_submitted:
            user, login_message = authenticate_with_reason(username, password)
            if user:
                sign_in(user)
                st.cache_resource.clear()
                st.rerun()
            st.error(login_message or "Incorrect username or password.")
    with create_tab:
        st.markdown(
            '<div class="login-card"><h3>Create your workspace</h3><p>Enter your employee profile, home base, login, and recovery information.</p></div>',
            unsafe_allow_html=True,
        )
        with st.form("create_account_form"):
            st.markdown(
                '<div class="login-card"><h3>Section 1 - Employee Information</h3><p>Name, company position, and S Number.</p></div>',
                unsafe_allow_html=True,
            )
            n1, n2 = st.columns(2)
            first_name = n1.text_input("First name", key="create_first_name")
            last_name = n2.text_input("Last name", key="create_last_name")
            p1, p2 = st.columns(2)
            position_title = p1.text_input("Position / Job Title", key="create_position_title")
            s_number = p2.text_input("S Number / Employee Number", key="create_s_number")

            st.markdown(
                '<div class="login-card"><h3>Section 2 - Address / Home Base</h3><p>Street, city, state, and ZIP are stored separately.</p></div>',
                unsafe_allow_html=True,
            )
            street_address = st.text_input("Street Address", key="create_street_address")
            a1, a2, a3 = st.columns([2, 1, 1])
            city = a1.text_input("City", key="create_city")
            state = a2.text_input("State", max_chars=2, key="create_state")
            zip_code = a3.text_input("ZIP Code", key="create_zip_code")

            st.markdown(
                '<div class="login-card"><h3>Section 3 - Login Information</h3><p>Email, username, and password for signing in.</p></div>',
                unsafe_allow_html=True,
            )
            new_username = st.text_input("Username", key="create_username")
            new_email = st.text_input("Email", key="create_email")
            pw1, pw2 = st.columns(2)
            new_password = pw1.text_input("Password", type="password", key="create_password")
            confirm_password = pw2.text_input("Confirm Password", type="password", key="create_confirm_password")

            st.markdown(
                '<div class="login-card"><h3>Section 4 - Account Recovery</h3><p>Used only for password reset.</p></div>',
                unsafe_allow_html=True,
            )
            secret_question = st.selectbox("Secret question", SECRET_QUESTIONS, key="create_secret_question")
            secret_answer = st.text_input("Secret answer", type="password", key="create_secret_answer")
            st.markdown('<div class="login-note">Your account gets its own database, so another person can build schedules for their technicians without touching yours.</div>', unsafe_allow_html=True)
            create_submitted = st.form_submit_button("Create Account", type="primary")
        if create_submitted:
            if new_password != confirm_password:
                ok, message = False, "Passwords do not match."
            else:
                ok, message = create_user(
                    first_name,
                    last_name,
                    new_username,
                    new_email,
                    new_password,
                    secret_question,
                    secret_answer,
                    position_title=position_title,
                    s_number=s_number,
                    street_address=street_address,
                    city=city,
                    state=state,
                    zip_code=zip_code,
                )
            if ok:
                user = authenticate(new_username, new_password)
                if user:
                    sign_in(user)
                    st.cache_resource.clear()
                    st.rerun()
            if ok:
                st.success(message)
            else:
                st.error(message)
    with reset_tab:
        st.markdown('<div class="login-card"><h3>Reset password</h3><p>Enter your email, answer your secret question, then choose a new password.</p></div>', unsafe_allow_html=True)
        reset_email = st.text_input("Username or email address", key="reset_email")
        recovery_user = find_user_by_email(reset_email) if reset_email.strip() else None
        if reset_email.strip() and not recovery_user:
            st.info("Enter the username or email address used for your account.")
        saved_question = recovery_user.get("secret_question") if recovery_user else ""
        reset_question_options = SECRET_QUESTIONS if not saved_question or saved_question in SECRET_QUESTIONS else SECRET_QUESTIONS + [saved_question]
        reset_question_index = reset_question_options.index(saved_question) if saved_question in reset_question_options else 0
        with st.form("reset_password_form"):
            reset_secret_question = st.selectbox(
                "Secret question",
                reset_question_options,
                index=reset_question_index,
                key="reset_secret_question",
            )
            secret_answer_reset = st.text_input("Secret answer", type="password", key="reset_secret_answer")
            rp1, rp2 = st.columns(2)
            new_reset_password = rp1.text_input("New password", type="password", key="reset_new_password")
            confirm_reset_password = rp2.text_input("Confirm new password", type="password", key="reset_confirm_password")
            reset_submitted = st.form_submit_button("Reset Password", type="primary")
        if reset_submitted:
            if new_reset_password != confirm_reset_password:
                ok, message = False, "New passwords do not match."
            elif recovery_user and saved_question and reset_secret_question != saved_question:
                ok, message = False, "Secret question did not match the account."
            else:
                ok, message = reset_password_with_secret(reset_email, secret_answer_reset, new_reset_password)
            if ok:
                st.success(message)
            else:
                st.error(message)
    return False


def badge(status):
    color = STATUS_COLORS.get(status, "#475569")
    return f'<span class="status-badge" style="background:{color}">{status}</span>'


def save_upload(uploaded_file, folder=UPLOAD_DIR):
    folder.mkdir(exist_ok=True)
    clean = uploaded_file.name.replace("/", "_").replace("\\", "_")
    path = folder / f"{date.today().isoformat()}_{clean}"
    path.write_bytes(uploaded_file.getvalue())
    return path


def df_search(df, label="Search"):
    term = st.text_input(label)
    if not term or df.empty:
        return df
    mask = df.astype(str).apply(lambda col: col.str.contains(term, case=False, na=False)).any(axis=1)
    return df[mask]


def ensure_database_or_stop():
    from src.database import apply_automatic_schedule_completion, get_database_status, init_db, show_database_setup

    if not require_login():
        st.stop()
    status = get_database_status()
    if not status["configured"]:
        show_database_setup()
        st.stop()
    if not status["connected"]:
        st.error("Database connection failed.")
        st.code(status["error"] or "")
        st.stop()
    init_db()
    apply_automatic_schedule_completion()
