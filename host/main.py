#!/usr/bin/env python3
import argparse
import threading
import time
import struct
import itertools
import queue
import logging
import pathlib
import json

import serial
# apt install python3-serial

from bases import LED, RPMChannel, Action
from scheduler import Scheduler
from buttons import BasicButton
from outputs import Output
from outputs.rpm_outputs import DimmedRPMOutput

import web
from mqtt import HomeAssistantMQTTBridge, ButtenPressEvent

ESCAPE_BYTE = 0xFF
ACK_BYTE = 0xFE
GAP_BYTE = 0xFD

PACKET_START = 0xB0

MESSAGE_LED_MASK = 0x80
MESSAGE_CONFIG_RESPONSE = 0x8E
MESSAGE_CONFIG_RESPONSE2 = 0x9A

def pack_message(message_type: int, payload: bytes) -> bytes:
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
    def __init__(self, receive_queue: queue.Queue):
        super().__init__(
            name=self.__class__.__name__,
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
            line: int,
            main_event: threading.Event,
            receive_queue: queue.Queue,
            ack_event: threading.Event
        ):
        super().__init__(
            name=self.__class__.__name__,
            daemon=True
        )
        self._serial_port = serial_port
        self._line = line
        self._main_event = main_event
        self._bus_link_receive_queue = receive_queue
        self._ack_event = ack_event
        self._logger = logging.getLogger(self.name)

    def run(self):
        self._logger.info("running")
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
                            self._bus_link_receive_queue.put((self._line, msg))
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
            name=self.__class__.__name__,
            daemon=True
        )
        self._serial_port = serial_port
        self._send_queue = send_queue
        self._ack_event = ack_event
        self._logger = logging.getLogger(self.name)

    def run(self):
        self._logger.info("running")
        while True:
            msg = self._send_queue.get()
            self._logger.debug("sending message %s", msg.hex(" "))
            assert msg[0] in (0x00, 0xb0)
            self._ack_event.clear()
            self._serial_port.write(msg)
            self._serial_port.flush()
            self._ack_event.wait(self.ACK_TIMEOUT)
            self._send_queue.task_done()


class LEDpingThread(threading.Thread):
    """
    Event generator to periodically unblock the main event loop to send LED update packets on all
    serial ports connected to keypad links.
    """
    def __init__(self, interval: float, event: threading.Event):
        super().__init__(
            name=self.__class__.__name__,
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

    def __init__(self, line: int, leds: dict[tuple[int, int, int], LED]):
        self._line = line
        self._leds = leds
        self._cycle = itertools.cycle(range(self.KEYPAD_PER_BUS // self.KEYPAD_PER_MESSAGE))

    def make_message(self, index: int):
        start = index * self.KEYPAD_PER_MESSAGE
        msg = [0xc0 + index]
        for keypad in range(start, start + self.KEYPAD_PER_MESSAGE):
            for b1 in range(6): # 6 bytes per keypad = LED_PER_KEYPAD * BITS_PER_LED / BITS_PER_BYTE
                msg.append(sum(
                    self._leds[
                        self._line,
                        keypad,
                        b1 * 4 + b2 # led
                    ].level << (b2 * 2) # 2 = BITS_PER_LED
                    for b2 in range(4) # 4 leds per byte = BITS_PER_BYTE / BITS_PER_LED
                ))

        return pack_message(MESSAGE_LED_MASK, bytes(msg))

    def make_next_message(self):
        return self.make_message(next(self._cycle))

    def make_message_for_keypad(self, keypad: int):
        return self.make_message(keypad // self.KEYPAD_PER_MESSAGE)

from typing import TypeVar

T = TypeVar('T')
def get_val[T](d: dict, k: str, t: type[T], default:bool=False) -> T:
    v = d.get(k)
    if v is None and default:
        return t()
    elif isinstance(v, t):
        return v
    else:
        raise TypeError(f"{k} should be of type {t}, but is {type(v)}")


class ConfigStack:
    def __init__(self, global_config, device_class, device_type, device_config):
        self._stack = [
            get_val(global_config, device_class, dict, True),
            get_val(global_config, device_type, dict, True),
            device_config,
        ]

    def push(self, config):
        self._stack.append(config)

    def pop(self):
        return self._stack.pop()

    def get(self, key, default=None):
        value = default
        for config in self._stack:
            value = config.get(key, value)
        return value

class ConfigProvider:
    def __init__(
            self,
            config_file_path: pathlib.Path,
            scheduler: Scheduler,
            leds: dict[tuple[int, int, int], LED],
            rpm_channels: dict[tuple[int, int, int], RPMChannel],
            home_assistant_mqtt_bridge:HomeAssistantMQTTBridge,
        ):
        self._config_file_path = config_file_path
        self._scheduler = scheduler
        self._leds = leds
        self._rpm_channels = rpm_channels
        self._home_assistant_mqtt_bridge = home_assistant_mqtt_bridge
        self._action_map: dict[tuple[tuple[int,...], str], Action] = {}
        self._outputs: dict[tuple[int, ...]|str, Output] = {}
        self._keypad_configs: dict[tuple[int, int, bool], bytes] = {}
        self._logger = logging.getLogger(self.__class__.__name__)

    def _get_output(self, alias: str, address: tuple[int, ...], error_context: str) -> Output:
        output_by_alias = self._outputs.get(alias)
        output_by_address = self._outputs.get(address)

        if output_by_alias is None and output_by_address is None:
            raise ValueError(f"either 'alias' or 'address' should be specified for {error_context}")
        elif output_by_alias is not None and output_by_address is not None and output_by_alias is not output_by_address:
            raise ValueError(f"only one of 'alias' or 'address' should be specified for {error_context}")
        elif output_by_alias is not None:
            return output_by_alias
        elif output_by_address is not None:
            return output_by_address
        else:
            raise RuntimeError("based on the checks above, this should not happen")

    def _parse_keypad(self,
                      global_config: dict,
                      device_config: dict,
                      device_type: str,
                      line: int,
                      address: int,
                      international: bool,
                      button_mask: tuple[int, int, int]) -> None:
        buttons_per_column = 5 if international else 7

        config_stack = ConfigStack(global_config, "keypad", device_type, device_config)

        button_indices: list[int] = []
        buttons_active: list[int] = [0, 0, 0]
        buttons_background_light: list[int] = [0, 0, 0]
        button_names: dict[int, str] = {}
        for button in get_val(device_config, "buttons", list, True):
            button_index = get_val(button, "button", int)
            button_address = (line, address, button_index)

            config_stack.push(button)

            # track which buttons are active and/or have a background light
            if button_index >= 24:
                raise ValueError(f"{button_index} is out of range. maximum 24 buttons per keypad (0 to 23).")
            i, j = divmod(button_index, buttons_per_column)
            j = 1 << j
            if not (button_mask[i] & j):
                raise ValueError(f"button {button_index} should not be configured for keypad type {device_type}")
            button_indices.append(button_index)
            buttons_active[i] |= j
            if config_stack.get("background_light", True) in (True, None):
                buttons_background_light[i] |= j

            button_name = get_val(button, "button_name", str, True)
            if button_name:
                button_names[button_index] = button_name

            # connect the actions to the different events
            for action_name in ("press", "release", "hold", "double-tap"):
                if action := get_val(button, action_name, dict, True):
                    config_stack.push(action)
                    match action_type := get_val(action, "action_type", str):
                        case "basic":
                            self._logger.info("creating basic button for address %s and action %s", button_address, action_name)
                            self._action_map[button_address, action_name] = \
                            basic_button = \
                            BasicButton(led=self._leds[button_address],
                                        scheduler=self._scheduler,
                                        toggle=config_stack.get("toggle", False))
                            for target in get_val(action, "targets", list, True):
                                if not isinstance(target, dict):
                                    raise TypeError

                                output = self._get_output(alias=get_val(target, "alias", str, True),
                                                        address=tuple(get_val(target, "address", list, True)),
                                                        error_context=f"button {button_address} {action_name} target")

                                on_value = target.get("on_value")
                                output.validate_value(value=on_value, allow_none=False)
                                assert isinstance(on_value, (float, int, bool)) # not really necessary, but makes pylance happy

                                off_value = target.get("off_value")
                                output.validate_value(value=off_value, allow_none=True)

                                basic_button.add_output(output=output,
                                                        on_value=on_value,
                                                        off_value=off_value,
                                                        delay_time=config_stack.get("delay_time", 0.0),
                                                        spread_time=config_stack.get("spread_time", 0.0),
                                                        rollback_time=config_stack.get("rollback_time", 0.0))

                        case _:
                            raise ValueError(f"unknown action type '{action_type}' for button {button_address}")

                    config_stack.pop() # action

            config_stack.pop() # button

        self._home_assistant_mqtt_bridge.register_keypad(
            line=line,
            keypad_index=address,
            button_indices=button_indices,
            keypad_name=get_val(device_config, "keypad_name", str, True),
            button_names=button_names,
            suggested_area=get_val(device_config, "suggested_area", str, True),
        )

        # prepare the messages that will be sent to the keypads to configure them
        background_brightness = config_stack.get("background_brightness", 28)

        if international:
            self._keypad_configs[line, address, True] = pack_message(
                MESSAGE_CONFIG_RESPONSE2,
                bytes([
                    address,
                    buttons_background_light[0],
                    buttons_background_light[1],
                    buttons_background_light[2],
                    buttons_active[0],
                    buttons_active[1],
                    buttons_active[2],
                    background_brightness,
                    0x12 # unknown/ special/ fixed value
                ])
            )
            extra_byte = buttons_background_light[0]
        else:
            # for US keypads, this is some sort of LED mask: 1-bits where *no* LED is present
            # * f0 = ST-4S-NI / ST-4SIR-NI
            # * c0 = ST-6BRL-NI
            # * 00 = ST-7B-NI
            extra_byte = 0xff ^ button_mask[0]

        self._keypad_configs[line, address, False] = pack_message(
            MESSAGE_CONFIG_RESPONSE,
            bytes([
                address,
                config_stack.get("led_off_brightness", 12),
                config_stack.get("double_tap_time", 30),
                config_stack.get("hold_time", 70),
                config_stack.get("local_ack_time", 0),
                config_stack.get("flash1_rate", 15),
                config_stack.get("flash2_rate", 70),
                background_brightness,
                extra_byte,
            ])
        )

    def _parse_device(self, global_config: dict, device_config: dict):
        line = get_val(device_config, "line", int)
        address = get_val(device_config, "address", int)
        device_type = get_val(device_config, "device_type", str)

        match device_type:
            case "HW-RPM-4A-230":
                config_stack = ConfigStack(global_config, "rpm", device_type, device_config)
                for channel in get_val(device_config, "channels", list, True):
                    config_stack.push(channel)

                    channel_index = get_val(channel, "channel", int)
                    channel_name = get_val(channel, "channel_name", str, True)
                    channel_alias = get_val(channel, "alias", str, True)
                    channel_address = (line, address, channel_index)
                    match channel_type := config_stack.get("channel_type"):
                        case "inc_dimmed":
                            rpm_channel = self._rpm_channels[channel_address]
                            rpm_channel.config = [0x01, 0x08, 0x12, 0x3d] # DIMMER/INC Dimmed/0W
                            output = DimmedRPMOutput(rpm_channel)
                            self._outputs[channel_address] = output
                            if channel_alias:
                                self._outputs[channel_alias] = output

                            self._home_assistant_mqtt_bridge.register_output(
                                output=output,
                                entity_kind="light",
                                channel_name=channel_name or channel_address,
                                supports_brightness=True,
                                suggested_area=get_val(device_config, "suggested_area", str, True)
                            )
                            output.add_monitor(self._home_assistant_mqtt_bridge)
                        case _:
                            raise ValueError(f"unknown RPM channel type '{channel_type}' for channel {channel_address}")

                    config_stack.pop()

            case "HWIS-4B":
                self._parse_keypad(global_config=global_config,
                                   device_config=device_config,
                                   device_type=device_type,
                                   line=line,
                                   address=address,
                                   international=True,
                                   button_mask=(0, 0xa, 0xa))

            case _:
                raise ValueError(f"unknown device type {device_type} for device {(line, address)}")

    def _setup_mqtt(self, config: dict):
        self._home_assistant_mqtt_bridge.connect(
            host=get_val(config, "host", str),
            port=get_val(config, "port", int),
            username=get_val(config, "username", str),
            password=get_val(config, "password", str),
        )

    def setup(self):
        with self._config_file_path.open("r") as config_file_fd:
            config_data = json.load(config_file_fd)
        if not isinstance(config_data, dict):
            raise TypeError("the JSON configuration should be a dictionary")
        global_config = get_val(config_data, "globals", dict, True)
        devices = get_val(config_data, "devices", list, True)
        for device in devices:
            if not isinstance(device, dict):
                raise TypeError("the device nodes should be dictionaries")
            self._parse_device(global_config, device)

        self._setup_mqtt(get_val(config_data, "mqtt", dict, True))

    def get_keypad_config(self, line: int, keypad: int, auxiliary: bool = False):
        return self._keypad_configs[line, keypad, auxiliary]

    def action(self,
               action_type,
               **attributes):
        match action_type:
            case "button":
                line = attributes.get("line")
                keypad = attributes.get("keypad")
                button = attributes.get("button")
                event = attributes.get("event")

                # TODO: use something safer than "assert"
                assert isinstance(line, int)
                assert isinstance(keypad, int)
                assert isinstance(button, int)
                assert isinstance(event, str)

                address = (line, keypad, button)
                action_key = (address, event)
                action = self._action_map.get(action_key)

                if action is None:
                    self._logger.warning("button %s, event %s is unhandled", address, event)
                    return

                self._logger.info("button %s, event %s triggered", address, event)
                action.action_trigger()

            case _:
                self._logger.warning("action type %s is unknown", action_type)


class RPMThread(threading.Thread):
    def __init__(self, serial_port, line: int, devices: dict[tuple[int, int, int], RPMChannel], scheduler: Scheduler, data_interval: float=0.1):
        super().__init__(
            name=self.__class__.__name__,
            daemon=True
        )
        self._serial_port = serial_port
        self._devices: dict[tuple[int, int, int], RPMChannel] = devices
        self._line = line
        self._scheduler: Scheduler = scheduler
        self._data_interval = data_interval
        self._logger = logging.getLogger(self.name)

    def run(self):
        for i in itertools.cycle(range(12)):
            self._scheduler.tick()
            self.loop(i % 4, i == 0)
            time.sleep(self._data_interval)

    def loop(self, index, hearbeat):
        # the structure of the packet is
        # * start byte which is 0x81 for the first, 0x82 for the second,
        #   0x83 for the third, 0x84 for the fourth packet and then starts over again.
        #   the 0x84 turns into 0xc4 roughly once per second to signal a heartbeat.
        # * then there are two bytes for each of the four channel for the first 6 devices on this line
        #   - the first byte is the output level (0x00 - 0x7f)
        #   - the second byte is one of four config bytes (looping together with the first start byte)
        # * then there is a (check)sum (modulo 0x7f) over the bytes so far
        # * then again two bytes for each channel of the remaining devices 7 and 8 on this line
        # * finally again a checksum. it's a continuation of the previous checksum
        #   (i.e. previous checksum + bytes of remaining devices)
        data = [0x81 + index + (hearbeat << 6)]
        for m in range(6):
            for c in range(4):
                channel_device = self._devices[self._line, m, c]
                data.append(channel_device.level)
                data.append(channel_device.config[index])
        data.append(sum(data) & 0x7f)
        for m in range(6,8):
            for c in range(4):
                channel_device = self._devices[self._line, m, c]
                data.append(channel_device.level)
                data.append(channel_device.config[index])
        data.append(sum(data[-(2*2*4+1):]) & 0x7f)

        msg = bytes(data)

        self._logger.debug("data to send: %s", msg.hex(" "))
        self._serial_port.write(msg)


class OpenHWI:
    @staticmethod
    def get_args():
        parser = argparse.ArgumentParser(description="OpenHWI")
        parser.add_argument("--ports", required=True, nargs="+", help="serial ports (e.g. /dev/ttyACM0)")
        parser.add_argument("--config", type=pathlib.Path, default="config.json")
        parser.add_argument("--led-interval", type=float, default=0.02)
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
        self._scheduler = Scheduler()
        self._main_loop_event = threading.Event()
        self._mqtt_receive_queue: queue.Queue[ButtenPressEvent] = queue.Queue()
        self._home_assistant_mqtt_bridge = HomeAssistantMQTTBridge(
            receive_queue=self._mqtt_receive_queue,
            event=self._main_loop_event,
        )
        self._web_server = WebThread()
        self._leds: dict[tuple[int, int, int], LED] = {}
        self._led_states: dict[int, LEDstate] = {}
        self._rpm_channels: dict[tuple[int, int, int], RPMChannel] = {}
        self._config = ConfigProvider(config_file_path=args.config,
                                      scheduler=self._scheduler,
                                      leds=self._leds,
                                      rpm_channels=self._rpm_channels,
                                      home_assistant_mqtt_bridge=self._home_assistant_mqtt_bridge)
        self._rpms: dict[int, RPMThread] = {}
        self._bus_link_senders: dict[int, BusLinkSenderThread] = {}
        self._bus_link_receivers: dict[int, BusLinkReceiverThread] = {}
        self._bus_link_send_queues: dict[int, queue.Queue] = {}
        self._bus_link_receive_queue = queue.Queue() # only one queue for all receivers! (elements are (line, msg)-tuples)

        self._last_keypad_rand = {}
        self._last_led_ping = 0
        self._led_ping = LEDpingThread(interval=args.led_interval,
                                       event=self._main_loop_event)

        for ident, (mode, name, ser) in self.get_ports(args.ports).items():
            match mode:
                case 1:
                    self._logger.info("use port %s for keypad", name)
                    self._leds.update({
                        (ident, k, b): LED(line=ident, keypad=k, button=b)
                        for k in range(32)
                        for b in range(24)
                    })
                    self._led_states[ident] = LEDstate(line=ident, leds=self._leds)
                    self._bus_link_send_queues[ident] = send_queue = queue.Queue()
                    ack_event = threading.Event()
                    self._bus_link_senders[ident] = BusLinkSenderThread(serial_port=ser,
                                                                        send_queue=send_queue,
                                                                        ack_event=ack_event)
                    self._bus_link_receivers[ident] = BusLinkReceiverThread(serial_port=ser,
                                                                            line=ident,
                                                                            main_event=self._main_loop_event,
                                                                            receive_queue=self._bus_link_receive_queue,
                                                                            ack_event=ack_event)

                case 2:
                    self._logger.info("use port %s for rpm", name)
                    self._rpm_channels.update({
                        (ident, m, c): RPMChannel(line=ident, module=m, channel=c)
                        for m in range(8)
                        for c in range(4)
                    })
                    self._rpms[ident] = RPMThread(serial_port=ser,
                                                  line=ident,
                                                  devices=self._rpm_channels,
                                                  scheduler=self._scheduler)

                case _:
                    self._logger.info("unused port %s due to unknown mode", name)

        self._config.setup()

    def get_ports(self, ports: list[str]) -> dict[int, tuple[int, str, serial.Serial]]:
        ports_classified: dict[int, tuple[int, str, serial.Serial]] = {}

        # open serial connections
        for port in ports:
            try:
                ser = serial.Serial(port=port)
            except serial.SerialException as exc:
                raise SystemExit(f"Failed to open serial connection: {exc}") from exc

            buffer = bytearray()

            # fetch mode
            ser.write(b"\xfa")
            ser.flush()
            while len(buffer) < 3:
                buffer += ser.read(3)
            if buffer[0:2] != b"\xff\xfa":
                self._logger.error("port %s returned an invalid mode", port)
                ser.close()
                continue
            mode = buffer[2]

            buffer.clear()

            ser.write(b"\xf9")
            ser.flush()
            while len(buffer) < 3:
                buffer += ser.read(3)
            if buffer[0:2] != b"\xff\xf9":
                self._logger.error("port %s returned an invalid identifier", port)
                ser.close()
                continue

            ident = buffer[2]

            if ident in ports_classified:
                self._logger.error("duplicate identifier %d for both %s and %s", ident, port, ports[ident][1])
                ser.close()
                continue

            self._logger.info("port %s has mode=%d, identifier=%d", port, mode, ident)
            ports_classified[ident] = (mode, port, ser)

        return ports_classified

    def go(self):
        for rpm in self._rpms.values():
            rpm.start()
        for bus_link_sender in self._bus_link_senders.values():
            bus_link_sender.start()
        for bus_link_receiver in self._bus_link_receivers.values():
            bus_link_receiver.start()
        self._led_ping.start()
        self._web_server.start()
        try:
            while True:
                self._main_loop_event.wait()
                self.loop()
                self._main_loop_event.clear()
        except KeyboardInterrupt:
            self._logger.info("stopped through keyboard interrupt")

        return 0

    def send_led_update(self, line: int, keypad: int|None = None):
        if keypad is None:
            msg = self._led_states[line].make_next_message()
        else:
            msg = self._led_states[line].make_message_for_keypad(keypad)
        self._logger.debug("send LED update: %s", msg.hex(" "))
        self._bus_link_send_queues[line].put(msg)
        self._last_led_ping = time.time()

    def send_ack(self, line: int):
        self._logger.info("sending micro-ack")
        self._bus_link_send_queues[line].put(b"\0")

    def loop(self):
        try:
            line, msg = self._bus_link_receive_queue.get(block=False)
            self.handle_message(
                line=line,
                msg=msg
            )
            self._bus_link_receive_queue.task_done()
        except queue.Empty:
            pass

        try:
            button_press_event = self._mqtt_receive_queue.get(block=False)

            # emit action to Web interface (via SocketIO)
            self._config.action(action_type="button",
                                line=button_press_event.line,
                                keypad=button_press_event.keypad_index,
                                button=button_press_event.button_index,
                                event=button_press_event.event)
            self._scheduler.tick()

            # after having handled the action and scheduled a tick, update the LED next to the button immediately
            self.send_led_update(line=button_press_event.line,
                                 keypad=button_press_event.keypad_index)
            self._mqtt_receive_queue.task_done()
        except queue.Empty:
            pass

        try:
            pass # TODO: receive messages from Web queue
        except queue.Empty:
            pass

        if time.time() - self._last_led_ping >= self._led_interval * 0.99:
            self._scheduler.tick()
            for line in self._led_states:
                self.send_led_update(line=line)

    def handle_ack(self, line: int):
        # TODO: integrate this protocol-level ACK into the flow
        self._logger.info("received micro-ack on line %d", line)

    def handle_config_request(self, line: int, msg: bytes):
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

        msg = self._config.get_keypad_config(line=line, keypad=keypad, auxiliary=aux)
        self._logger.info("send %s config: %s", subtype_str, msg.hex(" "))
        self._bus_link_send_queues[line].put(msg)

    def handle_config_response(self, line: int, msg: bytes):
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
            "line=%d "
            "keypad=%d "
            "led_off_brightness=0x%02x "
            "double_tap_time=0x%02x "
            "hold_time=0x%02x "
            "local_ack_time=0x%02x "
            "flash1_rate=0x%02x "
            "flash2_rate=0x%02x "
            "background_brightness=0x%02x "
            "special=0x%02x",
            line,
            keypad,
            led_off_brightness,
            double_tap_time,
            hold_time,
            local_ack_time,
            flash1_rate,
            flash2_rate,
            background_brightness,
            special
        )

    def handle_config2_response(self, line: int, msg: bytes):
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
            "handle packet: config response-2: line=%d keypad=%d bg=0x%02x,0x%02x,0x%02x active=0x%02x,0x%02x,0x%02x background=0x%02x",
            line,
            keypad,
            bg_unused,
            bg_left,
            bg_right,
            active_unused,
            active_left,
            active_right,
            background
        )

    def handle_button(self, line: int, msg: bytes):
        assert len(msg) == 0x09
        event_type = msg[1]
        keypad = msg[3]
        button = msg[4]
        rand = msg[5:7]

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
                self._logger.warning("unknown butten event on line %d", line)
                event_type_str = None

        self._logger.info(
            "handle packet: button: line=%d keypad=%d button=%d event_type=%s (%d)",
            line,
            keypad,
            button,
            event_type_str, event_type
        )

        self.send_ack(line=line)

        rand_key = (keypad, button, event_type)
        last_keypad_rand = self._last_keypad_rand.get(rand_key)
        if last_keypad_rand == rand:
            self._logger.info("skipping duplicate button event")
            return

        self._last_keypad_rand[rand_key] = rand

        # emit action to all three
        # - self._config
        # - MQTT (thereby to HomeAssistant)
        # - TODO: Web interface (via SocketIO)
        self._config.action(action_type="button",
                            line=line,
                            keypad=keypad,
                            button=button,
                            event=event_type_str)
        self._scheduler.tick()

        # after having handled the action and scheduled a tick, update the LED next to the button immediately
        self.send_led_update(line=line, keypad=keypad)

        self._home_assistant_mqtt_bridge.publish_keypad_button_event(
            line=line,
            keypad_index=keypad,
            button_index=button
        )

    def handle_led_status(self, line: int, msg: bytes):
        assert len(msg) == 0x1E
        subtype = msg[3]
        payload = msg[4:-2]
        self._logger.info(
            "handle packet: LED status: line=%d subtype=0x%02x payload=%s",
            line,
            subtype,
            payload.hex(" ")
        )

    def handle_flash_response(self, line: int, msg: bytes):
        assert len(msg) == 0x0E
        keypad = msg[3]
        self._logger.info(
            "handle packet: flash response: line=%d keypad=%d",
            line,
            keypad
        )

    def handle_cco_pulse(self, line: int, msg: bytes):
        assert len(msg) == 0x0E
        device = msg[3]
        relay = msg[4]
        pulse_time = msg[5]
        self._logger.info(
            "handle packet: CCO pulse: line=%d device=%d relay=%d pulse_time=%d",
            line,
            device,
            relay,
            pulse_time
        )

    def handle_unknown(self, line: int, msg: bytes):
        subtype = msg[1]
        self._logger.warning(
            "handle packet: unknown subtype=0x%02x line=%d payload=%s",
            subtype,
            line,
            msg[2:-2].hex(" ")
        )

    def handle_message(self, line: int, msg: bytes):
        self._logger.info("received message: %s", msg.hex(" "))

        if msg == b"\0":
            return self.handle_ack(line)

        if len(msg) < 3:
            self._logger.warning("packet too short: %s", msg.hex(" "))
            return None
        if msg[0] != PACKET_START:
            self._logger.warning("invalid start sequence: %s", msg.hex(" "))
            return None
        if len(msg) != msg[2]:
            self._logger.warning("length doesn't match data chunk: %s", msg.hex(" "))
            return None
        if sum(msg) & 0xFF:
            self._logger.warning("bad checksum: %s", msg.hex(" "))
            return None

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

        return handler(line, msg)

if __name__ == "__main__":
    raise SystemExit(OpenHWI().go())
