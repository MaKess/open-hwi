import logging
from dataclasses import dataclass

from bases import LED, MotorStates, Action, OutputMonitor
from outputs import Output
from outputs.rpm_outputs import MotorRPMOutput
from scheduler import Scheduler

class ShadeButton(Action, OutputMonitor):
    def __init__(self, led, scheduler) -> None:
        super().__init__()
        self._led: LED = led
        self._scheduler: Scheduler = scheduler
        self._items: list[MotorRPMOutput] = []

    def add_output(self, output: MotorRPMOutput) -> "ShadeButton":
        if not isinstance(output, MotorRPMOutput):
            raise TypeError
        self._items.append(output)
        output.add_monitor(self)
        return self

    def _is_moving(self):
        return any(item.is_moving() for item in self._items)

    def action_trigger(self):
        if not self._items:
            return

        match self._items[0].get_last_nonzero_value():
            case MotorStates.RAISE:
                direction = MotorStates.LOWER
            case MotorStates.LOWER:
                direction = MotorStates.RAISE
            case _:
                # graceful fallback: if no previous state is known, let's decide to "lower"
                direction = MotorStates.LOWER

        scheduler = self._scheduler
        for output in self._items:
            scheduler.cancel_events(output)
            scheduler.add_event(output=output,
                                target_value=direction)
            scheduler.add_event(output=output,
                                target_value=MotorStates.OFF,
                                delay_time=output.get_runtime(direction),
                                delay_is_rollback=True)

    def output_update(self) -> None:
        if self._is_moving():
            val = 0b10
        else:
            val = 0b00
        self._led.level = val


@dataclass
class BasicOutputItem:
    output: Output
    on_value: float | int | bool
    off_value: float | int | bool
    delay_time: float
    spread_time: float | None
    rollback_time: float | None
class BasicButton(Action, OutputMonitor):
    def __init__(
            self,
            led: LED,
            scheduler: Scheduler,
            toggle: bool=False
        ) -> None:
        super().__init__()
        self._led = led
        self._scheduler: Scheduler = scheduler
        self._toggle = toggle
        self._items: list[BasicOutputItem] = []
        self._logger = logging.getLogger(self.__class__.__name__)

    def add_output(self,
                   output: Output,
                   on_value: float | int | bool,
                   off_value: float | int | bool | None = None,
                   delay_time: float = 0.0,
                   spread_time: float|None = None,
                   rollback_time: float | None = None) -> "BasicButton":
        if off_value is None:
            off_value = type(on_value)()
        self._items.append(BasicOutputItem(output, on_value, off_value, delay_time, spread_time, rollback_time))
        output.add_monitor(self)
        return self

    def _is_moving(self):
        return any(item.output.is_moving() for item in self._items)

    def _is_on(self):
        return any(item.output.get_value() for item in self._items)

    def action_trigger(self):
        self._logger.info("Basic Button triggered")
        is_on = self._is_on()
        scheduler = self._scheduler
        for item in self._items:
            scheduler.cancel_events(item.output)
            scheduler.add_event(output=item.output,
                                target_value=item.off_value if is_on and self._toggle else item.on_value,
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
        self._led.level = val
