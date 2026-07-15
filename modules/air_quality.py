"""
modules/air_quality.py
Vantage - Sky Clarity (smoke / dust) Tracker

Flags airborne particulates - wildfire smoke, Saharan dust, general haze -
that affect sunrise/sunset color and visibility. Uses Open-Meteo's free
Air Quality API (CAMS model, no key required) for aerosol optical depth
(AOD), PM2.5, PM10, and mineral dust, plus the standard forecast API for
wind, which doubles as a lightweight "which way is it drifting" indicator.

AOD (aerosol optical depth) is the primary signal: it reflects the total
column of suspended particulate, including smoke/dust aloft that surface
PM2.5 readings can miss entirely (common with long-range wildfire smoke
transport and the Saharan Dust Layer).

Score: 0-100, banded every 20 points
  0-20    Clear
  20-40   Light Haze
  40-60   Moderate Haze
  60-80   Heavy Haze
  80-100  Severe

Alerts fire at score >= 60 (Heavy or worse) via ntfy.sh.

Dependencies: requests, astral (already required by sunrise_sunset.py)
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from datetime import datetime, timedelta
from astral import LocationInfo
from astral.sun import sun
from modules.base import BaseModule
from location import get_location

# ── Configuration ────────────────────────────────────────────────────────────

AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
FORECAST_URL    = "https://api.open-meteo.com/v1/forecast"

NTFY_TOPIC = "vantage-photography"

ALERT_SCORE_THRESHOLD = 60  # Heavy Haze or worse

BAND_COLORS = {
    "Clear":         "#838f72",
    "Light Haze":    "#c79a4b",
    "Moderate Haze": "#c17f3a",
    "Heavy Haze":    "#b5674c",
    "Severe":        "#8a3f2c",
    "Unknown":       "#9a9284",
}

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

# ── Helpers ───────────────────────────────────────────────────────────────────

def classify_aod(aod):
    """Map aerosol optical depth to a 0-100 score and a severity band."""
    if aod is None:
        return 0, "Unknown"
    bands = [(0.10, "Clear"), (0.30, "Light Haze"), (0.60, "Moderate Haze"), (1.00, "Heavy Haze")]
    lo, score_lo = 0.0, 0
    for hi, label in bands:
        if aod < hi:
            frac = (aod - lo) / (hi - lo)
            return round(score_lo + frac * 20), label
        lo, score_lo = hi, score_lo + 20
    frac = min((aod - 1.0) / 1.0, 1.0)
    return round(80 + frac * 20), "Severe"


def classify_source(pm2_5, pm10, dust, aod):
    """
    Rough heuristic for what's likely driving the reading. Not true source
    attribution (that needs back-trajectory modeling) - just a useful hint.
    """
    pm2_5 = pm2_5 or 0
    pm10  = pm10 or 0
    dust  = dust or 0
    aod   = aod or 0
    dust_frac = (dust / pm10) if pm10 else 0

    if dust_frac > 0.4 and dust > 15:
        return "Dust"
    if pm2_5 > 12 and dust_frac < 0.25:
        return "Smoke"
    if aod > 0.15 and pm2_5 <= 12 and dust <= 10:
        return "Aloft Haze"
    return "Background"


def _compass(deg):
    if deg is None:
        return "N/A"
    return COMPASS[int((deg / 22.5) + 0.5) % 16]


# ── Module ────────────────────────────────────────────────────────────────────

class AirQualityModule(BaseModule):
    name     = "Sky Clarity"
    interval = 1800  # refresh every 30 minutes

    def __init__(self):
        self._last_data = {}
        self._alerted_this_cycle = False
        # Populated from the shared location store at the start of each fetch.
        self._lat = None
        self._lon = None
        self._tz = None
        self._place = None

    # ── API calls ─────────────────────────────────────────────────────────────

    def _fetch_air_quality(self):
        resp = requests.get(
            AIR_QUALITY_URL,
            params={
                "latitude": self._lat,
                "longitude": self._lon,
                "hourly": "pm10,pm2_5,dust,aerosol_optical_depth",
                "current": "pm10,pm2_5,dust,aerosol_optical_depth,us_aqi",
                "timezone": self._tz,
                "forecast_days": 2,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _fetch_wind(self):
        resp = requests.get(
            FORECAST_URL,
            params={
                "latitude": self._lat,
                "longitude": self._lon,
                "current": "wind_speed_10m,wind_direction_10m",
                "wind_speed_unit": "mph",
                "timezone": self._tz,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _extract_current(self, aq):
        current = aq.get("current")
        if current:
            return current
        hourly = aq.get("hourly", {})
        if not hourly.get("time"):
            return {}
        return {
            "aerosol_optical_depth": hourly.get("aerosol_optical_depth", [None])[0],
            "pm2_5": hourly.get("pm2_5", [None])[0],
            "pm10": hourly.get("pm10", [None])[0],
            "dust": hourly.get("dust", [None])[0],
        }

    # ── Astronomical helpers (mirrors modules/sunrise_sunset.py) ────────────────

    def _get_sun_times(self, date):
        location = LocationInfo(
            name=self._place or "Home", region="USA", timezone=self._tz,
            latitude=self._lat, longitude=self._lon,
        )
        s = sun(location.observer, date=date, tzinfo=location.timezone)
        return s["sunrise"], s["sunset"]

    def _score_sun_events(self, hourly):
        """Find the AOD reading nearest each upcoming sunrise/sunset."""
        times = hourly.get("time", [])
        aods  = hourly.get("aerosol_optical_depth", [])
        pm25s = hourly.get("pm2_5", [])
        dusts = hourly.get("dust", [])

        parsed = []
        for i, t in enumerate(times):
            try:
                dt_local = datetime.fromisoformat(t)
            except ValueError:
                continue
            parsed.append((
                dt_local,
                aods[i] if i < len(aods) else None,
                pm25s[i] if i < len(pm25s) else None,
                dusts[i] if i < len(dusts) else None,
            ))
        if not parsed:
            return []

        now = datetime.now()
        today = now.date()
        events = []

        for day_offset in range(2):
            target_date = today + timedelta(days=day_offset)
            sunrise_dt, sunset_dt = self._get_sun_times(target_date)
            sunrise_naive = sunrise_dt.replace(tzinfo=None)
            sunset_naive = sunset_dt.replace(tzinfo=None)

            for name, evt_time in [("Sunrise", sunrise_naive), ("Sunset", sunset_naive)]:
                if evt_time < now - timedelta(minutes=30):
                    continue
                nearest = min(parsed, key=lambda p: abs((p[0] - evt_time).total_seconds()))
                _, aod, pm2_5, dust = nearest
                score, band = classify_aod(aod)
                events.append({
                    "event": name,
                    "event_time": evt_time.isoformat(),
                    "score": score,
                    "band": band,
                    "color": BAND_COLORS.get(band, BAND_COLORS["Unknown"]),
                    "aod": aod,
                    "pm2_5": pm2_5,
                    "dust": dust,
                })

        events.sort(key=lambda e: e["event_time"])
        return events

    # ── BaseModule interface ─────────────────────────────────────────────────

    def fetch(self):
        loc = get_location()
        self._lat, self._lon, self._tz = loc["lat"], loc["lon"], loc["tz"]
        self._place = loc["place_name"]

        try:
            aq = self._fetch_air_quality()
        except requests.RequestException as e:
            self._last_data = {"status": "error", "error": str(e)}
            return

        try:
            wind_current = self._fetch_wind().get("current", {})
        except requests.RequestException:
            wind_current = {}

        current = self._extract_current(aq)
        aod    = current.get("aerosol_optical_depth")
        pm2_5  = current.get("pm2_5")
        pm10   = current.get("pm10")
        dust   = current.get("dust")

        score, band = classify_aod(aod)
        wind_speed = wind_current.get("wind_speed_10m")
        wind_deg   = wind_current.get("wind_direction_10m")

        self._last_data = {
            "status": "ok",
            "score": score,
            "band": band,
            "color": BAND_COLORS.get(band, BAND_COLORS["Unknown"]),
            "source": classify_source(pm2_5, pm10, dust, aod),
            "current": {"pm2_5": pm2_5, "pm10": pm10, "dust": dust, "aod": aod},
            "wind": {
                "speed_mph": wind_speed,
                "from_deg": wind_deg,
                "from_compass": _compass(wind_deg),
            },
            "sun_events": self._score_sun_events(aq.get("hourly", {})),
            "place_name": self._place,
            "map": {
                "lat": self._lat,
                "lon": self._lon,
                "color": BAND_COLORS.get(band, BAND_COLORS["Unknown"]),
                "wind_from_deg": wind_deg,
                "wind_speed_mph": wind_speed,
            },
        }
        print(f"[Sky Clarity] {self._place}: AOD {aod} -> {band} ({score})")

    def status(self):
        if not self._last_data:
            return {"status": "pending", "band": "Unknown", "score": 0,
                     "color": BAND_COLORS["Unknown"], "sun_events": []}
        return self._last_data

    def check_alert(self):
        if self._last_data.get("status") != "ok":
            return False
        score = self._last_data.get("score", 0)
        if score >= ALERT_SCORE_THRESHOLD:
            if not self._alerted_this_cycle:
                self._alerted_this_cycle = True
                self._send_ntfy_alert()
                return True
            return False
        self._alerted_this_cycle = False
        return False

    # ── Notifications ─────────────────────────────────────────────────────────

    def _send_ntfy_alert(self):
        data = self._last_data
        title = f"\U0001F32B️ {data['band']} — Sky Clarity {data['score']}/100"
        message = (
            f"Source: {data['source']}\n"
            f"AOD {data['current']['aod']}  PM2.5 {data['current']['pm2_5']} µg/m³  "
            f"Dust {data['current']['dust']} µg/m³\n"
            f"Wind: {data['wind']['from_compass']} at {data['wind']['speed_mph']} mph"
        )
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": title, "Priority": "default", "Tags": "fog"},
                timeout=5,
            )
        except requests.RequestException:
            pass


# ── Standalone test ───────────────────────────────────────────────────────────
#   python modules/air_quality.py

if __name__ == "__main__":
    import pprint
    mod = AirQualityModule()
    print("Fetching Open-Meteo air quality + wind...")
    mod.fetch()
    print("\nStatus:")
    pprint.pprint(mod.status())
    print("\nAlert check:", mod.check_alert())
