# code.py — ESP32 CircuitPython subscriber for ambient LED lighting.
#
# Connects to Wi-Fi, subscribes to an MQTT topic, and drives 52 WS2812B LEDs
# with the RGB colors published by the laptop publisher.
#
# Hardware:
#   - ESP32 running CircuitPython 8.x or later
#   - WS2812B LED strip (52 LEDs) wired to LED_PIN
#   - 5V power supply for the LED strip (do NOT power from ESP32 3.3V pin)
#   - 300-500 ohm resistor on the data line is recommended
#
# CircuitPython libraries required (copy to /lib on CIRCUITPY):
#   - adafruit_minimqtt  (adafruit-circuitpython-minimqtt)
#   - neopixel           (adafruit-circuitpython-neopixel)
#
# Wi-Fi credentials:
#   Set CIRCUITPY_WIFI_SSID and CIRCUITPY_WIFI_PASSWORD in settings.toml.
#
# Install libraries via:
#   circup install adafruit_minimqtt neopixel

import time
import board
import neopixel
import socketpool
import wifi
import adafruit_minimqtt.adafruit_minimqtt as MQTT

# ─────────────────────────────────────────────
#  CONFIGURATION — edit these values to match
#  your setup before copying to the ESP32.
# ─────────────────────────────────────────────

BROKER_IP     = "{HOST}"       # IP of your local MQTT broker (same as publisher)
BROKER_PORT   = 1883                # Standard MQTT port
TOPIC         = "leds/colors"       # Must match publisher TOPIC

LED_PIN       = board.D7            # GPIO pin connected to the LED strip data line
LED_COUNT     = 52                  # Number of LEDs on the strip
LED_BRIGHT    = 0.4                 # Global brightness 0.0-1.0 (protect eyes & PSU)

# Reconnection backoff — seconds to wait before retrying a failed connection.
RECONNECT_DELAY = 3

# ─────────────────────────────────────────────
#  LED STRIP INITIALISATION
# ─────────────────────────────────────────────

# auto_write=False: we call strip.show() manually once per MQTT message
# so that all 52 LEDs update atomically in a single DMA burst.
strip = neopixel.NeoPixel(
    LED_PIN,
    LED_COUNT,
    brightness=LED_BRIGHT,
    auto_write=False,
    pixel_order=neopixel.GRB,      # WS2812B is GRB internally, not RGB
)

# Pre-allocate a reusable bytearray to hold the incoming MQTT payload.
# Reusing this buffer on every message avoids heap allocation and prevents
# GC pauses that would cause visible LED stutter.
_payload_buf = bytearray(LED_COUNT * 3)

# ─────────────────────────────────────────────
#  MQTT CALLBACKS
# ─────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    """Called when the MQTT connection is established."""
    print("[MQTT] Connected (rc={})".format(rc))
    # Re-subscribe inside on_connect so the subscription is automatically
    # restored after every reconnect — no extra logic needed in the main loop.
    client.subscribe(TOPIC)
    print("[MQTT] Subscribed to '{}'".format(TOPIC))


def on_disconnect(client, userdata, rc):
    """Called when the MQTT connection is dropped."""
    print("[MQTT] Disconnected (rc={})".format(rc))


def on_message(client, topic, message):
    """
    Called for every received MQTT message on the subscribed topic.

    Because the MQTT client is created with use_binary_mode=True, `message`
    is delivered as a raw bytearray — never a string. We copy it into our
    pre-allocated buffer to avoid any heap allocation in this callback.

    Payload format (156 bytes):
        [R0, G0, B0, R1, G1, B1, ..., R51, G51, B51]

    All 52 LEDs are written and strip.show() is called exactly once.
    """
    # Guard: skip malformed payloads without crashing the loop.
    if len(message) != LED_COUNT * 3:
        print("[MSG] Bad payload length: {} (expected {})".format(
            len(message), LED_COUNT * 3))
        return

    # Copy into pre-allocated buffer — no new heap object created.
    _payload_buf[:] = message

    # Write RGB triples into the neopixel buffer using a plain index loop.
    # Avoid list comprehensions or zip() — they allocate intermediate objects
    # on the CircuitPython heap and can trigger GC mid-animation.
    for i in range(LED_COUNT):
        offset = i * 3
        strip[i] = (
            _payload_buf[offset],       # R
            _payload_buf[offset + 1],   # G
            _payload_buf[offset + 2],   # B
        )

    # Flush all 52 pixels to the strip in one DMA burst.
    strip.show()


# ─────────────────────────────────────────────
#  WI-FI CONNECTION
# ─────────────────────────────────────────────

def connect_wifi():
    """
    Ensure Wi-Fi is connected.

    If CircuitPython already auto-connected via settings.toml on boot,
    this returns immediately. Otherwise retries indefinitely so the device
    recovers from transient failures without requiring a reboot.
    """
    if wifi.radio.connected:
        print("[WiFi] Already connected. IP:", str(wifi.radio.ipv4_address))
        return

    print("[WiFi] Connecting...")
    while True:
        try:
            # Reads CIRCUITPY_WIFI_SSID / CIRCUITPY_WIFI_PASSWORD from settings.toml.
            # To hardcode credentials instead, replace with:
            # wifi.radio.connect("YOUR_SSID", "YOUR_PASSWORD")
            wifi.radio.connect()
            print("[WiFi] Connected. IP:", str(wifi.radio.ipv4_address))
            return
        except Exception as e:
            print("[WiFi] Failed: {}. Retrying in {}s...".format(e, RECONNECT_DELAY))
            time.sleep(RECONNECT_DELAY)


# ─────────────────────────────────────────────
#  MQTT CLIENT SETUP
# ─────────────────────────────────────────────

def create_mqtt_client(pool):
    """
    Build and return a configured adafruit_minimqtt client.

    Key parameters explained:
    - use_binary_mode=True  : payload delivered to on_message as bytearray,
                              not decoded to str. Required for binary payloads.
    - socket_timeout=0.05   : how long a single socket read blocks (seconds).
                              Must be < loop timeout. Keep low for responsiveness.
    - recv_timeout=0.1      : must be strictly > socket_timeout per the library.
    - keep_alive=60         : MQTT keepalive interval in seconds.

    Not connected here — call connect_mqtt() separately so this function
    can be reused cleanly for reconnection after a network drop.
    """
    client = MQTT.MQTT(
        broker=BROKER_IP,
        port=BROKER_PORT,
        client_id="esp32-ambient-leds",
        is_ssl=False,
        socket_pool=pool,
        use_binary_mode=True,   # CRITICAL: deliver payload as bytearray, not str
        socket_timeout=0.05,    # 50ms socket read timeout — keeps loop() snappy
        recv_timeout=0.1,       # must be strictly > socket_timeout
        keep_alive=60,
    )

    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message

    return client


def connect_mqtt(client):
    """Connect the MQTT client, retrying indefinitely with backoff."""
    while True:
        try:
            print("[MQTT] Connecting to {}:{} ...".format(BROKER_IP, BROKER_PORT))
            client.connect()
            return
        except Exception as e:
            print("[MQTT] Failed: {}. Retrying in {}s...".format(e, RECONNECT_DELAY))
            time.sleep(RECONNECT_DELAY)


# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────

def main():
    # ── Step 1: Wi-Fi ────────────────────────────────────────────────────
    connect_wifi()

    # ── Step 2: Socket pool (required by adafruit_minimqtt) ──────────────
    pool = socketpool.SocketPool(wifi.radio)

    # ── Step 3: MQTT client ───────────────────────────────────────────────
    client = create_mqtt_client(pool)
    connect_mqtt(client)

    # ── Step 4: Main event loop ───────────────────────────────────────────
    # loop(timeout) blocks for up to `timeout` seconds waiting for a message,
    # then returns. Must be >= socket_timeout (0.05s). We use 0.1s so the
    # ESP32 checks for new frames up to 10 times per second — matching the
    # publisher's FPS — without burning CPU spinning on an empty socket.
    print("[LOOP] Entering main loop...")

    while True:
        try:
            client.loop(timeout=0.1)

        except MQTT.MMQTTException as e:
            # MQTT-level error (e.g. broker closed the connection).
            print("[MQTT] Error: {}. Reconnecting...".format(e))
            time.sleep(RECONNECT_DELAY)
            connect_mqtt(client)

        except OSError as e:
            # Network-level error (e.g. Wi-Fi dropped).
            # Must recreate the socket pool — it is bound to the old interface.
            print("[NET] OSError: {}. Reconnecting Wi-Fi + MQTT...".format(e))
            time.sleep(RECONNECT_DELAY)
            connect_wifi()
            pool = socketpool.SocketPool(wifi.radio)
            client = create_mqtt_client(pool)
            connect_mqtt(client)

        except Exception as e:
            # Catch-all — log and keep running rather than halting.
            print("[ERR] Unexpected error: {}".format(e))
            time.sleep(RECONNECT_DELAY)


# CircuitPython executes code.py top-to-bottom on boot — no __main__ guard.
main()