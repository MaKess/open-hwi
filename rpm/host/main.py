#!/usr/bin/env python3
import argparse
import logging
import serial
import time
import itertools

class Device:
    def __init__(self, module, channel):
        self.line = "01:01:01"
        self.module = module
        self.channel = channel
        self.level = 0
        self.config = [5, 0, 0, 1]

    def __str__(self):
        return f"Device {self.line}:{self.module + 1:02x}:{self.channel + 1:02x} " \
               f"Config={'-'.join(f'{c:02x}' for c in self.config)} " \
               f"Level={self.level:#04x} [{'#' * self.level}{'.' * (0x7f - self.level)}]"

class OpenHWI:
    @staticmethod
    def get_args():
        parser = argparse.ArgumentParser(description="OpenHWI")
        parser.add_argument("--port", required=True, help="serial port (e.g. /dev/ttyACM0)")
        parser.add_argument("--debug", action="store_true")
        return parser.parse_args()

    def __init__(self) -> None:
        args = self.get_args()

        logging.basicConfig(
            format="%(asctime)s %(name)-10s %(levelname)-8s %(message)s",
            level=logging.DEBUG if args.debug else logging.INFO
        )

        self._logger = logging.getLogger(self.__class__.__name__)

        # open serial connection
        try:
            ser = serial.Serial(port=args.port)
        except serial.SerialException as exc:
            raise SystemExit(f"Failed to open {args.port}: {exc}") from exc
        self._serial_port = ser

        self._logger.info("Serial port %s opened", args.port)

        self.cycle = itertools.cycle(range(4))

        self.devices = [
            [
                Device(module=m, channel=c) for c in range(4)
            ] for m in range(8)
        ]

    def loop(self, index, hearbeat):
        data = [0x81 + index + (hearbeat << 6)]
        for m in range(6):
            module = self.devices[m]
            for c in range(4):
                device = module[c]
                data.append(device.level)
                data.append(device.config[index])
        data.append(sum(data) & 0x7f)
        for m in range(6,8):
            module = self.devices[m]
            for c in range(4):
                device = module[c]
                data.append(device.level)
                data.append(device.config[index])
        data.append(sum(data[-(2*2*4+1):]) & 0x7f)

        msg = bytes(data)

        self._logger.debug("data to send: %s", msg.hex(" "))
        self._serial_port.write(msg)

    DATA_INTERVAL = 0.1
    BEAT_INTERVAL = 1.2

    def go(self):
        now = time.time()
        next_loop = now + self.DATA_INTERVAL
        next_beat = now + self.BEAT_INTERVAL
        try:
            while True:
                now = time.time()
                i = next(self.cycle)
                beat = i == 3 and now >= next_beat
                if beat:
                    next_beat += self.BEAT_INTERVAL


                # TODO: test hack

                self.devices[0][0].level = 0 if int(now) % 10 < 5 else 0x7f


                self.loop(i, beat)
                sleep_for = next_loop - now
                next_loop += self.DATA_INTERVAL
                self._logger.debug("sleeping: now=%.3f, next_loop=%.3f, sleep_for=%.3f", now, next_loop, sleep_for)
                time.sleep(sleep_for)

        except KeyboardInterrupt:
            self._logger.info("stopped through keyboard interrupt")



if __name__ == "__main__":
    raise SystemExit(OpenHWI().go())
