# Train telemetry receiver

The locomotive carries an IMU (accelerometer, gyro, magnetometer, barometer) on a
Jetson that broadcasts 50 readings a second over a 900 MHz XBee radio. This
service sits on the box with the matching XBee USB dongle and turns that stream
into JSON over HTTP, so the splash page can show live numbers next to the video.

You don't need to know anything about the radio or the packet format. Everything
arrives converted to real units. Read this in order: **Run it**, **Get the data**,
**What the fields mean**, then **Putting it on the page**.

Files:

| File | Purpose |
|---|---|
| `receiver.py` | The service: serial in, HTTP/JSON out |
| `xbee_link.py` | Radio framing and unit conversion (you won't touch this) |
| `Containerfile` | UBI 9.4 image |
| `xbee-receiver.container` | Podman Quadlet unit, starts it on boot |
| `requirements.txt` | just `pyserial` |

## Run it

Natively, for development (any Linux box with the dongle in):

    sudo usermod -aG dialout $USER     # once, then log out/in
    pip3 install -r requirements.txt
    python3 receiver.py                # http://localhost:8000

As a container on the RHEL server (the way it runs in the demo):

    podman build -t xbee-receiver:latest .
    sudo setsebool -P container_use_devices on          # once; lets the container open the dongle
    sudo cp xbee-receiver.container /etc/containers/systemd/
    sudo systemctl daemon-reload && sudo systemctl start xbee-receiver
    journalctl -fu xbee-receiver

Open `http://<server>:8000/` in a browser. That page is a minimal live readout,
useful to confirm the link is alive before touching the splash page, and its
source is a working example of everything below (view-source on it).

## Get the data

Four endpoints. All return JSON, all send `Access-Control-Allow-Origin: *`, so a
page served from any other host or port can call them directly.

### `GET /api/stream?hz=10` (recommended for live display)

Server-Sent Events. Holds the connection open and pushes one sample per event at
the rate you ask for (`hz` from 0.5 to 50; default 10). Each event's `data:` line
is one sample object (see fields below) with a `stats` object attached.

    curl -N http://<server>:8000/api/stream?hz=2

### `GET /api/latest`

The newest sample plus link stats. Poll this if you'd rather not use SSE.

    {"sample": { ...sample... }, "stats": { ...stats... }}

### `GET /api/history?seconds=10`

Array of samples from the last N seconds (up to about 10 s / 500 samples). For
sparklines or a "last few seconds" graph.

    {"samples": [ {...}, {...}, ... ], "stats": { ... }}

### `GET /api/stats`

Link health only. `POST /api/zero` re-zeroes altitude to the current pressure
(the track is flat, so altitude drifts with weather; zero it at the start of a run).

## What the fields mean

A sample:

```json
{
  "t_ms": 184220,                 "seq": 9211,      "host_time": 1727530000.123,
  "accel_g":   {"x": -0.03, "y": 0.02, "z": 1.00},
  "gyro_dps":  {"x": 0.4,   "y": -1.2, "z": 18.5},
  "mag_gauss": {"x": -0.06, "y": -0.83, "z": 0.35},
  "pressure_mbar": 1018.46,  "altitude_m": 0.12,  "temp_c": 26.7,
  "train": {
    "forward_g": -0.03, "lateral_g": 0.02, "vertical_g": 1.00,
    "pitch_deg": -1.7,  "roll_deg": 1.1,   "yaw_rate_dps": 18.5,
    "roughness_g": 0.012
  },
  "stats": {"connected": true, "rate_hz": 50, "packets": 9212,
            "dropped": 0, "crc_errors": 0, "age_s": 0.02}
}
```

| Field | Units | Notes |
|---|---|---|
| `t_ms` | ms | Node's clock since boot. Use for ordering, not wall time |
| `seq` | count | Wraps at 65535. Gaps = lost radio packets |
| `host_time` | s (epoch) | When the server received it |
| `accel_g.{x,y,z}` | g | Raw sensor axes. At rest, one axis reads ~1.0 (gravity) |
| `gyro_dps.{x,y,z}` | °/s | Rotation rate about each sensor axis |
| `mag_gauss.{x,y,z}` | gauss | Magnetometer. On the loco it mostly sees the motor; treat as decorative |
| `pressure_mbar` | mbar | Barometer |
| `altitude_m` | m | Relative to the last zero. ±0.3 m noise is normal |
| `temp_c` | °C | Sensor die temperature (runs a few degrees warm) |
| `train.forward_g` | g | Acceleration along the track: positive speeding up, negative braking |
| `train.lateral_g` | g | Sideways: shows curves (sign = direction of turn) |
| `train.vertical_g` | g | Up/down: ~1.0 at rest, spikes at rail joints |
| `train.pitch_deg` | ° | Nose up/down, i.e. grade. Only meaningful when not accelerating |
| `train.roll_deg` | ° | Lean, i.e. banking/tilt |
| `train.yaw_rate_dps` | °/s | Turn rate. Zero on straights, steady value on curves |
| `train.roughness_g` | g | RMS vertical vibration over the last second. "How rough is this track section" |
| `stats.rate_hz` | Hz | Should read ~50. Lower = radio trouble |
| `stats.age_s` | s | Time since last packet. Grey out the display if this exceeds ~1 |

The `train.*` block is the one to display. It's the raw axes remapped into the
train's frame (forward / lateral / up) plus a few derived quantities.

**Translating, if the numbers look wrong.** The remap depends on how the sensor is
bolted to the loco. If `vertical_g` doesn't sit near 1.0 with the train parked, or
`forward_g` doesn't go positive when it pulls away, the mapping is off. Fix it on the
server, not on the page: set `AXIS_MAP` to name which *sensor* axis is forward,
lateral, and up, with `-` to flip a sign. Examples: `AXIS_MAP=x,y,z` (default),
`AXIS_MAP=y,-x,z` (sensor rotated 90° on the frame), `AXIS_MAP=-x,-y,z` (mounted
backwards). Change the `Environment=` line in the Quadlet file and restart the
service. Park the train, check `vertical_g ≈ 1`; drive it forward, check
`forward_g > 0` briefly; done.

## Putting it on the page

Three lines of JavaScript, no library:

```html
<div id="imu">waiting for telemetry…</div>
<script>
  const es = new EventSource("http://<server>:8000/api/stream?hz=10");
  es.onmessage = (e) => {
    const s = JSON.parse(e.data), t = s.train;
    document.getElementById("imu").textContent =
      `fwd ${t.forward_g.toFixed(2)} g · lat ${t.lateral_g.toFixed(2)} g · ` +
      `rough ${t.roughness_g.toFixed(3)} g · yaw ${t.yaw_rate_dps.toFixed(0)} °/s · ` +
      `alt ${s.altitude_m.toFixed(1)} m · ${s.stats.rate_hz} Hz`;
  };
  es.onerror = () => { document.getElementById("imu").textContent = "telemetry offline"; };
</script>
```

`EventSource` reconnects on its own if the server restarts, so there's nothing
to manage. Use `hz=10` for numbers, `hz=25` or `50` if you draw a live trace.

Polling alternative, if SSE doesn't fit the page framework:

```js
setInterval(async () => {
  const {sample, stats} = await (await fetch("http://<server>:8000/api/latest")).json();
  if (!sample || stats.age_s > 1) return showOffline();
  render(sample.train);
}, 200);
```

Suggestions for what reads well over video: a lateral-g bar that swings left and
right through curves, a roughness value that jumps on the rough section of track,
and a yaw-rate arrow. Pitch/roll are near zero on flat track and don't earn their
screen space. Altitude is fun on a grade, noise otherwise. The magnetometer is
there for completeness only.

Serving the splash page from the same host as this service? Reverse-proxy `/api/`
to port 8000 and drop the `http://<server>:8000` prefix; SSE passes through nginx
with `proxy_buffering off;` on that location.

## If it's not working

| Symptom | Cause |
|---|---|
| `stats.connected` false | No dongle at `XBEE_PORT`. `ls /dev/ttyUSB*` on the server; the container needs `AddDevice` pointing at the right one |
| Connected, `rate_hz` 0, `age_s` climbing | Radio link down: node not powered, or radios not paired. Check the loco side first |
| `crc_errors` climbing | Baud mismatch between radio and node; should not happen once set up |
| `dropped` climbing steadily | Range or interference. Raise the node radio's power level |
| Page gets nothing but `curl` works | CORS or mixed-content: an https page can't load an http stream. Proxy it |