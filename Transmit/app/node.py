#!/usr/bin/env python3
"""AltIMU-10 v5 telemetry node for the Jetson Orin Nano (replaces the Arduino sketch).

Reads LSM6DS33, LIS3MDL and LPS25H over the 40-pin header I2C bus, packs the
same 35-byte frame the RHEL receiver (app.py) already decodes, and writes it to
the XBee USB dongle. Sending 'z' back over the link re-zeroes the printed altitude.

Wiring: AltIMU VIN->pin 1 (3.3 V), GND->pin 6, SDA->pin 3, SCL->pin 5  (bus 7)
        fallback: SDA->pin 27, SCL->pin 28                              (bus 1)

Environment:
  I2C_BUS      default 7
  XBEE_PORT    default /dev/ttyUSB0
  XBEE_BAUD    default 38400 (must match the dongle's ATBD)
  RATE_HZ      default 50
  PRINT_DIV    print every Nth sample to stdout, 0 = silent (default 5)
"""

import logging
import os
import struct
import time

import serial
from smbus2 import SMBus

log = logging.getLogger("node")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

I2C_BUS = int(os.environ.get("I2C_BUS", "7"))
XBEE_PORT = os.environ.get("XBEE_PORT", "/dev/ttyUSB0")
XBEE_BAUD = int(os.environ.get("XBEE_BAUD", "38400"))
RATE_HZ = float(os.environ.get("RATE_HZ", "50"))
PRINT_DIV = int(os.environ.get("PRINT_DIV", "5"))

# ---- chip addresses and registers (Pololu defaults, SA0 floating) ----
LSM6_ADDR, LIS3_ADDR, LPS_ADDR = 0x6B, 0x1E, 0x5D
WHO_AM_I = 0x0F
LSM6_ID, LIS3_ID, LPS_ID = 0x69, 0x3D, 0xBD

LSM6_CTRL1_XL, LSM6_CTRL2_G, LSM6_CTRL3_C, LSM6_OUTX_L_G = 0x10, 0x11, 0x12, 0x22
LIS3_CTRL1, LIS3_CTRL2, LIS3_CTRL3, LIS3_CTRL4, LIS3_OUT_X_L = 0x20, 0x21, 0x22, 0x23, 0x28
LPS_CTRL1, LPS_PRESS_XL = 0x20, 0x28
AUTO_INC = 0x80  # LIS3MDL and LPS25H need the sub-address MSB set for multi-byte reads

# ---- packet format: identical to main.cpp / app.py ----
SYNC = b"\xaa\x55"
PAYLOAD_LEN = 30
FMT_BODY = "<BIH9hih"

ACC_G_PER_LSB = 0.061 / 1000.0
GYR_DPS_PER_LSB = 8.75 / 1000.0
MAG_G_PER_LSB = 1.0 / 6842.0


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def altitude_m(p_mbar: float, p0_mbar: float) -> float:
    return (1.0 - (p_mbar / p0_mbar) ** 0.190263) * 44330.8


class AltIMU:
    """Minimal driver for the three chips on the AltIMU-10 v5."""

    def __init__(self, bus: SMBus):
        self.bus = bus

    def _check(self, addr, expected, name):
        try:
            got = self.bus.read_byte_data(addr, WHO_AM_I)
        except OSError as exc:
            raise SystemExit(f"{name} not answering at 0x{addr:02X} on i2c-{I2C_BUS}: {exc}. "
                             f"Check wiring; try I2C_BUS=1 with pins 27/28.") from exc
        if got != expected:
            raise SystemExit(f"{name} WHO_AM_I read 0x{got:02X}, expected 0x{expected:02X}")

    def init(self):
        self._check(LSM6_ADDR, LSM6_ID, "LSM6DS33")
        self._check(LIS3_ADDR, LIS3_ID, "LIS3MDL")
        self._check(LPS_ADDR, LPS_ID, "LPS25H")

        w = self.bus.write_byte_data
        w(LSM6_ADDR, LSM6_CTRL3_C, 0x44)   # BDU + register auto-increment
        w(LSM6_ADDR, LSM6_CTRL1_XL, 0x40)  # accel 104 Hz, +/-2 g
        w(LSM6_ADDR, LSM6_CTRL2_G, 0x40)   # gyro  104 Hz, +/-245 dps

        w(LIS3_ADDR, LIS3_CTRL1, 0x7C)     # ultra-high-perf XY, 80 Hz
        w(LIS3_ADDR, LIS3_CTRL2, 0x00)     # +/-4 gauss
        w(LIS3_ADDR, LIS3_CTRL3, 0x00)     # continuous conversion
        w(LIS3_ADDR, LIS3_CTRL4, 0x0C)     # ultra-high-perf Z

        w(LPS_ADDR, LPS_CTRL1, 0xC0)       # active, 25 Hz
        time.sleep(0.1)

    def read(self):
        """Returns (a[3], g[3], m[3], p_raw, t_raw) as raw signed integers."""
        raw = self.bus.read_i2c_block_data(LSM6_ADDR, LSM6_OUTX_L_G, 12)
        gx, gy, gz, ax, ay, az = struct.unpack("<6h", bytes(raw))

        raw = self.bus.read_i2c_block_data(LIS3_ADDR, LIS3_OUT_X_L | AUTO_INC, 6)
        mx, my, mz = struct.unpack("<3h", bytes(raw))

        raw = bytes(self.bus.read_i2c_block_data(LPS_ADDR, LPS_PRESS_XL | AUTO_INC, 5))
        p_raw = int.from_bytes(raw[0:3], "little", signed=True)
        t_raw = struct.unpack("<h", raw[3:5])[0]

        return (ax, ay, az), (gx, gy, gz), (mx, my, mz), p_raw, t_raw


def build_packet(t_ms: int, seq: int, a, g, m, p_raw: int, t_raw: int) -> bytes:
    body = struct.pack(FMT_BODY, PAYLOAD_LEN, t_ms & 0xFFFFFFFF, seq & 0xFFFF,
                       *a, *g, *m, p_raw, t_raw)
    return SYNC + body + struct.pack("<H", crc16_ccitt(body))


def wait_for_devices():
    """Block until the I2C bus and the XBee port both exist (USB can enumerate late at boot)."""
    i2c_dev = f"/dev/i2c-{I2C_BUS}"
    while True:
        missing = [d for d in (i2c_dev, XBEE_PORT) if not os.path.exists(d)]
        if not missing:
            return
        log.warning("waiting for %s", ", ".join(missing))
        time.sleep(2)


def run():
    period = 1.0 / RATE_HZ
    with SMBus(I2C_BUS) as bus, serial.Serial(XBEE_PORT, XBEE_BAUD, timeout=0) as xbee:
        imu = AltIMU(bus)
        imu.init()
        log.info("AltIMU-10 v5 up on i2c-%d; XBee on %s at %d; %.0f Hz",
                 I2C_BUS, XBEE_PORT, XBEE_BAUD, RATE_HZ)

        _, _, _, p_raw, _ = imu.read()
        p0 = p_raw / 4096.0
        seq = 0
        t0 = time.monotonic()
        next_tick = t0

        while True:
            now = time.monotonic()
            if now < next_tick:
                time.sleep(next_tick - now)
                now = time.monotonic()
            next_tick += period
            if now - next_tick > 1.0:      # fell far behind (suspend, stall): resync
                next_tick = now + period

            if xbee.in_waiting and xbee.read(xbee.in_waiting).find(b"z") >= 0:
                p0 = imu.read()[3] / 4096.0
                log.info("altitude zeroed at %.2f mbar", p0)

            a, g, m, p_raw, t_raw = imu.read()
            t_ms = int((now - t0) * 1000)
            xbee.write(build_packet(t_ms, seq, a, g, m, p_raw, t_raw))

            if PRINT_DIV and seq % PRINT_DIV == 0:
                p_mbar = p_raw / 4096.0
                print(f"t={t_ms} | a(g) {a[0]*ACC_G_PER_LSB:.3f} {a[1]*ACC_G_PER_LSB:.3f} {a[2]*ACC_G_PER_LSB:.3f}"
                      f" | g(dps) {g[0]*GYR_DPS_PER_LSB:.1f} {g[1]*GYR_DPS_PER_LSB:.1f} {g[2]*GYR_DPS_PER_LSB:.1f}"
                      f" | m(G) {m[0]*MAG_G_PER_LSB:.3f} {m[1]*MAG_G_PER_LSB:.3f} {m[2]*MAG_G_PER_LSB:.3f}"
                      f" | p {p_mbar:.2f} mb alt {altitude_m(p_mbar, p0):.1f} m T {42.5 + t_raw/480.0:.1f} C",
                      flush=True)
            seq = (seq + 1) & 0xFFFF


def main():
    """Run forever: recover from a yanked dongle or a bus hiccup instead of exiting."""
    while True:
        wait_for_devices()
        try:
            run()
        except (serial.SerialException, OSError) as exc:
            log.error("%s; restarting in 2 s", exc)
            time.sleep(2)


if __name__ == "__main__":
    main()