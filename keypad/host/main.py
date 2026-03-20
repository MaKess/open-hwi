#!/usr/bin/env python3
import argparse
import threading
import time
import struct
import itertools
import queue
import logging
import serial

ESCAPE_BYTE = 0xFF
ACK_BYTE = 0xFE
GAP_BYTE = 0xFD

PACKET_START = 0xB0

MESSAGE_LED_MASK = 0x80

def pack_message(message_type: int, payload: bytes):
    length = 3 + len(payload) + 2
    checksum = -(PACKET_START + message_type + length + sum(payload)) & 0xff
    return struct.pack(f"<BBB{len(payload)}sBx",
        PACKET_START,
        message_type,
        length,
        payload,
        checksum
    )


class BusLinkReceiverThread(threading.Thread):
    def __init__(
            self,
            serial_port: serial.Serial,
            main_event: threading.Event,
            receive_queue: queue.Queue,
            ack_event: threading.Event
        ):
        super().__init__(
            name="Bus-Link-Receiver",
            daemon=True
        )
        self._serial_port = serial_port
        self._main_event = main_event
        self._receive_queue = receive_queue
        self._ack_event = ack_event
        self._logger = logging.getLogger(self.name)

    def run(self):
        escaping = False
        buffer = bytearray()
        while True:
            chunk = self._serial_port.read(1)
            if not chunk:
                continue
            byte = chunk[0]
            if escaping:
                escaping = False

                match byte:
                    case 0xFE: # ACK_BYTE
                        self._ack_event.set()
                    case 0xFD: # GAP_BYTE
                        if buffer:
                            self._receive_queue.put(bytes(buffer))
                            self._main_event.set()
                            buffer.clear()
                    case 0xFF: # ESCAPE_BYTE
                        buffer.append(byte)
                    case _:
                        self._logger.error("received invalid escape byte 0x%02x", byte)
            elif byte == 0xFF: # ESCAPE_BYTE
                escaping = True
            else:
                buffer.append(byte)


class BusLinkSenderThread(threading.Thread):
    ACK_TIMEOUT = 1.0

    def __init__(
            self,
            serial_port: serial.Serial,
            send_queue: queue.Queue,
            ack_event: threading.Event
        ):
        super().__init__(
            name="Bus-Link-Sender",
            daemon=True
        )
        self._serial_port = serial_port
        self._send_queue = send_queue
        self._ack_event = ack_event
        self._logger = logging.getLogger(self.name)

    def run(self):
        while True:
            msg = self._send_queue.get()
            self._ack_event.clear()
            self._serial_port.write(msg)
            self._serial_port.flush()
            self._ack_event.wait(self.ACK_TIMEOUT)


class LEDpingThread(threading.Thread):
    def __init__(self, interval: float, event: threading.Event):
        super().__init__(
            name="LED-ping",
            daemon=True
        )
        self._event = event
        self._interval = interval
        self._logger = logging.getLogger(self.name)

    def run(self):
        while True:
            self._logger.debug("wakeup!")
            self._event.set()
            time.sleep(self._interval)


class LEDstate:
    LED_PER_KEYPAD = 24
    KEYPAD_PER_BUS = 32
    KEYPAD_PER_MESSAGE = 4
    BITS_PER_LED = 2
    BITS_PER_BYTE = 8

    def __init__(self):
        self.state : list[list[int]] = [
            [0 for _ in range(self.LED_PER_KEYPAD)]
            for _ in range(self.KEYPAD_PER_BUS)
        ]
        self.cycle = itertools.cycle(range(self.KEYPAD_PER_BUS // self.KEYPAD_PER_MESSAGE))

    def __getitem__(self, key):
        if isinstance(key, int):
            return sum(
                state << (i * 2)
                for i, state in enumerate(self.state[key])
            )
        elif isinstance(key, slice):
            assert key.step in (1, None)
            return b"".join(
                struct.pack("<Q", self[key2])[0:6] # 6 = LED_PER_KEYPAD * BITS_PER_LED / BITS_PER_BYTE
                for key2 in range(key.start, key.stop)
            )
        else:
            raise TypeError()

    def make_next_message(self):
        index = next(self.cycle)
        start = index * self.KEYPAD_PER_MESSAGE
        payload = struct.pack(
            "<B24s", # 24 = KEYPAD_PER_MESSAGE * LED_PER_KEYPAD * BITS_PER_LED / BITS_PER_BYTE
            0xc0 + index,
            self[start:start + self.KEYPAD_PER_MESSAGE]
        )
        return pack_message(
            MESSAGE_LED_MASK,
            payload
        )


class OpenHWI:
    @staticmethod
    def get_args():
        parser = argparse.ArgumentParser(description="OpenHWI")
        parser.add_argument("port", help="serial port (e.g. /dev/ttyACM0)")
        parser.add_argument("--led-interval", type=float, default=0.1)
        parser.add_argument("--debug", action="store_true")
        return parser.parse_args()

    def __init__(self):
        args = self.get_args()

        self._led_interval = args.led_interval

        logging.basicConfig(
            format="%(asctime)s %(name)-10s %(levelname)-8s %(message)s",
            level=logging.DEBUG if args.debug else logging.INFO
        )

        self._logger = logging.getLogger("main")

        # open serial connection
        try:
            ser = serial.Serial(port=args.port)
        except serial.SerialException as exc:
            raise SystemExit(f"Failed to open {args.port}: {exc}") from exc

        self._led_state = LEDstate()

        self._main_loop_event = main_loop_event = threading.Event()
        ack_event = threading.Event()
        self._send_queue = send_queue = queue.Queue()
        self._receive_queue = receive_queue = queue.Queue()

        bus_link_sender = BusLinkSenderThread(ser, send_queue, ack_event)
        bus_link_sender.start()

        bus_link_receiver = BusLinkReceiverThread(ser, main_loop_event, receive_queue, ack_event)
        bus_link_receiver.start()

        led_ping = LEDpingThread(args.led_interval, main_loop_event)
        led_ping.start()
        self._last_led_ping = 0

    def go(self):
        try:
            while True:
                self._main_loop_event.wait()
                self.loop()
                self._main_loop_event.clear()
        except KeyboardInterrupt:
            self._logger.info("stopped through keyboard interrupt")

        return 0

    def loop(self):
        now = time.time()

        try:
            self.handle_message(self._receive_queue.get(block=False))
        except queue.Empty:
            pass

        if now - self._last_led_ping >= self._led_interval * 0.99:
            msg = self._led_state.make_next_message()
            self._logger.debug("send LED update: %s", msg.hex())
            self._last_led_ping = now

    def handle_config_request(self, msg):
        assert len(msg) == 0x06
        subtype = msg[1]
        keypad = msg[3]
        print(f"subtype={subtype:#04x} "
              f"keypad={keypad}")

    def handle_config_response(self, msg):
        assert len(msg) == 0x0E
        keypad = msg[3]
        led_off_brightness = msg[4]
        double_tap_time = msg[5]
        hold_time = msg[6]
        local_ack_time = msg[7]
        flash1_rate = msg[8]
        flash2_rate = msg[9]
        background_brightness = msg[10]
        special = msg[11]
        print(f"keypad={keypad} "
            f"led_off_brightness={led_off_brightness:#04x} "
            f"double_tap_time={double_tap_time:#04x} "
            f"hold_time={hold_time:#04x} "
            f"local_ack_time={local_ack_time:#04x} "
            f"flash1_rate={flash1_rate:#04x} "
            f"flash2_rate={flash2_rate:#04x} "
            f"background_brightness={background_brightness:#04x} "
            f"special={special:#04x}")

    def handle_config2_response(self, msg):
        assert len(msg) == 0x0E
        keypad = msg[3]
        bg_right = msg[4]
        bg_left = msg[5]
        bg_unused = msg[6]
        active_right = msg[7]
        active_left = msg[8]
        active_unused = msg[9]
        background = msg[10]
        print(f"keypad={keypad} "
              f"bg={bg_unused:#04x},{bg_left:#04x},{bg_right:#04x} "
              f"active={active_unused:#04x},{active_left:#04x},{active_right:#04x} "
              f"background={background:#04x}")

    def handle_button(self, msg):
        assert len(msg) == 0x09
        event_type = msg[1]
        keypad = msg[3]
        button = msg[4]
        print(f"event_type={event_type} "
              f"keypad={keypad} "
              f"button={button}")

    def handle_led_status(self, msg):
        assert len(msg) == 0x1E
        subtype = msg[3]
        payload = msg[4:-2]
        print(f"subtype={subtype:#04x} "
              f"payload={payload.hex(" ")}")

    def handle_flash_response(self, msg):
        assert len(msg) == 0x0E
        keypad = msg[3]
        print(f"keypad={keypad}")

    def handle_cco_pulse(self, msg):
        assert len(msg) == 0x0E
        device = msg[3]
        relay = msg[4]
        pulse_time = msg[5]
        print(f"device={device} "
              f"relay={relay} "
              f"pulse_time={pulse_time}")

    def handle_unknown(self, msg):
        subtype = msg[1]
        print(f"subtype={subtype} "
              f"payload={msg[2:-2].hex(" ")}")

    def handle_message(self, msg):
        self._logger.info("received message: %s", msg.hex())

        if len(msg) < 3:
            self._logger.warning("packet too short: %s", msg.hex())
            return
        if msg[0] != PACKET_START:
            self._logger.warning("invalid start sequence: %s", msg.hex())
            return
        if len(msg) != msg[2]:
            self._logger.warning("length doesn't match data chunk: %s", msg.hex())
            return
        if sum(msg) & 0xFF:
            self._logger.warning("bad checksum: %s", msg.hex())
            return

        match msg[1]:
            case 0x01 | 0x02 | 0x03 | 0x04:
                handler = self.handle_button
            case 0x06 | 0x0A:
                handler = self.handle_config_request
            case 0x80:
                handler = self.handle_led_status
            case 0x89:
                handler = self.handle_flash_response
            case 0x8E:
                handler = self.handle_config_response
            case 0x9A:
                handler = self.handle_config2_response
            case 0x8A:
                handler = self.handle_cco_pulse
            case _:
                handler = self.handle_unknown

        handler(msg)

if __name__ == "__main__":
    raise SystemExit(OpenHWI().go())
