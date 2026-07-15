# Vantage

Personal photography-conditions tracker. A small Flask app that runs a set of
pluggable modules on a schedule, each watching a natural-sky condition worth
photographing, and presents them on a minimalist dashboard. Built to run
always-on on a dedicated box and be viewed from other machines and wall
displays on a home LAN.

This document is both the project reference and the deployment hand-off. The
**[Deployment](#deployment)** section at the end lists what is already handled
in-code versus what still needs an infrastructure decision.

---

## What it does

| Module | Dashboard name | Watches | Data source | Poll interval |
|---|---|---|---|---|
| `modules/aurora.py` | Aurora | Geomagnetic Kp index (aurora likelihood) | NOAA SWPC | 15 min |
| `modules/air_quality.py` | Sky Clarity | Wildfire smoke / Saharan dust / haze via aerosol optical depth, PM2.5/PM10, dust | Open-Meteo Air Quality + Forecast | 30 min |
| `modules/sunrise_sunset.py` | Sunrise / Sunset | Photographic quality score of the next sunrise/sunset from cloud mix, humidity, wind | US NWS (api.weather.gov) | 60 min |
| `modules/aircraft.py` | Aircraft Traffic | Passive collector logging aircraft bearing/elevation as seen from the ground, to plan sun-intercept shots | OpenSky Network | 5 min |

The Aircraft module also powers a **`/planner`** page: enter a zip + date +
sun-elevation band (Golden hour / Midday / All daylight) and it computes the
sun's path and which direction to point, cross-referenced against accumulated
traffic history.

Alerts (aurora threshold, high-scoring sunrise/sunset, heavy haze) are pushed
via **ntfy.sh** to the topic `vantage-photography`.

---

## Architecture

- **Web / API**: Flask app (`app.py`). Server-rendered Jinja templates, no
  build step, no JS framework. One small client-side Leaflet map on the Sky
  Clarity card (loaded from unpkg CDN).
- **Scheduling**: `scheduler.py` wraps APScheduler's `BackgroundScheduler`.
  Each module declares its own `interval`; the scheduler fetches on that
  cadence and runs every module once immediately at startup. **Runs in-process
  with the web server — a single process, one scheduler.**
- **Module contract**: `modules/base.py` defines `BaseModule` (`fetch()`,
  `status()`, `check_alert()`). `app.py` auto-discovers any `BaseModule`
  subclass under `modules/` at startup — dropping in a new module file is all
  it takes to add a card.
- **Location**: `location.py` is a shared, DB-persisted store for the
  dashboard's active zip. The live modules (Sky Clarity, Sunrise/Sunset) read
  it at fetch time, so the dashboard zip entry re-points them. The Aircraft
  collector is deliberately **anchored to a fixed home constant** (not the
  dashboard zip) so its accumulated history stays coherent. Aurora is global
  (Kp index) and location-independent.
- **Storage**: single SQLite file `vantage.db` in the project root. Tables:
  `sunrise_sunset_scores`, `aircraft_observations`, `settings`.

### Project layout

```
vantage/
├── app.py                 # Flask app, routes, entry point (serve())
├── scheduler.py           # APScheduler wrapper
├── location.py            # shared zip/location store + geocoding
├── requirements.txt
├── vantage.db             # SQLite (gitignored — NOT in the repo)
├── modules/
│   ├── base.py            # BaseModule contract
│   ├── aurora.py
│   ├── air_quality.py     # "Sky Clarity"
│   ├── sunrise_sunset.py
│   └── aircraft.py        # collector + /planner sun-geometry functions
├── templates/
│   ├── dashboard.html
│   └── planner.html
└── static/css/dashboard.css
```

### Web routes

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Dashboard (auto-refreshes every 5 min via meta refresh) |
| `/location` | POST | Set active zip, re-fetch modules, redirect to `/` |
| `/planner` | GET | Sun/aircraft intercept planner (`?zip=&date=&band=`) |
| `/api/status` | GET | All module statuses as JSON |

There is **no authentication** — any client on the network can view the
dashboard and change the location setting. Intended for a trusted home LAN.

---

## Running it

### Dependencies

Python **3.11+** (developed on 3.14.6). Install into a venv:

```
python -m venv venv
venv/Scripts/pip install -r requirements.txt      # Windows
# venv/bin/pip install -r requirements.txt         # Linux
```

Packages: flask, apscheduler, astral, requests, pgeocode, waitress.

### Configuration (environment variables)

| Var | Default | Meaning |
|---|---|---|
| `VANTAGE_HOST` | `127.0.0.1` | Bind address. **Set to `0.0.0.0` to serve the LAN.** |
| `VANTAGE_PORT` | `5002` | TCP port. |
| `VANTAGE_DEBUG` | (unset) | Truthy = Flask dev server + interactive debugger. **Localhost dev only — never on a network** (the debugger allows remote code execution). Unset = serve with waitress (production WSGI). |

### Start

```
venv/Scripts/python app.py            # waitress, localhost:5002
```
```
VANTAGE_HOST=0.0.0.0 venv/bin/python app.py    # waitress, all interfaces
```

`app.py:serve()` starts the scheduler, then serves with waitress (default) or
the Flask dev server (`VANTAGE_DEBUG` set).

---

## Outbound network dependencies

The box needs outbound internet. Nothing here needs an API key.

| Host | Used by | Notes |
|---|---|---|
| `services.swpc.noaa.gov` | Aurora | HTTPS |
| `air-quality-api.open-meteo.com`, `api.open-meteo.com` | Sky Clarity | HTTPS |
| `api.weather.gov` | Sunrise/Sunset | HTTPS, requires a descriptive User-Agent (already set) |
| `opensky-network.org` | Aircraft | HTTPS, anonymous access is rate-limited; failed polls skip a cycle gracefully |
| `ntfy.sh` | Alerts (outbound POST) | topic `vantage-photography` — public unless changed |
| GeoNames (via `pgeocode`) | Geocoding | On **first run**, pgeocode downloads a ~2.8 MB US postal dataset to `~/.cache/pgeocode` (needs internet + write access to that dir once; cached thereafter) |

Only **inbound** port is the web port (default 5002).

---

## Current state

**Built and working (verified in-browser against live data):**

- All four modules fetching real data and rendering on the dashboard.
- Japandi-styled dashboard with per-module card layouts + generic fallback.
- Dashboard location switcher (default zip `46227`), persisted across restarts;
  changing it re-points the live modules and re-fetches immediately.
- Planner with three sun-elevation bands and per-run "look this way" summaries.
- Aircraft collector logging to SQLite; anchored home stays put when the
  dashboard zip changes (confirmed).
- Production entry point: waitress + env-var host/port/debug (this change).

**Not yet built / known limitations:**

- **No auth** on the web UI (fine for trusted LAN; do not expose to the internet).
- **Timezone is state-level** in the planner/geocoder — a zip right on an
  internal state timezone boundary can be off by an hour.
- **Collector home is a code constant** (`HOME_LATITUDE`/`HOME_LONGITUDE` in
  `modules/aircraft.py`, currently downtown Indianapolis), not UI-configurable.
- **Traffic history needs weeks** of continuous running to be useful, and only
  accumulates within 45 km of the collector home.
- **Smoke/dust "map"** is a wind-drift proxy, not a measured plume boundary.
- `vantage.db` has no pruning (growth is modest — a long way off mattering).

---

## Deployment

Target machine is **TBD** — leaning toward a dedicated Linux VM (Proxmox),
possibly Windows 11 bare-metal. It will have a **static IP** (assigned once the
machine exists) and run constantly. Access needed from multiple LAN machines
and wall/kiosk displays.

### Already handled in-code (target-agnostic)

- Serves via **waitress** (pure-Python production WSGI, identical on Windows and
  Linux — this is why the OS choice doesn't affect the app layer).
- Bind host / port / debug are **environment-driven** (`VANTAGE_HOST` etc.).
- Single-process scheduler — **do not run multiple workers** (each would spawn
  its own poller and duplicate DB writes). Waitress's default threaded single
  process is correct; keep it that way.
- Dashboard is kiosk-friendly (auto-refreshes itself every 5 min).

### Infrastructure decisions for the sysadmin

1. **OS / runtime**: Linux VM vs Windows 11. Affects only venv path
   (`bin/` vs `Scripts/`) and the auto-start mechanism below — not the app.
   Python **3.11+** required.
2. **Auto-start as a service**, start-on-boot + restart-on-failure:
   - Linux: a **systemd** unit running `venv/bin/python app.py` with
     `Environment=VANTAGE_HOST=0.0.0.0`, `WorkingDirectory=` the project root.
   - Windows: **Task Scheduler** (at-startup, restart on failure) or **NSSM** as
     a true service.
   - Working directory must be the project root (SQLite path is relative).
3. **Environment**: set `VANTAGE_HOST=0.0.0.0`, `VANTAGE_PORT` if not 5002,
   leave `VANTAGE_DEBUG` unset.
4. **Firewall**: allow inbound TCP on the web port from the LAN only. **Do not
   port-forward to the internet** (no auth on the app).
5. **Outbound internet**: required (see table above), including a one-time
   pgeocode dataset download to `~/.cache/pgeocode` on first run.
6. **Persistence / backup**: `vantage.db` holds all accumulated aircraft
   history and is **not** in git. Put it on persistent storage; consider a
   periodic backup once history is worth keeping.
7. **Optional**: reverse proxy (nginx/Caddy) if you want a hostname or port-80
   access instead of `http://<ip>:5002`. Not required for a home LAN.

### Decisions for the owner (not infra)

- **Set the collector home** (`HOME_LATITUDE`/`HOME_LONGITUDE` in
  `modules/aircraft.py`) to the real shooting area **before** starting long-term
  collection — otherwise weeks of history accumulate for the wrong location.
- Confirm the **default dashboard zip** (`DEFAULT_ZIP` in `location.py`).
- Consider changing the **ntfy topic** from the default `vantage-photography` to
  something private (anyone who knows the topic name receives your alerts).
