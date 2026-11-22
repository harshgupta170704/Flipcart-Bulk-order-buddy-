"""How long each step of one order or one sign-in took.

The flows call ``mark("step name")`` as each step ends; the run wraps the
flow in ``timing()`` and reads ``summary()`` afterwards, e.g.

    open product 2.1s · to checkout 6.4s · quantity 1.2s · ... · total 41.0s

The current timer lives in a context variable, so every account in a run
(each its own asyncio task) has its own, and a flow called outside a run
(the CLI, a test) marks nothing and pays nothing. Marking never raises.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current: ContextVar["StepTimer | None"] = ContextVar("step_timer", default=None)


class StepTimer:
    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self.started = self.last = clock()
        self.steps: list[tuple[str, float]] = []

    def mark(self, name: str) -> None:
        now = self._clock()
        self.steps.append((name, now - self.last))
        self.last = now

    def finish(self, name: str) -> None:
        """Mark the last step, unless nothing happened since the last mark."""
        if self._clock() - self.last >= 0.05:
            self.mark(name)

    @property
    def total(self) -> float:
        return self.last - self.started

    def summary(self) -> str:
        if not self.steps:
            return ""
        parts = [f"{name} {seconds:.1f}s" for name, seconds in self.steps]
        return " · ".join(parts) + f" · total {self.total:.1f}s"

    def slowest(self) -> tuple[str, float] | None:
        return max(self.steps, key=lambda step: step[1]) if self.steps else None


def mark(name: str) -> None:
    """End the current step of the running flow, if one is being timed."""
    timer = _current.get()
    if timer is not None:
        try:
            timer.mark(name)
        except Exception:  # noqa: BLE001 — timing never breaks a flow
            pass


@contextmanager
def timing(clock: Callable[[], float] = time.perf_counter) -> Iterator[StepTimer]:
    timer = StepTimer(clock)
    token = _current.set(timer)
    try:
        yield timer
    finally:
        _current.reset(token)
