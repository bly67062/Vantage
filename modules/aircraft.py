"""
modules/aircraft.py
Vantage - Aircraft / Sun Intercept

Two halves:

  1. AircraftTrafficModule (BaseModule) - a passive background collector.
     Polls OpenSky Network (free, no key needed for low-volume anonymous
     use) for aircraft near a home watch area every few minutes, and logs
     each sighting's bearing and apparent elevation *as seen from the
     ground* - not the aircraft's own heading - since that's what tells
     us which patch of sky actually sees traffic.

  2. Planner functions - on-demand helpers used by the /planner route.
     Given a zip code and date, compute the sun's azimuth/elevation path
     through the low-sun hours and cross-reference it against whatever
     traffic history has accumulated near that location. Sun geometry is
     useful immediately; the traffic overlay improves as data accrues
     (likely needs days to weeks of collection to be meaningful).

This predicts *where to point and when* - a planning tool. Real-time
"warn me when a plane is about to cross the sun" tracking needs an
ADS-B receiver (e.g. RTL-SDR) at the shooting location and is deliberately
out of scope here; see project notes.
"""

import sys, os, math, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from astral import Observer
from astral.sun import azimuth, elevation
from modules.base import BaseModule

# ── Configuration ────────────────────────────────────────────────────────────

HOME_LATITUDE  = 39.7684
HOME_LONGITUDE = -86.1581
HOME_TIMEZONE  = "America/Indiana/Indianapolis"

WATCH_RADIUS_KM = 45     # how far out to poll OpenSky around home
DB_PATH = "vantage.db"

OPENSKY_URL = "https://opensky-network.org/api/states/all"

BEARING_BIN_DEG = 22.5   # 16-point compass
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

# Approximate primary IANA timezone per US state. Good enough for regional
# scouting; a handful of states (IN, MI, KY, TN, FL, TX...) really do split
# across two zones, so results near those internal boundaries can be off by
# an hour. Not worth a geospatial timezone-lookup dependency for a planning
# tool.
STATE_TIMEZONES = {
    'AL': 'America/Chicago', 'AK': 'America/Anchorage', 'AZ': 'America/Phoenix',
    'AR': 'America/Chicago', 'CA': 'America/Los_Angeles', 'CO': 'America/Denver',
    'CT': 'America/New_York', 'DE': 'America/New_York', 'DC': 'America/New_York',
    'FL': 'America/New_York', 'GA': 'America/New_York', 'HI': 'Pacific/Honolulu',
    'ID': 'America/Denver', 'IL': 'America/Chicago', 'IN': 'America/Indiana/Indianapolis',
    'IA': 'America/Chicago', 'KS': 'America/Chicago', 'KY': 'America/New_York',
    'LA': 'America/Chicago', 'ME': 'America/New_York', 'MD': 'America/New_York',
    'MA': 'America/New_York', 'MI': 'America/Detroit', 'MN': 'America/Chicago',
    'MS': 'America/Chicago', 'MO': 'America/Chicago', 'MT': 'America/Denver',
    'NE': 'America/Chicago', 'NV': 'America/Los_Angeles', 'NH': 'America/New_York',
    'NJ': 'America/New_York', 'NM': 'America/Denver', 'NY': 'America/New_York',
    'NC': 'America/New_York', 'ND': 'America/Chicago', 'OH': 'America/New_York',
    'OK': 'America/Chicago', 'OR': 'America/Los_Angeles', 'PA': 'America/New_York',
    'RI': 'America/New_York', 'SC': 'America/New_York', 'SD': 'America/Chicago',
    'TN': 'America/Chicago', 'TX': 'America/Chicago', 'UT': 'America/Denver',
    'VT': 'America/New_York', 'VA': 'America/New_York', 'WA': 'America/Los_Angeles',
    'WV': 'America/New_York', 'WI': 'America/Chicago', 'WY': 'America/Denver',
}

EARTH_RADIUS_KM = 6371.0

# ── Geo helpers ──────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Compass bearing from point 1 to point 2, in degrees."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass(deg):
    if deg is None:
        return "N/A"
    return COMPASS[int((deg / 22.5) + 0.5) % 16]


def timezone_for_state(state_code):
    return STATE_TIMEZONES.get((state_code or "").upper(), HOME_TIMEZONE)


# ── Sun geometry ─────────────────────────────────────────────────────────────

def sun_path(lat, lon, target_date, tz_name, step_minutes=5,
             elevation_floor=-2, elevation_ceiling=25):
    """
    Sample the sun's azimuth/elevation through the day at `step_minutes`
    resolution, restricted to the low-sun band that's relevant for
    silhouette shots (elevation_floor/ceiling in degrees).
    """
    observer = Observer(latitude=lat, longitude=lon)
    tz = ZoneInfo(tz_name)
    start = datetime.combine(target_date, datetime.min.time(), tzinfo=tz)

    samples = []
    for i in range(0, 24 * 60, step_minutes):
        t = start + timedelta(minutes=i)
        el = elevation(observer, t)
        if el < elevation_floor or el > elevation_ceiling:
            continue
        az = azimuth(observer, t)
        samples.append({"time": t, "azimuth": az, "elevation": el})
    return samples


def traffic_count_near(bearing, elev_deg, bearing_tolerance=15, elev_tolerance=5):
    """Count logged sightings within a bearing/elevation window of a sun sample."""
    lo_e, hi_e = elev_deg - elev_tolerance, elev_deg + elev_tolerance
    lo_b, hi_b = bearing - bearing_tolerance, bearing + bearing_tolerance

    with sqlite3.connect(DB_PATH) as conn:
        if lo_b < 0 or hi_b > 360:
            row = conn.execute("""
                SELECT COUNT(*) FROM aircraft_observations
                WHERE elevation_deg BETWEEN ? AND ?
                AND (bearing_deg >= ? OR bearing_deg <= ?)
            """, (lo_e, hi_e, lo_b % 360, hi_b % 360)).fetchone()
        else:
            row = conn.execute("""
                SELECT COUNT(*) FROM aircraft_observations
                WHERE elevation_deg BETWEEN ? AND ?
                AND bearing_deg BETWEEN ? AND ?
            """, (lo_e, hi_e, lo_b, hi_b)).fetchone()
    return row[0] if row else 0


def find_intercept_windows(lat, lon, target_date, tz_name,
                            prime_floor=1, prime_ceiling=15):
    """
    Sun path for the day, restricted to the shootable low-sun band, each
    sample annotated with how much aircraft traffic has historically been
    seen in that patch of sky (only if the point falls within the home
    watch radius - data doesn't exist anywhere else yet).
    """
    samples = sun_path(lat, lon, target_date, tz_name,
                        elevation_floor=0, elevation_ceiling=20)
    has_traffic_data = haversine_km(lat, lon, HOME_LATITUDE, HOME_LONGITUDE) <= WATCH_RADIUS_KM

    windows = []
    for s in samples:
        traffic = traffic_count_near(s["azimuth"], s["elevation"]) if has_traffic_data else None
        windows.append({
            "time": s["time"],
            "azimuth": round(s["azimuth"], 1),
            "compass": compass(s["azimuth"]),
            "elevation": round(s["elevation"], 1),
            "prime": prime_floor <= s["elevation"] <= prime_ceiling,
            "traffic_count": traffic,
        })
    return windows, has_traffic_data


# ── Collector (BaseModule) ──────────────────────────────────────────────────

class AircraftTrafficModule(BaseModule):
    name     = "Aircraft Traffic"
    interval = 300  # poll every 5 minutes - anonymous OpenSky access is rate-limited

    def __init__(self):
        self._last_data = {}
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS aircraft_observations (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at     TEXT NOT NULL,
                    icao24          TEXT,
                    distance_km     REAL,
                    bearing_deg     REAL,
                    elevation_deg   REAL,
                    altitude_m      REAL,
                    heading_deg     REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_aircraft_bearing_elev
                ON aircraft_observations (bearing_deg, elevation_deg)
            """)
            conn.commit()

    def _bounding_box(self):
        lat_pad = WATCH_RADIUS_KM / 111.0
        lon_pad = WATCH_RADIUS_KM / (111.0 * math.cos(math.radians(HOME_LATITUDE)))
        return {
            "lamin": HOME_LATITUDE - lat_pad, "lamax": HOME_LATITUDE + lat_pad,
            "lomin": HOME_LONGITUDE - lon_pad, "lomax": HOME_LONGITUDE + lon_pad,
        }

    def fetch(self):
        try:
            resp = requests.get(OPENSKY_URL, params=self._bounding_box(), timeout=15)
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as e:
            self._last_data = {"status": "error", "error": str(e)}
            return

        states = payload.get("states") or []
        now = datetime.now(timezone.utc).isoformat()
        rows = []

        for s in states:
            icao24, callsign, country, tpos, last_contact, lon, lat, baro_alt, \
                on_ground, velocity, heading = s[:11]
            geo_alt = s[13] if len(s) > 13 else None
            if on_ground or lat is None or lon is None:
                continue
            altitude_m = baro_alt if baro_alt is not None else geo_alt
            if altitude_m is None:
                continue

            dist = haversine_km(HOME_LATITUDE, HOME_LONGITUDE, lat, lon)
            if dist > WATCH_RADIUS_KM:
                continue

            brg = bearing_deg(HOME_LATITUDE, HOME_LONGITUDE, lat, lon)
            elev = math.degrees(math.atan2(altitude_m, dist * 1000))
            rows.append((now, icao24, dist, brg, elev, altitude_m, heading))

        if rows:
            with sqlite3.connect(DB_PATH) as conn:
                conn.executemany("""
                    INSERT INTO aircraft_observations
                        (observed_at, icao24, distance_km, bearing_deg, elevation_deg, altitude_m, heading_deg)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, rows)
                conn.commit()

        self._last_data = self._summarize()
        print(f"[Aircraft Traffic] logged {len(rows)} sightings this cycle")

    def _summarize(self):
        with sqlite3.connect(DB_PATH) as conn:
            total = conn.execute("SELECT COUNT(*) FROM aircraft_observations").fetchone()[0]
            unique = conn.execute("SELECT COUNT(DISTINCT icao24) FROM aircraft_observations").fetchone()[0]
            first_seen_raw = conn.execute("SELECT MIN(observed_at) FROM aircraft_observations").fetchone()[0]
            top_bearings = conn.execute("""
                SELECT ROUND(bearing_deg / ?) * ? AS bin, COUNT(*) AS n
                FROM aircraft_observations
                GROUP BY bin ORDER BY n DESC LIMIT 5
            """, (BEARING_BIN_DEG, BEARING_BIN_DEG)).fetchall()

        # observed_at is stored in UTC; convert to home-local before display
        # so it matches every other timestamp shown on the dashboard.
        first_seen = None
        if first_seen_raw:
            first_seen = datetime.fromisoformat(first_seen_raw).astimezone(ZoneInfo(HOME_TIMEZONE))

        return {
            "status": "ok",
            "total_observations": total,
            "unique_aircraft": unique,
            "collecting_since": first_seen,
            "top_bearings": [
                {"bearing": b % 360, "compass": compass(b % 360), "count": n}
                for b, n in top_bearings
            ],
        }

    def status(self):
        if not self._last_data:
            return {"status": "pending", "total_observations": 0}
        return self._last_data

    def check_alert(self):
        return False


# ── Standalone test ───────────────────────────────────────────────────────────
#   python modules/aircraft.py

if __name__ == "__main__":
    import pprint
    mod = AircraftTrafficModule()
    print("Polling OpenSky...")
    mod.fetch()
    print("\nStatus:")
    pprint.pprint(mod.status())

    print("\nSun path sample for today:")
    windows, has_data = find_intercept_windows(
        HOME_LATITUDE, HOME_LONGITUDE, datetime.now().date(), HOME_TIMEZONE
    )
    pprint.pprint(windows[:5])
    print("has_traffic_data:", has_data)
