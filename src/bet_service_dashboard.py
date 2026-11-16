"""Compact dashboard integration; detailed entry and mapping stay in the BET tracker."""
import logging
from datetime import date

import pandas as pd
import streamlit as st

from src.bet_service_store import load_bet_services
from src.bet_services import (working_today, service_status_rows, completion_report,
                              daily_productivity, reporting_range, team_progress)
from src.bet_service_reporting import render_progress_metrics


def render_bet_service_dashboard():
    with st.expander('BET Service Progress', expanded=False):
        today = working_today()
        year = today.year
        st.caption(f'{year} service cycle · explicit completions only. Scheduled work is not counted as completed.')
        try:
            data = load_bet_services(year)
        except Exception:
            logging.getLogger(__name__).exception('Dashboard BET service read failed')
            st.warning('BET service progress is temporarily unavailable. Open BET Service Tracker to retry.')
            st.page_link('pages/21_BET_Service_Tracker.py',label='Open BET Service Tracker')
            return
        rows = [r for r in service_status_rows(data) if r['bet_assigned'] and r['field_service']]
        render_progress_metrics(rows)
        summary = team_progress(rows)
        st.dataframe(pd.DataFrame([{'BET Technician / Team': r['owner_label'], 'Assigned Stores': r['assigned_stores'],
            'Power Washed': r['power_washing'], 'Lot Striped': r['lot_striping'], 'Both Complete': r['both_complete'],
            'Services Completed': r['total_services_completed'], 'Remaining Services': r['remaining_services']} for r in summary]),
            use_container_width=True,hide_index=True)
        period = st.selectbox('BET Completion Report', ['Today','This Week','This Month','Current BET Cycle','Custom Date Range'],key='dashboard_bet_period')
        custom = None
        if period == 'Custom Date Range':
            a,b = st.columns(2)
            start = a.date_input('BET From',date(year,1,1),key='dashboard_bet_from')
            end = b.date_input('BET Through',today,key='dashboard_bet_through')
            if end < start:
                st.error('The end date must be on or after the start date.')
                return
            custom = (start,end)
        start,end = reporting_range(period,today,year,custom)
        records = completion_report(data,start,end)
        st.write(f'**{len(records)} services completed across {len({r["store_id"] for r in records})} stores** in this reporting period.')
        st.caption('Historical credit below stays with the technician/team who did the work, even after territory changes.')
        st.dataframe(pd.DataFrame(daily_productivity(records)),use_container_width=True,hide_index=True)
        st.page_link('pages/21_BET_Service_Tracker.py',label='Open BET Service Tracker — store drilldowns, entry, map and history')
