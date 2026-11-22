"""Slowing down password guessing at the sign-in door (spec FR-056).

Wrong passwords are counted against two keys at once — the email typed and
the address they came from — and a key that reaches its limit inside fifteen
minutes is locked for fifteen minutes; a right password clears the email's
count. The two keys carry different limits (:class:`Key`): five for an email,
which is one account under attack, and many more for an address, because a
whole office shares one address behind its NAT and one colleague's typos must
not lock the door for everyone. The counts live in this process (a dict under
a lock): good enough for one API process, and a second process would only be
slower to lock, never looser than no limit at all. The same guard throttles
reset-link requests so nobody can use the mail server as a cannon.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

#: Wrong attempts allowed inside the window before a key is locked, for a key
#: that names no limit of its own.
MAX_FAILURES = 5
#: Seconds the window spans, and the lock lasts once it closes.
WINDOW_SECONDS = 15 * 60
#: Every this many wrong attempts, keys quiet for a whole window are forgotten.
_SWEEP_EVERY = 100


@dataclass(frozen=True)
class Key:
    """One thing wrong attempts are counted against, with its own limit."""

    name: str
    max_failures: int = MAX_FAILURES


class LoginGuard:
    def __init__(self, *, max_failures: int = MAX_FAILURES, window: float = WINDOW_SECONDS) -> None:
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def _key(self, item: str | Key) -> Key:
        """A bare name counts against the guard's own limit."""
        return item if isinstance(item, Key) else Key(item, self.max_failures)

    def _sweep(self, now: float) -> None:
        for key in [k for k, until in self._locked_until.items() if until <= now]:
            del self._locked_until[key]
        for key in [k for k, hits in self._failures.items() if not hits or hits[-1] + self.window <= now]:
            del self._failures[key]

    def retry_after(self, keys: Sequence[str | Key], now: float | None = None) -> int:
        """Seconds the caller must wait before another attempt, 0 when none."""
        now = time.monotonic() if now is None else now
        with self._lock:
            wait = 0.0
            for item in keys:
                until = self._locked_until.get(self._key(item).name)
                if until is not None and until > now:
                    wait = max(wait, until - now)
            return int(wait + 0.999) if wait > 0 else 0

    def failed(self, keys: Sequence[str | Key], now: float | None = None) -> None:
        """Count one wrong attempt against each key; lock a key that hit its limit."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._calls += 1
            if self._calls % _SWEEP_EVERY == 0:
                self._sweep(now)
            for item in keys:
                key = self._key(item)
                hits = self._failures.setdefault(key.name, deque())
                while hits and hits[0] + self.window <= now:
                    hits.popleft()
                hits.append(now)
                # A limit below one would lock before any attempt: one is the floor.
                if len(hits) >= max(1, key.max_failures):
                    self._locked_until[key.name] = now + self.window
                    hits.clear()

    def succeeded(self, keys: Sequence[str | Key]) -> None:
        """A right password forgets the key's wrong attempts (not an address lock)."""
        with self._lock:
            for item in keys:
                self._failures.pop(self._key(item).name, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()


#: The process-wide guard the auth API uses.
guard = LoginGuard()
