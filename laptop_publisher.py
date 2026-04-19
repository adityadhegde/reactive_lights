"""
publisher.py — Laptop-side MQTT publisher for ambient LED lighting.

Captures the screen at ~10 FPS, divides each frame into 52 equal vertical
zones, filters pixels by saturation and brightness in HSV space, computes
the mean RGB for each zone, and publishes a compact 156-byte binary payload
to an MQTT broker.

Dependencies:
    pip install mss numpy paho-mqtt

Usage:
    python publisher.py
"""

import time
import sys

import mss
import numpy as np
import paho.mqtt.client as mqtt

# ─────────────────────────────────────────────
#  CONFIGURATION — edit these values to match
#  your setup before running.
# ─────────────────────────────────────────────

BROKER_IP   = "{HOST}"     # IP address of your local MQTT broker
BROKER_PORT = 1883               # Standard MQTT port (no TLS)
TOPIC       = "leds/colors"      # Topic the ESP32 subscribes to
FPS         = 10                 # Target refresh rate (frames per second)
LED_COUNT   = 52                 # Number of LEDs on the strip

# Screen region to capture. Set to None to capture the full primary monitor.
# To capture a specific region: {"left": 0, "top": 0, "width": 1920, "height": 1080}
CAPTURE_REGION = None

# ── Pixel filtering thresholds (HSV space) ───────────────────────────────────
#
# Filtering in HSV lets us independently control what counts as "too dark"
# and "too washed out" — something a plain RGB threshold cannot do.
#
# V_MIN : minimum Value (brightness). Pixels darker than this are excluded.
#         Range 0.0–1.0. 0.08 removes near-black regions and letterbox bars.
#
# S_MIN : minimum Saturation. Pixels less saturated than this are excluded.
#         Range 0.0–1.0. 0.15 removes whites, greys, and subtitle backgrounds
#         while keeping pastel and lightly-tinted content.
#
# A pixel must pass BOTH thresholds to contribute to a zone's mean.
# If every pixel in a zone is filtered out, that LED is set to (0,0,0) — off.
#
# Tuning guide:
#   More white suppression   → raise S_MIN (try 0.20–0.30)
#   Less color being ignored → lower S_MIN (try 0.08–0.12)
#   Include more dark scenes → lower V_MIN (try 0.04)
#   Cut more black bars      → raise V_MIN (try 0.12)

V_MIN = 0.08    # exclude pixels darker than 8% brightness
S_MIN = 0.15    # exclude pixels less saturated than 15% (whites, greys)

# ─────────────────────────────────────────────
#  DERIVED CONSTANTS (do not edit)
# ─────────────────────────────────────────────

FRAME_INTERVAL = 1.0 / FPS          # Seconds per frame
PAYLOAD_SIZE   = LED_COUNT * 3      # 52 zones × 3 bytes (R, G, B) = 156 bytes


# ─────────────────────────────────────────────
#  MQTT CALLBACKS
# ─────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[MQTT] Connected to broker at {BROKER_IP}:{BROKER_PORT}")
    else:
        print(f"[MQTT] Connection failed with code {rc}")
        sys.exit(1)


def on_disconnect(client, userdata, rc):
    if rc != 0:
        print(f"[MQTT] Unexpected disconnection (rc={rc}). Reconnecting…")


def on_publish(client, userdata, mid):
    pass


# ─────────────────────────────────────────────
#  HSV CONVERSION
# ─────────────────────────────────────────────

def rgb_to_sv(rgb_f32: np.ndarray):
    """
    Compute Saturation and Value for an array of RGB pixels.

    Parameters
    ----------
    rgb_f32 : np.ndarray
        Shape (N, 3), dtype float32, values in [0.0, 1.0]. Channel order R,G,B.

    Returns
    -------
    S : np.ndarray  shape (N,)  Saturation in [0.0, 1.0]
    V : np.ndarray  shape (N,)  Value      in [0.0, 1.0]

    We only compute S and V (not H) because we filter on those two axes only.
    This avoids the full colorsys conversion and is ~3× faster.
    """
    r = rgb_f32[:, 0]
    g = rgb_f32[:, 1]
    b = rgb_f32[:, 2]

    V = np.maximum(np.maximum(r, g), b)            # Value = max channel
    min_c = np.minimum(np.minimum(r, g), b)
    delta = V - min_c

    # Saturation = delta / V, but 0 where V == 0 (pure black).
    S = np.where(V > 0.0, delta / V, 0.0)

    return S, V


# ─────────────────────────────────────────────
#  COLOR EXTRACTION
# ─────────────────────────────────────────────

def build_valid_mask(frame_bgra: np.ndarray) -> np.ndarray:
    """
    Build a full-frame boolean mask of pixels that should contribute to
    zone color averages, based on HSV saturation and brightness thresholds.

    Excluded pixels (mask == False):
      - Too dark  : V < V_MIN  (black bars, unlit regions)
      - Too white : S < S_MIN  (subtitles, blown-out highlights, grey UI)

    Parameters
    ----------
    frame_bgra : np.ndarray
        Full screenshot, shape (H, W, 4), dtype uint8, channel order BGRA.

    Returns
    -------
    np.ndarray
        Boolean mask of shape (H, W). True = pixel is valid.
    """
    H, W = frame_bgra.shape[:2]

    # Convert BGR → RGB, normalize to [0,1], flatten to (H*W, 3).
    # We index channels [2,1,0] to go BGR→RGB in one step.
    rgb = frame_bgra[:, :, [2, 1, 0]].astype(np.float32) / 255.0
    rgb_flat = rgb.reshape(-1, 3)                       # (H*W, 3)

    S_flat, V_flat = rgb_to_sv(rgb_flat)                # each shape (H*W,)

    # A pixel is valid only if it is bright enough AND saturated enough.
    valid_flat = (V_flat >= V_MIN) & (S_flat >= S_MIN)  # (H*W,) bool

    return valid_flat.reshape(H, W)                     # (H, W) bool


def extract_zone_colors(frame_bgra: np.ndarray, valid_mask: np.ndarray,
                        led_count: int) -> bytes:
    """
    Divide the frame into `led_count` equal VERTICAL zones (left → right).
    For each zone, average only the pixels that pass the HSV validity mask.

    LED 0  → leftmost screen zone
    LED 51 → rightmost screen zone

    If an entire zone has no valid pixels (e.g. a solid white subtitle bar or
    a black letterbox), that LED is set to (0, 0, 0) — off.

    Parameters
    ----------
    frame_bgra  : np.ndarray   Shape (H, W, 4), dtype uint8, BGRA.
    valid_mask  : np.ndarray   Shape (H, W), dtype bool.
    led_count   : int          Number of LEDs / zones.

    Returns
    -------
    bytes  156-byte payload: [R0,G0,B0, R1,G1,B1, ..., R51,G51,B51]
    """
    _, width = frame_bgra.shape[:2]
    zone_width = width // led_count

    # Work in float32 BGR — dropped alpha, ready for masked averaging.
    bgr = frame_bgra[:, :, :3].astype(np.float32)

    colors = np.zeros((led_count, 3), dtype=np.uint8)

    for i in range(led_count):
        x_start = i * zone_width
        x_end   = x_start + zone_width

        zone_mask = valid_mask[:, x_start:x_end]       # (H, zone_w) bool
        valid_count = zone_mask.sum()

        if valid_count == 0:
            # No valid pixels — LED stays off.
            continue

        zone_bgr    = bgr[:, x_start:x_end, :]         # (H, zone_w, 3)
        masked_bgr  = zone_bgr[zone_mask]               # (valid_count, 3)
        mean_bgr    = masked_bgr.mean(axis=0)           # (3,) [B, G, R]

        colors[i, 0] = np.uint8(mean_bgr[2])            # R
        colors[i, 1] = np.uint8(mean_bgr[1])            # G
        colors[i, 2] = np.uint8(mean_bgr[0])            # B

    return colors[::-1].tobytes()


# ─────────────────────────────────────────────
#  MQTT CLIENT SETUP
# ─────────────────────────────────────────────

def create_mqtt_client() -> mqtt.Client:
    """Create, configure, and connect the MQTT client."""
    client = mqtt.Client(client_id="ambient-led-publisher", clean_session=True)
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_publish    = on_publish

    print(f"[MQTT] Connecting to {BROKER_IP}:{BROKER_PORT} …")
    client.connect(BROKER_IP, BROKER_PORT, keepalive=60)

    # Background thread handles PINGREQ/PINGRESP without blocking capture loop.
    client.loop_start()

    return client


# ─────────────────────────────────────────────
#  CAPTURE LOOP
# ─────────────────────────────────────────────

def get_capture_region(sct: mss.mss) -> dict:
    if CAPTURE_REGION is not None:
        return CAPTURE_REGION
    return sct.monitors[1]


def run_capture_loop(client: mqtt.Client) -> None:
    """
    Main fixed-timestep capture loop.
    Captures frames at exactly FPS Hz, extracts zone colors, and publishes.
    """
    print(f"[CAPTURE] Starting capture loop at {FPS} FPS …")
    print(f"[CAPTURE] Publishing {PAYLOAD_SIZE} bytes/frame to '{TOPIC}'")
    print(f"[CAPTURE] HSV filter: V >= {V_MIN}, S >= {S_MIN}")
    print("[CAPTURE] Press Ctrl+C to stop.\n")

    frames_sent = 0
    loop_start  = time.perf_counter()
    deadline    = time.perf_counter()

    with mss.mss() as sct:
        region = get_capture_region(sct)
        print(f"[CAPTURE] Region: {region}")

        while True:
            # ── Capture ──────────────────────────────────────────────────
            screenshot = sct.grab(region)
            frame = np.frombuffer(screenshot.raw, dtype=np.uint8).reshape(
                screenshot.height, screenshot.width, 4
            )

            # ── Build HSV mask once per frame, reuse across all 52 zones ─
            valid_mask = build_valid_mask(frame)

            # ── Extract zone colors ───────────────────────────────────────
            payload = extract_zone_colors(frame, valid_mask, LED_COUNT)

            # ── Publish (QoS 0 — fire-and-forget, lowest latency) ─────────
            result = client.publish(TOPIC, payload, qos=0, retain=False)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                print(f"[MQTT] Publish failed: rc={result.rc}")

            frames_sent += 1

            # ── Diagnostics (every 5 seconds) ─────────────────────────────
            elapsed = time.perf_counter() - loop_start
            if elapsed >= 5.0:
                print(f"[STATS] Actual FPS: {frames_sent/elapsed:.1f} | "
                      f"Frames sent: {frames_sent} | "
                      f"Payload: {PAYLOAD_SIZE} B")
                frames_sent = 0
                loop_start  = time.perf_counter()

            # ── Fixed timestep sleep ──────────────────────────────────────
            deadline += FRAME_INTERVAL
            sleep_for = deadline - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

def main():
    client = create_mqtt_client()

    try:
        run_capture_loop(client)
    except KeyboardInterrupt:
        print("\n[CAPTURE] Stopped by user.")
    finally:
        print("[MQTT] Turning off all LEDs …")
        off_payload = bytes(PAYLOAD_SIZE)
        client.publish(TOPIC, off_payload, qos=1)
        time.sleep(0.3)

        print("[MQTT] Disconnecting …")
        client.loop_stop()
        client.disconnect()
        print("[MQTT] Done.")


if __name__ == "__main__":
    main()