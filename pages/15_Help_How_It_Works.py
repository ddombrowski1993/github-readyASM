import streamlit as st

st.set_page_config(page_title="Help / How To", layout="wide")

from src.utils import apply_theme, ensure_database_or_stop, page_header, sidebar_nav


apply_theme()
sidebar_nav()
ensure_database_or_stop()


def help_box(title, body):
    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.write(body)


def bullet_list(items):
    for item in items:
        st.markdown(f"- {item}")


page_header(
    "Help / How To",
    "Fast guide for stores, assignments, schedules, reports, and manager review.",
)

st.info("Rule of thumb: Stores holds the master list. Areas and Maps owns assignments. Schedulers build from assignments. Reports and Dashboard show what needs attention.")

st.subheader("Start Here")
flow_cols = st.columns(4)
with flow_cols[0]:
    help_box("1. Stores", "Upload stores, fix addresses/coordinates, and keep active status clean.")
with flow_cols[1]:
    help_box("2. Employees", "Add people, roles, active status, home/base locations, and user cleanup.")
with flow_cols[2]:
    help_box("3. Areas and Maps", "Assign Brand, PMT, and Calibration stores. Use this for staffing changes.")
with flow_cols[3]:
    help_box("4. Schedulers", "Build schedules only after assignments and coordinates are ready.")

st.subheader("Where To Go")
where_cols = st.columns(2)
with where_cols[0]:
    help_box("Dashboard", "Daily status: scheduled work, missed work, follow-ups, weather, and setup problems.")
    help_box("Stores", "Master store list only. Assignment fields may show, but assignment changes belong in Areas and Maps.")
    help_box("Employees", "Employee profile, role, active/inactive status, home/base address, and PMT removal flow.")
    help_box("Areas and Maps", "Main assignment control center for Brand Enhancement, PMT, Calibration, rebalance, and replacement.")
with where_cols[1]:
    help_box("Schedulers", "Brand Enhancement, PMT Monthly, and Calibration schedules pull from Areas and Maps.")
    help_box("View Schedule", "Published schedules, completion status, rescheduled work, and schedule problems.")
    help_box("Follow-Ups", "Manual follow-ups, dropdown list management, open/overdue/completed tracking.")
    help_box("Reports", "Export management-ready summaries for assignments, workload, exceptions, follow-ups, and schedule completion.")

st.subheader("Most Used Tasks")
task_tabs = st.tabs(["Assignments", "PMT Changes", "Schedules", "Reports", "Fix Problems"])

with task_tabs[0]:
    st.markdown("**Assignment Layers**")
    bullet_list(
        [
            "**Brand Enhancement:** team/crew/area assignment.",
            "**PMT:** individual technician territory, normally routed from home/base.",
            "**Calibration:** individual technician territory, routed from home, base city, or manual start.",
            "Changing one layer does not overwrite the other layers.",
        ]
    )
    st.markdown("**Assign or rebalance stores**")
    bullet_list(
        [
            "Open **Areas and Maps**.",
            "Choose **Brand Enhancement**, **PMT**, or **Calibration**.",
            "Use upload, map selection, manual selection, or **Staffing Change & Territory Rebalance**.",
            "Preview changes, export if needed, check the reviewed box, then save.",
        ]
    )

with task_tabs[1]:
    st.markdown("**PMT reassignment from Employees**")
    bullet_list(
        [
            "Open **Employees** and mark the PMT inactive.",
            "Use **PMT Technician Removal & Territory Redistribution**.",
            "Pick unassigned, closest single tech, or split across nearby techs.",
            "Download **PMT Reassignment Preview Excel**.",
            "Workbook sheet: **Store Changes**.",
            "Compare **Original Technician** to **Newly Assigned Technician**.",
            "Check the reviewed box, then click **Apply PMT Reassignment**.",
        ]
    )
    st.markdown("**PMT rebalance from Areas and Maps**")
    bullet_list(
        [
            "Open **Areas and Maps -> PMT -> Staffing Change & Territory Rebalance**.",
            "Choose **Move stores to one selected technician** or **Spread stores across multiple nearby technicians**.",
            "For spread mode, select every PMT that should be allowed to receive stores.",
            "Generate the assignment preview.",
            "Use **All stores** when a new/zero-store PMT should receive stores from several nearby technicians.",
            "The auto-suggest avoids dropping one source technician too low unless that technician is truly overloaded.",
            "Download **Store Change Excel Export** at the bottom of the reassignment section.",
            "After saving, the same export remains at the bottom as the last saved reassignment export.",
            "Workbook sheet: **Report**.",
            "Compare **Original Technician** to **Newly Assigned Technician**.",
            "Check the reviewed box, then click **Save Assignment Plan**.",
            "To reverse the latest saved change, use **Undo Last Change** in the sidebar.",
        ]
    )

with task_tabs[2]:
    st.markdown("**Publish any schedule**")
    bullet_list(
        [
            "Select the assigned team or technician.",
            "Fix missing assignment or coordinate warnings.",
            "Set dates, workdays, capacity, and route options.",
            "Generate the draft.",
            "Review table, route/map, and exports.",
            "Check the reviewed box, then publish.",
        ]
    )
    st.markdown("**PMT Monthly Scheduler**")
    bullet_list(
        [
            "**Part 1:** build and publish monthly PMT route.",
            "**Part 2:** mark completed stores and review carryover/not scheduled.",
            "**Part 3:** adjust a published PMT schedule.",
            "**Part 4:** export PMT schedule, carryover, and not-scheduled details.",
            "PMT work counts completed at month end only when no exception was recorded.",
        ]
    )
    st.markdown("**PMT Pace**")
    bullet_list(
        [
            "PMT Pace compares scheduled PMT stores against completed PMT stores for the current month.",
            "If it says no completions are logged, go to **PMT Monthly Scheduler -> Part 2**.",
            "Select the run, PMT, and month, check completed stores, then click **Save Completed PMT Stores**.",
            "At month end, PMT work with no recorded exception can count as completed automatically.",
        ]
    )

with task_tabs[3]:
    st.markdown("**Use Reports when you need to explain what is happening.**")
    bullet_list(
        [
            "**Assignment / Workload:** who owns which stores and where workload is uneven.",
            "**Data Quality:** missing coordinates, missing assignments, bad setup records.",
            "**Schedule Completion:** completed, missed, delayed, duplicate, or open schedule work.",
            "**Follow-Up:** open, overdue, completed, vendor/person/company issues.",
            "**Manager Roll-Up:** combined view across managed workspaces.",
        ]
    )

with task_tabs[4]:
    quick_fixes = [
        ("No stores show up", "Upload stores in Stores and confirm you are in the right workspace."),
        ("Map is empty", "Stores need latitude/longitude. Fix coordinates in Stores or re-import with addresses."),
        ("Wrong stores imported", "If Excel is filtered, make sure hidden rows are actually hidden before upload."),
        ("PMT/Calibration tech has zero stores", "Use Areas and Maps -> staffing rebalance or manual assignment."),
        ("Cannot build schedule", "Fix assignments and coordinates first, then return to the scheduler."),
        ("PMT not scheduled is high", "Monthly capacity was lower than assigned workload. Review carryover before next cycle."),
        ("PMT pace shows no completions", "Mark completed stores in PMT Monthly Scheduler -> Part 2."),
        ("Manager cannot edit", "Switch from All Managed Users to one specific workspace in the sidebar."),
        ("Report is empty", "Check date range, scope, work group, and filters."),
        ("Duplicate schedule count", "A schedule may have been published twice. Delete the accidental duplicate run."),
    ]
    for problem, fix in quick_fixes:
        st.markdown(f"- **{problem}:** {fix}")

st.subheader("Details When Needed")
with st.expander("Imports"):
    bullet_list(
        [
            "Preview uploads before saving when possible.",
            "Bad rows go to review instead of crashing the app.",
            "Blank uploaded cells should not overwrite good existing data.",
            "Store imports can fill missing coordinates from addresses when possible.",
            "Multi-sheet PMT/Calibration workbooks scan for assignment and home-address sheets.",
            "Employee matching prefers S number/employee number, email, exact name, first/last, then phone.",
        ]
    )

with st.expander("Schedule Changes"):
    bullet_list(
        [
            "Use **Brand Enhancement Scheduler -> Schedule Adjustment Center** for rain, snow, call-off, pause/resume, deferred WO swaps, pull-forward, or manual changes.",
            "Flow: select schedule -> choose reason -> select date -> review original work -> preview revision -> confirm -> export.",
            "Completed stores stay locked. Unfinished stores move forward. The change is logged.",
            "Deferred WO swaps show what normal stores were pushed and which deferred work replaced them.",
        ]
    )

with st.expander("Follow-Ups"):
    bullet_list(
        [
            "Enter follow-ups manually from Follow-Ups.",
            "Use **Manage Follow-Up Dropdown Lists** for type, category, vendor, person, or company options.",
            "Old values stay visible on saved records even if removed from the active dropdown list.",
        ]
    )

with st.expander("Manager/Admin"):
    bullet_list(
        [
            "**User:** works in their own workspace.",
            "**Manager:** can review managed-user roll-ups and switch into one workspace to edit.",
            "**Admin:** can access Admin Controls.",
            "Manager roll-up is read-only. Switch to one specific workspace before editing assignments or downloading a store-change reassignment export.",
            "Use Admin Controls -> Account Storage Check if accounts disappear after sleep or redeploy.",
        ]
    )

st.subheader("Rules To Remember")
bullet_list(
    [
        "Assignments belong in **Areas and Maps**, not Stores.",
        "Brand Enhancement, PMT, and Calibration are separate assignment layers.",
        "Schedulers use existing assignments; do not re-upload assignment files unless data changed.",
        "Preview staffing/rebalance changes, check reviewed, then save.",
        "Use **Undo Last Change** in the sidebar for the latest saved schedule, staffing, or assignment change.",
        "Fix missing coordinates before trusting maps or routes.",
        "In manager roll-up mode, choose one workspace before editing.",
    ]
)
