"""BET cycle progress drilldowns and historical-credit reporting."""
from datetime import date
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from src.bet_services import (SERVICES, WORK_TIMEZONE, completion_report, daily_productivity,
    reporting_range, service_status_rows, service_totals, team_progress, working_today)

STATUS_FILTERS = ["All", "No Services Complete", "Power Wash Only", "Lot Striping Only", "Both Complete",
                  "Power Washing Completed", "Lot Striping Completed", "Remaining Services",
                  "Power Washing Remaining", "Lot Striping Remaining"]


def filter_status(rows, status):
    def matches(row):
        pw, ls = row['power_washing_complete'], row['lot_striping_complete']
        return {
            'All': True, 'No Services Complete': not pw and not ls,
            'Power Wash Only': pw and not ls, 'Lot Striping Only': ls and not pw,
            'Both Complete': pw and ls, 'Power Washing Completed': pw, 'Lot Striping Completed': ls,
            'Remaining Services': row['remaining_services'] > 0,
            'Power Washing Remaining': row['power_washing_applicable'] and not pw,
            'Lot Striping Remaining': row['lot_striping_applicable'] and not ls,
        }[status]
    return [r for r in rows if matches(r)]


def status_table(rows):
    columns = ['Store', 'Address', 'City / State', 'Current BET Technician', 'Current BET Team / Area',
               'Power Wash Status', 'Power Wash Date', 'Power Wash Credited To',
               'Lot Striping Status', 'Lot Striping Date', 'Lot Striping Credited To', 'Total Services Completed']
    result = []
    for r in rows:
        def state(s):
            return 'Completed' if r[s+'_complete'] else 'Still Needed' if r[s+'_applicable'] else 'Not Applicable'
        result.append(dict(zip(columns, [r['store_number'], r['address'] or '',
            ', '.join(str(r[k] or '') for k in ['city','state']).strip(', '),
            r['bet_employee_name'] or 'Team-based assignment', r['bet_team_name'],
            state('power_washing'), r['power_washing_date'], r['power_washing_credited_to'],
            state('lot_striping'), r['lot_striping_date'], r['lot_striping_credited_to'], r['total_services_completed']])))
    return pd.DataFrame(result, columns=columns)


def history_table(records):
    rows = []
    for r in records:
        stamp = r['completed_at']
        if stamp and stamp.tzinfo is None:
            from datetime import timezone
            stamp = stamp.replace(tzinfo=timezone.utc)
        rows.append({"ID": r['id'], "Store": r['store_number'], "Service": SERVICES[r['service_type']],
            "Service Year": r['service_year'], "Completed Date": r['completed_date'],
            "Entered Timestamp (Eastern)": stamp.astimezone(ZoneInfo(WORK_TIMEZONE)).isoformat() if stamp else '',
            "Credited BET Technician": r['bet_employee_name'], "Credited BET Team / Area": r['bet_team_name'],
            "Entered By": r['entered_by'], "Source": r['source'], "Work Order": r['work_order_number'],
            "Notes": r['notes'], "Entry Status": 'Active' if r['active'] else 'Voided',
            "Voided By": r['voided_by'], "Voided At": r['voided_at'], "Correction Reason": r['void_reason']})
    return pd.DataFrame(rows)


def render_progress_metrics(rows):
    totals = service_totals(rows)
    cols = st.columns(4)
    cols[0].metric('Power Washing', f"{totals['power_washing_applicable_completed']} / {totals['power_washing_expected']}")
    cols[1].metric('Lot Striping', f"{totals['lot_striping_applicable_completed']} / {totals['lot_striping_expected']}")
    cols[2].metric('Both Services Complete', totals['both_complete'])
    cols[3].metric('Total Services Completed', totals['total_services_completed'])
    st.caption(f"{totals['assigned_stores']} current BET stores · {totals['remaining_services']} applicable services remaining. Store counts and service counts are separate.")


def render_overview(data):
    rows = [r for r in service_status_rows(data) if r['bet_assigned'] and r['field_service']]
    st.subheader('Current Territory Progress')
    st.caption('Current assignments define workload. A completed service stays complete for this year after reassignment; historical worker credit is shown separately below.')
    render_progress_metrics(rows)
    group_by = st.radio('Group progress by', ['BET Team / Area', 'BET Technician'], horizontal=True)
    summary = team_progress(rows, by='technician' if group_by == 'BET Technician' else 'team')
    table = pd.DataFrame([{'BET Technician / Team': r['owner_label'], 'Assigned Stores': r['assigned_stores'],
        'Power Washed': r['power_washing'], 'Lot Striped': r['lot_striping'], 'Both Complete': r['both_complete'],
        'Total Services Completed': r['total_services_completed'], 'Remaining Services': r['remaining_services']} for r in summary])
    st.dataframe(table, use_container_width=True, hide_index=True)
    for team in summary:
        with st.expander(f"{team['owner_label']} — {team['assigned_stores']} assigned stores"):
            owned = [r for r in rows if r['technician_key' if group_by == 'BET Technician' else 'owner_key'] == team['owner_key']]
            render_progress_metrics(owned)
            for service, label in SERVICES.items():
                expected = team[service + '_expected']
                applicable_done = team[service + '_applicable_completed']
                ratio = applicable_done / expected if expected else 0
                st.write(f"{label}: {applicable_done} / {expected} ({ratio:.1%}) · {team[service + '_remaining']} remaining")
            st.write(f"Services completed: {team['applicable_services_completed']} / {team['expected_services']} applicable services")
            show = st.selectbox('Show stores behind these counts', STATUS_FILTERS, key='bet_drill_' + team['owner_key'])
            st.dataframe(status_table(filter_status(owned, show)), use_container_width=True, hide_index=True)
    st.subheader('Completed Today — Historical Work Credit')
    st.caption('Credit follows the technician/team recorded at completion, even if ownership later changes.')
    today = working_today()
    records = completion_report(data, today, today)
    st.dataframe(pd.DataFrame(daily_productivity(records)), use_container_width=True, hide_index=True)
    st.subheader('Work Credited During This Service Year')
    records = [r for r in data['completions'] if r['active']]
    st.dataframe(pd.DataFrame(daily_productivity(records)), use_container_width=True, hide_index=True)
    with st.expander('Stores behind historical credit totals'):
        names = sorted({r['bet_employee_name'] or r['bet_team_name'] or 'Unassigned' for r in records})
        credit = st.selectbox('Credited BET technician / team', ['All'] + names, key='bet_credit_drill')
        service = st.selectbox('Credited service', ['All'] + list(SERVICES.values()), key='bet_credit_service')
        records = [r for r in records if (credit == 'All' or (r['bet_employee_name'] or r['bet_team_name'] or 'Unassigned') == credit)
                   and (service == 'All' or SERVICES[r['service_type']] == service)]
        st.dataframe(history_table(records), use_container_width=True, hide_index=True)


def render_history(data):
    rows = service_status_rows(data)
    st.subheader('Current-Cycle Store Status')
    current = st.columns(3)
    owners = {r['owner_key']: r['owner_label'] for r in rows}
    owner = current[0].selectbox('Current BET Technician / Team', ['All'] + sorted(owners), format_func=lambda key: owners.get(key,key))
    status = current[1].selectbox('Completion Status', STATUS_FILTERS)
    number = current[2].text_input('Store Number', key='bet_history_store').strip()
    selected = [r for r in rows if (owner == 'All' or r['owner_key'] == owner)
                and (not number or number.casefold() in str(r['store_number']).casefold())]
    selected = filter_status(selected,status)
    technicians = {r['technician_key']: r['technician_label'] for r in rows}
    technician = st.selectbox('Current BET Technician', ['All'] + sorted(technicians), format_func=lambda key:technicians.get(key,key))
    if technician != 'All':
        selected = [r for r in selected if r['technician_key'] == technician]
    service_filter = st.selectbox('Service', ['All', *SERVICES.values()], key='bet_status_service')
    if service_filter != 'All':
        service = next(s for s,label in SERVICES.items() if label == service_filter)
        selected = [r for r in selected if r[service + '_applicable'] or r[service + '_complete']]
    table = status_table(selected)
    st.dataframe(table, use_container_width=True, hide_index=True)
    st.download_button('Export Store Service Status', table.to_csv(index=False), file_name=f"bet_service_status_{data['year']}.csv", mime='text/csv')
    st.subheader('Completion History / Audit')
    st.caption('Date filters below apply to explicit completion events. The cycle-status table above always shows the selected year.')
    period = st.selectbox('Completion Date Range', ['Today','This Week','This Month','Current BET Cycle','Custom Date Range'], index=3)
    today = working_today()
    custom = None
    if period == 'Custom Date Range':
        a,b = st.columns(2)
        start = a.date_input('From', date(data['year'],1,1))
        end = b.date_input('Through', min(today,date(data['year'],12,31)))
        if end < start:
            st.error('The end date must be on or after the start date.')
            return
        custom = (start,end)
    start,end = reporting_range(period,today,data['year'],custom)
    include_voided = st.checkbox('Include voided entries / corrections', value=True)
    records = completion_report(data,start,end,include_voided)
    names = sorted({r['bet_employee_name'] or r['bet_team_name'] or 'Unassigned' for r in records})
    credit = st.selectbox('Historical Credited BET Technician / Team', ['All'] + names)
    service = st.selectbox('Historical Service', ['All'] + list(SERVICES.values()))
    records = [r for r in records if (credit == 'All' or (r['bet_employee_name'] or r['bet_team_name'] or 'Unassigned') == credit)
        and (service == 'All' or SERVICES[r['service_type']] == service)
        and (not number or number.casefold() in r['store_number'].casefold())]
    if owner != 'All':
        store_ids = {r['id'] for r in rows if r['owner_key'] == owner}
        records = [r for r in records if r['store_id'] in store_ids]
    st.dataframe(pd.DataFrame(daily_productivity(records)), use_container_width=True, hide_index=True)
    history = history_table(records)
    st.dataframe(history, use_container_width=True, hide_index=True)
    st.download_button('Export Completion History', history.to_csv(index=False), file_name=f"bet_service_history_{data['year']}.csv", mime='text/csv')
