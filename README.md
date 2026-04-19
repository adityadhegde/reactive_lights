# Reactive Lights (Lights Antigravity)

**Screen-content-reactive ambient LED lighting** using an ESP32 and WS2812B LED strip with MQTT.

This project captures your desktop screen in real-time (~10 FPS), divides it into 52 vertical zones, analyzes the dominant colors (filtered by saturation and brightness), and streams the results to an ESP32 microcontroller that drives a WS2812B RGB LED strip. The result: LED colors that react instantly to what's on your screen.

---

## Features

- **Real-time screen capture** at ~10 FPS with minimal CPU impact
- **Smart color filtering** using HSV thresholds to ignore whites, greys, and blacks
- **52-zone vertical sampling** mapping the full screen width to individual LEDs
- **MQTT-based communication** for decoupled publisher/subscriber architecture
- **Minimal latency** with binary payloads (156 bytes per frame) and optimized CircuitPython
- **Robust reconnection** with exponential backoff for Wi-Fi and MQTT failures
- **Energy-efficient** with configurable LED brightness and GC-aware memory patterns

---

## Hardware Requirements

### Laptop / Publisher
- Any computer running Python 3.7+ (Windows, macOS, or Linux)

### ESP32 / Subscriber
- **Microcontroller:** ESP32 (any variant with enough pins)
- **LED Strip:** 52× WS2812B (NeoPixel) addressable RGB LEDs
- **Power Supply:** 5V, ≥3A recommended (do **not** power LEDs from ESP32 3.3V)
- **Data Line Protection:** 300–500 Ω resistor between ESP32 pin and LED data input (recommended but optional)
- **Connections:**
  - 5V from power supply → LED strip 5V
  - GND from power supply → LED strip GND → ESP32 GND (common ground essential)
  - GPIO7 (configurable) → LED strip data input (via resistor)

### Network
- **Local MQTT broker** (same Wi-Fi network as both devices)  
  *Options:* Mosquitto, Home Assistant, Synology, etc.

---

## Software Requirements

### Laptop (Publisher)
```bash
pip install mss numpy paho-mqtt
```

- **mss** – Fast screengrab (platform-independent)
- **numpy** – Efficient pixel math (HSV filtering, zone averaging)
- **paho-mqtt** – MQTT client

### ESP32 (Subscriber)
Requires **CircuitPython 8.x** or later with these libraries (copy to `/lib` on CIRCUITPY):
```
adafruit-circuitpython-minimqtt
adafruit-circuitpython-neopixel
```

**Install via:**
```bash
circup install adafruit_minimqtt neopixel
```

Or download `.mpy` files and copy manually to `/lib` on the ESP32.

---

## Setup Instructions

### 1. Laptop (Publisher)

1. **Install dependencies:**
   ```bash
   pip install mss numpy paho-mqtt
   ```

2. **Edit `laptop_publisher.py`:**
   - Set `BROKER_IP` to your local MQTT broker's IP address (e.g., `"192.168.1.100"`)
   - Adjust `FPS` if desired (default: 10)
   - Tune `V_MIN` and `S_MIN` thresholds for your screen content (see tuning guide below)
   - Optional: set `CAPTURE_REGION` to capture only part of your screen

3. **Run:**
   ```bash
   python laptop_publisher.py
   ```

### 2. ESP32 (Subscriber)

1. **Flash CircuitPython** (if not already installed):
   - Download [CircuitPython 8.x for ESP32](https://circuitpython.org/downloads)
   - Use [esptool.py](https://github.com/espressif/esptool) or the Adafruit web flasher

2. **Set Wi-Fi credentials** in `settings.toml` on the CIRCUITPY drive:
   ```toml
   CIRCUITPY_WIFI_SSID = "YOUR_SSID"
   CIRCUITPY_WIFI_PASSWORD = "YOUR_PASSWORD"
   ```

3. **Install required libraries:**
   ```bash
   circup install adafruit_minimqtt neopixel
   ```

4. **Copy `esp32_subscriber.py` to the ESP32 as `code.py`:**
   ```bash
   cp esp32_subscriber.py /path/to/CIRCUITPY/code.py
   ```

5. **Edit `code.py` on the ESP32:**
   - Set `BROKER_IP` to match your MQTT broker
   - Adjust `LED_PIN` if using a different GPIO
   - Tune `LED_BRIGHT` to protect eyes and power supply (default: 0.4)

6. **The script runs automatically on boot.** Watch the REPL for connection status and diagnostics.

---

## Configuration Guide

### Laptop Publisher

#### HSV Filtering Thresholds
The publisher filters pixels in HSV (Hue, Saturation, Value) space before averaging:

- **`V_MIN`** (default: `0.08`)  
  Minimum brightness. Pixels darker than 8% are excluded.  
  → Raises → cuts black bars and dark UI  
  → Lowers → includes more shadow detail

- **`S_MIN`** (default: `0.15`)  
  Minimum saturation. Pixels less saturated than 15% are excluded.  
  → Raises → removes whites, greys, and washed-out colors (try `0.20–0.30`)  
  → Lowers → includes pastels and tinted content (try `0.08–0.12`)

**Tuning examples:**
- *Sports with bright white scoreboards?* Raise `S_MIN` to 0.25–0.30
- *Dark movies with subtle colors?* Lower `V_MIN` to 0.04
- *Anime/games with saturated pastels?* Lower `S_MIN` to 0.10

#### Capture Region
By default, the full primary monitor is captured. To restrict to a region:
```python
CAPTURE_REGION = {"left": 0, "top": 0, "width": 1920, "height": 1080}
```

### ESP32 Subscriber

- **`LED_PIN`** – GPIO connected to LED data line (default: `board.D7` = GPIO7)
- **`LED_COUNT`** – Number of LEDs on the strip (default: 52, must match publisher)
- **`LED_BRIGHT`** – Global brightness 0.0–1.0 (default: 0.4)

---

## How It Works

### Publisher (Laptop)

1. **Capture** the screen at fixed intervals (~10 FPS)
2. **Build HSV mask** – Mark valid pixels (bright enough, saturated enough)
3. **Divide into 52 zones** – Split the frame width equally
4. **Average zone colors** – Compute mean RGB for each zone using only valid pixels
5. **Publish** 156-byte binary payload (`[R0,G0,B0, …, R51,G51,B51]`) to MQTT topic `leds/colors`
6. **Repeat** at the desired FPS with minimal latency

### Subscriber (ESP32)

1. **Connect** to Wi-Fi using credentials from `settings.toml`
2. **Subscribe** to MQTT topic `leds/colors`
3. **Wait** for messages on the broker
4. **On message:**
   - Validate payload length (must be 156 bytes)
   - Copy payload bytes into pre-allocated buffer (no heap allocation)
   - Update neopixel strip with RGB values
   - Call `strip.show()` once (atomic DMA burst)
5. **Reconnect** automatically if Wi-Fi or MQTT drops
6. **Turn off** all LEDs (send black payload) on graceful shutdown

### MQTT Protocol

- **Broker:** Local, no TLS required
- **Topic:** `leds/colors`
- **QoS:** 0 (fire-and-forget for lowest latency)
- **Payload:** 156 bytes, binary format (not text)
- **Rate:** ~10 Hz (configurable)

---

## Troubleshooting

### Laptop Publisher

**"Connection refused" on startup**
- Verify MQTT broker is running and reachable
- Check `BROKER_IP` matches your broker's actual IP address

**Low FPS, CPU usage high**
- Reduce `FPS` value
- Try different `CAPTURE_REGION` (smaller region = faster capture)
- Ensure NumPy is installed (critical for performance)

**Colors washed out or desaturated**
- Raise `S_MIN` to remove whites and greys
- Adjust `V_MIN` if blacks are being included

### ESP32 Subscriber

**No connection to broker**
- Verify Wi-Fi credentials in `settings.toml`
- Check that ESP32 is on the same network as the broker
- Ensure `BROKER_IP` matches your broker

**LEDs not updating or flickering**
- Verify data line wiring (include resistor if not already)
- Check common ground between 5V PSU and ESP32
- Reduce `LED_BRIGHT` if using a underpowered supply (voltage sag causes glitches)
- Inspect REPL output for "Bad payload length" errors

**Random disconnects**
- Verify Wi-Fi signal strength
- Check MQTT broker logs for drops
- Ensure power supply can handle current spikes (add capacitor across 5V rails if needed)

---

## Performance Notes

- **Latency:** ~50–100 ms end-to-end (capture + HSV filter + publish + MQTT network + LED write)
- **Throughput:** ~10 FPS × 156 bytes = ~15 KB/s bandwidth (negligible on modern networks)
- **CPU (laptop):** ~5–15% on modern CPUs (mss + NumPy are efficient)
- **Memory (ESP32):** ~30–40 KB used (CircuitPython manages the heap carefully to avoid GC pauses)

---

## License

See [LICENSE](LICENSE) file.

---