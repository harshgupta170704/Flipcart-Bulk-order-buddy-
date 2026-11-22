"""Is this process stopping? Asked by the job engine, answered by the API.

The API owns the stop (``app.api.jobs.begin_shutdown``, audit F-32): once it
has begun, no new run may start, and a batch already running must not start
another account either. An account begun then would be cut off when the
process exits, and an order cut off between Pay and Flipkart's confirmation is
an "unconfirmed" row a person has to settle by hand.

The batches are built in ``app.jobs.tasks``, which ``app.api.jobs`` imports to
start them, so the engine cannot import the API back to read its flag — that
would be an import cycle. Instead the API hands over how to read it
(:func:`set_probe`, once, when it is imported) and the engine asks here
(:func:`is_stopping`). This module imports nothing of the app, so either side
may import it.

In a process with no API — an arq worker, a script — nothing has handed a
probe over and the answer is always "no": nothing there can begin the API's
stop.
"""

from __future__ import annotations

from collections.abc import Callable


def _never() -> bool:
    return False


_probe: Callable[[], bool] = _never


def set_probe(probe: Callable[[], bool]) -> None:
    """Answer :func:`is_stopping` with ``probe`` from now on.

    ``probe`` is called at every check, so it must read the flag afresh each
    time rather than capture its value once.
    """
    global _probe
    _probe = probe


def is_stopping() -> bool:
    """True once this process has been asked to stop."""
    return bool(_probe())
