from dataclasses import dataclass
from enum import Enum
import time
import logging

class Action:
    def action_trigger(self):
        raise NotImplementedError


class OutputMonitor:
    def output_update(self) -> None:
        raise NotImplementedError


@dataclass
class OutputItem:
    output: "Output"
    on_value: float | int | bool
    off_value: float | int | bool
    delay_time: float
    spread_time: float | None
    rollback_time: float | None


class ToggleButton(Action, OutputMonitor):
    def __init__(self, keypad_number, button_number, led_state, scheduler) -> None:
        super().__init__()
        self._keypad_number = keypad_number
        self._button_number = button_number
        self._led_state = led_state
        self._scheduler: "Scheduler" = scheduler
        self._items: list[OutputItem] = []

    def add_output(self,
                   output: "Output",
                   on_value: float | int | bool,
                   off_value: float | int | bool | None = None,
                   delay_time: float = 0.0,
                   spread_time: float|None = None,
                   rollback_time: float | None = None) -> "ToggleButton":
        if off_value is None:
            off_value = type(on_value)()
        self._items.append(OutputItem(output, on_value, off_value, delay_time, spread_time, rollback_time))
        return self

    def _is_moving(self):
        return any(item.output.is_moving() for item in self._items)

    def _is_on(self):
        return any(item.output.get_value() for item in self._items)

    def action_trigger(self):
        is_on = self._is_on()
        scheduler = self._scheduler
        for item in self._items:
            scheduler.cancel_events(item.output)
            scheduler.add_event(output=item.output,
                                target_value=item.off_value if is_on else item.on_value,
                                spread_time=item.spread_time,
                                delay_time=item.delay_time)
            if not is_on and item.rollback_time:
                scheduler.add_event(output=item.output,
                                    target_value=item.off_value,
                                    spread_time=item.spread_time,
                                    delay_time=item.rollback_time + item.delay_time)

    def output_update(self) -> None:
        if self._is_moving():
            val = 0b10
        elif self._is_on():
            val = 0b01
        else:
            val = 0b00
        self._led_state.state[self._keypad_number][self._button_number] = val


class Output:
    def __init__(self) -> None:
        self._value = None
        self._moving: bool = False
        self._output_monitors: list[OutputMonitor] = []
        # for motor/shade control this allows single button control with raise-stop-lower-stop
        self._last_nonzero_value: float | int | bool | None = None

    def is_moving(self) -> bool:
        return self._moving

    def get_min_max(self):
        raise NotImplementedError

    def get_value(self):
        return self._value

    def set_value(self, value, moving: bool = False) -> None:
        self._value = value
        self._moving = moving
        if value:
            self._last_nonzero_value = value

    def _update_monitors(self) -> None:
        for output_monitor in self._output_monitors:
            output_monitor.output_update()


class RPMOutput(Output):
    def __init__(self, rpm_channel) -> None:
        super().__init__()
        self._rpm_channel = rpm_channel

class OnOffRPMOutput(RPMOutput):
    def __init__(self, rpm_channel) -> None:
        super().__init__(rpm_channel)
        self._value = bool(rpm_channel.level)

    def get_min_max(self):
        return False, True

    def set_value(self, value: bool, moving = False) -> None:
        if not isinstance(value, (bool, int)):
            raise TypeError

        old_value = self._value

        # sanitize value
        value = bool(value)

        # set value
        super().set_value(value, moving)

        # update RPM channel
        self._rpm_channel.level = int(value * 0x7f)

        # ping monitors if the value has changed
        if value != old_value:
            self._update_monitors()

class DimmedRPMOutput(RPMOutput):
    def __init__(self, rpm_channel) -> None:
        super().__init__(rpm_channel)
        self._value = rpm_channel.level / 0x7f

    def get_min_max(self):
        return 0.0, 1.0

    def set_value(self, value: float, moving = False) -> None:
        if not isinstance(value, (bool, int, float)):
            raise TypeError

        old_value = self._value

        # sanitize value
        value = max(0.0, min(1.0, float(value)))

        # set value
        super().set_value(value, moving)

        # update RPM channel
        self._rpm_channel.level = int(self._value * 0x7f)

        # ping monitors if the value has changed
        if value != old_value:
            self._update_monitors()


MotorStates = Enum("MotorStates", [
    ("OFF", 0),
    ("RAISE", 0x10), # TODO: THIS IS WRONG!! check for real value
    ("LOWER", 0x20) # TODO: THIS IS WRONG!! check for real value
])

class MotorRPMOutput(RPMOutput):
    @staticmethod
    def _sanitize_value(val: MotorStates | int):
        try:
            return MotorStates(val)
        except ValueError:
            return MotorStates.OFF

    def __init__(self, rpm_channel) -> None:
        super().__init__(rpm_channel)
        self._state = self._sanitize_value(rpm_channel.level)
        self._value = self._state.value

    def get_min_max(self):
        return None

    def set_value(self, value: MotorStates, moving = False) -> None:
        if not isinstance(value, MotorStates):
            raise TypeError

        old_value = self._value

        # sanitize value
        # with the assertion above this is not necessary
        #value = self._sanitize_value(value)

        # set value
        super().set_value(value, moving)

        # update RPM channel
        self._rpm_channel.level = value.value

        # ping monitors if the value has changed
        if value != old_value:
            self._update_monitors()

class CCOOutput(Output):
    def __init__(self, led_state, device, channel) -> None:
        super().__init__()
        self._led_state = led_state
        self._device = device
        self._channel = channel

    def set_value(self, value, moving = False) -> None:
        if not isinstance(value, bool):
            raise TypeError

        old_value = self._value

        # sanitize value
        value = bool(value)

        # set value
        super().set_value(value, moving)

        # update LED state with CCO values
        val = 0b10 if value else 0b01
        self._led_state[self._device][16 + self._channel] = val
        if self._channel <= 7:
            self._led_state[self._device][9 + self._channel] = val

        # ping monitors if the value has changed
        if value != old_value:
            self._update_monitors()


@dataclass
class EventItem:
    output: "Output"
    start_time: float
    target_time: float
    target_value: float | int | bool
    rate: float
    is_rollback: bool


class Scheduler:
    def __init__(self) -> None:
        self.events: list[EventItem] = []
        self._logger = logging.getLogger(self.__class__.__name__)

    def add_event(self,
                  output: Output,
                  target_value: float,
                  spread_time: float|None = None,
                  delay_time: float = 0.0,
                  delay_is_rollback: bool = False):
        now = time.time()
        start_time = now + delay_time


        if spread_time is None:
            target_time = start_time
            rate_ = 0.0
        else:
            target_time = start_time + spread_time

            min_, max_ = output.get_min_max()
            full_range = max_ - min_

            if True: # TODO: make this configurable
                rate_ = full_range / spread_time
            else:
                current_value = output.get_value()
                assert isinstance(current_value, float)
                rate_ = (target_value - current_value) / spread_time

        self.events.append(EventItem(output=output,
                                     start_time=start_time,
                                     target_time=target_time,
                                     target_value=target_value,
                                     rate=rate_,
                                     is_rollback=delay_is_rollback))

    def cancel_events(self, output: Output):
        if not self.events:
            # trivial optimization to skip the below code when no events are scheduled.
            # (the code below should work fine though)
            return
        self.events = [event for event in self.events if event.output is not output]

    def tick(self):
        if not self.events:
            # trivial optimization to skip the below code when no events are scheduled.
            # (the code below should work fine though)
            return

        now = time.time()
        events_next = []
        for event in self.events:
            if now < event.start_time:
                if not event.is_rollback:
                    event.output.set_value(event.output.get_value(), True)
            elif now >= event.target_time:
                event.output.set_value(event.target_value, False)
            else:
                new_value = event.target_value - (event.target_time - now) * event.rate
                event.output.set_value(new_value, True)
                events_next.append(event)

        self.events = events_next
