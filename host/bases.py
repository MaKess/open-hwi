from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

@dataclass
class RPMChannel:
    line: int
    module: int
    channel: int
    level: int = 0
    config: list[int] = field(default_factory=lambda:[5, 0, 0, 1])

@dataclass
class LED:
    line: int
    keypad: int
    button: int
    level: int = 0

class MotorStates(Enum):
    OFF = 0
    RAISE = 0x10 # TODO: THIS IS WRONG!! check for real value
    LOWER = 0x20 # TODO: THIS IS WRONG!! check for real value

    def __bool__(self):
        return self != MotorStates.OFF

class Action(ABC):
    @abstractmethod
    def action_trigger(self):
        ...


class OutputMonitor(ABC):
    @abstractmethod
    def output_update(self) -> None:
        ...
