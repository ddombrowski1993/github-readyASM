import json
import re
from collections import defaultdict, deque
from datetime import datetime

import pandas as pd

from src.imports import clean_store_number
from src.maps import PALETTE, haversine_miles, stable_color
from src.smart_import import clean_text, mapped_dataframe, scan_workbook


STORE_ALIASES = ["Store Number", "Store #", "Store", "Site Number", "Site #", "Site"]
LANDSCAPING_ALIASES = ["Landscaping", "Landscaping Vendor", "Landscape Vendor", "Landscaper"]
SCHEDULE_ALIASES = ["Landscaping Schedule", "Landscape Schedule", "Schedule", "Service Schedule"]
LOCATION_ALIASES = {
    "address": ["Address", "Street Address", "Store Address", "Location Address"],
    "city": ["City", "Store City", "Location City"],
    "state": ["State", "ST", "Store State", "Location State"],
    "zip": ["ZIP", "Zip Code", "Postal Code", "Postal"],
}


def ensure_landscaping_tables():
    from src.database import _apply_workspace_search_path, get_database_url, get_engine
    from sqlalchemy import text

    engine = get_engine(get_database_url())
    vendor_id_type = "integer primary key autoincrement" if engine.dialect.name == "sqlite" else "serial primary key"
    timestamp_type = "timestamp"
    active_default = "1" if engine.dialect.name == "sqlite" else "true"
    with engine.begin() as conn:
        _apply_workspace_search_path(conn)
        conn.execute(
            text(
                f"""
                create table if not exists landscaping_vendors (
                    id {vendor_id_type},
                    vendor_name varchar(180) not null unique,
                    normalized_name varchar(180) not null unique,
                    display_color varchar(20),
                    notes text,
                    active boolean not null default {active_default},
                    created_at {timestamp_type} not null default current_timestamp,
                    updated_at {timestamp_type} not null default current_timestamp
                )
                """
            )
        )
        conn.execute(
            text(
                f"""
                create table if not exists landscaping_import_runs (
                    id {vendor_id_type},
                    file_name varchar(255) not null,
                    imported_at {timestamp_type} not null default current_timestamp,
                    imported_by varchar(220),
                    source varchar(180) default 'Landscaping upload',
                    rows_found integer default 0,
                    unique_stores integer default 0,
                    matched_stores integer default 0,
                    unmatched_stores integer default 0,
                    updated_assignments integer default 0,
                    unchanged_assignments integer default 0,
                    new_vendors_created integer default 0,
                    blank_vendor_rows integer default 0,
                    unmatched_json text,
                    changes_json text,
                    created_at {timestamp_type} not null default current_timestamp,
                    updated_at {timestamp_type} not null default current_timestamp
                )
                """
            )
        )
        conn.execute(
            text(
                f"""
                create table if not exists store_landscaping_assignments (
                    id {vendor_id_type},
                    store_id integer not null unique,
                    landscaping_vendor_id integer,
                    landscaping_schedule varchar(180),
                    source varchar(180),
                    import_run_id integer,
                    active boolean not null default {active_default},
                    created_at {timestamp_type} not null default current_timestamp,
                    updated_at {timestamp_type} not null default current_timestamp
                )
                """
            )
        )
        conn.execute(text("create index if not exists ix_landscaping_vendors_normalized on landscaping_vendors (normalized_name)"))
        conn.execute(text("create index if not exists ix_store_landscaping_store on store_landscaping_assignments (store_id)"))
        conn.execute(text("create index if not exists ix_store_landscaping_vendor on store_landscaping_assignments (landscaping_vendor_id)"))
        conn.execute(text("create index if not exists ix_landscaping_import_runs_uploaded on landscaping_import_runs (imported_at)"))


def header_key(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def normalize_vendor_name(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def vendor_key(value):
    return normalize_vendor_name(value).lower()


def find_column(columns, aliases, fallback_index=None):
    lookup = {header_key(column): column for column in columns}
    for alias in aliases:
        column = lookup.get(header_key(alias))
        if column:
            return column
    for column in columns:
        column_key = header_key(column)
        if any(header_key(alias) and header_key(alias) in column_key for alias in aliases):
            return column
    if fallback_index is not None and 0 <= int(fallback_index) < len(columns):
        return list(columns)[int(fallback_index)]
    return ""


def landscaping_color_for_name(name, used_colors=None):
    used_colors = used_colors or set()
    first = stable_color(name)
    if first not in used_colors:
        return first
    for color in PALETTE:
        if color not in used_colors:
            return color
    return first


def scan_landscaping_workbook(uploaded_file):
    scans = scan_workbook(uploaded_file, "stores")
    usable = []
    for item in scans:
        df = item.get("df", pd.DataFrame())
        if df.empty:
            usable.append({**item, "store_column": "", "landscaping_column": "", "schedule_column": "", "landscaping_score": 0})
            continue
        store_column = find_column(df.columns, STORE_ALIASES, fallback_index=0)
        landscaping_column = find_column(df.columns, LANDSCAPING_ALIASES, fallback_index=22 if len(df.columns) > 22 else None)
        schedule_column = find_column(df.columns, SCHEDULE_ALIASES)
        score = int(bool(store_column)) * 500 + int(bool(landscaping_column)) * 700 + len(df)
        usable.append(
            {
                **item,
                "store_column": store_column,
                "landscaping_column": landscaping_column,
                "schedule_column": schedule_column,
                "landscaping_score": score,
            }
        )
    usable.sort(key=lambda item: item["landscaping_score"], reverse=True)
    return usable


def _existing_store_lookup():
    from src.database import safe_query

    ensure_landscaping_tables()
    stores = safe_query(
        """
        select id, store_number, store_name, address, city, state, zip, latitude, longitude,
               market, area, service_type, active
        from stores
        where active = true
        """,
        use_cache=False,
    )
    if stores.empty:
        return stores, {}
    stores = stores.copy()
    stores["normalized_store_number"] = stores["store_number"].apply(clean_store_number)
    lookup = {}
    stripped_candidates = defaultdict(list)
    for _, row in stores.iterrows():
        number = row["normalized_store_number"]
        if not number:
            continue
        lookup[number] = row
        stripped = number.lstrip("0")
        if stripped and stripped != number:
            stripped_candidates[stripped].append(row)
    for stripped, matches in stripped_candidates.items():
        if stripped not in lookup and len(matches) == 1:
            lookup[stripped] = matches[0]
    return stores, lookup


def build_landscaping_preview(incoming, mapping):
    source = mapped_dataframe(incoming, {key: value for key, value in mapping.items() if value})
    for field in ["store_number", "landscaping_vendor", "landscaping_schedule", "address", "city", "state", "zip"]:
        if field not in source.columns:
            source[field] = ""
    source["store_number"] = source["store_number"].apply(clean_store_number)
    source["landscaping_vendor"] = source["landscaping_vendor"].apply(normalize_vendor_name)
    source["vendor_key"] = source["landscaping_vendor"].apply(vendor_key)
    source["landscaping_schedule"] = source["landscaping_schedule"].apply(clean_text)
    source = source[source["store_number"].astype(str).str.strip().ne("")].copy()

    _, store_lookup = _existing_store_lookup()
    rows = []
    seen = set()
    duplicate_rows = 0
    for _, row in source.iterrows():
        store_number = row["store_number"]
        if store_number not in store_lookup and store_number.lstrip("0") in store_lookup:
            store_number = store_number.lstrip("0")
        if store_number in seen:
            duplicate_rows += 1
            continue
        seen.add(store_number)
        store = store_lookup.get(store_number)
        vendor = row["landscaping_vendor"]
        rows.append(
            {
                "Store": store_number,
                "Landscaping Vendor": vendor,
                "Landscaping Schedule": row.get("landscaping_schedule", ""),
                "Match Status": "Matched" if store is not None else "Unmatched",
                "Location": "Existing coordinates"
                if store is not None and pd.notna(store.get("latitude")) and pd.notna(store.get("longitude"))
                else "Matched, missing coordinates"
                if store is not None
                else "Store not found",
                "Address From Upload": row.get("address", ""),
                "City From Upload": row.get("city", ""),
                "State From Upload": row.get("state", ""),
                "ZIP From Upload": row.get("zip", ""),
                "store_id": int(store.get("id")) if store is not None else None,
                "Blank Landscaping Vendor": not bool(vendor),
            }
        )
    preview = pd.DataFrame(rows)
    summary = {
        "rows_found": int(len(source)),
        "unique_stores": int(len(preview)),
        "matched_stores": int((preview["Match Status"] == "Matched").sum()) if not preview.empty else 0,
        "unmatched_stores": int((preview["Match Status"] == "Unmatched").sum()) if not preview.empty else 0,
        "landscaping_vendors": int(preview.loc[~preview["Blank Landscaping Vendor"], "Landscaping Vendor"].nunique()) if not preview.empty else 0,
        "blank_vendor_rows": int(preview["Blank Landscaping Vendor"].sum()) if not preview.empty else 0,
        "duplicate_store_rows_skipped": int(duplicate_rows),
    }
    return preview, summary


def apply_landscaping_import(preview, file_name, imported_by="", safe_update=True):
    if preview is None or preview.empty:
        raise ValueError("There are no preview rows to import.")
    used_colors = set()
    summary = {
        "rows_found": int(len(preview)),
        "unique_stores": int(preview["Store"].nunique()),
        "matched_stores": int((preview["Match Status"] == "Matched").sum()),
        "unmatched_stores": int((preview["Match Status"] == "Unmatched").sum()),
        "updated_assignments": 0,
        "unchanged_assignments": 0,
        "new_vendors_created": 0,
        "blank_vendor_rows": int(preview["Blank Landscaping Vendor"].sum()),
        "changes": [],
        "unmatched": [],
    }
    from src.database import session_scope
    from sqlalchemy import select
    from src.models import LandscapingImportRun, LandscapingVendor, StoreLandscapingAssignment

    ensure_landscaping_tables()
    with session_scope(action_label="Landscaping assignments imported") as session:
        existing_vendors = {
            vendor.normalized_name: vendor
            for vendor in session.scalars(select(LandscapingVendor)).all()
        }
        used_colors.update(vendor.display_color for vendor in existing_vendors.values() if vendor.display_color)
        run = LandscapingImportRun(
            file_name=file_name,
            imported_at=datetime.utcnow(),
            imported_by=imported_by,
            rows_found=summary["rows_found"],
            unique_stores=summary["unique_stores"],
            matched_stores=summary["matched_stores"],
            unmatched_stores=summary["unmatched_stores"],
            blank_vendor_rows=summary["blank_vendor_rows"],
        )
        session.add(run)
        session.flush()
        for _, row in preview.iterrows():
            if row["Match Status"] != "Matched" or not row.get("store_id"):
                summary["unmatched"].append(
                    {
                        "Store Number": row.get("Store", ""),
                        "Address": row.get("Address From Upload", ""),
                        "Landscaping Vendor": row.get("Landscaping Vendor", ""),
                        "Reason": "Store number not found in master Stores database.",
                    }
                )
                continue
            vendor_name = normalize_vendor_name(row.get("Landscaping Vendor", ""))
            if not vendor_name and safe_update:
                continue
            vendor = None
            if vendor_name:
                key = vendor_key(vendor_name)
                vendor = existing_vendors.get(key)
                if vendor is None:
                    vendor = LandscapingVendor(
                        vendor_name=vendor_name,
                        normalized_name=key,
                        display_color=landscaping_color_for_name(vendor_name, used_colors),
                    )
                    session.add(vendor)
                    session.flush()
                    existing_vendors[key] = vendor
                    used_colors.add(vendor.display_color)
                    summary["new_vendors_created"] += 1
            assignment = session.scalar(
                select(StoreLandscapingAssignment).where(StoreLandscapingAssignment.store_id == int(row["store_id"]))
            )
            previous_name = assignment.vendor.vendor_name if assignment and assignment.vendor else ""
            previous_schedule = assignment.landscaping_schedule if assignment else ""
            new_vendor_id = vendor.id if vendor else None
            new_schedule = clean_text(row.get("Landscaping Schedule", ""))
            if assignment is None:
                assignment = StoreLandscapingAssignment(store_id=int(row["store_id"]))
                session.add(assignment)
            changed = assignment.landscaping_vendor_id != new_vendor_id or (new_schedule and previous_schedule != new_schedule)
            assignment.landscaping_vendor_id = new_vendor_id
            if new_schedule:
                assignment.landscaping_schedule = new_schedule
            assignment.source = file_name
            assignment.import_run_id = run.id
            assignment.active = True
            if changed:
                summary["updated_assignments"] += 1
                summary["changes"].append(
                    {
                        "Store Number": row.get("Store", ""),
                        "Previous Vendor": previous_name,
                        "New Vendor": vendor_name,
                    }
                )
            else:
                summary["unchanged_assignments"] += 1
        run.updated_assignments = summary["updated_assignments"]
        run.unchanged_assignments = summary["unchanged_assignments"]
        run.new_vendors_created = summary["new_vendors_created"]
        run.unmatched_json = json.dumps(summary["unmatched"])
        run.changes_json = json.dumps(summary["changes"])
    return summary


def landscaping_assignments_df():
    from src.database import safe_query

    ensure_landscaping_tables()
    return safe_query(
        """
        select s.id as store_id, s.store_number, s.store_name, s.address, s.city, s.state, s.zip,
               s.latitude, s.longitude, s.market, s.area, coalesce(s.service_type, 'Standard') as service_type,
               v.id as vendor_id, v.vendor_name as landscaping_vendor, v.display_color,
               a.landscaping_schedule, a.source, a.updated_at as assignment_updated_at
        from stores s
        left join store_landscaping_assignments a on a.store_id = s.id and a.active = true
        left join landscaping_vendors v on v.id = a.landscaping_vendor_id and v.active = true
        where s.active = true
        order by s.store_number
        """,
        use_cache=False,
    )


def vendor_summary(df):
    if df.empty:
        return pd.DataFrame(columns=["Landscaping Vendor", "Stores", "% of Landscaping Stores"])
    assigned = df.copy()
    assigned["Landscaping Vendor"] = assigned["landscaping_vendor"].fillna("").replace("", "Unassigned")
    total = max(len(assigned), 1)
    grouped = assigned.groupby("Landscaping Vendor", dropna=False).size().reset_index(name="Stores")
    grouped["% of Landscaping Stores"] = (grouped["Stores"] / total * 100).round(1).astype(str) + "%"
    return grouped.sort_values(["Stores", "Landscaping Vendor"], ascending=[False, True])


def manual_assign_store(store_id, vendor_name, schedule="", source="Manual correction"):
    from src.database import session_scope
    from sqlalchemy import select
    from src.models import LandscapingVendor, StoreLandscapingAssignment

    ensure_landscaping_tables()
    clean_name = normalize_vendor_name(vendor_name)
    key = vendor_key(clean_name)
    with session_scope(action_label="Landscaping assignment manually updated") as session:
        used_colors = {color for (color,) in session.query(LandscapingVendor.display_color).all() if color}
        vendor = None
        if clean_name:
            vendor = session.scalar(select(LandscapingVendor).where(LandscapingVendor.normalized_name == key))
            if vendor is None:
                vendor = LandscapingVendor(
                    vendor_name=clean_name,
                    normalized_name=key,
                    display_color=landscaping_color_for_name(clean_name, used_colors),
                )
                session.add(vendor)
                session.flush()
        assignment = session.scalar(select(StoreLandscapingAssignment).where(StoreLandscapingAssignment.store_id == int(store_id)))
        if assignment is None:
            assignment = StoreLandscapingAssignment(store_id=int(store_id))
            session.add(assignment)
        assignment.landscaping_vendor_id = vendor.id if vendor else None
        if schedule:
            assignment.landscaping_schedule = clean_text(schedule)
        assignment.source = source
        assignment.active = True


def _cluster_points(points, max_gap_miles=85):
    if len(points) <= 1:
        return [points]
    neighbors = defaultdict(list)
    for i, first in enumerate(points):
        for j in range(i + 1, len(points)):
            second = points[j]
            if haversine_miles(first[0], first[1], second[0], second[1]) <= max_gap_miles:
                neighbors[i].append(j)
                neighbors[j].append(i)
    clusters = []
    visited = set()
    for start in range(len(points)):
        if start in visited:
            continue
        queue = deque([start])
        visited.add(start)
        cluster = []
        while queue:
            idx = queue.popleft()
            cluster.append(points[idx])
            for neighbor in neighbors[idx]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        clusters.append(cluster)
    return clusters


def _convex_hull_lon_lat(points):
    unique = sorted({(float(lon), float(lat)) for lat, lon in points})
    if len(unique) <= 1:
        return unique

    def cross(origin, a, b):
        return (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])

    lower = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def territory_shapes(df, min_points=3):
    shapes = []
    if df.empty:
        return shapes
    mapped = df.dropna(subset=["latitude", "longitude"]).copy()
    mapped["latitude"] = pd.to_numeric(mapped["latitude"], errors="coerce")
    mapped["longitude"] = pd.to_numeric(mapped["longitude"], errors="coerce")
    mapped = mapped.dropna(subset=["latitude", "longitude"])
    for vendor, group in mapped.groupby("landscaping_vendor"):
        vendor_name = normalize_vendor_name(vendor)
        if not vendor_name:
            continue
        color = group["display_color"].dropna().astype(str).iloc[0] if group["display_color"].dropna().any() else stable_color(vendor_name)
        points = [(float(row["latitude"]), float(row["longitude"])) for _, row in group.iterrows()]
        for cluster in _cluster_points(points):
            center = (sum(point[0] for point in cluster) / len(cluster), sum(point[1] for point in cluster) / len(cluster))
            if len(cluster) < int(min_points):
                shapes.append({"vendor": vendor_name, "color": color, "center": center, "points": cluster, "hull": []})
                continue
            hull_lon_lat = _convex_hull_lon_lat(cluster)
            hull = [(lat, lon) for lon, lat in hull_lon_lat]
            shapes.append({"vendor": vendor_name, "color": color, "center": center, "points": cluster, "hull": hull})
    return shapes
