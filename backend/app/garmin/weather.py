"""Historical weather for a run, from Open-Meteo (free, no API key). Lets the coach
read a run in context — a 'Very Weak' effort at 34°C/90% humidity is expected, not
alarming. Keyless, so nothing to store as a secret."""

import logging
import time
from datetime import date, datetime

import httpx

logger = logging.getLogger(__name__)

FORECAST_API = "https://api.open-meteo.com/v1/forecast"       # covers ~last 92 days
ARCHIVE_API = "https://archive-api.open-meteo.com/v1/archive"  # older (few-day delay)
HOURLY = "temperature_2m,relative_humidity_2m,apparent_temperature"


def fetch_weather(lat: float, lon: float, when_local: datetime) -> dict | None:
    """Temp / humidity / feels-like at the run's start hour, or None if unavailable.

    The archive (ERA5) endpoint holds everything older than a few days but lags real time;
    the forecast endpoint only reliably holds the most recent days (it returns 200 with
    all-null values for older dates). So prefer the endpoint likely to have data for this
    date, and fall back to the other if the first comes back empty — this covers the
    archive's few-day delay at the boundary in both directions."""
    if lat is None or lon is None:
        return None
    days_ago = (date.today() - when_local.date()).days
    order = (FORECAST_API, ARCHIVE_API) if days_ago <= 5 else (ARCHIVE_API, FORECAST_API)
    for base in order:
        w = _fetch_one(base, lat, lon, when_local)
        if w:
            return w
    return None


def _fetch_one(base: str, lat: float, lon: float, when_local: datetime) -> dict | None:
    """Query one Open-Meteo endpoint; return the reading or None if it has no data."""
    d = when_local.date()
    hour_key = when_local.strftime("%Y-%m-%dT%H:00")
    params = {
        "latitude": round(lat, 3), "longitude": round(lon, 3),
        "hourly": HOURLY, "timezone": "auto",
        "start_date": d.isoformat(), "end_date": d.isoformat(),
    }
    try:
        resp = None
        for attempt in range(3):  # Open-Meteo occasionally resets a burst connection
            try:
                resp = httpx.get(base, params=params, timeout=20)
                break
            except httpx.HTTPError:
                if attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))
        if resp is None or resp.status_code != 200:
            logger.warning("Open-Meteo %s", resp.status_code if resp else "no response")
            return None
        h = resp.json().get("hourly", {})
        times = h.get("time", [])
        if not times:
            return None
        idx = times.index(hour_key) if hour_key in times else min(
            range(len(times)), key=lambda i: abs(int(times[i][11:13]) - when_local.hour)
        )
        temp = h.get("temperature_2m", [None] * len(times))[idx]
        hum = h.get("relative_humidity_2m", [None] * len(times))[idx]
        feels = h.get("apparent_temperature", [None] * len(times))[idx]
        if temp is None and hum is None:
            return None
        return {
            "temp_c": round(temp, 1) if temp is not None else None,
            "humidity": round(hum) if hum is not None else None,
            "feels_c": round(feels, 1) if feels is not None else None,
        }
    except httpx.HTTPError as exc:
        logger.warning("Weather fetch failed: %s", exc)
        return None
