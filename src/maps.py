import hashlib
import html
import json
import struct
import zlib

import folium
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from folium.plugins import Draw, MarkerCluster, PolyLineTextPath
from streamlit_folium import st_folium


STATUS_COLORS = {
    "Scheduled": "#2563eb",
    "Completed": "#16a34a",
    "Not Completed": "#dc2626",
    "Rain Delay": "#f97316",
    "Needs Rescheduled": "#7c3aed",
    "Available": "#6b7280",
    "Available Deferred WO": "#6b7280",
    "Assigned": "#0891b2",
    "In Progress": "#0f766e",
    "Open": "#d97706",
    "Overdue": "#b91c1c",
    "Cancelled": "#64748b",
}


PALETTE = [
    "#2563eb",
    "#dc2626",
    "#16a34a",
    "#7c3aed",
    "#ea580c",
    "#0891b2",
    "#be123c",
    "#4f46e5",
    "#0f766e",
    "#ca8a04",
    "#9333ea",
    "#0284c7",
    "#65a30d",
    "#c026d3",
    "#db2777",
    "#475569",
    "#1d4ed8",
    "#b45309",
    "#15803d",
    "#6d28d9",
    "#0369a1",
    "#a21caf",
    "#047857",
    "#9f1239",
    "#4338ca",
    "#92400e",
    "#155e75",
    "#854d0e",
    "#166534",
    "#581c87",
]


def stable_color(value):
    if not value:
        return "gray"
    digest = hashlib.md5(str(value).encode("utf-8")).hexdigest()
    return PALETTE[int(digest[:2], 16) % len(PALETTE)]


def center_for(df):
    valid = df.dropna(subset=["latitude", "longitude"])
    if valid.empty:
        return [41.4993, -81.6944]
    return [float(valid["latitude"].mean()), float(valid["longitude"].mean())]


def map_html(fmap):
    return fmap.get_root().render().encode("utf-8")


def route_preview_svg(stores_df, width=1100, height=520):
    route_df = stores_df.copy()
    if route_df.empty or not {"latitude", "longitude"}.issubset(route_df.columns):
        return ""
    route_df["latitude"] = pd.to_numeric(route_df["latitude"], errors="coerce")
    route_df["longitude"] = pd.to_numeric(route_df["longitude"], errors="coerce")
    route_df = route_df.dropna(subset=["latitude", "longitude"])
    if route_df.empty:
        return ""
    sort_cols = [col for col in ["schedule_date", "sequence_number", "store_number"] if col in route_df.columns]
    if sort_cols:
        route_df = route_df.sort_values(sort_cols)

    pad = 42
    min_lat, max_lat = float(route_df["latitude"].min()), float(route_df["latitude"].max())
    min_lon, max_lon = float(route_df["longitude"].min()), float(route_df["longitude"].max())
    lat_span = max(max_lat - min_lat, 0.0001)
    lon_span = max(max_lon - min_lon, 0.0001)

    points = []
    for stop_number, (_, row) in enumerate(route_df.iterrows(), start=1):
        x = pad + ((float(row["longitude"]) - min_lon) / lon_span) * (width - pad * 2)
        y = pad + ((max_lat - float(row["latitude"])) / lat_span) * (height - pad * 2)
        points.append((stop_number, x, y, row))

    polyline = " ".join(f"{x:.1f},{y:.1f}" for _, x, y, _ in points)
    show_numbers = len(points) <= 175
    show_store_labels = len(points) <= 60
    markers = []
    for stop_number, x, y, row in points:
        store = html.escape(str(row.get("store_number", "")))
        city = html.escape(str(row.get("city", "")))
        date_text = html.escape(str(row.get("schedule_date", "")))
        marker_label = str(stop_number) if show_numbers else ""
        label = f"<text x='{x + 9:.1f}' y='{y - 9:.1f}' class='store-label'>Store {store}</text>" if show_store_labels else ""
        markers.append(
            f"""
            <g>
              <title>Stop {stop_number} | Store {store} | {city} | {date_text}</title>
              <circle cx="{x:.1f}" cy="{y:.1f}" r="8" class="route-point" />
              <text x="{x:.1f}" y="{y + 4:.1f}" class="route-number">{html.escape(marker_label)}</text>
              {label}
            </g>
            """
        )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="Draft route preview">
  <style>
    .route-bg {{ fill: #f8fafc; stroke: #cbd5e1; stroke-width: 1; }}
    .route-line {{ fill: none; stroke: #111827; stroke-width: 4; stroke-linecap: round; stroke-linejoin: round; opacity: 0.78; }}
    .route-point {{ fill: #2563eb; stroke: #ffffff; stroke-width: 2; }}
    .route-number {{ fill: #ffffff; font-size: 9px; font-weight: 800; text-anchor: middle; pointer-events: none; font-family: Arial, sans-serif; }}
    .store-label {{ fill: #334155; font-size: 11px; font-weight: 700; font-family: Arial, sans-serif; paint-order: stroke; stroke: #f8fafc; stroke-width: 3px; stroke-linejoin: round; }}
  </style>
  <rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="12" class="route-bg" />
  <polyline points="{polyline}" class="route-line" />
  {''.join(markers)}
</svg>"""


def _png_chunk(chunk_type, data):
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def _png_bytes(width, height, pixels):
    raw = bytearray()
    row_bytes = width * 3
    for y in range(height):
        raw.append(0)
        start = y * row_bytes
        raw.extend(pixels[start : start + row_bytes])
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def _set_pixel(pixels, width, height, x, y, color):
    if 0 <= x < width and 0 <= y < height:
        offset = (y * width + x) * 3
        pixels[offset : offset + 3] = bytes(color)


def _draw_line(pixels, width, height, x0, y0, x1, y1, color, thickness=3):
    x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        radius = max(1, int(thickness // 2))
        for ox in range(-radius, radius + 1):
            for oy in range(-radius, radius + 1):
                _set_pixel(pixels, width, height, x0 + ox, y0 + oy, color)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _draw_circle(pixels, width, height, cx, cy, radius, color, outline=(255, 255, 255)):
    cx, cy = int(round(cx)), int(round(cy))
    radius = int(radius)
    for y in range(cy - radius, cy + radius + 1):
        for x in range(cx - radius, cx + radius + 1):
            distance = (x - cx) ** 2 + (y - cy) ** 2
            if distance <= radius**2:
                edge = distance >= (radius - 2) ** 2
                _set_pixel(pixels, width, height, x, y, outline if edge else color)


_DIGITS = {
    "0": ["111", "101", "101", "101", "111"],
    "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"],
    "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"],
    "7": ["111", "001", "001", "001", "001"],
    "8": ["111", "101", "111", "101", "111"],
    "9": ["111", "101", "111", "001", "111"],
    "S": ["111", "100", "111", "001", "111"],
    "E": ["111", "100", "111", "100", "111"],
}


def _draw_text(pixels, width, height, text, x, y, color=(17, 24, 39), scale=2):
    cursor = int(x)
    for char in str(text).upper():
        if char == " ":
            cursor += 4 * scale
            continue
        glyph = _DIGITS.get(char)
        if not glyph:
            cursor += 4 * scale
            continue
        for gy, row in enumerate(glyph):
            for gx, value in enumerate(row):
                if value == "1":
                    for sy in range(scale):
                        for sx in range(scale):
                            _set_pixel(pixels, width, height, cursor + gx * scale + sx, int(y) + gy * scale + sy, color)
        cursor += 4 * scale


def _draw_label_box(pixels, width, height, text, x, y):
    text = str(text)
    scale = 2
    box_w = max(20, len(text) * 8 + 8)
    box_h = 18
    x = max(4, min(int(x), width - box_w - 4))
    y = max(4, min(int(y), height - box_h - 4))
    for yy in range(y, y + box_h):
        for xx in range(x, x + box_w):
            edge = yy in {y, y + box_h - 1} or xx in {x, x + box_w - 1}
            _set_pixel(pixels, width, height, xx, yy, (203, 213, 225) if edge else (255, 255, 255))
    _draw_text(pixels, width, height, text, x + 4, y + 4, (15, 23, 42), scale)


def route_preview_png(stores_df, width=1100, height=520):
    route_df = stores_df.copy()
    if route_df.empty or not {"latitude", "longitude"}.issubset(route_df.columns):
        return b""
    route_df["latitude"] = pd.to_numeric(route_df["latitude"], errors="coerce")
    route_df["longitude"] = pd.to_numeric(route_df["longitude"], errors="coerce")
    route_df = route_df.dropna(subset=["latitude", "longitude"])
    if route_df.empty:
        return b""
    sort_cols = [col for col in ["schedule_date", "sequence_number", "store_number"] if col in route_df.columns]
    if sort_cols:
        route_df = route_df.sort_values(sort_cols)

    bg = (248, 250, 252)
    pixels = bytearray(bg * (width * height))
    pad = 44
    min_lat, max_lat = float(route_df["latitude"].min()), float(route_df["latitude"].max())
    min_lon, max_lon = float(route_df["longitude"].min()), float(route_df["longitude"].max())
    lat_span = max(max_lat - min_lat, 0.0001)
    lon_span = max(max_lon - min_lon, 0.0001)

    for grid_index in range(1, 5):
        gx = pad + (width - pad * 2) * grid_index / 5
        gy = pad + (height - pad * 2) * grid_index / 5
        _draw_line(pixels, width, height, gx, pad, gx, height - pad, (226, 232, 240), 1)
        _draw_line(pixels, width, height, pad, gy, width - pad, gy, (226, 232, 240), 1)
    _draw_line(pixels, width, height, pad, pad, width - pad, pad, (203, 213, 225), 2)
    _draw_line(pixels, width, height, width - pad, pad, width - pad, height - pad, (203, 213, 225), 2)
    _draw_line(pixels, width, height, width - pad, height - pad, pad, height - pad, (203, 213, 225), 2)
    _draw_line(pixels, width, height, pad, height - pad, pad, pad, (203, 213, 225), 2)

    points = []
    for _, row in route_df.iterrows():
        x = pad + ((float(row["longitude"]) - min_lon) / lon_span) * (width - pad * 2)
        y = pad + ((max_lat - float(row["latitude"])) / lat_span) * (height - pad * 2)
        points.append((x, y))

    for start, end in zip(points, points[1:]):
        _draw_line(pixels, width, height, start[0], start[1], end[0], end[1], (17, 24, 39), 4)
    label_every = 1 if len(points) <= 120 else max(2, len(points) // 80)
    for index, (x, y) in enumerate(points):
        color = (37, 99, 235)
        if index == 0:
            color = (22, 163, 74)
        elif index == len(points) - 1:
            color = (220, 38, 38)
        _draw_circle(pixels, width, height, x, y, 8, color)
        if index == 0 or index == len(points) - 1 or index % label_every == 0:
            _draw_label_box(pixels, width, height, str(index + 1), x + 10, y - 22)
    if points:
        _draw_label_box(pixels, width, height, "S", points[0][0] - 24, points[0][1] + 12)
        _draw_label_box(pixels, width, height, "E", points[-1][0] - 24, points[-1][1] + 12)
    return _png_bytes(width, height, pixels)


def render_route_preview(stores_df, height=520):
    route_df = stores_df.copy()
    if route_df.empty or not {"latitude", "longitude"}.issubset(route_df.columns):
        st.info("No static backup preview could be drawn because coordinates are missing.")
        return ""
    route_df["latitude"] = pd.to_numeric(route_df["latitude"], errors="coerce")
    route_df["longitude"] = pd.to_numeric(route_df["longitude"], errors="coerce")
    route_df = route_df.dropna(subset=["latitude", "longitude"])
    if route_df.empty:
        st.info("No static backup preview could be drawn because coordinates are missing.")
        return ""
    sort_cols = [col for col in ["schedule_date", "sequence_number", "store_number"] if col in route_df.columns]
    if sort_cols:
        route_df = route_df.sort_values(sort_cols)
    png = route_preview_png(route_df, height=height)
    if png:
        cities = _unique_preview_values(route_df.get("city", pd.Series(dtype=str)), limit=6)
        first_store = route_df.iloc[0].get("store_number", "") if not route_df.empty else ""
        last_store = route_df.iloc[-1].get("store_number", "") if not route_df.empty else ""
        label_every = 1 if len(route_df) <= 120 else max(2, len(route_df) // 80)
        summary_cols = st.columns(4)
        summary_cols[0].metric("Route Stops", len(route_df))
        summary_cols[1].metric("Start Store", first_store or "-")
        summary_cols[2].metric("End Store", last_store or "-")
        summary_cols[3].metric("Number Labels", "All" if label_every == 1 else f"Every {label_every}")
        st.info(
            "Static backup route preview: green = start, red = end, blue = stops. "
            + (f"Large route, showing every {label_every} stop number plus start/end." if label_every > 1 else "Every stop is numbered.")
        )
        st.caption(
            f"Route picture: {len(route_df)} stops"
            + (f" | Cities: {cities}" if cities else "")
            + (f" | Start store: {first_store} | End store: {last_store}" if first_store or last_store else "")
        )
        st.image(png)
    route_df = route_df.copy()
    route_df.insert(0, "route_stop", range(1, len(route_df) + 1))
    display_cols = [
        col
        for col in [
            "route_stop",
            "schedule_date",
            "sequence_number",
            "store_number",
            "city",
            "state",
            "distance_from_previous",
            "miles_from_previous_stop",
            "distance_from_home",
            "latitude",
            "longitude",
        ]
        if col in route_df.columns
    ]
    st.caption("Static backup route stops.")
    render_plain_table(route_df[display_cols], max_rows=250)
    return route_df[display_cols].to_csv(index=False)


def _unique_preview_values(values, limit=6):
    cleaned = []
    for value in values:
        text = str(value or "").strip()
        if not text or text.lower() in {"nan", "none"}:
            continue
        if text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= int(limit):
            break
    suffix = "..." if len(set(str(value).strip() for value in values if str(value or "").strip())) > len(cleaned) else ""
    return ", ".join(cleaned) + suffix


def render_plain_table(df, max_rows=250):
    if df is None or df.empty:
        st.info("No rows to show.")
        return
    preview = df.head(int(max_rows)).copy()
    st.markdown(
        """
        <style>
          table.asm-plain-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
          }
          table.asm-plain-table th {
            background: #f1f5f9;
            color: #1f2937;
            text-align: left;
            border: 1px solid #cbd5e1;
            padding: 6px 8px;
            position: sticky;
            top: 0;
          }
          table.asm-plain-table td {
            border: 1px solid #e2e8f0;
            padding: 6px 8px;
            vertical-align: top;
          }
          div.asm-plain-table-wrap {
            max-height: 520px;
            overflow: auto;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            background: white;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div class='asm-plain-table-wrap'>{preview.to_html(index=False, escape=True, classes='asm-plain-table')}</div>",
        unsafe_allow_html=True,
    )
    if len(df) > len(preview):
        st.caption(f"Showing first {len(preview)} of {len(df)} rows. Use export for the full draft.")


def drawing_to_geometry_json(drawing):
    return json.dumps(drawing.get("geometry", drawing))


def add_area_overlays(fmap, areas_df):
    if areas_df is None or areas_df.empty:
        return fmap
    for _, row in areas_df.iterrows():
        try:
            geometry = json.loads(row["geometry_json"])
        except Exception:
            continue
        if not geometry or not geometry.get("coordinates"):
            continue
        if geometry.get("type") == "Polygon" and (not geometry.get("coordinates", [[]])[0]):
            continue
        label = row.get("area_name", "Area")
        color = row.get("color") or stable_color(label)
        folium.GeoJson(
            {"type": "Feature", "geometry": geometry, "properties": {"name": label}},
            name=label,
            style_function=lambda feature, line_color=color: {
                "color": line_color,
                "weight": 4,
                "fillColor": line_color,
                "fillOpacity": 0.12,
            },
            tooltip=label,
        ).add_to(fmap)
    return fmap


def point_in_polygon(lat, lon, polygon):
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        lat_i, lon_i = polygon[i]
        lat_j, lon_j = polygon[j]
        intersects = ((lat_i > lat) != (lat_j > lat)) and (
            lon < (lon_j - lon_i) * (lat - lat_i) / ((lat_j - lat_i) or 1e-12) + lon_i
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def distance_to_line_miles(lat, lon, line):
    from math import cos, radians

    if len(line) < 2:
        return float("inf")
    lat_scale = 69.0
    lon_scale = 69.0 * cos(radians(float(lat)))
    best = float("inf")
    px = float(lon) * lon_scale
    py = float(lat) * lat_scale
    for start, end in zip(line, line[1:]):
        lat1, lon1 = start
        lat2, lon2 = end
        ax = float(lon1) * lon_scale
        ay = float(lat1) * lat_scale
        bx = float(lon2) * lon_scale
        by = float(lat2) * lat_scale
        dx = bx - ax
        dy = by - ay
        if dx == 0 and dy == 0:
            best = min(best, ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5)
            continue
        t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        nearest_x = ax + t * dx
        nearest_y = ay + t * dy
        best = min(best, ((px - nearest_x) ** 2 + (py - nearest_y) ** 2) ** 0.5)
    return best


def drawing_contains_point(drawing, lat, lon, line_buffer_miles=3.0, close_lines_as_areas=True):
    geometry = drawing.get("geometry", {})
    properties = drawing.get("properties", {})
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates", [])

    if geometry_type == "Polygon" and coordinates:
        polygon = [(point[1], point[0]) for point in coordinates[0]]
        return point_in_polygon(float(lat), float(lon), polygon)

    if geometry_type == "LineString" and coordinates:
        line = [(point[1], point[0]) for point in coordinates]
        if close_lines_as_areas and len(line) >= 3:
            return point_in_polygon(float(lat), float(lon), line)
        return distance_to_line_miles(float(lat), float(lon), line) <= line_buffer_miles

    if geometry_type == "Point" and properties.get("radius"):
        center_lon, center_lat = coordinates
        radius_miles = float(properties["radius"]) / 1609.344
        return haversine_miles(float(center_lat), float(center_lon), float(lat), float(lon)) <= radius_miles

    return False


def stores_within_drawings(stores_df, drawings, line_buffer_miles=3.0, close_lines_as_areas=True):
    if stores_df.empty or not drawings:
        return stores_df.iloc[0:0].copy()
    stores_df = stores_df.copy()
    stores_df["latitude"] = pd.to_numeric(stores_df["latitude"], errors="coerce")
    stores_df["longitude"] = pd.to_numeric(stores_df["longitude"], errors="coerce")
    stores_df = stores_df.dropna(subset=["latitude", "longitude"])
    mask = stores_df.apply(
        lambda row: any(
            drawing_contains_point(
                drawing,
                row["latitude"],
                row["longitude"],
                line_buffer_miles,
                close_lines_as_areas,
            )
            for drawing in drawings
        ),
        axis=1,
    )
    return stores_df[mask].copy()


def haversine_miles(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt

    r = 3958.8
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def render_store_map(
    stores_df,
    employees_df=None,
    color_by="status",
    show_homes=True,
    height=650,
    enable_draw=False,
    key=None,
    cluster=True,
    area_overlays=None,
    show_route_path=False,
    max_route_points=150,
    static_preview=False,
):
    stores_df = stores_df.copy()
    if stores_df.empty:
        st.info("No mapped records found. Upload stores with latitude and longitude first.")
        return None, {}
    stores_df["latitude"] = pd.to_numeric(stores_df["latitude"], errors="coerce")
    stores_df["longitude"] = pd.to_numeric(stores_df["longitude"], errors="coerce")
    stores_df = stores_df.dropna(subset=["latitude", "longitude"])
    fmap = folium.Map(location=center_for(stores_df), zoom_start=8, tiles="OpenStreetMap")
    marker_parent = fmap
    if cluster and len(stores_df) >= 75:
        marker_parent = MarkerCluster(name="Stores", disableClusteringAtZoom=11).add_to(fmap)
    route_path_enabled = show_route_path and len(stores_df) >= 2
    if show_route_path and len(stores_df) > int(max_route_points):
        st.caption(f"Route labels are reduced because this map has {len(stores_df)} stops. Store points and route line are still shown.")
    if route_path_enabled:
        route_df = stores_df.copy()
        sort_cols = [col for col in ["schedule_date", "sequence_number", "store_number"] if col in route_df.columns]
        if sort_cols:
            route_df = route_df.sort_values(sort_cols)
        route_points = [
            [float(row["latitude"]), float(row["longitude"])]
            for _, row in route_df.iterrows()
            if pd.notna(row["latitude"]) and pd.notna(row["longitude"])
        ]
        if len(route_points) >= 2:
            route_line = folium.PolyLine(
                route_points,
                color="#111827",
                weight=4,
                opacity=0.78,
                tooltip="Scheduled route order",
            ).add_to(fmap)
            if len(route_points) <= int(max_route_points):
                PolyLineTextPath(
                    route_line,
                    "  >  ",
                    repeat=True,
                    offset=7,
                    attributes={"fill": "#111827", "font-weight": "bold", "font-size": "16"},
                ).add_to(fmap)
        label_every = 1 if len(route_df) <= 60 else max(2, len(route_df) // 50)
        for stop_number, (_, row) in enumerate(route_df.iterrows(), start=1):
            if stop_number not in {1, len(route_df)} and (stop_number - 1) % label_every != 0:
                continue
            folium.Marker(
                [float(row["latitude"]), float(row["longitude"])],
                icon=folium.DivIcon(
                    html=f"""
                    <div style="
                        background:#111827;
                        color:white;
                        border:2px solid white;
                        border-radius:999px;
                        width:24px;
                        height:24px;
                        line-height:20px;
                        text-align:center;
                        font-size:12px;
                        font-weight:800;
                        box-shadow:0 1px 4px rgba(0,0,0,.35);
                    ">{stop_number}</div>
                    """
                ),
                tooltip=f"Stop {stop_number}: Store {row.get('store_number', '')}",
            ).add_to(fmap)
    for _, row in stores_df.iterrows():
        if color_by == "technician":
            color = stable_color(row.get("technician", "") or row.get("team_name", ""))
        elif color_by == "team":
            color = stable_color(row.get("team_name", "") or row.get("technician", ""))
        else:
            color = STATUS_COLORS.get(row.get("status", row.get("store_status", "")), "#2563eb")
        popup = f"""
        <b>Store {row.get('store_number','')}</b><br>
        {row.get('address','')}<br>
        {row.get('city','')}, {row.get('state','')}<br>
        Technician: {row.get('technician','')}<br>
        Team: {row.get('team_name','')}<br>
        Status: {row.get('status', row.get('store_status',''))}<br>
        Notes: {row.get('notes','')}
        """
        tooltip = f"Store {row.get('store_number','')} - {row.get('city','')}"
        folium.CircleMarker(
            [float(row["latitude"]), float(row["longitude"])],
            radius=6,
            color="#ffffff",
            weight=1,
            fill=True,
            fill_color=color,
            fill_opacity=0.92,
            popup=folium.Popup(popup, max_width=320),
            tooltip=tooltip,
        ).add_to(marker_parent)
    if show_homes and employees_df is not None and not employees_df.empty:
        employees_df = employees_df.copy()
        employees_df["home_latitude"] = pd.to_numeric(employees_df["home_latitude"], errors="coerce")
        employees_df["home_longitude"] = pd.to_numeric(employees_df["home_longitude"], errors="coerce")
        for _, row in employees_df.dropna(subset=["home_latitude", "home_longitude"]).iterrows():
            popup = f"<b>{row.get('full_name','')}</b><br>{row.get('role','')}<br>{row.get('home_city','')}, {row.get('home_state','')}"
            folium.CircleMarker(
                [float(row["home_latitude"]), float(row["home_longitude"])],
                radius=8,
                color="#111827",
                weight=2,
                fill=True,
                fill_color="#111827",
                fill_opacity=0.9,
                popup=popup,
            ).add_to(fmap)
    add_area_overlays(fmap, area_overlays)
    if enable_draw:
        Draw(
            export=False,
            draw_options={
                "polyline": True,
                "polygon": False,
                "rectangle": True,
                "circle": True,
                "marker": False,
                "circlemarker": False,
            },
            edit_options={"edit": True, "remove": True},
        ).add_to(fmap)
    returned_objects = ["all_drawings"] if enable_draw else []
    if static_preview:
        components.html(fmap.get_root().render(), height=height, scrolling=False)
        return fmap, {}
    map_data = st_folium(fmap, width=None, height=height, key=key, returned_objects=returned_objects)
    return fmap, map_data
