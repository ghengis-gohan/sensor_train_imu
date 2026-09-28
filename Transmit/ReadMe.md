# AltIMU-10 v5 on the Jetson Orin Nano Developer Kit

Telemetry node for the Mission AI Possible train: the Jetson reads the Pololu
AltIMU-10 v5 over I²C, packs the readings into the 35-byte frame the receiver
already understands, and streams them out an XBee-PRO 900HP USB dongle. The node
runs as a RHEL 9.4 (UBI) container that starts on boot.

Files:

| File | Purpose |
|---|---|
| `node.py` | Reads the three sensors, builds packets, writes them to the XBee |
| `Containerfile` | UBI 9.4 minimal image, `node.py` as the entrypoint |
| `xbee-node.service` | systemd unit that runs the container at boot |
| `requirements.txt` | `smbus2`, `pyserial` |

## 1. Wiring

The AltIMU is a 3.3 V I²C device. The Orin Nano's 40-pin header is 3.3 V logic with
pull-ups already on the carrier, so it wires straight in: four jumpers, no shifter,
no regulator. Power off before wiring.

| AltIMU pin | Header pin | Signal |
|---|---|---|
| `VIN` | **1** | 3.3 V. Never pin 2 or 4 (5 V): the board pulls SDA/SCL up to VIN and the Jetson's GPIO is not 5 V tolerant |
| `GND` | **6** | Ground |
| `SDA` | **3** | I2C1 SDA → `/dev/i2c-7` |
| `SCL` | **5** | I2C1 SCL → `/dev/i2c-7` |
| `VDD` | — | leave unconnected |
| `SA0` | — | leave unconnected (default addresses) |

Pin 1 is the corner pin nearest the barrel jack / edge of the carrier; pins are
numbered odd on the inside row, even on the outside row, the same layout as a
Raspberry Pi. If bus 7 shows nothing in step 2, move `SDA` to pin **27** and `SCL`
to pin **28** (I2C0 → `/dev/i2c-1`) and set `I2C_BUS=1` everywhere below.

Mounting: bolt the AltIMU to the locomotive frame, not to the Jetson carrier, with
`X` pointing forward and `Z` up. Keep it a few centimetres from the traction motor;
the magnetometer will still see the motor, but the accelerometer and gyro won't care.

The XBee goes in a USB dongle plugged into any of the Jetson's USB-A ports. It
enumerates as `/dev/ttyUSB0` (FTDI). Antenna on before power.

## 2. Verify on the host first

    sudo apt install -y i2c-tools
    sudo usermod -aG i2c,dialout $USER      # log out and back in
    i2cdetect -y -r 7

Expected:

         0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
    10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- 1e --
    50: -- -- -- -- -- -- -- -- -- -- -- -- -- 5d -- --
    60: -- -- -- -- -- -- -- -- -- -- -- 6b -- -- -- --

`0x1e` LIS3MDL (mag), `0x5d` LPS25H (baro), `0x6b` LSM6DS33 (accel/gyro).
All three or nothing: if the row is empty, it's wiring or the wrong bus.

Then run the node natively once, with the dongle in:

    pip3 install -r requirements.txt
    PRINT_DIV=5 python3 node.py

You should see the same `t=... | a(g) ...` lines the Arduino produced, and the Pi's
`receive.py` (or the RHEL dashboard) should be scrolling at 50 Hz. Ctrl-C when happy.
Everything from here on is packaging.

## 3. Build the container

On the Jetson (Docker ships with JetPack):

    docker build -t xbee-node:latest .

Or on an x86 box with `--platform linux/arm64` and push/load it. The image is UBI
9.4 minimal plus Python and two pip packages, ~120 MB.

## 4. Run it by hand

The container needs exactly two device nodes from the host:

    docker run --rm -it \
      --device /dev/i2c-7:/dev/i2c-7 \
      --device /dev/ttyUSB0:/dev/ttyUSB0 \
      -e PRINT_DIV=5 \
      xbee-node:latest

`--device` passes the node through with its host permissions and lets the cgroup
device controller allow it; nothing else (no `--privileged`, no `/dev` bind mount)
is required. The container runs as root inside, which is what makes the `i2c`
and `dialout` group ownership on the host nodes a non-issue.

`node.py` waits if either device is missing and restarts itself if the dongle is
yanked, so the container never exits on a transient.

## 5. Start on boot

    sudo cp xbee-node.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now xbee-node
    journalctl -fu xbee-node

The unit runs `docker run` with the two devices and the environment shown in
step 4, restarts on failure, and stops the container cleanly on shutdown. Docker
itself is enabled by default on JetPack. If you install a recent Podman (≥ 4.4)
instead, the same thing is a Quadlet file with `AddDevice=` lines; the Docker unit
is provided because JetPack's Ubuntu 22.04 ships Podman 3.4, which predates Quadlet.

Optional stable device name: if other FTDI devices might appear, add

    SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", SYMLINK+="xbee"

to `/etc/udev/rules.d/99-xbee.rules` and use `/dev/xbee` in the unit.

## 6. Environment reference

| Variable | Default | Meaning |
|---|---|---|
| `I2C_BUS` | `7` | `/dev/i2c-N` the AltIMU is on (`1` for pins 27/28) |
| `XBEE_PORT` | `/dev/ttyUSB0` | XBee dongle |
| `XBEE_BAUD` | `38400` | must match the radios' `BD` |
| `RATE_HZ` | `50` | packet rate; 38400 baud caps this near 100 |
| `PRINT_DIV` | `0` | print every Nth sample; `0` = silent (use `5` when debugging) |

## 7. Troubleshooting

| Symptom | Look at |
|---|---|
| `LSM6DS33 not answering` in the log | Wiring, or bus 7 vs 1. `i2cdetect` on the host is the arbiter |
| `waiting for /dev/ttyUSB0` forever | Dongle not enumerated: `dmesg \| grep FTDI`, try another port |
| Log says up, receiver sees nothing | Radio pairing: both XBees must share `ID`/`HP`, both at `BD 5`. `xbee_config.py --read` on each |
| Receiver shows CRC errors | Baud mismatch somewhere in radio/node/receiver |
| `Permission denied` on `/dev/i2c-7` when running natively | Group membership not applied yet: log out and in |
| Mag axis pegged or wandering | Expected on a locomotive; the motor's field dominates |