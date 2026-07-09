import math

import pandas as pd


ASSIGNMENT_LAYERS = {
    "Brand Enhancement": {
        "owner_columns": ["brand_team", "brand_area"],
        "fallback_columns": ["brand_technician", "brand_person"],
    },
    "PMT": {
        "owner_columns": ["pmt_technician", "pmt_person"],
        "fallback_columns": ["pmt_team", "pmt_area"],
    },
    "Calibration": {
        "owner_columns": ["calibration_technician", "calibration_person"],
        "fallback_columns": ["calibration_team", "calibration_area"],
    },
}


def miles_per_longitude_degree(latitude):
    return max(69.0 * math.cos(math.radians(float(latitude or 0))), 1.0)


def geographic_coverage_summary(stores_df, work_group="All Work Groups"):
    if stores_df is None or stores_df.empty:
        return pd.DataFrame(
            columns=[
                "Work Group",
                "Assigned To",
                "Store Count",
                "Coverage Sq Miles",
                "North-South Miles",
                "East-West Miles",
                "Max Spread Miles",
                "Avg Stores / 100 Sq Miles",
                "Drive Time Risk",
            ]
        )

    rows = []
    groups = [work_group] if work_group in ASSIGNMENT_LAYERS else list(ASSIGNMENT_LAYERS.keys())
    stores = stores_df.copy()
    stores["latitude"] = pd.to_numeric(stores.get("latitude"), errors="coerce")
    stores["longitude"] = pd.to_numeric(stores.get("longitude"), errors="coerce")

    for group in groups:
        config = ASSIGNMENT_LAYERS[group]
        owner_col = next((column for column in config["owner_columns"] if column in stores.columns), None)
        fallback_col = next((column for column in config["fallback_columns"] if column in stores.columns), None)
        if not owner_col:
            continue
        layer = stores.dropna(subset=["latitude", "longitude"]).copy()
        if layer.empty:
            continue
        layer["Assigned To"] = layer[owner_col].fillna("").astype(str).str.strip()
        if fallback_col:
            fallback = layer[fallback_col].fillna("").astype(str).str.strip()
            layer.loc[layer["Assigned To"].eq(""), "Assigned To"] = fallback
        layer = layer[layer["Assigned To"].ne("")]
        if layer.empty:
            continue
        for assigned_to, assigned in layer.groupby("Assigned To", dropna=False):
            count = len(assigned)
            min_lat = float(assigned["latitude"].min())
            max_lat = float(assigned["latitude"].max())
            min_lon = float(assigned["longitude"].min())
            max_lon = float(assigned["longitude"].max())
            center_lat = float(assigned["latitude"].mean())
            north_south = abs(max_lat - min_lat) * 69.0
            east_west = abs(max_lon - min_lon) * miles_per_longitude_degree(center_lat)
            square_miles = north_south * east_west
            max_spread = math.hypot(north_south, east_west)
            density = (count / square_miles * 100) if square_miles > 0 else count * 100
            if square_miles >= 2500 or max_spread >= 90:
                risk = "High"
            elif square_miles >= 900 or max_spread >= 45:
                risk = "Medium"
            else:
                risk = "Low"
            rows.append(
                {
                    "Work Group": group,
                    "Assigned To": assigned_to,
                    "Store Count": count,
                    "Coverage Sq Miles": round(square_miles, 1),
                    "North-South Miles": round(north_south, 1),
                    "East-West Miles": round(east_west, 1),
                    "Max Spread Miles": round(max_spread, 1),
                    "Avg Stores / 100 Sq Miles": round(density, 1),
                    "Drive Time Risk": risk,
                }
            )

    result = pd.DataFrame(rows)
    if result.empty:
        return geographic_coverage_summary(pd.DataFrame())
    return result.sort_values(["Coverage Sq Miles", "Max Spread Miles", "Store Count"], ascending=[False, False, False])
