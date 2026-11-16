"""A completion overlay on existing BET ownership, never a second territory model."""
from html import escape
import math
import re

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from src.bet_services import SERVICES, service_status_rows
from src.bet_service_reporting import STATUS_FILTERS, filter_status
from src.bet_service_ui import render_completion_form
from src.maps import add_area_overlays, map_html

# Shared unchanged with Areas and Maps. No completion status appears in this palette.
ASSIGNMENT_COLORS = {
    "unassigned": "#9ca3af", "current_area": "#16a34a", "selected": "#f59e0b",
    "other_same_group": "#dc2626", "different_group": "#2563eb",
}


def assignment_status_color(status):
    return ASSIGNMENT_COLORS.get(status, "#2563eb")


def completion_halo_color(row, mode="Overall Completion"):
    if mode in SERVICES.values():
        service = next(key for key, label in SERVICES.items() if label == mode)
        return "#16a34a" if row.get(service + "_complete") else None
    count = sum(bool(row.get(s + "_complete")) for s in SERVICES)
    return "#16a34a" if count == 2 else "#eab308" if count == 1 else None


def add_completion_halo(fmap, row, mode="Overall Completion", inner_radius=5):
    color = completion_halo_color(row, mode)
    if not color:
        return None
    ring = folium.CircleMarker([float(row['latitude']),float(row['longitude'])], radius=inner_radius+5,
        color=color, weight=3, fill=False, opacity=1, interactive=False)
    ring.add_to(fmap)
    return ring


def completion_popup(row):
    text = []
    for service, label in SERVICES.items():
        if row.get(service + '_complete'):
            when = row.get(service + '_date')
            text.append(f"{label}: Completed {when:%m/%d/%Y}" if when else f"{label}: Completed")
            credit = row.get(service + '_credited_to')
            if credit:
                text.append('Credited to: ' + str(credit))
        else:
            text.append(label + (': Not Completed' if row.get(service + '_applicable', True) else ': Not Applicable'))
    return '<br>'.join(escape(value) for value in text)


def add_completion_legend(fmap, mode="Overall Completion"):
    description = ('No ring = No services completed<br><span style="color:#a16207">●</span> Yellow ring = One service completed<br>'
                   '<span style="color:#16a34a">●</span> Green ring = Both services completed') if mode == 'Overall Completion' else (
                   f'No ring = {escape(mode)} remaining<br><span style="color:#16a34a">●</span> Green ring = {escape(mode)} completed')
    fmap.get_root().html.add_child(folium.Element(
        '<div style="position:fixed;bottom:30px;left:16px;z-index:9999;background:white;padding:12px;border:1px solid #cbd5e1;border-radius:6px">'
        '<b>BET Service Status</b><br>' + description + '</div>'))


def build_service_map(rows, areas=(), mode="Overall Completion", selected_team_id=None):
    valid = [r for r in rows if r.get('latitude') is not None and r.get('longitude') is not None
             and math.isfinite(float(r['latitude'])) and math.isfinite(float(r['longitude']))
             and -90 <= float(r['latitude']) <= 90 and -180 <= float(r['longitude']) <= 180]
    if not valid:
        return None
    center = [sum(float(r[k]) for r in valid)/len(valid) for k in ('latitude','longitude')]
    fmap = folium.Map(location=center, zoom_start=14 if len(valid)==1 else 8, tiles='OpenStreetMap')
    add_area_overlays(fmap, pd.DataFrame(areas))
    for row in valid:
        # Match Areas & Maps' Brand Enhancement assignment state/color exactly.
        state = 'current_area' if selected_team_id and row['bet_team_id']==selected_team_id else 'other_same_group' if row['bet_team_id'] else 'unassigned'
        radius = 7 if state == 'current_area' else 5
        add_completion_halo(fmap,row,mode,inner_radius=radius)
        text = [f"Store {row['store_number']}", row.get('address') or 'Address unavailable',
                ', '.join(str(row.get(k) or '') for k in ('city','state')).strip(', '),
                'BET Technician: ' + (row['bet_employee_name'] or 'Team-based assignment'),
                'BET Team / Area: ' + (row['bet_team_name'] or 'Unassigned')]
        popup = f'<span data-bet-store-id="{int(row["id"])}"></span>' + '<br>'.join(escape(t) for t in text)
        popup += '<br><br>' + completion_popup(row) + '<br><br>Use Service Changes below the map.'
        folium.CircleMarker([float(row['latitude']),float(row['longitude'])], radius=radius,
            color='#ffffff', weight=2 if state=='current_area' else 1, fill=True,
            fill_color=assignment_status_color(state), fill_opacity=.92,
            tooltip=f"Store {row['store_number']} — {row['owner_label']}", popup=folium.Popup(popup,max_width=360)).add_to(fmap)
    if len(valid)>1:
        fmap.fit_bounds([[min(float(r['latitude']) for r in valid),min(float(r['longitude']) for r in valid)],
                         [max(float(r['latitude']) for r in valid),max(float(r['longitude']) for r in valid)]])
    add_completion_legend(fmap,mode)
    fmap.get_root().html.add_child(folium.Element(
        '<div style="position:fixed;top:14px;right:14px;z-index:9999;background:white;padding:12px;border:1px solid #cbd5e1;border-radius:6px">'
        '<b>BET Territory Assignment</b><br>'
        '<span style="color:#dc2626">●</span> Assigned BET team / area<br>'
        '<span style="color:#16a34a">●</span> Selected team / area<br>'
        '<span style="color:#9ca3af">●</span> No team / area<br>Same inner-dot colors as Areas &amp; Maps.</div>'))
    return fmap


def clicked_store_id(map_data, rows):
    popup = str((map_data or {}).get('last_object_clicked_popup') or '')
    match = re.search(r'data-bet-store-id=["\x27](\d+)',popup)
    available = {r['id'] for r in rows}
    if match and int(match.group(1)) in available:
        return int(match.group(1))
    click = (map_data or {}).get('last_object_clicked') or {}
    if 'lat' not in click or 'lng' not in click:
        return None
    matches = [r['id'] for r in rows if r.get('latitude') is not None and r.get('longitude') is not None
               and abs(float(r['latitude'])-float(click['lat']))<.00001 and abs(float(r['longitude'])-float(click['lng']))<.00001]
    return matches[0] if len(matches)==1 else None


def render_service_map(data):
    rows = [r for r in service_status_rows(data) if r['bet_assigned'] and r['field_service']]
    f1,f2,f3,f4 = st.columns(4)
    owners = {r['owner_key']:r['owner_label'] for r in rows}
    owner = f1.selectbox('BET Technician / Team', ['All']+sorted(owners), format_func=lambda k:owners.get(k,k),key='bet_map_owner')
    mode = f2.selectbox('Completion Display',['Overall Completion',*SERVICES.values()])
    status = f3.selectbox('Completion Status',STATUS_FILTERS,key='bet_map_status')
    number = f4.text_input('Find Store Number',key='bet_map_number').strip()
    technicians = {r['technician_key']:r['technician_label'] for r in rows}
    technician = st.selectbox('BET Technician', ['All']+sorted(technicians),format_func=lambda key:technicians.get(key,key),key='bet_map_technician')
    filtered = filter_status([r for r in rows if (owner=='All' or owner==r['owner_key'])
        and (technician=='All' or technician==r['technician_key'])
        and (not number or number.casefold() in str(r['store_number']).casefold())],status)
    if not filtered:
        st.info('No current BET stores match these filters.')
        return
    chosen_team = next((r['bet_team_id'] for r in filtered if r['owner_key']==owner),None)
    fmap = build_service_map(filtered,data['areas'],mode,chosen_team)
    if fmap is None:
        st.warning('Matching stores have no valid map coordinates. They can still be updated in Log Service Completion.')
        return
    missing = sum(r['latitude'] is None or r['longitude'] is None for r in filtered)
    if missing:
        st.caption(f'{missing} matching stores have no coordinates; use Log Service Completion for these stores.')
    # Change the key with filters to reset old viewport/clicks. Saved data rebuilds halos on rerun.
    scope = st.session_state.get("effective_account_slug") or st.session_state.get("active_account_slug") or st.session_state.get("account_slug") or "workspace"
    key = f"bet_service_map_{scope}_{data['year']}_{owner}_{technician}_{mode}_{status}_{number}"
    map_data = st_folium(fmap,key=key,height=620,width=None,
        returned_objects=['last_object_clicked','last_object_clicked_popup'])
    st.download_button('Export BET Service Map',map_html(fmap),file_name=f"bet_service_map_{data['year']}.html",mime='text/html')
    sid = clicked_store_id(map_data,filtered)
    token = (key,str((map_data or {}).get('last_object_clicked_popup')),str((map_data or {}).get('last_object_clicked')))
    if sid and st.session_state.get('bet_map_last_click') != token:
        st.session_state['bet_map_last_click'] = token
        st.session_state['bet_map_selected_store'] = sid
    lookup = {r['id']:r for r in filtered}
    selected = st.session_state.get('bet_map_selected_store')
    options = list(lookup)
    selected = selected if selected in options else options[0]
    st.subheader('Selected Store — Service Changes')
    sid = st.selectbox('Store selected on map',options,index=options.index(selected),
        format_func=lambda i:str(lookup[i]['store_number']),key='bet_map_store_'+key+'_'+str(selected))
    render_completion_form(lookup[sid],data,key_prefix='map',source='Map entry')
