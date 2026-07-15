"""
location.py
Vantage - shared location store

A single source of truth for "where are we looking from," used by the
location-driven modules (Sky Clarity, Sunrise/Sunset) and the dashboard's
zip entry. Persisted in the same SQLite database so it survives restarts.

The Aircraft Traffic collector deliberately does NOT read from here - it
stays anchored to its own fixed home so its accumulated sighting history
stays coherent (see modules/aircraft.py).
"""

import math
import sqlite3
from functools import lru_cache

import pgeocode

DB_PATH = "vantage.db"
DEFAULT_ZIP = "46227"   # south Indianapolis

_geocoder = pgeocode.Nominatim("us")

# Approximate primary IANA timezone per US state. Good enough for a photo
# planning tool; a few states straddle two zones, so results right at an
# internal boundary can be off by an hour.
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

DEFAULT_TIMEZONE = "America/Indiana/Indianapolis"


def timezone_for_state(state_code):
    return STATE_TIMEZONES.get((state_code or "").upper(), DEFAULT_TIMEZONE)


def _init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()


@lru_cache(maxsize=256)
def geocode(zip_code):
    """Return a location dict for a US zip, or None if it can't be resolved."""
    geo = _geocoder.query_postal_code(zip_code)
    if geo is None or geo.latitude is None or math.isnan(geo.latitude):
        return None
    return {
        "zip": zip_code,
        "lat": float(geo.latitude),
        "lon": float(geo.longitude),
        "tz": timezone_for_state(geo.state_code),
        "place_name": f"{geo.place_name}, {geo.state_code}",
    }


def _stored_zip():
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'zip'").fetchone()
    return row[0] if row else None


def get_location():
    """Current dashboard location, falling back to DEFAULT_ZIP."""
    zip_code = _stored_zip() or DEFAULT_ZIP
    loc = geocode(zip_code)
    if loc is None:
        loc = geocode(DEFAULT_ZIP)
    return loc


def set_location(zip_code):
    """Validate and persist a new dashboard zip. Returns the location dict on
    success, or None if the zip couldn't be geocoded (nothing is saved)."""
    zip_code = (zip_code or "").strip()
    loc = geocode(zip_code)
    if loc is None:
        return None
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('zip', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (zip_code,),
        )
        conn.commit()
    return loc
