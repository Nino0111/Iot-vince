#!/usr/bin/env python3
"""GreenTrack Pi server: reads sensors, runs auto rules, drives relays, serves the dashboard,
and (optionally) mirrors everything to Firebase Realtime Database over plain REST.
Run:  pip install flask requests gpiozero spidev smbus2 adafruit-circuitpython-dht
      FIREBASE_URL=https://YOUR-PROJECT-default-rtdb.firebaseio.com python3 pi_server.py
Libraries that are missing (e.g. on a PC) switch that sensor to simulated values."""
import os, time, json, random, sqlite3, threading
from flask import Flask, jsonify, request, send_from_directory
try: import requests
except ImportError: requests = None

HERE = os.path.dirname(os.path.abspath(__file__))
FB = os.environ.get('FIREBASE_URL', '').rstrip('/')
PORT = int(os.environ.get('PORT', 3000))

# ── Pin map (BCM numbers) ────────────────────────────────────────────────
RELAY = {'pump': 17, 'valve1': 27, 'valve2': 22, 'fan': 5, 'mist': 6, 'light': 13}  # active-LOW board
DHT_PIN, TRIG_PIN, ECHO_PIN = 4, 23, 24      # ECHO goes through the logic level converter
SOIL_CH = [0, 1, 2, 3, 4]                    # MCP3008 channels: zone1=0,1  zone2=2,3  spare=4
SOIL_DRY_RAW, SOIL_WET_RAW = 800, 380        # CALIBRATE: raw reading in dry air / in water
TANK_FULL_CM, TANK_EMPTY_CM = 25, 55         # sensor-to-water distance when full / empty

RULES = {'soilOn': 35, 'soilOff': 60, 'waterMin': 15, 'fanOnC': 33, 'fanOffC': 30,
         'mistBelowRH': 55, 'luxOn': 8000, 'lightFrom': 6, 'lightTo': 18, 'maxWaterMin': 10}
mode = {k: 'auto' for k in RELAY}            # auto | on | off  (manual override)
out = {k: False for k in RELAY}
state = {'sensors': {}, 'sim': {}}
lock = threading.Lock()

# ── Hardware (each part falls back to simulation) ────────────────────────
def _try(name, fn):
    try: r = fn(); state['sim'][name] = False; return r
    except Exception as e: state['sim'][name] = True; print(f'[sim] {name}: {e}'); return None

gpio = _try('relays', lambda: __import__('gpiozero'))
relays = {}
if gpio:
    relays = {k: gpio.OutputDevice(p, active_high=False, initial_value=False) for k, p in RELAY.items()}
adc = _try('soil', lambda: [gpio.MCP3008(channel=c) for c in SOIL_CH])
bus = _try('light', lambda: __import__('smbus2').SMBus(1))
dht = _try('dht', lambda: __import__('adafruit_dht').DHT22(getattr(__import__('board'), f'D{DHT_PIN}')))
sonar = _try('level', lambda: gpio.DistanceSensor(echo=ECHO_PIN, trigger=TRIG_PIN, max_distance=1.5))

def clamp(v): return max(0, min(100, v))
def read_soil():
    if adc: return [round(clamp((SOIL_DRY_RAW - a.value * 1023) / (SOIL_DRY_RAW - SOIL_WET_RAW) * 100), 1) for a in adc]
    return [round(clamp(random.gauss(48, 3)), 1) for _ in SOIL_CH]
def read_lux():
    if bus:
        bus.write_byte(0x23, 0x10); time.sleep(0.2)
        d = bus.read_i2c_block_data(0x23, 0x10, 2); return round((d[0] << 8 | d[1]) / 1.2, 0)
    return round(random.gauss(9000, 800), 0)
def read_dht():
    if dht: return round(dht.temperature, 1), round(dht.humidity, 1)
    return round(random.gauss(31, .5), 1), round(random.gauss(62, 1), 1)
def read_water():
    if sonar:
        cm = sonar.distance * 100
        return round(clamp((TANK_EMPTY_CM - cm) / (TANK_EMPTY_CM - TANK_FULL_CM) * 100), 1)
    return round(clamp(random.gauss(70, .5)), 1)

# ── Database ────────────────────────────────────────────────────────────
db = sqlite3.connect(os.path.join(HERE, 'greentrack.db'), check_same_thread=False)
db.execute('CREATE TABLE IF NOT EXISTS readings(ts INT, temp REAL, hum REAL, lux REAL, water REAL, z1 REAL, z2 REAL)')

# ── Control loop ────────────────────────────────────────────────────────
valve_since = {'valve1': None, 'valve2': None}
def decide(d):
    r, h = RULES, time.localtime().tm_hour
    want = {}
    for z, k in ((1, 'valve1'), (2, 'valve2')):
        s = d['zones'][z - 1]
        if s < r['soilOn']: want[k] = True
        elif s > r['soilOff']: want[k] = False
        else: want[k] = out[k]
        if want[k] and d['water'] < r['waterMin']: want[k] = False          # tank empty: never run dry
        if want[k]:
            valve_since[k] = valve_since[k] or time.time()
            if time.time() - valve_since[k] > r['maxWaterMin'] * 60: want[k] = False   # safety timeout
        elif s > r['soilOn']: valve_since[k] = None
    want['fan'] = True if d['temp'] > r['fanOnC'] else False if d['temp'] < r['fanOffC'] else out['fan']
    want['mist'] = d['hum'] < r['mistBelowRH'] and d['temp'] > r['fanOnC'] and d['water'] >= r['waterMin']
    want['light'] = r['lightFrom'] <= h < r['lightTo'] and d['lux'] < r['luxOn']
    for k in ('pump', 'valve1', 'valve2', 'fan', 'mist', 'light'):
        if mode[k] != 'auto': want[k] = mode[k] == 'on'
    want['pump'] = want['valve1'] or want['valve2'] or want['mist'] if mode['pump'] == 'auto' else want['pump']
    return want

def loop():
    last_log = 0
    while True:
        try:
            t, h = read_dht(); z = read_soil()
            d = {'ts': int(time.time()), 'temp': t, 'hum': h, 'lux': read_lux(), 'water': read_water(), 'soil': z,
                 'zones': [round(sum(z[0:2]) / 2, 1), round(sum(z[2:4]) / 2, 1)]}
            with lock:
                for k, v in decide(d).items():
                    out[k] = bool(v)
                    if k in relays: relays[k].on() if v else relays[k].off()
                d.update(out=dict(out), mode=dict(mode), rules=RULES, sim=state['sim'])
                state['data'] = d
            if time.time() - last_log > 60:
                db.execute('INSERT INTO readings VALUES (?,?,?,?,?,?,?)', (d['ts'], t, h, d['lux'], d['water'], *d['zones'])); db.commit(); last_log = time.time()
        except Exception as e: print('loop error:', e)
        time.sleep(2)

def firebase_sync():
    seen = {}
    while requests and FB:
        try:
            requests.put(f'{FB}/greenhouse/data.json', json=state.get('data'), timeout=6)
            cmds = requests.get(f'{FB}/greenhouse/commands.json', timeout=6).json() or {}
            for dev, c in cmds.items():
                if dev in mode and c.get('ts') != seen.get(dev) and c.get('mode') in ('auto', 'on', 'off'):
                    mode[dev] = c['mode']; seen[dev] = c['ts']
        except Exception as e: print('firebase:', e)
        time.sleep(5)

# ── API + dashboard ─────────────────────────────────────────────────────
app = Flask(__name__)
@app.after_request
def cors(r): r.headers['Access-Control-Allow-Origin'] = '*'; r.headers['Access-Control-Allow-Headers'] = 'Content-Type'; return r
@app.route('/')
def home(): return send_from_directory(HERE, 'index.html')
@app.route('/api/data')
def api_data(): return jsonify(state.get('data') or {})
@app.route('/api/history')
def api_hist():
    since = int(time.time() - float(request.args.get('hours', 24)) * 3600)
    rows = db.execute('SELECT ts,temp,hum,lux,water,z1,z2 FROM readings WHERE ts>=? ORDER BY ts', (since,)).fetchall()
    return jsonify([dict(zip(('ts', 'temp', 'hum', 'lux', 'water', 'z1', 'z2'), r)) for r in rows])
@app.route('/api/control', methods=['POST', 'OPTIONS'])
def api_control():
    j = request.get_json(silent=True) or {}
    if j.get('device') in mode and j.get('mode') in ('auto', 'on', 'off'): mode[j['device']] = j['mode']; return jsonify(ok=True)
    return jsonify(ok=False, error='device and mode (auto|on|off) required'), 400
@app.route('/api/rules', methods=['GET', 'POST', 'OPTIONS'])
def api_rules():
    if request.method == 'POST':
        for k, v in (request.get_json(silent=True) or {}).items():
            if k in RULES and isinstance(v, (int, float)): RULES[k] = v
    return jsonify(RULES)

if __name__ == '__main__':
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=firebase_sync, daemon=True).start()
    print(f'GreenTrack on http://0.0.0.0:{PORT}  firebase={"on" if FB else "off"}')
    app.run(host='0.0.0.0', port=PORT)
