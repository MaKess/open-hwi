import json
import logging
from dataclasses import dataclass
from typing import Literal
from queue import Queue
import threading
import paho.mqtt.client as mqtt
from bases import OutputMonitor
from outputs import Output
from scheduler import Scheduler

# apt install python3-paho-mqtt

@dataclass(frozen=True)
class _OutputRegistration:
    output: Output
    line: int
    output_index: int
    channel_index: int
    entity_kind: Literal["light", "switch"]
    state_topic: str
    command_topic: str
    brightness_state_topic: str | None
    brightness_command_topic: str | None


@dataclass(frozen=True)
class _ButtonRegistration:
    line: int
    keypad_index: int
    button_index: int
    command_topic: str
    event_topic: str


@dataclass(frozen=True)
class _LedRegistration:
    line: int
    keypad_index: int
    button_index: int
    state_topic: str


@dataclass(frozen=True)
class ButtenPressEvent:
    line: int
    keypad_index: int
    button_index: int
    event: Literal["press"]


class HomeAssistantMQTTBridge(OutputMonitor):
    def __init__(
        self,
        receive_queue: Queue[ButtenPressEvent],
        event: threading.Event,
        scheduler: Scheduler,
        client_id: str = "openhwi-host",
        discovery_prefix: str = "homeassistant",
        base_topic: str = "openhwi",
    ) -> None:
        self._receive_queue = receive_queue
        self._main_loop_event = event
        self._scheduler = scheduler
        self._logger = logging.getLogger(self.__class__.__name__)
        self._discovery_prefix = discovery_prefix.rstrip("/")
        self._base_topic = base_topic.rstrip("/")
        self._availability_topic = f"{self._base_topic}/bridge/availability"
        self._output_command_subscription = f"{self._base_topic}/outputs/+/set"
        self._output_brightness_command_subscription = f"{self._base_topic}/outputs/+/brightness/set"
        self._button_command_subscription = f"{self._base_topic}/keypads/+/buttons/+/set"

        self._outputs: dict[tuple[int, int, int], _OutputRegistration] = {}
        self._buttons: dict[tuple[int, int, int], _ButtonRegistration] = {}
        self._leds: dict[tuple[int, int, int], _LedRegistration] = {}

        self._client = mqtt.Client(client_id=client_id)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.will_set(self._availability_topic, payload="offline", qos=1, retain=True)

    def connect(
        self,
        host: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        keepalive: int = 60,
    ) -> None:
        assert not self._client.is_connected()
        if username is not None:
            self._client.username_pw_set(username=username, password=password)

        self._client.connect(host=host, port=port, keepalive=keepalive)
        self._client.loop_start()

    def register_output(
        self,
        output: Output,
        entity_kind: Literal["light", "switch"],
        channel_name: str | None = None,
        supports_brightness: bool = False,
        suggested_area: str | None = None,
    ) -> None:
        if entity_kind not in ("light", "switch"):
            raise ValueError("entity_kind must be 'light' or 'switch'")

        line, output_index, channel_index = output.get_address()
        safe_output_id = f"{line}_{output_index}_{channel_index}"
        topic_base = f"{self._base_topic}/outputs/{safe_output_id}"
        state_topic = f"{topic_base}/state"
        command_topic = f"{topic_base}/set"
        brightness_state_topic = None
        brightness_command_topic = None

        payload: dict[str, str | int] = {
            "dev": {  # device
                "ids": f"openhwi_device_{safe_output_id}",  # identifiers
                "mf": "Lutron/OpenHWI",  # manufacturer
                "mdl": "Homeworks Illumination",  # model
                "via_device": "openhwi_host",
            },
            "o": {  # origin
                "name": "OpenHWI",
            },
            "~": topic_base,  # topic base prefix
            "uniq_id": f"openhwi_output_{safe_output_id}",  # unique_id
            "stat_t": "~/state",  # state_topic
            "cmd_t": "~/set",  # command_topic
            "avty_t": self._availability_topic,  # availability_topic
            "pl_on": "ON",  # payload_on
            "pl_off": "OFF",  # payload_off
        }
        if suggested_area:
            payload["sa"] = suggested_area  # suggested_area
        if channel_name:
            payload["name"] = channel_name

        if entity_kind == "light" and supports_brightness:
            brightness_state_topic = f"{topic_base}/brightness/state"
            brightness_command_topic = f"{topic_base}/brightness/set"
            payload["bri_stat_t"] = "~/brightness/state"  # brightness_state_topic
            payload["bri_cmd_t"] = "~/brightness/set"  # brightness_command_topic
            payload["bri_scl"] = 100  # brightness_scale

        self._publish_discovery(
            component=entity_kind,
            object_id=f"output_{safe_output_id}",
            payload=payload,
        )

        self._outputs[line, output_index, channel_index] = _OutputRegistration(
            output=output,
            line=line,
            output_index=output_index,
            channel_index=channel_index,
            entity_kind=entity_kind,
            state_topic=state_topic,
            command_topic=command_topic,
            brightness_state_topic=brightness_state_topic,
            brightness_command_topic=brightness_command_topic,
        )

    def register_keypad(
        self,
        line: int,
        keypad_index: int,
        button_indices: list[int],
        keypad_name: str | None = None,
        button_names: dict[int, str] | None = None,
        suggested_area: str | None = None,
    ) -> None:
        safe_keypad_id = f"{line}_{keypad_index}"
        components_payload: dict[str, dict[str, str]] = {}
        topic_base = f"{self._base_topic}/keypads/{safe_keypad_id}"

        for button_index in button_indices:
            command_topic = f"{topic_base}/buttons/{button_index}/set"
            event_topic = f"{topic_base}/buttons/{button_index}/event"
            led_state_topic = f"{topic_base}/leds/{button_index}/state"

            # Button for incoming events (HomeAssitant pressend -> send to OpenHWI)
            components_payload[f"button_cmd_{button_index}"] = entity_payload = {
                "p": "button",  # platform
                "uniq_id": f"openhwi_keypad_{safe_keypad_id}_button_{button_index}_cmd",  # unique_id
                "cmd_t": f"~/buttons/{button_index}/set",  # command_topic
            }
            if suggested_area:
                entity_payload["sa"] = suggested_area  # suggested_area
            if button_names and (button_name := button_names.get(button_index)):
                entity_payload["name"] = button_name

            # Button/Automation for outgoing events (OpenHWI pressed -> send to HomeAssistant)
            components_payload[f"button_trig_{button_index}"] = entity_payload = {
                "p": "device_automation",  # platform
                "atype": "trigger",  # automation_type
                "type": "button_short_press",
                "stype": f"button_{button_index}",  # subtype
                "t": f"~/buttons/{button_index}/event",  # topic
                "pl": "PRESS",  # payload
            }

            self._buttons[line, keypad_index, button_index] = _ButtonRegistration(
                line=line,
                keypad_index=keypad_index,
                button_index=button_index,
                command_topic=command_topic,
                event_topic=event_topic,
            )


            # LED state (OpenHWI informs HomeAssistant about current state of LED)
            components_payload[f"led_{button_index}"] = entity_payload = {
                "p": "binary_sensor",  # platform
                "uniq_id": f"openhwi_keypad_{safe_keypad_id}_led_{button_index}",  # unique_id
                "stat_t": f"~/leds/{button_index}/state",  # state_topic
                "pl_on": "ON",  # payload_on
                "pl_off": "OFF",  # payload_off
            }
            if suggested_area:
                entity_payload["sa"] = suggested_area  # suggested_area
            if button_names and (button_name := button_names.get(button_index)):
                entity_payload["name"] = button_name

            self._leds[line, keypad_index, button_index] = _LedRegistration(
                line=line,
                keypad_index=keypad_index,
                button_index=button_index,
                state_topic=led_state_topic,
            )

        keypad_discovery_payload: dict[str, str] = {
            "~": topic_base,
            "dev": {  # device
                "ids": f"openhwi_keypad_{safe_keypad_id}",  # identifiers
                "name": keypad_name,
                "mf": "Lutron/OpenHWI",  # manufacturer
                "mdl": "Homeworks Illumination Keypad",  # model
                "via_device": "openhwi_host",
            },
            "o": {  # origin
                "name": "OpenHWI",
            },
            "avty_t": self._availability_topic,  # availability_topic
            "cmps": components_payload,  # components
        }

        self._publish_discovery(
            component="device",
            object_id=f"keypad_{safe_keypad_id}",
            payload=keypad_discovery_payload,
        )

    def output_update(self, output: Output):
        """
        implementation of the "output_update" method mandated by the abstract "OutputMonitor" base class.
        it will be called whenever an output changes its value.
        # publish_output_state
        """

        try:
            registration = self._outputs[output.get_address()]
        except KeyError:
            return

        value = output.get_value()

        self._publish(registration.state_topic, "ON" if value else "OFF", retain=True)
        if registration.brightness_state_topic is not None:
            # dimmable outputs represent the brightness as a float between 0.0 and 1.0,
            # but for HomeAssistant we want to have an int between 0 and 100
            scaled = int(value * 100)
            clamped = max(0, min(100, scaled))
            self._publish(registration.brightness_state_topic, str(clamped), retain=True)

    def publish_keypad_button_event(self, line: int, keypad_index: int, button_index: int) -> None:
        self._publish(self._buttons[line, keypad_index, button_index].event_topic,
                      "PRESS",
                      retain=False)

    def publish_keypad_led_state(self, line: int, keypad_index: int, button_index: int, is_on: bool) -> None:
        self._publish(self._leds[line, keypad_index, button_index].state_topic,
                      "ON" if is_on else "OFF",
                      retain=True)

    def handle_output_command(self, line: int, output_index: int, channel_index: int, command: str, payload: str) -> None:
        self._logger.info(
            "handle output command line=%d output_index=%d channel_index=%d command=%s payload=%s",
            line,
            output_index,
            channel_index,
            command,
            payload,
        )
        try:
            registration = self._outputs[line, output_index, channel_index]
        except KeyError:
            return

        match (command, payload):
            case ("state", "OFF"):
                value = 0
            case ("state", "ON"):
                value = 1
            case ("brightness", brightness_payload):
                try:
                    value = float(brightness_payload) / 100.0
                except ValueError as ex:
                    self._logger.error(f"received invalid brightness value {brightness_payload} for output {registration.output.get_address()}")
                    return
            case _:
                self._logger.error(f"received unknown output command/ payload ({command} / {payload}) for output {registration.output.get_address()}")
                return

        self._scheduler.add_event(
            output=registration.output,
            target_value=value,
        )

    def handle_keypad_button_command(
        self,
        line: int,
        keypad_index: int,
        button_index: int,
        payload: str,
    ) -> None:
        self._logger.info(
            "keypad button press line=%s keypad_index=%s button_index=%s payload=%s",
            line,
            keypad_index,
            button_index,
            payload,
        )
        self._receive_queue.put(ButtenPressEvent(
            line,
            keypad_index,
            button_index,
            "press",
        ))
        self._main_loop_event.set()

    def close(self) -> None:
        self._publish(self._availability_topic, "offline", retain=True)
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._logger.info("connected to MQTT broker: reason_code=%s", reason_code)
        self._publish(self._availability_topic, "online", retain=True)
        client.subscribe(self._output_command_subscription, qos=1)
        client.subscribe(self._output_brightness_command_subscription, qos=1)
        client.subscribe(self._button_command_subscription, qos=1)

    def _on_message(self, client, userdata, msg) -> None:
        topic = msg.topic
        payload = msg.payload.decode("utf-8").strip()

        def parse_id(id_value: str, expected_tokens: int) -> tuple[int, ...] | None:
            id_tokens = id_value.split("_")
            if len(id_tokens) != expected_tokens:
                self._logger.warning("ignoring command with invalid id topic=%s payload=%s", topic, payload)
                return None
            try:
                return tuple(int(v) for v in id_tokens)
            except ValueError:
                self._logger.warning("ignoring command with non-numeric id topic=%s payload=%s", topic, payload)
                return None

        topic_prefix = f"{self._base_topic}/"
        if not topic.startswith(topic_prefix):
            self._logger.warning("ignoring MQTT message outside base topic topic=%s payload=%s", topic, payload)
            return

        parts = topic[len(topic_prefix):].split("/")
        match parts:
            case ["outputs", output_id, "set"]:
                output_key = parse_id(output_id, expected_tokens=3)
                if output_key is None:
                    return

                registration = self._outputs.get(output_key)
                if registration is None:
                    self._logger.warning("ignoring command for unknown output topic=%s payload=%s", topic, payload)
                    return
                self.handle_output_command(
                    registration.line,
                    registration.output_index,
                    registration.channel_index,
                    "state",
                    payload)

            case ["outputs", output_id, "brightness", "set"]:
                output_key = parse_id(output_id, expected_tokens=3)
                if output_key is None:
                    return

                registration = self._outputs.get(output_key)
                if registration is None:
                    self._logger.warning("ignoring brightness command for unknown output topic=%s payload=%s", topic, payload)
                    return
                self.handle_output_command(
                    registration.line,
                    registration.output_index,
                    registration.channel_index,
                    "brightness",
                    payload)

            case ["keypads", keypad_id, "buttons", button_index_raw, "set"]:
                keypad_key = parse_id(keypad_id, expected_tokens=2)
                if keypad_key is None:
                    return

                try:
                    button_index = int(button_index_raw)
                except ValueError:
                    self._logger.warning("ignoring command with non-numeric keypad/button id topic=%s payload=%s", topic, payload)
                    return

                line, keypad_index = keypad_key
                registration = self._buttons.get((line, keypad_index, button_index))
                if registration is None:
                    self._logger.warning("ignoring command for unknown button topic=%s payload=%s", topic, payload)
                    return

                self.handle_keypad_button_command(
                    registration.line,
                    registration.keypad_index,
                    registration.button_index,
                    payload,
                )

            case _:
                self._logger.warning("ignoring MQTT message on unhandled topic=%s payload=%s", topic, payload)

    def _publish(self, topic: str, payload: str, retain: bool) -> None:
        self._client.publish(topic=topic, payload=payload, qos=1, retain=retain)

    def _publish_discovery(self, component: str, object_id: str, payload: dict[str, str | int]) -> None:
        topic = f"{self._discovery_prefix}/{component}/openhwi/{object_id}/config"
        self._publish(topic=topic, payload=json.dumps(payload), retain=True)
