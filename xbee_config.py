#!/usr/bin/env python3
"""Configure an XBee-PRO 900HP (S3B, DigiMesh) for the AltIMU telemetry link.

Finds the radio's current baud, enters command mode, applies the link settings,
writes them to flash, and prints the radio's SH/SL so you can pair the other one.

    python3 xbee_config.py --ni pi               # set BD=38400, AP=0, name it; leave the rest
    python3 xbee_config.py --read                # show current settings, change nothing
    python3 xbee_config.py --id 1AB4 --hp 0      # also set network ID / preamble
    python3 xbee_config.py --broadcast           # DH=0 DL=FFFF instead of a paired address
    python3 xbee_config.py --dh 13A200 --dl 41B31EC9   # unicast to a specific radio
    python3 xbee_config.py --pl 1                # low power for the bench (4 = max)

Only BD and AP are always set; everything else changes only if you pass a flag, so a
radio that is already paired keeps its ID/HP/DH/DL. Run once per radio in the dongle.
Note: on the S3B the RF data rate is fixed by firmware (8x7x = DigiMesh 200k); there
is no BR parameter.
"""

import argparse
import sys
import time

import serial

BAUDS = (9600, 38400, 115200, 19200, 57600)

ALWAYS = {
    "BD": "5",      # interface baud 38400, matches the sketch and receive.py
    "AP": "0",      # transparent mode
}

READ_PARAMS = ["VR", "HV", "SH", "SL", "ID", "HP", "BD", "AP", "PL", "DH", "DL", "NI"]


def enter_command_mode(ser):
    ser.reset_input_buffer()
    time.sleep(1.2)                 # guard time before
    ser.write(b"+++")
    time.sleep(1.2)                 # guard time after
    reply = ser.read(ser.in_waiting or 1)
    return b"OK" in reply


def at(ser, cmd, value=None, timeout=1.0):
    line = f"AT{cmd}" + (f" {value}" if value is not None else "") + "\r"
    ser.reset_input_buffer()
    ser.write(line.encode())
    deadline = time.time() + timeout
    buf = b""
    while time.time() < deadline and not buf.endswith(b"\r"):
        buf += ser.read(1)
    return buf.decode(errors="replace").strip()


def open_radio(port):
    for baud in BAUDS:
        ser = serial.Serial(port, baud, timeout=0.1)
        if enter_command_mode(ser):
            print(f"# radio answered at {baud} baud")
            return ser
        ser.close()
    sys.exit("no OK from the radio at any common baud; check the dongle and that nothing else has the port open")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--read", action="store_true", help="show settings, change nothing")
    p.add_argument("--id", help="network ID (hex), same on both radios")
    p.add_argument("--hp", help="preamble ID 0-7, same on both radios")
    p.add_argument("--dh", help="destination high (other radio's SH)")
    p.add_argument("--dl", help="destination low (other radio's SL)")
    p.add_argument("--broadcast", action="store_true", help="DH=0, DL=FFFF")
    p.add_argument("--pl", help="power level 0-4 (1 for bench, 4 for range)")
    p.add_argument("--ni", help="node identifier, e.g. node or pi")
    args = p.parse_args()

    ser = open_radio(args.port)

    print("# current settings")
    for k in READ_PARAMS:
        print(f"  {k} = {at(ser, k)}")

    if args.read:
        at(ser, "CN")
        return

    to_set = dict(ALWAYS)
    for key, val in (("ID", args.id), ("HP", args.hp), ("PL", args.pl), ("NI", args.ni),
                     ("DH", args.dh), ("DL", args.dl)):
        if val is not None:
            to_set[key] = val
    if args.broadcast:
        to_set["DH"], to_set["DL"] = "0", "FFFF"

    print("# applying")
    ok = True
    for k, v in to_set.items():
        r = at(ser, k, v)
        print(f"  {k} {v} -> {r}")
        ok &= r == "OK"
    if not ok:
        at(ser, "CN")
        sys.exit("a setting was rejected; nothing written")

    print(f"  WR -> {at(ser, 'WR', timeout=2.0)}")
    print(f"  CN -> {at(ser, 'CN')}")
    print("# written. This radio now talks at 38400; unplug/replug the dongle before the next one.")


if __name__ == "__main__":
    main()