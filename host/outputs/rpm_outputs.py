import logging

from bases import MotorStates, RPMChannel
from . import Output

class RPMOutput(Output):
    def __init__(self, rpm_channel) -> None:
        super().__init__()
        self._rpm_channel = rpm_channel

class OnOffRPMOutput(RPMOutput):
    def __init__(self, rpm_channel) -> None:
        super().__init__(rpm_channel)
        self._value = bool(rpm_channel.level)

    def validate_value(self, value, allow_none:bool=False):
        if not isinstance(value, (bool, int)) and (not allow_none or value is not None):
            raise TypeError

    def get_min_max(self):
        return False, True

    def set_value(self, value: bool, moving = False) -> None:
        self.validate_value(value)

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
        self._logger = logging.getLogger(self.__class__.__name__)

    def validate_value(self, value, allow_none: bool=False):
        if not isinstance(value, (bool, int, float)) and (not allow_none or value is not None):
            raise TypeError

    def get_min_max(self):
        return 0.0, 1.0

    def set_value(self, value: float, moving = False) -> None:
        self.validate_value(value)

        old_value = self._value

        # sanitize value
        value = max(0.0, min(1.0, float(value)))

        # set value
        super().set_value(value, moving)

        # update RPM channel
        val = int(self._value * 0x7f)
        self._logger.info("setting RPM output to %d", val)
        self._rpm_channel.level = val

        # ping monitors if the value has changed
        if value != old_value:
            self._update_monitors()


class MotorRPMOutput(RPMOutput):
    @staticmethod
    def _sanitize_value(val: MotorStates | int):
        try:
            return MotorStates(val)
        except ValueError:
            return MotorStates.OFF

    def __init__(self, rpm_channel: RPMChannel, raise_time: float, lower_time: float) -> None:
        super().__init__(rpm_channel)
        self._state = self._sanitize_value(rpm_channel.level)
        self._value = self._state.value
        self._raise_time = raise_time
        self._lower_time = lower_time

    def validate_value(self, value, allow_none:bool=False):
        if not isinstance(value, MotorStates) and (not allow_none or value is not None):
            raise TypeError

    def get_min_max(self):
        return None, None

    def get_runtime(self, direction: MotorStates) -> float:
        match direction:
            case MotorStates.RAISE:
                return self._raise_time
            case MotorStates.LOWER:
                return self._lower_time
            case _:
                raise ValueError

    def is_moving(self) -> bool:
        return self._value != MotorStates.OFF

    def set_value(self, value: MotorStates, moving = False) -> None:
        if not isinstance(value, MotorStates):
            raise TypeError

        if moving != bool(value):
            raise ValueError("'moving' parameter should not be specified manually for MotorRPMOutput.set_value()")

        old_value = self._value

        # sanitize value
        # with the assertion above this is not necessary
        #value = self._sanitize_value(value)

        # set value
        super().set_value(value, moving=False)

        # update RPM channel
        self._rpm_channel.level = value.value

        # ping monitors if the value has changed
        if value != old_value:
            self._update_monitors()
