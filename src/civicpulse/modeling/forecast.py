"""Create and persist rolling, next-24-hour Poisson forecasts for RAG/dashboard use."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
from scipy.stats import poisson

from civicpulse import config
from civicpulse.modeling import poisson_model

# Same locations currently covered by the hourly Open-Meteo historical weather job.
CITY_COORDINATES = {
    "Bahawalpur": (29.3956, 71.6836),
    "Faisalabad": (31.4504, 73.1350),
    "Gujranwala": (32.1877, 74.1945),
    "Islamabad": (33.6844, 73.0479),
    "Karachi": (24.8607, 67.0011),
    "Lahore": (31.5204, 74.3587),
    "Mardan": (34.1989, 72.0407),
    "Multan": (30.1575, 71.5249),
    "Peshawar": (34.0151, 71.5249),
    "Quetta": (30.1798, 66.9750),
    "Rawalpindi": (33.5651, 73.0169),
    "Sialkot": (32.4945, 74.5229),
}
WEATHER_FIELDS = ("temperature_2m", "precipitation", "wind_speed_10m")


def _fetch_forecast_weather(city: str) -> tuple[dict[str, dict], str | None]:
    """Fetch hourly forecast weather for one supported city; return empty values on failure."""
    coordinates = CITY_COORDINATES.get(city)
    if coordinates is None:
        return {}, None
    latitude, longitude = coordinates
    try:
        response = requests.get(
            config.OPEN_METEO_FORECAST_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "hourly": ",".join(WEATHER_FIELDS),
                "forecast_days": 2,
                "timezone": "UTC",
            },
            timeout=(10, 30),
        )
        response.raise_for_status()
        payload = response.json()["hourly"]
        weather_by_hour = {}
        for index, stamp in enumerate(payload["time"]):
            weather_by_hour[pd.Timestamp(stamp, tz="UTC")] = {
                name: payload[name][index] for name in WEATHER_FIELDS
            }
        return weather_by_hour, response.url
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        print(f"Weather forecast unavailable for {city}; model will impute weather: {exc}")
        return {}, None


def generate_hourly_forecasts(
    con,
    *,
    horizon_hours: int = 24,
    retention_days: int = 30,
) -> dict[str, object]:
    """Fit using all Gold observations through the latest hour; save future forecasts."""
    if horizon_hours < 1 or horizon_hours > 48:
        raise ValueError("horizon_hours must be between 1 and 48.")
    if retention_days < 1:
        raise ValueError("retention_days must be positive.")

    latest = con.execute(
        "SELECT max(hour_utc) FROM gold.hourly_issue_counts_with_weather"
    ).fetchone()[0]
    if latest is None:
        raise RuntimeError("Gold has no hourly observations; build the warehouse first.")

    latest = pd.Timestamp(latest, tz="UTC") if pd.Timestamp(latest).tzinfo is None else pd.Timestamp(latest).tz_convert("UTC")
    training_cutoff = latest.floor("h") + pd.Timedelta(hours=1)
    cutoff_iso = training_cutoff.isoformat()
    now_utc = pd.Timestamp(datetime.now(timezone.utc)).floor("h")
    forecast_start = max(training_cutoff, now_utc + pd.Timedelta(hours=1))

    categories = con.execute(
        "SELECT DISTINCT city, issue_type FROM gold.hourly_issue_counts_with_weather "
        "WHERE city IS NOT NULL AND issue_type IS NOT NULL ORDER BY city, issue_type"
    ).fetchall()
    if not categories:
        raise RuntimeError("Gold has no city/issue categories to forecast.")
    cities = sorted({city for city, _ in categories})

    selected = poisson_model.select_poisson_alpha(con, cutoff_utc=cutoff_iso)
    fitted = poisson_model.fit_explainable_poisson_model(
        con, cutoff_utc=cutoff_iso, alpha=float(selected["selected_alpha"])
    )
    forecast_hours = pd.date_range(
        start=forecast_start,
        periods=horizon_hours,
        freq="h",
    )
    weather_by_city: dict[str, dict[str, dict]] = {}
    weather_urls: dict[str, str | None] = {}
    for city in cities:
        values, url = _fetch_forecast_weather(city)
        weather_by_city[city] = values
        weather_urls[city] = url

    rows = []
    for forecast_hour in forecast_hours:
        for city, issue in categories:
            weather = weather_by_city.get(city, {}).get(forecast_hour, {})
            rows.append({
                "hour_utc": forecast_hour,
                "city": city,
                "issue_type": issue,
                "hour_of_day": forecast_hour.hour,
                "day_of_week": forecast_hour.dayofweek,
                **{name: weather.get(name, np.nan) for name in WEATHER_FIELDS},
            })

    future_frame = pd.DataFrame(rows)
    features = future_frame[fitted["feature_columns"]]
    transformed = fitted["preprocessor"].transform(features)
    means = np.clip(fitted["model"].predict(transformed), 0, None)
    lower = poisson.ppf(0.025, means)
    upper = poisson.ppf(0.975, means)

    run_id = "poisson_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    generated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    con.execute("""
        CREATE TABLE IF NOT EXISTS gold.poisson_forecasts (
            forecast_run_id VARCHAR,
            generated_at_utc TIMESTAMP,
            forecast_for_utc TIMESTAMP,
            city VARCHAR,
            issue_type VARCHAR,
            expected_reports DOUBLE,
            lower_95 BIGINT,
            upper_95 BIGINT,
            training_cutoff_utc TIMESTAMP,
            training_rows BIGINT,
            alpha DOUBLE,
            temperature_2m DOUBLE,
            precipitation DOUBLE,
            wind_speed_10m DOUBLE,
            weather_source_url VARCHAR
        )
    """)
    con.executemany(
        """INSERT INTO gold.poisson_forecasts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                run_id,
                generated_at,
                row.hour_utc.to_pydatetime().replace(tzinfo=None),
                row.city,
                row.issue_type,
                float(means[index]),
                int(lower[index]),
                int(upper[index]),
                training_cutoff.to_pydatetime().replace(tzinfo=None),
                int(fitted["training_rows"]),
                float(selected["selected_alpha"]),
                None if pd.isna(row.temperature_2m) else float(row.temperature_2m),
                None if pd.isna(row.precipitation) else float(row.precipitation),
                None if pd.isna(row.wind_speed_10m) else float(row.wind_speed_10m),
                weather_urls.get(row.city),
            )
            for index, row in enumerate(future_frame.itertuples(index=False))
        ],
    )
    con.execute(
        "DELETE FROM gold.poisson_forecasts WHERE generated_at_utc < ?",
        [generated_at - timedelta(days=retention_days)],
    )
    return {
        "forecast_run_id": run_id,
        "generated_at_utc": generated_at.replace(tzinfo=timezone.utc).isoformat(),
        "training_cutoff_utc": cutoff_iso,
        "training_rows": int(fitted["training_rows"]),
        "alpha": float(selected["selected_alpha"]),
        "forecast_rows": len(rows),
        "cities": len(cities),
        "horizon_hours": horizon_hours,
    }
