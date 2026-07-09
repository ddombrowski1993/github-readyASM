import streamlit as st

st.set_page_config(page_title="Help / How To", layout="wide")

from src.utils import apply_theme, ensure_database_or_stop, page_header, sidebar_nav


apply_theme()
sidebar_nav()
ensure_database_or_stop()


def help_box(title, body, color="#2563eb"):
    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.write(body)


def step_box(number, title, body):
    with st.container(border=True):
        st.markdown(f"**Step {number}: {title}**")
        st.write(body)


page_header(
    "Help / How To",
    "The clean one-stop workflow for stores, assignments, schedules, reports, and manager review.",
)

st.info(
    "Simple rule: Stores is the master list. Areas and Maps is the assignment control center. Schedulers build from those assignments. Reports, Dashboard, and Manager Roll-Up explain what is happening."
)

st.subheader("Main Workflow")
for number, title, body in [
    ("1", "Stores", "Upload and maintain the master store list: store number, address, city, state, ZIP, latitude, longitude, active status, and missing-coordinate review."),
    ("2", "Employees", "Add or maintain people, roles, active status, home/base locations, and account/user cleanup. PMT uses home/base location. Calibration can use home address, main nearby city, or manual scheduler start point."),
    ("3", "Areas and Maps", "Assign stores to Brand Enhancement, PMT, and Calibration. This is the main assignment control center and the place to fix staffing changes."),
    ("4", "Schedulers", "Open the matching scheduler. Brand Enhancement, PMT Monthly, and Calibration each pull from the assignment layer already built in Areas and Maps."),
    ("5", "View Schedule", "Review published work, completion status, rescheduled work, and schedule problems."),
    ("6", "Follow-Ups", "Add follow-up items manually, manage dropdown lists, and review open, overdue, and completed issues."),
    ("7", "Reports", "Use reports for assignment readiness, missing data, workload balance, territory size, follow-ups, schedule completion, and manager updates."),
    ("8", "Dashboard / Manager Roll-Up", "Use the Dashboard for daily readiness and Manager Roll-Up for combined views across managed workspaces."),
]:
    step_box(number, title, body)

st.subheader("Where Work Belongs")
cols = st.columns(2)
with cols[0]:
    help_box("Stores", "Master store database only. Upload, clean, activate/deactivate, fix coordinates, and export stores. Assignment columns may be visible for reference, but Stores is not the main assignment page.")
    help_box("Employees", "Employee records, roles, active/inactive status, home address, home coordinates, main nearby city for Calibration, and account/user cleanup.")
    help_box("Areas and Maps", "Assignment control center. Choose Brand Enhancement, PMT, or Calibration; upload assignment/address files; assign manually; rebalance technicians; export assignments; refresh map areas.")
    help_box("Follow-Ups", "Manual follow-up entry, editable dropdown lists for follow-up type/category/vendor, attachments, and open/overdue/completed follow-up review.")
with cols[1]:
    help_box("Brand Enhancement Scheduler", "Team/crew/area based. Select an assigned Brand area, validate stores, configure schedule settings, generate, review, and publish.")
    help_box("PMT Monthly Scheduler", "Technician/home-address based. Uses PMT assignments from Areas and Maps, technician home/base location, monthly targets, completion tracking, carryover, stores that did not fit, and route options.")
    help_box("Calibration Scheduler", "Technician based with flexible start point: home address, assigned base/main city, or manual start location.")
    help_box("Reports", "Readiness and management view: assignments, workload balance, territory size, missing coordinates, follow-ups, schedule completion, and manager roll-up.")

st.subheader("Assignment Layers")
with st.expander("Brand Enhancement, PMT, and Calibration stay separate", expanded=True):
    st.markdown(
        """
        A store can have three different assignment layers at the same time:

        - **Brand Enhancement Team:** area or crew assignment.
        - **PMT Technician:** individual technician assignment, normally routed from home/base location.
        - **Calibration Technician:** individual technician assignment, routed from home, main nearby city, or manual start point.

        Editing PMT does not overwrite Brand Enhancement or Calibration. Editing Calibration does not overwrite PMT or Brand Enhancement. Brand Enhancement remains team/area based.
        """
    )

st.subheader("Areas and Maps")
area_tabs = st.tabs(["Brand Enhancement", "PMT", "Calibration", "Staffing Changes"])
with area_tabs[0]:
    st.markdown(
        """
        Use **Brand Enhancement** for teams, crews, areas, and area-based assignment.

        Typical flow:

        - Create or select a Brand Enhancement area/team.
        - Upload assignments if you already have them, or use map tools/manual assignment.
        - Review assigned and unassigned stores.
        - Refresh map areas if needed.
        - Open the Brand Enhancement Scheduler.
        """
    )
with area_tabs[1]:
    st.markdown(
        """
        Use **PMT** for individual technician territories.

        PMT depends on:

        - Assigned PMT stores.
        - PMT technician home/base address.
        - Store coordinates.
        - Technician coordinates when using auto-suggest or routing.

        The PMT tab can detect technicians with zero stores, suggest nearby stores, pull stores from overloaded technicians, and preview reassignment before saving.
        """
    )
with area_tabs[2]:
    st.markdown(
        """
        Use **Calibration** for individual technician territories with flexible routing.

        Calibration technician setup only needs the fields required for assignment and routing:

        - Technician name.
        - City they live in.
        - Main nearby city.
        - Optional home address.

        The Calibration Scheduler then chooses the routing start point: home address, assigned base/main city, or manual start location.
        """
    )
with area_tabs[3]:
    st.markdown(
        """
        Use **Staffing Change & Territory Rebalance** when someone is hired, leaves, transfers, or has too many stores.

        Available actions:

        - Detect active technicians with zero assigned stores.
        - Auto-suggest stores for a new technician.
        - Pull stores from overloaded technicians.
        - Pull stores from one selected technician, such as a temporary coverage person.
        - Preview proposed store changes before saving.
        - Export the preview.
        - Deactivate or replace a technician with typed confirmation.
        - Keep manual polygon/rectangle map assignment available for business-specific decisions.

        Rebalance changes are logged and only update the selected work group layer.
        """
    )

st.subheader("Imports: What To Expect")
with st.expander("Store, Employee, and Assignment Imports", expanded=True):
    st.markdown(
        """
        - Uploads should show a preview before saving when possible.
        - Bad rows should go to review instead of crashing the app.
        - Filtered Excel store sheets should import only visible filtered rows.
        - Blank sheets, bad headers, missing columns, and messy files should show friendly messages.
        - Store imports can fill missing coordinates from addresses and missing addresses from coordinates when possible.
        - Existing good data should not be overwritten by blank uploaded cells.
        - Multi-sheet PMT/Calibration assignment workbooks should scan for assignment and home-address sheets instead of blindly using the first sheet.
        - Extra employee columns such as employee number, phone, email, manager, or notes should not break address detection.
        - Matching should prefer employee number/S number, email, exact name, first/last name, phone, then review rows when not confident.
        """
    )

with st.expander("Follow-Ups", expanded=True):
    st.markdown(
        """
        Follow-Ups are entered manually from the Follow-Ups page.

        Use **Manage Follow-Up Dropdown Lists** to add or deactivate workspace-specific options for:

        - Follow-up Type
        - Category
        - Vendor / Person / Company

        Existing built-in options stay available. Values already used on saved follow-ups also remain available so old records still make sense.
        """
    )

st.subheader("Schedulers And Schedule Changes")
with st.expander("Publishing Schedules", expanded=True):
    st.markdown(
        """
        Brand Enhancement, PMT, and Calibration schedulers follow the same publish pattern:

        - Select the assigned work group or technician.
        - Validate assignments and missing coordinate warnings.
        - Configure dates, work days, capacity, and route options.
        - Generate a draft.
        - Review the draft table, route/map, and exports.
        - Check **I have reviewed this schedule and confirm I am ready to publish it**.
        - Publish the schedule.

        The app blocks duplicate schedule publishing when the same person/team already has open schedule items for the same stores and dates. PMT also checks technician/store/month so an accidental second click does not duplicate a monthly run.

        If a scheduled work day passes without a recorded delay, pause, call-off, cancellation, or other exception, the app automatically considers that work **Completed**. PMT work follows the same rule by month: when the month passes with no recorded exception, the PMT work for that month counts as **Completed**.
        """
    )

with st.expander("Schedule Adjustment Center", expanded=True):
    st.markdown(
        """
        Use **Brand Enhancement Scheduler -> Schedule Adjustment Center** when a published schedule changes after the plan was created.

        Common reasons:

        - Rain delay.
        - Snow delay or freezing weather.
        - Crew call-off.
        - Team unavailable.
        - Pause or resume schedule.
        - Deferred work orders assigned/scheduled instead of normal scheduled stores.
        - Pull future work forward.
        - Manual schedule adjustment.

        The workflow is:

        **Select Published Schedule -> Choose What Happened -> Select Affected Date -> Review Original Work -> Preview Revised Schedule -> Confirm -> Export Revised Schedule / Change Log**

        The app keeps completed stores locked, pushes unfinished stores forward, recalculates the estimated completion date, and records the change in the schedule history/audit log.
        """
    )

with st.expander("PMT Monthly Scheduler: Completion, Carryover, And Rotation", expanded=True):
    st.markdown(
        """
        PMT is monthly, so it works differently than daily Brand Enhancement or Calibration schedules.

        The PMT page is split into four parts:

        - **Part 1 - Build A New PMT Monthly Schedule:** choose the PMT, schedule months, monthly target, route options, then generate and publish the schedule.
        - **Part 2 - Track PMT Completion, Carryover, And Stores That Did Not Fit:** review what was scheduled, what was completed, what missed, and what did not fit into the selected months.
        - **Part 3 - Manage A Published PMT Schedule:** adjust a published PMT schedule when stores were missed or need to move.
        - **Part 4 - Export PMT Schedules:** export the schedule, carryover, and not-scheduled details.

        PMT status meanings:

        - **Scheduled:** the store is on the current PMT route/month.
        - **Completed:** the store was marked complete, or the PMT month ended with no recorded exception.
        - **Not Completed:** the store was scheduled but was missed and should be reviewed.
        - **Carryover:** the store was missed or pushed and should be prioritized in the next cycle.
        - **Not Scheduled / Did Not Fit:** the store is assigned to the PMT, but there was not enough schedule capacity in the selected months to include it.
        - **Overdue / Skipped:** the store has been missed too long or intentionally skipped and needs review.

        To mark PMT stores completed during the current month:

        1. Open **PMT Monthly Scheduler**.
        2. Go to **Part 2 - Track PMT Completion, Carryover, And Stores That Did Not Fit**.
        3. Use **Mark PMT Stores Completed**.
        4. Select the published run, PMT, and month.
        5. Check the stores that were completed.
        6. Click **Save Completed PMT Stores**.

        If no stores are marked complete during the current month, the Dashboard may show **No completions logged yet** for PMT pace. That means PMTs are scheduled, but completion has not been recorded yet. At month end, PMT work with no recorded exception counts as **Completed**.

        Next PMT schedules prioritize work in this order: stores that did not fit, carryover stores, not completed stores, stores never completed, stores with the oldest completion date, then route distance from the PMT home/base location.
        """
    )

with st.expander("Deferred WO Swaps", expanded=True):
    st.markdown(
        """
        If a crew needs deferred work scheduled instead of normal Brand Enhancement stores:

        - Choose **Deferred Work Orders Completed Instead** in Schedule Adjustment Center.
        - Select the affected date/team.
        - Select the deferred WOs to assign/schedule for that date.
        - Preview the normal stores that will be pushed.
        - Confirm the revision.

        Revised schedule exports, daily work summaries, and deferred WO swap exports show what changed and why. Mark the deferred WO complete from the Deferred Work Orders page after the work is actually finished.

        Different deferred work orders at the same store are allowed. They are tracked by work order details, not treated as duplicate store schedule errors.
        """
    )

st.subheader("Manager And Admin Access")
with st.expander("Roles", expanded=True):
    st.markdown(
        """
        - **User:** Full operational access for their own workspace, but no Admin Controls.
	    - **Manager:** User access plus roll-up views for assigned managed users.
	    - **Admin:** User access plus Admin Controls.

	    In **All Managed Users** mode, managers can review combined data. Build, edit, upload, publish, or delete tools may be hidden when the action requires one specific workspace.

        Manager roll-up includes active stores, employees, scheduled work, completed work, open/overdue follow-ups, deferred WOs, missing coordinates, unassigned stores, duplicate open schedule items, paused schedules, PMT home-location gaps, and zero-store PMT/Calibration technicians.
	    """
    )

with st.expander("Account Storage Check", expanded=True):
    st.markdown(
        """
        User accounts should not disappear just because the website goes to sleep. The Admin Controls page now includes an **Account Storage Check** section that shows where login accounts and workspace databases are being saved.

        Use this check if someone creates an account and later sees “no account exists.”

        - For local desktop use, local SQLite storage is expected.
        - For hosted deployments, local app-folder SQLite files can be lost after rebuilds, redeploys, or container replacement.
        - If the check warns about local app-folder storage on hosted Streamlit, move account/database storage to persistent external storage before relying on it for the director demo.
        """
    )

st.subheader("How To Review Work")
tabs = st.tabs(["Daily", "Weekly", "Monthly"])
with tabs[0]:
    st.markdown(
        """
        Daily review:

        - Start on Dashboard.
        - Check scheduled work, employees off today, open follow-ups, overdue follow-ups, weather concerns, and schedule problems.
        - Open Follow-Ups or View Schedule to fix what needs attention.
        """
    )
with tabs[1]:
    st.markdown(
        """
        Weekly review:

        - Use Reports for Schedule Completion, Follow-Up, Exception / Problem, and Team Performance reports.
        - Look for overdue follow-ups, missed work, PM completion gaps, deferred WOs, and data quality issues.
        - Export PDF for sharing or Excel for filtering.
        """
    )
with tabs[2]:
    st.markdown(
        """
        Monthly review:

        - Check PMT monthly progress, Brand Enhancement area progress, Calibration progress, and assignment balance.
        - Review PMT stores that did not fit, carryover stores, and not completed stores before building the next PMT cycle.
        - Use Workload / Store Assignment and Data Quality reports before building new schedules.
        - Adjust assignments in Areas and Maps when workloads are uneven.
        - Use Staffing Change & Territory Rebalance when a technician has zero stores, too many stores, or has left/transferred.
        """
    )

st.subheader("Important Rules")
st.markdown(
    """
    - Assignments belong in **Areas and Maps**, not Stores.
    - Brand Enhancement, PMT, and Calibration assignments are separate.
    - Use the scheduler that matches the work group.
    - Schedulers should use assignments already created in Areas and Maps; do not re-upload the same assignment file unless you are changing the assignment data.
    - Rebalance and replacement changes must be previewed and confirmed before saving.
    - PMT routing starts from technician home/base location.
    - PMT is monthly: current-month work is not automatically complete until it is marked complete or the month ends with no exception.
    - PMT stores that do not fit into the selected schedule months should be reviewed before the next cycle.
    - Calibration routing can start from home address, main/base city, or manual start location.
    - Fix missing coordinates before relying on maps or routes.
    - Completed follow-ups do not count as open.
    - Reports are for analysis, not just raw exports.
    - In manager roll-up mode, select one specific workspace before editing or building schedules.
    - Bad upload data should create warnings or review rows, not Python errors.
    """
)

st.subheader("Quick Fix Guide")
quick_fixes = [
    ("No stores show up", "Go to Stores and upload stores for the current workspace. Check that you are not viewing the wrong user workspace."),
    ("Wrong stores imported", "If the Excel file is filtered, make sure rows are actually filtered/hidden in Excel before uploading."),
    ("Map is empty", "Stores need valid latitude and longitude. Re-import with addresses or fix coordinates in Stores."),
    ("PMT or Calibration tech has zero stores", "Open Areas and Maps, choose PMT or Calibration, then use Staffing Change & Territory Rebalance to auto-suggest or manually assign stores."),
    ("PMT Pace says no completions logged yet", "The current month has PMT stores scheduled, but no stores have been marked completed yet. Open PMT Monthly Scheduler -> Part 2 -> Mark PMT Stores Completed."),
    ("PMT Stores Not Scheduled is high", "Those stores are assigned to PMTs but did not fit into the selected schedule months based on monthly target/capacity. Review them before building the next PMT cycle."),
    ("Calibration route starts from the wrong place", "Open the Calibration Scheduler and choose the routing start point: home address, assigned base city, or manual start location."),
    ("Cannot build a schedule", "Check assignments first in Areas and Maps, confirm technician/store coordinates, then use the scheduler validation section."),
    ("Employee import says account/address is missing", "Check the detected sheet and mapped columns. Extra columns are okay, but the app still needs a confident person identity and address/city/state fields."),
    ("Account disappeared after sleep/redeploy", "Open Admin Controls -> Account Storage Check and verify the account database path is persistent for the environment."),
    ("Duplicate schedule count appears", "A schedule was probably published twice. Delete the accidental duplicate run, then use the scheduler guard message to avoid publishing the same stores/dates again."),
    ("Manager cannot edit", "Switch from All Managed Users to one specific workspace in the sidebar."),
    ("Report looks empty", "Check the date range, scope, work group, and filters."),
]
for problem, fix in quick_fixes:
    st.markdown(f"- **{problem}:** {fix}")
