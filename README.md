# GreenTrack — dragon fruit greenhouse dashboard (Raspberry Pi + Firebase)

Same idea as CrabTrack: the Pi runs the server and dashboard, the browser keeps the last reading when offline, and Firebase is an optional second route for remote viewing and control.

## Run on the Raspberry Pi
    sudo apt install python3-pip python3-gpiozero python3-spidev python3-smbus2 -y
    pip install flask requests adafruit-circuitpython-dht --break-system-packages
    sudo raspi-config   # Interface Options: enable SPI and I2C
    FIREBASE_URL=https://YOUR-PROJECT-default-rtdb.firebaseio.com python3 pi_server.py
Open `http://raspberrypi.local:3000`. Leave `FIREBASE_URL` out to run Pi-only. Any sensor whose library or wiring is missing runs as simulated and is labelled that way on the page.

## Wiring (BCM pins, edit at the top of pi_server.py)
| Part | Pin |
|---|---|
| Relays (active-LOW): pump / zone 1 valve / zone 2 valve / fan / mister / grow light | 17 / 27 / 22 / 5 / 6 / 13 |
| DHT22 | 4 |
| JSN-SR04T trigger / echo (echo through the logic level converter) | 23 / 24 |
| MCP3008 (SPI): soil Z1 = CH0, CH1 · Z2 = CH2, CH3 · spare = CH4 | SPI0 |
| BH1750 | I2C (SDA 2, SCL 3) |
| DS3231 | I2C, add `dtoverlay=i2c-rtc,ds3231` to /boot/firmware/config.txt |

Calibrate the soil sensors: put one in dry air and one in water, then set `SOIL_DRY_RAW` and `SOIL_WET_RAW`. Set `TANK_FULL_CM` and `TANK_EMPTY_CM` by measuring the sensor-to-water distance with the tank full and empty.

## Firebase
1. Create a Realtime Database. For a test, set rules to `{"rules":{"greenhouse":{".read":true,".write":true}}}`. This is open to anyone with the URL, so tighten it before real use.
2. Paste the database URL into Config on the dashboard and into `FIREBASE_URL` on the Pi.
3. The Pi writes `/greenhouse/data` every 5 s and reads `/greenhouse/commands`, so you can switch outputs away from home.

## Safety built into the rules
Valves and mister are locked out when the tank is below its minimum, and each valve stops after the maximum watering time even if the soil still reads dry.
