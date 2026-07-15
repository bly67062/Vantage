import importlib
import os
import pkgutil
from datetime import datetime
import modules
from scheduler import VantageScheduler
from flask import Flask, jsonify, render_template, request, redirect
import location
from modules.aircraft import (
    find_intercept_windows, summarize_windows, ELEVATION_BANDS, DEFAULT_BAND,
)

app = Flask(__name__)

@app.template_filter('fmt_time')
def fmt_time(value):
    """Format a datetime (or ISO8601 string) as '6:14 PM · Wed Jul 15' without
    relying on platform-specific strftime flags (Windows lacks %-I / %-d)."""
    if isinstance(value, datetime):
        d = value
    else:
        try:
            d = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return value
    hour = d.strftime('%I').lstrip('0') or '12'
    return f"{hour}:{d.strftime('%M %p')} · {d.strftime('%a %b')} {d.day}"

@app.template_filter('fmt_clock')
def fmt_clock(value):
    """Format a datetime as just '6:14 PM', for compact list rows."""
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return value
    hour = value.strftime('%I').lstrip('0') or '12'
    return f"{hour}:{value.strftime('%M %p')}"

def load_modules():
    """Scan the modules folder and load anything that isn't base.py"""
    loaded = []
    for finder, name, ispkg in pkgutil.iter_modules(modules.__path__):
        if name == 'base':
            continue
        module = importlib.import_module(f'modules.{name}')
        from modules.base import BaseModule  # moved out of inner loop
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            try:
                if isinstance(attr, type) and issubclass(attr, BaseModule) and attr is not BaseModule:
                    instance = attr()
                    loaded.append(instance)
            except TypeError:
                continue
    return loaded

active_modules = load_modules()
scheduler = VantageScheduler(active_modules)

@app.route('/')
def dashboard():
    """Render the dashboard with each module's current status."""
    results = []
    for mod in active_modules:
        try:
            data = mod.status()
        except Exception as e:
            data = {'error': str(e)}
        results.append({'name': mod.name, 'data': data})
    now = datetime.now()
    hour = now.strftime('%I').lstrip('0') or '12'
    generated_at = f"{now.strftime('%a %b')} {now.day} · {hour}:{now.strftime('%M %p')}"
    return render_template('dashboard.html', modules=results, generated_at=generated_at,
                            loc=location.get_location())

@app.route('/location', methods=['POST'])
def update_location():
    """Set the dashboard zip, then re-fetch modules so the change shows
    immediately. Invalid zips redirect back with an error and change nothing."""
    zip_code = request.form.get('zip', '').strip()
    loc = location.set_location(zip_code)
    if loc is None:
        return redirect(f'/?loc_error={zip_code}')
    for mod in active_modules:
        try:
            mod.fetch()
        except Exception:
            pass
    return redirect('/')

@app.route('/planner')
def planner():
    """Sun/aircraft intercept planner: zip + date + elevation band -> sun
    windows with whatever traffic history has accumulated near that location."""
    zip_code = request.args.get('zip', '').strip()
    date_str = request.args.get('date', '').strip()
    band = request.args.get('band', DEFAULT_BAND)
    if band not in ELEVATION_BANDS:
        band = DEFAULT_BAND
    result = None
    error = None

    if zip_code:
        geo = location.geocode(zip_code)
        if geo is None:
            error = f"Couldn't find zip code \"{zip_code}\""
        else:
            try:
                target_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else datetime.now().date()
            except ValueError:
                target_date = datetime.now().date()
                error = "Invalid date - showing today instead"

            windows, has_traffic_data = find_intercept_windows(
                geo['lat'], geo['lon'], target_date, geo['tz'], band=band)
            result = {
                'place_name': geo['place_name'],
                'date': target_date.isoformat(),
                'runs': summarize_windows(windows, ELEVATION_BANDS[band]['step']),
                'has_windows': bool(windows),
                'has_traffic_data': has_traffic_data,
            }

    return render_template('planner.html', zip_code=zip_code, date=date_str,
                            band=band, bands=ELEVATION_BANDS, result=result, error=error)

@app.route('/api/status')
def api_status():
    """Return current status of all modules as JSON."""
    results = {}
    for mod in active_modules:
        try:
            results[mod.name] = mod.status()
        except Exception as e:
            results[mod.name] = {'error': str(e)}
    return jsonify(results)

def _truthy(value):
    return (value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def serve():
    """Start the scheduler and serve the app.

    Configured entirely by environment variables so the same code runs on a
    laptop for dev and on a dedicated always-on box for production:

      VANTAGE_HOST   bind address. Default 127.0.0.1 (localhost only).
                     Set to 0.0.0.0 to serve the whole LAN.
      VANTAGE_PORT   TCP port. Default 5002.
      VANTAGE_DEBUG  when truthy, use Flask's dev server + interactive
                     debugger (localhost dev only — never on a network).
                     Otherwise serve with waitress, a production WSGI server.
    """
    host = os.environ.get('VANTAGE_HOST', '127.0.0.1')
    port = int(os.environ.get('VANTAGE_PORT', '5002'))
    debug = _truthy(os.environ.get('VANTAGE_DEBUG'))

    scheduler.start()

    if debug:
        print(f"Vantage (dev server) on http://{host}:{port}")
        app.run(host=host, port=port, debug=True, use_reloader=False)
    else:
        from waitress import serve as waitress_serve
        print(f"Vantage on http://{host}:{port} (waitress)")
        waitress_serve(app, host=host, port=port)


if __name__ == '__main__':
    serve()