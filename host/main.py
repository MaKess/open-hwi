#!/usr/bin/env python3
import argparse
import threading
import time
import struct
import itertools
import queue
import logging
import serial
from typing import Any

from .scheduler import Action, Scheduler, ToggleButton, Output, RPMOutput
from . import web

ESCAPE_BYTE = 0xFF
ACK_BYTE = 0xFE
GAP_BYTE = 0xFD

PACKET_START = 0xB0

MESSAGE_LED_MASK = 0x80
MESSAGE_CONFIG_RESPONSE = 0x8E
MESSAGE_CONFIG_RESPONSE2 = 0x9A

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


class WebThread(threading.Thread):
    def __init__(self):
        super().__init__(
            name="Web-Server",
            daemon=True
        )
        self._logger = logging.getLogger(self.name)

    def run(self):
        web.serve(
            name=self.name,
            logger=self._logger
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
                            msg = bytes(buffer)
                            self._logger.debug("received chunk %s", msg.hex(" "))
                            self._receive_queue.put(msg)
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
            self._logger.debug("sending message %s", msg.hex(" "))
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

    def make_message(self, index: int):
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

    def make_next_message(self):
        return self.make_message(next(self.cycle))

    def make_message_for_keypad(self, keypad):
        return self.make_message(keypad // LEDstate.KEYPAD_PER_MESSAGE)


class ConfigProvider:
    def __init__(self, scheduler: Scheduler, led_state: LEDstate, rpm_channels: list[list["RPMChannel"]]):
        self._action_map: dict[tuple[tuple[int,...], str], Action] = {}

        # >>> TODO: this should come from a config file

        # TODO: only set this for outputs that are really configured. ([5, 0, 0, 1] is already the default config)
        for module in rpm_channels:
            for channel in module:
                channel.config = [5, 0, 0, 1]

        outputs: dict[tuple[int, ...], Output] = {
            (0, 0): RPMOutput(rpm_channels[0][0])
        }

        self._action_map[((21, 1), "press")] = ToggleButton(keypad_number=21,
                                                            button_number=1,
                                                            led_state=led_state,
                                                            scheduler=scheduler) \
                                                .add_output(output=outputs[0, 0],
                                                            on_value=1.0)
        # <<< TODO

        self._logger = logging.getLogger(self.__class__.__name__)

    def make_message(self, keypad: int, auxiliary: bool = False):
        # >>> TODO: these values should come from a config file
        led_off_brightness = 0x0c # Nightlight
        double_tap_time = 0x1e # Fast (1/2 s)
        hold_time = 0x46 # Medium (0.5 s)
        local_ack_time = 0x38 # TODO!
        local_ack_time = 0 # TODO!
        flash1_rate = 0x0f # Extra fast
        flash2_rate = 0x46 # Medium Slow
        background_brightness = 0x1c # Medium

        bg_right = 0x0a
        bg_left = 0x0a
        bg_unused = 0x00
        active_right = 0x0a
        active_left = 0x0a
        active_unused = 0x00
        special = 0x12 # TODO!
        # <<< TODO

        if not auxiliary:
            return pack_message(
                MESSAGE_CONFIG_RESPONSE,
                bytes([
                    keypad,
                    led_off_brightness,
                    double_tap_time,
                    hold_time,
                    local_ack_time,
                    flash1_rate,
                    flash2_rate,
                    background_brightness,
                    bg_right
                ])
            )
        else:
            return pack_message(
                MESSAGE_CONFIG_RESPONSE2,
                bytes([
                    keypad,
                    bg_right,
                    bg_left,
                    bg_unused,
                    active_right,
                    active_left,
                    active_unused,
                    background_brightness,
                    special
                ])
            )

    def action(self,
               action_type,
               **attributes):
        match action_type:
            case "button":
                keypad = attributes.get("keypad")
                button = attributes.get("button")
                event = attributes.get("event")

                # TODO: use something safer than "assert"
                assert isinstance(keypad, int)
                assert isinstance(button, int)
                assert isinstance(event, str)

                address = (keypad, button)
                action_key = (address, event)
                action = self._action_map.get(action_key)

                if action is not None:
                    self._logger.info("button %s, event %s triggered", address, event)
                    action.action_trigger()
                else:
                    self._logger.warning("button %s, event %s is unhandled", address, event)

            case _:
                self._logger.warning("action type %s is unknown", action_type)

class RPMChannel:
    def __init__(self, line, module, channel):
        self.line = line
        self.module = module
        self.channel = channel
        self.level = 0
        self.config = [5, 0, 0, 1]


class RPMThread(threading.Thread):
    DATA_INTERVAL = 0.1
    BEAT_INTERVAL = 1.2

    def __init__(self, serial_port, devices):
        super().__init__(
            name="RPM",
            daemon=True
        )
        self._serial_port = serial_port
        self._devices = devices
        self._logger = logging.getLogger(self.name)

    def run(self):
        cycle = itertools.cycle(range(4))
        now = time.time()
        next_loop = now + self.DATA_INTERVAL
        next_beat = now + self.BEAT_INTERVAL
        while True:
            now = time.time()
            i = next(cycle)
            beat = i == 3 and now >= next_beat
            if beat:
                next_beat += self.BEAT_INTERVAL
            self.loop(i, beat)
            sleep_for = next_loop - now
            next_loop += self.DATA_INTERVAL
            self._logger.debug("sleeping: now=%.3f, next_loop=%.3f, sleep_for=%.3f", now, next_loop, sleep_for)
            time.sleep(sleep_for)

    def loop(self, index, hearbeat):
        data = [0x81 + index + (hearbeat << 6)]
        for m in range(6):
            module = self._devices[m]
            for c in range(4):
                device = module[c]
                data.append(device.level)
                data.append(device.config[index])
        data.append(sum(data) & 0x7f)
        for m in range(6,8):
            module = self._devices[m]
            for c in range(4):
                device = module[c]
                data.append(device.level)
                data.append(device.config[index])
        data.append(sum(data[-(2*2*4+1):]) & 0x7f)

        msg = bytes(data)

        self._logger.debug("data to send: %s", msg.hex(" "))
        self._serial_port.write(msg)


class OpenHWI:
    @staticmethod
    def get_args():
        parser = argparse.ArgumentParser(description="OpenHWI")
        parser.add_argument("--port-keypad", required=True, help="serial port (e.g. /dev/ttyACM0)")
        parser.add_argument("--port-rpm", required=True, help="serial port (e.g. /dev/ttyACM1)")
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

        self._logger = logging.getLogger(self.__class__.__name__)

        # open serial connections
        try:
            serial_keypad = serial.Serial(port=args.port_keypad)
            serial_rpm = serial.Serial(port=args.port_rpm)
        except serial.SerialException as exc:
            raise SystemExit(f"Failed to open serial connection: {exc}") from exc

        self._logger.info("Serial port %s opened for keypad", args.port_keypad)
        self._logger.info("Serial port %s opened for RPM", args.port_rpm)

        self._led_state = LEDstate()

        self._scheduler = Scheduler()

        self._last_keypad_rand = {}

        # TODO: we should be able to handle multiple RPM lines
        self._rpm_channels = [
            [
                RPMChannel(line=0, module=m, channel=c) for c in range(4)
            ] for m in range(8)
        ]
        self._config = ConfigProvider(scheduler=self._scheduler,
                                      led_state=self._led_state,
                                      rpm_channels=self._rpm_channels)

        self._main_loop_event = main_loop_event = threading.Event()
        ack_event = threading.Event()
        self._send_queue = send_queue = queue.Queue()
        self._receive_queue = receive_queue = queue.Queue()

        self._bus_link_sender = BusLinkSenderThread(serial_keypad, send_queue, ack_event)
        self._bus_link_receiver = BusLinkReceiverThread(serial_keypad, main_loop_event, receive_queue, ack_event)
        self._led_ping = LEDpingThread(args.led_interval, main_loop_event)

        self._rpm = RPMThread(serial_rpm, self._rpm_channels)

        self._web_server = WebThread()

    def go(self):
        self._bus_link_sender.start()
        self._bus_link_receiver.start()
        self._led_ping.start()
        self._web_server.start()
        self._rpm.start()
        self._last_led_ping = 0
        try:
            while True:
                self._main_loop_event.wait()
                self.loop()
                self._main_loop_event.clear()
        except KeyboardInterrupt:
            self._logger.info("stopped through keyboard interrupt")

        return 0

    def send_led_update(self, keypad: int|None = None):
        if keypad is None:
            msg = self._led_state.make_next_message()
        else:
            msg = self._led_state.make_message_for_keypad(keypad)
        self._logger.debug("send LED update: %s", msg.hex(" "))
        self._send_queue.put(msg)
        self._last_led_ping = time.time()

    def send_ack(self):
        self._send_queue.put(b"\0")

    def loop(self):
        try:
            self.handle_message(self._receive_queue.get(block=False))
        except queue.Empty:
            pass

        if time.time() - self._last_led_ping >= self._led_interval * 0.99:
            self.send_led_update()

    def handle_ack(self):
        # TODO: integrate this protocol-level ACK into the flow
        pass

    def handle_config_request(self, msg):
        assert len(msg) == 0x06
        subtype = msg[1]
        keypad = msg[3]
        self._logger.info(
            "handle packet: config request: subtype=0x%02x keypad=%d",
            subtype,
            keypad
        )
        match subtype:
            case 0x06:
                aux = False
                subtype_str = "basic"
            case 0x0a:
                aux = True
                subtype_str = "auxiliary"
            case _:
                aux = None
                subtype_str = None
                self._logger.warning("unknown config request")
                return

        msg = self._config.make_message(keypad, aux)
        self._logger.info("send %s config: %s", subtype_str, msg.hex(" "))
        self._send_queue.put(msg)

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

        self._logger.info(
            "handle packet: config response: "
            f"keypad={keypad} "
            f"led_off_brightness={led_off_brightness:#04x} "
            f"double_tap_time={double_tap_time:#04x} "
            f"hold_time={hold_time:#04x} "
            f"local_ack_time={local_ack_time:#04x} "
            f"flash1_rate={flash1_rate:#04x} "
            f"flash2_rate={flash2_rate:#04x} "
            f"background_brightness={background_brightness:#04x} "
            f"special={special:#04x}"
        )

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
        self._logger.info(
            "handle packet: config response-2: "
            f"keypad={keypad} "
            f"bg={bg_unused:#04x},{bg_left:#04x},{bg_right:#04x} "
            f"active={active_unused:#04x},{active_left:#04x},{active_right:#04x} "
            f"background={background:#04x}"
        )

    def handle_button(self, msg):
        assert len(msg) == 0x09
        event_type = msg[1]
        keypad = msg[3]
        button = msg[4]
        rand = msg[5:7]

        rand_key = (keypad, button, event_type)
        last_keypad_rand = self._last_keypad_rand.get(rand_key)
        if last_keypad_rand == rand:
            self._logger.info("skipping duplicate button event")
            return
        else:
            self._last_keypad_rand[rand_key] = rand

        match event_type:
            case 0x01:
                event_type_str = "press"
            case 0x02:
                event_type_str = "release"
            case 0x03:
                event_type_str = "double-tap"
            case 0x04:
                event_type_str = "hold"
            case _:
                self._logger.warning("unknown butten event")
                event_type_str = None

        self._logger.info(
            "handle packet: button press: keypad=%d button=%d event_type=%s (%d)",
            keypad,
            button,
            event_type_str, event_type
        )

        # TODO: should we integrate this check somewhere?
        # if keypad >= self._led_state.KEYPAD_PER_BUS or button >= self._led_state.LED_PER_KEYPAD:
        #     self._logger.warning("out of bounds keypad/button")

        self._config.action(action_type="button",
                            keypad=keypad,
                            button=button,
                            event=event_type_str)
        self.send_ack()

    def handle_led_status(self, msg):
        assert len(msg) == 0x1E
        subtype = msg[3]
        payload = msg[4:-2]
        self._logger.info(
            "handle packet: LED status: subtype=0x%02x payload=%s",
            subtype,
            payload.hex(" ")
        )

    def handle_flash_response(self, msg):
        assert len(msg) == 0x0E
        keypad = msg[3]
        self._logger.info(
            "handle packet: flash response: keypad=%d",
            keypad
        )

    def handle_cco_pulse(self, msg):
        assert len(msg) == 0x0E
        device = msg[3]
        relay = msg[4]
        pulse_time = msg[5]
        self._logger.info(
            "handle packet: CCO pulse: device=%d relay=%d pulse_time=%d",
            device,
            relay,
            pulse_time
        )

    def handle_unknown(self, msg):
        subtype = msg[1]
        self._logger.warning(
            "handle packet: unknown subtype=0x%02x payload=%s",
            subtype,
            msg[2:-2].hex(" ")
        )

    def handle_message(self, msg):
        self._logger.info("received message: %s", msg.hex(" "))

        if msg == b"\0":
            return self.handle_ack()

        if len(msg) < 3:
            self._logger.warning("packet too short: %s", msg.hex(" "))
            return
        if msg[0] != PACKET_START:
            self._logger.warning("invalid start sequence: %s", msg.hex(" "))
            return
        if len(msg) != msg[2]:
            self._logger.warning("length doesn't match data chunk: %s", msg.hex(" "))
            return
        if sum(msg) & 0xFF:
            self._logger.warning("bad checksum: %s", msg.hex(" "))
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

        return handler(msg)

if __name__ == "__main__":
    raise SystemExit(OpenHWI().go())
