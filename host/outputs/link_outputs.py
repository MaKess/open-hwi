import logging

from . import Output

class CCOOutput(Output):
    def __init__(self, leds, line, device, channel) -> None:
        super().__init__()
        self._leds = leds
        self._line = line
        self._device = device
        self._channel = channel
        self._logger = logging.getLogger(self.__class__.__name__)

    def validate_value(self, value, allow_none:bool=False):
        if not isinstance(value, bool) and (not allow_none or value is not None):
            raise TypeError

    def get_min_max(self):
        return False, True

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
        self._leds[self._line, self._device, 16 + self._channel] = val
        if self._channel <= 7:
            self._leds[self._line, self._device, 9 + self._channel] = val

        # ping monitors if the value has changed
        if value != old_value:
            self._update_monitors()
