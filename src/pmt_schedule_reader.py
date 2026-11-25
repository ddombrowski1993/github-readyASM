"""Current PMT assignment and canonical active schedule readers."""
import pandas as pd
from sqlalchemy import text

from src.database import safe_query
from src.scheduler import haversine_miles


def clean(value):
    return str(value or "").strip()


def month_label(value):
    return value.strftime("%B %Y")


def to_float(value):
    try:
        if clean(value) == "":
            return None
        return float(value)
    except ValueError:
        return None


def read_query(sql, params=None, session=None, use_cache=False):
    if session is None:
        return safe_query(sql, params, use_cache=use_cache)
    return pd.read_sql_query(text(sql), session.connection(), params=params)


def current_assignments_from_database(session=None, use_cache=True):
    return read_query(
        """
        select e.id as employee_id, e.full_name as technician_name, e.home_address, e.home_city, e.home_state, e.home_zip,
               e.home_latitude, e.home_longitude, coalesce(e.monthly_pmt_store_target, 10) as monthly_target,
               s.id as store_id, s.store_number, s.address as store_address, s.city as store_city, s.state as store_state,
               s.zip as store_zip, s.latitude, s.longitude
        from employees e
        join stores s on s.assigned_pmt_employee_id = e.id
        where e.active = true
          and s.active = true
          and coalesce(s.service_type, 'Standard') = 'Standard'
        order by e.full_name, s.store_number
        """,
        session=session, use_cache=use_cache,
    )


def normalize_pmt_schedule_rows(df):
    if df.empty:
        return ensure_pmt_schedule_columns(df)
    df = df.copy()
    df["schedule_date"] = pd.to_datetime(df["schedule_date"], errors="coerce")
    df = df.dropna(subset=["schedule_date"])
    if df.empty:
        return ensure_pmt_schedule_columns(df)
    df["month_start"] = df["schedule_date"].dt.to_period("M").dt.to_timestamp().dt.date
    df["month"] = df["month_start"].apply(month_label)
    df["schedule_date"] = df["schedule_date"].dt.date
    if "original_schedule_date" in df.columns:
        df["original_schedule_date"] = pd.to_datetime(df.get("original_schedule_date"), errors="coerce").dt.date
    for column in [
        "schedule_item_id",
        "schedule_id",
        "pmt_schedule_run_id",
        "employee_id",
        "store_id",
        "sequence_number",
        "assigned_pmt_employee_id",
    ]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    if "work_type" not in df.columns:
        df["work_type"] = "PMT"
    if "estimated_drive_time" not in df.columns:
        df["estimated_drive_time"] = ""
    if {"home_latitude", "home_longitude", "latitude", "longitude"}.issubset(df.columns):
        df["distance_from_home"] = df.apply(
            lambda row: round(
                haversine_miles(float(row["home_latitude"]), float(row["home_longitude"]), float(row["latitude"]), float(row["longitude"])),
                1,
            )
            if pd.notna(row.get("home_latitude"))
            and pd.notna(row.get("home_longitude"))
            and pd.notna(row.get("latitude"))
            and pd.notna(row.get("longitude"))
            else None,
            axis=1,
        )
    elif "distance_from_home" not in df.columns:
        df["distance_from_home"] = None
    previous_distances = {}
    if {"employee_id", "month", "sequence_number", "store_number", "latitude", "longitude"}.issubset(df.columns):
        for _, group in df.sort_values(["employee_id", "month_start", "sequence_number", "store_number"]).groupby(["employee_id", "month"], dropna=False):
            prev_lat = to_float(group.iloc[0].get("home_latitude"))
            prev_lon = to_float(group.iloc[0].get("home_longitude"))
            for idx, row in group.iterrows():
                lat = to_float(row.get("latitude"))
                lon = to_float(row.get("longitude"))
                if prev_lat is not None and prev_lon is not None and lat is not None and lon is not None:
                    previous_distances[idx] = round(haversine_miles(prev_lat, prev_lon, lat, lon), 1)
                    prev_lat = lat
                    prev_lon = lon
                else:
                    previous_distances[idx] = None
    df["miles_from_previous_stop"] = df.index.map(previous_distances) if previous_distances else df.get("miles_from_previous_stop", "")
    return ensure_pmt_schedule_columns(df)


def pmt_inactive_source_mask(df):
    if df.empty or "schedule_source" not in df.columns:
        return pd.Series(False, index=df.index)
    source = df["schedule_source"].fillna("").astype(str).str.lower()
    return (
        source.str.contains("superseded", regex=False)
        | source.str.contains("old schedule snapshot", regex=False)
        | source.str.contains("overflow removed", regex=False)
        | source.str.contains("rebuild removed", regex=False)
        | source.str.contains("out-of-period cleanup", regex=False)
    )


def canonicalize_current_pmt_rows(df, include_completed=False, deduplicate=True):
    if df.empty or "store_id" not in df.columns:
        return ensure_pmt_schedule_columns(df)
    scoped = df.copy()
    scoped = scoped[~pmt_inactive_source_mask(scoped)].copy()
    if scoped.empty:
        return ensure_pmt_schedule_columns(scoped)
    if not include_completed:
        scoped = scoped[pmt_active_item_mask(scoped)].copy()
    else:
        keep_mask = pmt_active_item_mask(scoped) | pmt_completed_item_mask(scoped)
        scoped = scoped[keep_mask].copy()
    if scoped.empty or not deduplicate:
        return ensure_pmt_schedule_columns(scoped)

    scoped["_status_rank"] = 2
    scoped.loc[pmt_completed_item_mask(scoped), "_status_rank"] = 1
    scoped.loc[pmt_active_item_mask(scoped), "_status_rank"] = 0
    scoped["_assigned_rank"] = 1
    if {"employee_id", "assigned_pmt_employee_id"}.issubset(scoped.columns):
        employee_ids = pd.to_numeric(scoped["employee_id"], errors="coerce").astype("Int64")
        assigned_ids = pd.to_numeric(scoped["assigned_pmt_employee_id"], errors="coerce").astype("Int64")
        scoped.loc[employee_ids.eq(assigned_ids), "_assigned_rank"] = 0
    scoped["_updated_sort"] = pd.to_datetime(scoped.get("updated_at"), errors="coerce")
    scoped["_created_sort"] = pd.to_datetime(scoped.get("created_at"), errors="coerce")
    scoped["_item_sort"] = pd.to_numeric(scoped.get("schedule_item_id"), errors="coerce").fillna(0)
    sort_cols = [
        "store_id",
        "_status_rank",
        "_assigned_rank",
        "_updated_sort",
        "_created_sort",
        "_item_sort",
    ]
    scoped = scoped.sort_values(
        sort_cols,
        ascending=[True, True, True, False, False, False],
        na_position="last",
    )
    keep_indices = scoped.groupby("store_id", dropna=False).head(1).index
    scoped = scoped.loc[keep_indices].copy()
    return ensure_pmt_schedule_columns(scoped.drop(columns=["_status_rank", "_assigned_rank", "_updated_sort", "_created_sort", "_item_sort"], errors="ignore"))


def get_current_active_pmt_schedule_rows(
    run_id, employee_id=None, start_date=None, end_date=None, include_completed=False, session=None, deduplicate=True,
):
    params = {
        "run_id": int(run_id),
        "include_completed": bool(include_completed),
    }
    optional_filters = []
    if employee_id is not None:
        optional_filters.append("and si.employee_id = :employee_id")
        params["employee_id"] = int(employee_id)
    if start_date is not None:
        optional_filters.append("and date(si.schedule_date) >= date(:start_date)")
        params["start_date"] = start_date
    if end_date is not None:
        optional_filters.append("and date(si.schedule_date) <= date(:end_date)")
        params["end_date"] = end_date
    optional_filter_sql = "\n          ".join(optional_filters)
    if optional_filter_sql:
        optional_filter_sql = f"\n          {optional_filter_sql}"
    df = read_query(
        f"""
        select si.id as schedule_item_id, si.schedule_id, si.pmt_schedule_run_id,
               si.schedule_date, si.sequence_number, si.status, si.completion_notes as notes,
               si.created_at, si.updated_at,
               e.id as employee_id, e.full_name as technician, e.home_latitude, e.home_longitude,
               s.id as store_id, s.store_number, s.address, s.city, s.state, s.zip,
               s.assigned_pmt_employee_id, owner.full_name as assigned_technician,
               s.latitude, s.longitude, si.work_type, si.cycle_label, si.schedule_source, si.original_schedule_date
        from schedule_items si
        left join pmt_schedule_runs r on r.id = si.pmt_schedule_run_id
        left join employees e on e.id = si.employee_id
        left join stores s on s.id = si.store_id
        left join employees owner on owner.id = s.assigned_pmt_employee_id
        where si.pmt_schedule_run_id = :run_id
          and si.work_type = 'PMT'
          and coalesce(lower(trim(r.status)), '') not in ('deleted','snapshot','archived','historical')
          {optional_filter_sql}
          and (
              (
                  :include_completed = true
                  and coalesce(nullif(lower(trim(si.status)), ''), 'scheduled') not in ('cancelled','canceled','skipped','deleted','transferred','superseded','archived')
              )
              or (
                  :include_completed = false
                  and coalesce(nullif(lower(trim(si.status)), ''), 'scheduled') not in ('completed','complete','cancelled','canceled','skipped','deleted','transferred','superseded','archived')
              )
          )
          and (r.cycle_start is null or date(si.schedule_date) >= date(r.cycle_start))
          and (r.cycle_end is null or date(si.schedule_date) <= date(r.cycle_end))
        order by si.schedule_date, e.full_name, si.sequence_number, s.store_number
        """,
        params,
        session=session,
    )
    if df.empty:
        return ensure_pmt_schedule_columns(df)
    normalized = normalize_pmt_schedule_rows(df)
    return canonicalize_current_pmt_rows(normalized, include_completed=include_completed, deduplicate=deduplicate)


PMT_ACTIVE_STATUSES = {"scheduled", "needs rescheduled", "rescheduled", "rain delay", "not completed"}


PMT_COMPLETED_STATUSES = {"completed", "complete"}


PMT_CANCELED_STATUSES = {"cancelled", "canceled", "skipped", "deleted", "transferred", "superseded", "archived"}


PMT_NON_EDITABLE_RUN_STATUSES = {"snapshot", "deleted", "archived", "historical"}


CANONICAL_PMT_COLUMNS = [
    "schedule_item_id",
    "schedule_id",
    "pmt_schedule_run_id",
    "employee_id",
    "technician",
    "store_id",
    "store_number",
    "schedule_date",
    "month_start",
    "month",
    "sequence_number",
    "status",
    "work_type",
    "schedule_source",
    "notes",
]


def ensure_pmt_schedule_columns(df, extra_columns=None):
    columns = list(dict.fromkeys(CANONICAL_PMT_COLUMNS + list(extra_columns or [])))
    if df is None:
        return pd.DataFrame(columns=columns)
    normalized = df.copy()
    for column in columns:
        if column not in normalized.columns:
            normalized[column] = pd.NA
    return normalized


def normalize_schedule_status(value):
    return clean(value).lower() or "scheduled"


def pmt_active_item_mask(df):
    statuses = df.get("status", pd.Series([], dtype=str)).apply(normalize_schedule_status)
    return statuses.isin(PMT_ACTIVE_STATUSES)


def pmt_completed_item_mask(df):
    statuses = df.get("status", pd.Series([], dtype=str)).apply(normalize_schedule_status)
    return statuses.isin(PMT_COMPLETED_STATUSES)


def pmt_canceled_item_mask(df):
    statuses = df.get("status", pd.Series([], dtype=str)).apply(normalize_schedule_status)
    return statuses.isin(PMT_CANCELED_STATUSES)



def editable_pmt_runs(session=None, end_on_or_after=None, latest_only=False):
    completion_filter = "and date(cycle_end) >= date(:end_on_or_after)" if end_on_or_after is not None else ""
    limit = "limit 1" if latest_only else ""
    return read_query(
        f"""
        select id, run_name, cycle_start, cycle_end
        from pmt_schedule_runs
        where coalesce(lower(trim(status)), '') not in ('deleted','snapshot','archived','historical')
        {completion_filter}
        order by created_at desc, id desc
        {limit}
        """,
        {"end_on_or_after": end_on_or_after},
        session=session,
    )


def current_pmt_run(session=None):
    runs = editable_pmt_runs(session, latest_only=True)
    return None if runs.empty else runs.iloc[0]
