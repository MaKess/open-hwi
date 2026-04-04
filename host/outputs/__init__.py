from abc import ABC, abstractmethod

from bases import OutputMonitor


class Output(ABC):
    def __init__(self) -> None:
        self._value = None
        self._moving: bool = False
        self._output_monitors: list[OutputMonitor] = []
        # for motor/shade control this allows single button control with raise-stop-lower-stop
        self._last_nonzero_value = None

    @abstractmethod
    def validate_value(self, value, allow_none:bool=False):
        ...

    def add_monitor(self, monitor: OutputMonitor):
        self._output_monitors.append(monitor)

    def is_moving(self) -> bool:
        return self._moving

    @abstractmethod
    def get_min_max(self) -> tuple:
        ...

    def get_last_nonzero_value(self):
        return self._last_nonzero_value

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
