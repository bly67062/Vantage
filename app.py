import importlib
import pkgutil
import modules
from scheduler import VantageScheduler
from flask import Flask, jsonify, render_template, redirect

app = Flask(__name__)

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
    return redirect('/api/status')

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