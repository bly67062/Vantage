"""
modules/sunrise_sunset.py
Vantage - Sunrise/Sunset Quality Scorer

Scores the photographic quality of upcoming sunrise and sunset windows
using NWS hourly forecast data. Looks for the altocumulus/broken mid-level
cloud conditions that produce vivid color like the 5/27/26 Indianapolis sunrise.

Scoring factors:
  - Mid-level cloud cover (600-700mb / ~6,500-10,000ft) — the money layer
  - Low-level cloud cover (below 6,500ft) — can block color if too thick
  - High cloud cover (above 20,000ft) — diffuses and washes out color
  - Surface relative humidity — higher RH amplifies saturation
  - Wind speed — calm surface aids fog/haze layer that boosts color
  - Time proximity to golden hour — score degrades if window is far off

Score: 0–100
  80–100  Excellent — high probability of vivid color, go shoot
  60–79   Good — worth watching, likely worthwhile
  40–59   Moderate — possible color, not reliable
  0–39    Poor — overcast, clear, or wrong cloud mix

Alerts fire at score >= 70 and are sent via ntfy.sh.

Dependencies:
    pip install astral requests
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
import sqlite3
import json
from datetime import datetime, timedelta, timezone
from astral import LocationInfo
from astral.sun import sun
from modules.base import BaseModule

# ── Configuration ────────────────────────────────────────────────────────────

# Indianapolis coordinates
LATITUDE  = 39.7684
LONGITUDE = -86.1581
TIMEZONE  = "America/Indiana/Indianapolis"

# NWS API — no key required, just a descriptive User-Agent
NWS_POINTS_URL = f"https://api.weather.gov/points/{LATITUDE},{LONGITUDE}"
NWS_USER_AGENT = "(Vantage photography tracker, joefent@Gmail.com)"

# ntfy.sh topic for push alerts — change to your topic
NTFY_TOPIC = "vantage-photography"

# Score threshold to trigger an alert
ALERT_THRESHOLD = 70

# SQLite database path (relative to project root)
DB_PATH = "vantage.db"

# ── Module ────────────────────────────────────────────────────────────────────

class SunriseSunsetModule(BaseModule):
    name     = "Sunrise / Sunset"
    interval = 3600  # refresh hourly

    def __init__(self):
        self._forecast_url  = None   # populated on first fetch
        self._last_data     = {}     # raw results dict
        self._alert_sent    = {}     # track sent alerts: {event_key: True}
        self._init_db()

    # ── Database ──────────────────────────────────────────────────────────────

    def _init_db(self):
        """Create the scores table if it doesn't exist."""
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sunrise_sunset_scores (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    event       TEXT    NOT NULL,   -- 'sunrise' or 'sunset'
                    event_time  TEXT    NOT NULL,   -- ISO8601
                    score       INTEGER NOT NULL,
                    label       TEXT    NOT NULL,
                    factors     TEXT    NOT NULL,   -- JSON blob of raw inputs
                    recorded_at TEXT    NOT NULL
                )
            """)
            conn.commit()

    def _save_score(self, event, event_time, score, label, factors):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO sunrise_sunset_scores
                    (event, event_time, score, label, factors, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                event,
                event_time.isoformat(),
                score,
                label,
                json.dumps(factors),
                datetime.now(timezone.utc).isoformat()
            ))
            conn.commit()

    # ── NWS API helpers ───────────────────────────────────────────────────────

    def _get_forecast_url(self):
        """
        NWS requires a two-step lookup:
          1. /points/{lat},{lon}  →  returns the gridpoint URL
          2. gridpoint URL/forecast/hourly  →  actual hourly data
        We cache the gridpoint URL so we only hit step 1 once.
        """
        if self._forecast_url:
            return self._forecast_url

        resp = requests.get(
            NWS_POINTS_URL,
            headers={"User-Agent": "joefent@gmail.com"},
            timeout=10
        )
        resp.raise_for_status()
        props = resp.json()["properties"]
        self._forecast_url = props["forecastHourly"]
        return self._forecast_url

    def _fetch_hourly_forecast(self):
        """Return list of hourly forecast period dicts from NWS."""
        url = self._get_forecast_url()
        resp = requests.get(
            url,
            headers={"User-Agent": NWS_USER_AGENT},
            timeout=10
        )
        resp.raise_for_status()
        return resp.json()["properties"]["periods"]

    # ── Astronomical helpers ──────────────────────────────────────────────────

    def _get_sun_times(self, date):
        """
        Return sunrise and sunset datetime objects for a given date.
        Uses the Astral library — no external API needed.
        """
        location = LocationInfo(
            name="Indianapolis",
            region="USA",
            timezone=TIMEZONE,
            latitude=LATITUDE,
            longitude=LONGITUDE
        )
        s = sun(location.observer, date=date, tzinfo=location.timezone)
        return s["sunrise"], s["sunset"]

    # ── Scoring engine ────────────────────────────────────────────────────────

    def _find_forecast_period(self, periods, target_time):
        """
        Find the hourly forecast period whose start time is closest to
        target_time (within ±90 minutes). Returns None if no match found.
        """
        best      = None
        best_diff = timedelta(minutes=91)

        for period in periods:
            # NWS timestamps are ISO8601 with timezone offset
            start = datetime.fromisoformat(period["startTime"])
            diff  = abs(start - target_time)
            if diff < best_diff:
                best_diff = diff
                best      = period

        return best

    def _score_period(self, period):
        """
        Score a single hourly forecast period for photographic sky quality.

        NWS hourly data gives us:
          - shortForecast: text like "Partly Cloudy", "Mostly Cloudy", etc.
          - cloudLayers: list of dicts with 'amount' and 'base' (when available)
          - relativeHumidity: dict with 'value'
          - windSpeed: string like "10 mph"
          - temperature, dewpoint, etc.

        The NWS shortForecast text is our main cloud-cover signal since
        cloudLayers is not always present in all NWS grid cells.

        Returns (score: int, label: str, factors: dict)
        """
        factors = {}

        # ── Cloud cover from shortForecast text ──
        forecast_text = period.get("shortForecast", "").lower()

        # Map NWS text to a cloud cover bucket
        # These buckets drive the mid-level cloud scoring below
        if any(x in forecast_text for x in ["clear", "sunny", "fair"]):
            cloud_bucket = "clear"
        elif any(x in forecast_text for x in ["mostly clear", "mostly sunny"]):
            cloud_bucket = "mostly_clear"
        elif any(x in forecast_text for x in ["partly cloudy", "partly sunny"]):
            cloud_bucket = "partly_cloudy"   # SWEET SPOT for vivid sunrise/sunset
        elif any(x in forecast_text for x in ["mostly cloudy"]):
            cloud_bucket = "mostly_cloudy"
        elif any(x in forecast_text for x in ["overcast", "cloudy"]):
            cloud_bucket = "overcast"
        elif any(x in forecast_text for x in ["fog", "mist", "haze"]):
            cloud_bucket = "fog_haze"        # can be spectacular if thin
        elif any(x in forecast_text for x in ["thunderstorm", "rain", "snow", "shower"]):
            cloud_bucket = "precip"
        else:
            cloud_bucket = "unknown"

        factors["forecast_text"] = period.get("shortForecast", "N/A")
        factors["cloud_bucket"]  = cloud_bucket

        # ── Cloud score ──
        # Partly cloudy broken altocumulus is peak. Clear skies are actually
        # mediocre — you need clouds to scatter the color.
        cloud_score_map = {
            "clear":        30,   # boring, no scattering surface
            "mostly_clear": 50,   # some color possible
            "partly_cloudy": 90,  # broken mid-level cloud = vivid color
            "mostly_cloudy": 55,  # can still be good if upper layer is thin
            "overcast":      15,  # blocks light entirely
            "fog_haze":      60,  # low fog + color can be otherworldly
            "precip":         5,  # rain kills it
            "unknown":       40,
        }
        cloud_score = cloud_score_map.get(cloud_bucket, 40)

        # ── Humidity bonus ──
        # Higher RH means more moisture in the air to scatter/saturate color.
        # NWS returns relativeHumidity as {"unitCode": "...", "value": 72}
        rh = None
        rh_raw = period.get("relativeHumidity")
        if rh_raw and isinstance(rh_raw, dict):
            rh = rh_raw.get("value")
        elif isinstance(rh_raw, (int, float)):
            rh = rh_raw

        rh_bonus = 0
        if rh is not None:
            factors["relative_humidity"] = rh
            if rh >= 75:
                rh_bonus = 10
            elif rh >= 60:
                rh_bonus = 5
            elif rh >= 45:
                rh_bonus = 2
        else:
            factors["relative_humidity"] = "N/A"

        # ── Wind penalty ──
        # High wind stirs the atmosphere and can break up the color-scattering
        # layers. Calm conditions favor saturation.
        wind_str = period.get("windSpeed", "0 mph")
        try:
            wind_mph = int(wind_str.split()[0])
        except (ValueError, IndexError):
            wind_mph = 0

        factors["wind_mph"] = wind_mph

        wind_penalty = 0
        if wind_mph > 20:
            wind_penalty = 15
        elif wind_mph > 12:
            wind_penalty = 7
        elif wind_mph > 7:
            wind_penalty = 3

        # ── Final score ──
        raw_score = cloud_score + rh_bonus - wind_penalty
        score = max(0, min(100, raw_score))   # clamp to 0–100

        # ── Label ──
        if score >= 80:
            label = "Excellent"
        elif score >= 60:
            label = "Good"
        elif score >= 40:
            label = "Moderate"
        else:
            label = "Poor"

        return score, label, factors

    # ── BaseModule interface ──────────────────────────────────────────────────

    def fetch(self):
        """
        Fetch hourly forecast and score the next sunrise and sunset.
        Results are stored in self._last_data and written to SQLite.
        """
        now     = datetime.now(timezone.utc)
        today   = now.date()
        results = {}

        try:
            periods = self._fetch_hourly_forecast()
        except requests.RequestException as e:
            self._last_data = {"error": str(e)}
            return

        # Score today's remaining events + tomorrow's if today's are past
        for day_offset in range(2):
            target_date = today + timedelta(days=day_offset)
            sunrise_dt, sunset_dt = self._get_sun_times(target_date)

            for event_name, event_time in [("sunrise", sunrise_dt), ("sunset", sunset_dt)]:
                # Skip events already more than 30 minutes in the past
                if event_time < now - timedelta(minutes=30):
                    continue

                period = self._find_forecast_period(periods, event_time)
                if not period:
                    continue

                score, label, factors = self._score_period(period)

                event_key = f"{event_name}_{target_date.isoformat()}"
                results[event_key] = {
                    "event":      event_name,
                    "event_time": event_time.isoformat(),
                    "score":      score,
                    "label":      label,
                    "factors":    factors,
                }

                # Save to DB (overwrite today's entry if already there)
                self._save_score(event_name, event_time, score, label, factors)

        self._last_data = results

    def status(self):
        """
        Return current state as a dict for the dashboard card.
        """
        if not self._last_data:
            return {
                "module":  self.name,
                "status":  "No data — run fetch() first",
                "events":  []
            }

        if "error" in self._last_data:
            return {
                "module": self.name,
                "status": "error",
                "error":  self._last_data["error"],
                "events": []
            }

        events = []
        for key, data in sorted(self._last_data.items()):
            events.append({
                "event":      data["event"].capitalize(),
                "event_time": data["event_time"],
                "score":      data["score"],
                "label":      data["label"],
                "forecast":   data["factors"].get("forecast_text", "N/A"),
                "humidity":   data["factors"].get("relative_humidity", "N/A"),
                "wind_mph":   data["factors"].get("wind_mph", "N/A"),
            })

        best = max(events, key=lambda e: e["score"]) if events else None

        return {
            "module":     self.name,
            "status":     "ok",
            "best_event": best,
            "events":     events,
        }

    def check_alert(self):
        """
        Return True if any upcoming event scores above ALERT_THRESHOLD
        and we haven't already alerted for it this cycle.
        Sends ntfy.sh push notification when alert fires.
        """
        if not self._last_data or "error" in self._last_data:
            return False

        alert_fired = False

        for key, data in self._last_data.items():
            if data["score"] >= ALERT_THRESHOLD and key not in self._alert_sent:
                self._alert_sent[key] = True
                alert_fired = True
                self._send_ntfy_alert(data)

        return alert_fired

    # ── Notifications ─────────────────────────────────────────────────────────

    def _send_ntfy_alert(self, data):
        """
        Push a notification via ntfy.sh.
        Sends to https://ntfy.sh/{NTFY_TOPIC}
        """
        event_name  = data["event"].capitalize()
        event_time  = datetime.fromisoformat(data["event_time"])
        time_str    = event_time.strftime("%-I:%M %p")   # e.g. "6:14 AM"
        score       = data["score"]
        label       = data["label"]
        forecast    = data["factors"].get("forecast_text", "N/A")

        title   = f"📷 {label} {event_name} — Score {score}/100"
        message = (
            f"{event_name} at {time_str}\n"
            f"Sky: {forecast}\n"
            f"RH: {data['factors'].get('relative_humidity', 'N/A')}%  "
            f"Wind: {data['factors'].get('wind_mph', 'N/A')} mph"
        )

        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={
                    "Title":    title,
                    "Priority": "high" if score >= 80 else "default",
                    "Tags":     "sunrise,camera,sunny",
                },
                timeout=5
            )
        except requests.RequestException:
            # Don't crash the scheduler if ntfy is unreachable
            pass


# ── Standalone test ───────────────────────────────────────────────────────────
# Run this file directly to test without spinning up the full Flask app:
#   python modules/sunrise_sunset.py

if __name__ == "__main__":
    import pprint
    mod = SunriseSunsetModule()
    print("Fetching NWS forecast...")
    mod.fetch()
    print("\nStatus:")
    pprint.pprint(mod.status())
    print("\nAlert check:", mod.check_alert())
