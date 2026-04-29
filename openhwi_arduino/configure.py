#!/usr/bin/env python3

import argparse
import serial

def get_args():
    parser = argparse.ArgumentParser(description="OpenHWI Arduino")
    parser.add_argument("--port", required=True, help="serial port (e.g. /dev/ttyACM0)")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--set-mode", type=int, help="set the Arduino's operation mode: 1=Link, 2=RPM")
    action.add_argument("--set-ident", type=int, help="set the Arduino's identifier")
    action.add_argument("--get-mode", action="store_true", help="get the Arduino's operation mode: 1=Link, 2=RPM")
    action.add_argument("--get-ident", action="store_true", help="get the Arduino's identifier")
    return parser.parse_args()

def main(args):
    try:
        ser = serial.Serial(port=args.port)
    except serial.SerialException as exc:
        raise SystemExit(f"Failed to open serial connection: {exc}") from exc

    if args.set_mode is not None:
        ser.write(bytes((0xfc, args.set_mode)))
        ser.flush()

    if args.set_ident is not None:
        ser.write(bytes((0xfb, args.set_ident)))
        ser.flush()

    if args.get_mode:
        ser.write(b"\xfa")
        ser.flush()
        buffer = bytearray()
        while True:
            buffer += ser.read(3)
            if len(buffer) >= 3:
                assert buffer[0:2] == b"\xff\xfa"
                print(f"mode: {buffer[2]}")
                break

    if args.get_ident:
        ser.write(b"\xf9")
        ser.flush()
        buffer = bytearray()
        while True:
            buffer += ser.read(3)
            if len(buffer) >= 3:
                assert buffer[0:2] == b"\xff\xf9"
                print(f"ident: {buffer[2]}")
                break

if __name__ == "__main__":
    main(get_args())