from dataclasses import dataclass
import time
import logging

from outputs import Output


@dataclass
class EventItem:
    output: Output
    start_time: float
    target_time: float
    target_value: float | int | bool
    rate: float | None
    is_rollback: bool


class Scheduler:
    def __init__(self) -> None:
        self.events: list[EventItem] = []
        self._logger = logging.getLogger(self.__class__.__name__)

    def add_event(self,
                  output: Output,
                  target_value,
                  spread_time: float|None = None,
                  delay_time: float = 0.0,
                  delay_is_rollback: bool = False):
        now = time.time()
        start_time = now + delay_time

        if spread_time is None:
            target_time = start_time
            rate_ = None # if used, this will provoke a "TypeError" in "tick()" below
        else:
            assert isinstance(target_value, float), "only a float value can be spread across some time interval"
            target_time = start_time + spread_time

            min_, max_ = output.get_min_max()
            full_range = max_ - min_

            if False: # TODO: make this configurable
                # wrong! this would always give a positive rate, even when the target is lower than the current value
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
            self._logger.info("handle event tick at %f: %s", now, event)
            if now < event.start_time:
                if not event.is_rollback:
                    event.output.set_value(event.output.get_value(), True)
                events_next.append(event)
            elif now >= event.target_time:
                event.output.set_value(event.target_value, False)
            else:
                assert isinstance(event.rate, float)
                new_value = event.target_value - (event.target_time - now) * event.rate
                event.output.set_value(new_value, True)
                events_next.append(event)

        self.events = events_next
