"""AI mode: an order run paced in batches (step E).

Instead of trying every account at once, an AI run goes in batches:

1. Start with ``min_batch`` accounts.
2. When the batch is done, count the orders it placed. If at least
   ``scale_up_pct`` percent of the batch placed (rounded up, at least one),
   the product is selling: the next batch starts **at once** and is **twice**
   as big (capped at ``MAX_BATCH``).
3. Otherwise the next batch goes back to ``min_batch`` and waits until
   ``max_interval`` has passed since the last batch started.
4. The run stops when every account has been tried, the run's target
   quantity is reached, the operator cancels, or ``run_duration`` has passed.

This keeps a run from burning its whole account pool when stock or limits
have run out (the inferred behaviour of the competitor's AI mode; ours is the
simplest version of it, stated here).
"""

from __future__ import annotations

#: The order form's "Order execution version". Normal Speed works through at
#: most NORMAL_SPEED_AT_ONCE accounts at a time; High Speed uses the configured
#: concurrency (Settings → max concurrency). The "Higher MCC" variants run at
#: the same speed and are recorded on the run so their results can be compared.
EXECUTION_VERSIONS: dict[str, str] = {
    "normal": "Normal Speed (Reliable)",
    "normal_mcc": "Normal Speed (Reliable, Higher MCC Testing Mode)",
    "high": "High Speed (testing mode)",
    "high_mcc": "High Speed (Higher MCC Testing Mode)",
}
DEFAULT_EXECUTION_VERSION = "high"
NORMAL_SPEED_AT_ONCE = 2


def accounts_at_once(version: str | None, configured: int) -> int:
    """How many accounts a run works through at once for its execution version."""
    configured = max(1, int(configured or 1))
    if (version or DEFAULT_EXECUTION_VERSION).startswith("normal"):
        return min(NORMAL_SPEED_AT_ONCE, configured)
    return configured

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

#: The largest batch scaling up can reach.
MAX_BATCH = 200
#: How often a wait between batches checks for a cancel or a stop.
WAIT_STEP_SECONDS = 1.0

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([mhd])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}
MIN_DURATION_S = 3600
MAX_DURATION_S = 2 * 86400


def parse_duration(text: str | None) -> float:
    """"12h", "60m", "2d" -> seconds; ValueError outside 1h–2d or unreadable."""
    match = _DURATION.match(text or "")
    if not match:
        raise ValueError("write the run duration like 12h, 90m or 2d")
    seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
    if not MIN_DURATION_S <= seconds <= MAX_DURATION_S:
        raise ValueError("the run duration must be between 1h and 2d")
    return seconds


@dataclass(frozen=True)
class Pacing:
    max_interval_s: float = 60.0
    min_batch: int = 10
    scale_up_pct: int = 20
    run_seconds: float = 12 * 3600

    def as_payload(self) -> dict:
        return asdict(self)

    def threshold(self, batch_size: int) -> int:
        """Orders a batch must place to scale up."""
        return max(1, math.ceil(self.scale_up_pct / 100 * batch_size))


@dataclass
class PacedResult:
    #: How many accounts were handed to a batch.
    started: int
    #: Why the run stopped before trying every account, or None.
    stopped_because: str | None
    #: The size of every batch, in order.
    batches: list[int]


async def run_paced(
    account_ids: list[int],
    run_batch: Callable[[list[int], int], Awaitable[int]],
    pacing: Pacing,
    *,
    should_stop: Callable[[], str | None],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> PacedResult:
    """Go through ``account_ids`` in paced batches.

    ``run_batch(batch, offset)`` runs one batch (``offset`` is the position of
    its first account in the run) and returns how many orders it placed.
    ``should_stop()`` returns a reason to stop now ("cancelled", "target
    reached"), or None to go on.
    """
    started_at = clock()
    position = 0
    size = max(1, pacing.min_batch)
    batches: list[int] = []

    while position < len(account_ids):
        reason = should_stop()
        if reason:
            return PacedResult(position, reason, batches)
        if clock() - started_at >= pacing.run_seconds:
            return PacedResult(position, "the run duration ran out", batches)

        batch = account_ids[position : position + size]
        batch_started = clock()
        placed = await run_batch(batch, position)
        batches.append(len(batch))
        position += len(batch)
        if position >= len(account_ids):
            break

        if placed >= pacing.threshold(len(batch)):
            size = min(size * 2, MAX_BATCH)
            continue

        size = max(1, pacing.min_batch)
        deadline = batch_started + pacing.max_interval_s
        while clock() < deadline:
            reason = should_stop()
            if reason:
                return PacedResult(position, reason, batches)
            if clock() - started_at >= pacing.run_seconds:
                return PacedResult(position, "the run duration ran out", batches)
            await sleep(min(WAIT_STEP_SECONDS, max(0.0, deadline - clock())))

    return PacedResult(position, None, batches)
