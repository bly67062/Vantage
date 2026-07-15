import importlib
import math
import pkgutil
from datetime import datetime
import pgeocode
import modules
from scheduler import VantageScheduler
from flask import Flask, jsonify, render_template, request
from modules.aircraft import find_intercept_windows, timezone_for_state

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
_geocoder = pgeocode.Nominatim('us')

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
    return render_template('dashboard.html', modules=results, generated_at=generated_at)

@app.route('/planner')
def planner():
    """Sun/aircraft intercept planner: zip + date -> low-sun windows with
    whatever traffic history has accumulated near that location."""
    zip_code = request.args.get('zip', '').strip()
    date_str = request.args.get('date', '').strip()
    result = None
    error = None

    if zip_code:
        geo = _geocoder.query_postal_code(zip_code)
        if geo is None or math.isnan(geo.latitude):
            error = f"Couldn't find zip code \"{zip_code}\""
        else:
            lat, lon = float(geo.latitude), float(geo.longitude)
            tz_name = timezone_for_state(geo.state_code)

            try:
                target_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else datetime.now().date()
            except ValueError:
                target_date = datetime.now().date()
                error = "Invalid date - showing today instead"

            windows, has_traffic_data = find_intercept_windows(lat, lon, target_date, tz_name)
            result = {
                'place_name': f"{geo.place_name}, {geo.state_code}",
                'date': target_date.isoformat(),
                'windows': windows,
                'has_traffic_data': has_traffic_data,
            }

    return render_template('planner.html', zip_code=zip_code, date=date_str,
                            result=result, error=error)

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

if __name__ == '__main__':
    scheduler.start()
    app.run(debug=True, port=5002, use_reloader=False)