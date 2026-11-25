"""Maps for current PMT territory and priority route previews."""
from html import escape

import folium

from src.maps import stable_color
from src.pmt_carryover import coordinates_available

HALO = "#c2410c"


def carryover_map(stores, backlog, route=None):
    points = [row for row in stores.itertuples(index=False) if coordinates_available(row)]
    if not points:
        return None
    fmap = folium.Map(location=[points[0].latitude, points[0].longitude], zoom_start=9)
    bounds = [[row.latitude, row.longitude] for row in points]
    if len(points) > 1:
        fmap.fit_bounds(bounds)
    stops = {}
    if route is not None and not route.empty:
        stops = dict(zip(route["store_id"], route["Route Order"]))
        if len(route) > 1:
            folium.PolyLine(route[["latitude", "longitude"]].values.tolist(), color="#334155", weight=3).add_to(fmap)
    owners = sorted({row.technician_name for row in points})
    colors = {name: stable_color(name) if name != "Unassigned" else "#9ca3af" for name in owners}
    for row in points:
        store_id = int(row.store_id)
        evidence = backlog.get(store_id)
        point = [row.latitude, row.longitude]
        stop = stops.get(store_id)
        popup = [f"<b>Store {escape(str(row.store_number))}</b>", f"Current PMT: {escape(row.technician_name)}"]
        if evidence:
            popup.append("Carryover: Yes")
            for label in ("Source Cycle", "Cancelled WOs", "Previous PM Tech", "Current Schedule Status"):
                popup.append(f"{label}: {escape(str(evidence[label] or '—'))}")
            popup.append("Priority: Carryover" if evidence["Priority"] else "Priority: Paused")
        if stop is not None:
            popup.append(f"Proposed stop: {int(stop)}")
        if evidence:
            folium.CircleMarker(point, radius=12, color=HALO, weight=3, fill=False, interactive=False).add_to(fmap)
        folium.CircleMarker(
            point, radius=8, color="#ffffff", weight=1, fill=True, fill_opacity=1,
            fill_color=colors[row.technician_name],
            popup=folium.Popup("<br>".join(popup), max_width=330),
            tooltip=f"Store {escape(str(row.store_number))}" + (f" · Stop {int(stop)}" if stop is not None else ""),
        ).add_to(fmap)
        if stop is not None:
            folium.Marker(point, icon=folium.DivIcon(
                icon_size=(24, 14), icon_anchor=(12, 7),
                html=f'<div style="text-align:center;color:white;font-weight:bold;font-size:10px;pointer-events:none">{int(stop)}</div>',
            ), interactive=False).add_to(fmap)
    owner_legend = "".join(f'<div><span style="color:{colors[name]}">●</span> {escape(name)}</div>' for name in owners)
    fmap.get_root().html.add_child(folium.Element(
        '<div style="position:absolute;z-index:9999;bottom:20px;left:12px;background:white;color:#111827;'
        'padding:9px 13px;border-radius:6px;max-height:45%;overflow:auto">'
        f'{owner_legend}<hr style="margin:6px 0"><b>Carryover status</b><br>'
        f'● Normal current-cycle store<br><span style="color:{HALO};font-weight:bold">◯</span> Prior-cycle carryover</div>'
    ))
    return fmap
