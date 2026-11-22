"""AI mode: price/stock tracker & adaptive batch pacing (BOB parity).

In BulkOrderBuddy (BOB), operators use **AI Mode** both for flash-sale pacing
and as an automated **Price & Stock Tracker / Sniper** over ``Run Duration``
(1h–2d):

1. Set ``Final Amount Limit`` to your desired buy price (often *below* the
   current Flipkart price) and ``Run Duration`` (e.g. ``12h`` or ``2d``).
2. **Watching / Price-Tracking Phase:** While the product is above the target
   cap or out of stock, AI mode does not burn the whole account pool at once.
   With ``single_probe=True`` (default), it probes with **1 account** first;
   if that probe stops cleanly before payment (``price_over_limit``,
   ``out_of_stock``, ``seller_mismatch``, ``delivery_sla_not_met``, etc.),
   AI mode records the live checkout price, **recycles** that non-buying
   account back to the tail of the eligible queue, and waits ``max_interval``
   before the next probe.
3. **Scale-Up Burst Phase:** The instant a probe or batch places at least
   ``threshold(batch_size)`` orders (either ``ceil(scale_up_pct% * batch)`` or
   a fixed ``scale_up_count``), AI mode skips the interval wait (**0 s wait**),
   scales up immediately (``1 -> min_batch -> 2x -> 4x ...``, capped at
   ``MAX_BATCH`` and remaining budget), and buys as fast as possible while the
   price drop or stock window lasts.
4. **Auto-Reset:** If Flipkart raises the price or sells out mid-run, the next
   batch falls below threshold, AI mode resets back to watching mode, recycles
   any market-blocked accounts, and keeps tracking until ``Total Quantity`` is
   reached, ``Run Duration`` expires, no eligible accounts remain, or the
   operator cancels.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import math
import re
import time

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


#: Supported modes for ``Pacing.scale_up_mode``.
SCALE_UP_MODES: tuple[str, ...] = ("percent", "count")

#: The largest batch scaling up can reach.
MAX_BATCH = 200
#: How often a wait between batches checks for a cancel or a stop.
WAIT_STEP_SECONDS = 1.0
#: Maximum price-probe points kept in ``AiTrackerState.history``.
MAX_PRICE_HISTORY = 50

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
    #: ``"percent"`` (ceil(scale_up_pct% * batch)) or ``"count"`` (fixed placed count).
    scale_up_mode: str = "percent"
    #: Used when ``scale_up_mode == "count"``; None keeps percent mode.
    scale_up_count: int | None = None
    #: When True, probe 1 account first while watching (0 placed on market block),
    #: jumping to ``min_batch`` / doubling immediately once a probe places.
    single_probe: bool = False
    #: When True, accounts that stopped cleanly due to price > target or
    #: out-of-stock are recycled to the tail of the queue so tracking runs for
    #: the full ``run_seconds``.
    recycle_accounts: bool = False

    def as_payload(self) -> dict:
        data = asdict(self)
        if self.scale_up_mode == "percent" and self.scale_up_count is None:
            data.pop("scale_up_count", None)
        return data

    def threshold(self, batch_size: int) -> int:
        """Orders a batch must place to scale up immediately."""
        batch_size = max(1, int(batch_size))
        if self.scale_up_mode == "count" and self.scale_up_count is not None:
            return max(1, min(batch_size, int(self.scale_up_count)))
        return max(1, math.ceil(self.scale_up_pct / 100 * batch_size))


@dataclass
class BatchOutcome:
    """Rich report from one AI batch, powering price tracking & account recycling."""

    placed: int
    #: Account IDs from this batch that stopped cleanly due to market conditions
    #: (price > target, out of stock, seller unavailable, transient) and may be
    #: safely retried on a later interval.
    retryable_ids: list[int] = field(default_factory=list)
    #: Live checkout / product prices observed during this batch.
    seen_prices: list[float] = field(default_factory=list)
    #: Stock signal observed during this batch (True/False/None).
    in_stock: bool | None = None
    #: Whether failures in this batch were market-driven (price / stock / seller).
    market_blocked: bool = False
    #: Short summary of the latest outcome in this batch.
    last_reason: str | None = None


@dataclass
class PacedResult:
    #: How many unique accounts were tried at least once (or total dispatches
    #: in legacy single-pass mode).
    started: int
    #: Why the run stopped before exhausting every account, or None.
    stopped_because: str | None
    #: The size of every batch, in order.
    batches: list[int]
    #: Total attempts across all batches (includes recycled probes).
    total_attempts: int = 0
    #: Total orders placed across all batches.
    total_placed: int = 0
    #: Total times an account was recycled for a later price/stock probe.
    recycled_attempts: int = 0
    #: Final AI price-tracker telemetry snapshot.
    tracker: dict | None = None
    #: Unique account IDs that were never attempted (still pending).
    untried_ids: list[int] = field(default_factory=list)


def _iso_utc(dt: datetime | None = None) -> str:
    now = dt or datetime.now(timezone.utc)
    return now.isoformat(timespec="seconds").replace("+00:00", "Z")


async def run_paced(
    account_ids: list[int],
    run_batch: Callable[[list[int], int], Awaitable[int | BatchOutcome]],
    pacing: Pacing,
    *,
    should_stop: Callable[[], str | None],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    target_price: float | None = None,
    remaining_needed: Callable[[], int | None] | None = None,
    on_state: Callable[[dict], Awaitable[None] | None] | None = None,
) -> PacedResult:
    """Go through ``account_ids`` in paced batches, optionally acting as a
    continuous BOB-style AI Price & Stock Tracker with account recycling.

    ``run_batch(batch, offset)`` runs one batch and returns either an ``int``
    (legacy single-pass placement count) or a :class:`BatchOutcome` (enabling
    live price tracking, single-probe watching, and round-robin recycling of
    over-target / out-of-stock accounts).
    """
    started_at = clock()
    pool: deque[int] = deque(account_ids)
    tried_unique: set[int] = set()
    total_attempts = 0
    total_placed = 0
    recycled_attempts = 0
    batches: list[int] = []

    min_batch = max(1, int(pacing.min_batch))
    # In single-probe tracking mode, start in watching state with 1 probe so a
    # run started while price > target never wastes min_batch checkouts up front.
    watching = bool(pacing.single_probe)
    size = 1 if watching else min_batch

    # Live AI Price-Tracker state persisted to Job.payload["ai_tracker"]
    last_seen_price: float | None = None
    lowest_seen_price: float | None = None
    highest_seen_price: float | None = None
    in_stock: bool | None = None
    last_reason: str | None = None
    last_probe_at: str | None = None
    history: list[dict] = []

    async def emit(status: str, next_probe_at: str | None = None, stopped_because: str | None = None) -> dict:
        snap = {
            "status": status,
            "target_price": target_price,
            "last_seen_price": last_seen_price,
            "lowest_seen_price": lowest_seen_price,
            "highest_seen_price": highest_seen_price,
            "in_stock": in_stock,
            "batches_run": len(batches),
            "total_attempts": total_attempts,
            "placed_orders": total_placed,
            "recycled_attempts": recycled_attempts,
            "eligible_accounts": len(pool),
            "current_batch_size": size,
            "watching": watching,
            "single_probe": bool(pacing.single_probe),
            "recycle_accounts": bool(pacing.recycle_accounts),
            "scale_up_mode": pacing.scale_up_mode,
            "last_probe_at": last_probe_at,
            "next_probe_at": next_probe_at,
            "last_reason": last_reason,
            "stopped_because": stopped_because,
            "history": list(history[-MAX_PRICE_HISTORY:]),
        }
        if on_state is not None:
            maybe = on_state(snap)
            if asyncio.iscoroutine(maybe):
                await maybe
        return snap

    def _finish(stopped_because: str | None, snap: dict | None = None) -> PacedResult:
        untried = [aid for aid in account_ids if aid not in tried_unique]
        return PacedResult(
            started=len(tried_unique),
            stopped_because=stopped_because,
            batches=batches,
            total_attempts=total_attempts,
            total_placed=total_placed,
            recycled_attempts=recycled_attempts,
            tracker=snap,
            untried_ids=untried,
        )

    while pool:
        reason = should_stop()
        if reason:
            snap = await emit("stopped", stopped_because=reason)
            return _finish(reason, snap)
        if clock() - started_at >= pacing.run_seconds:
            reason = "the run duration ran out"
            snap = await emit("stopped", stopped_because=reason)
            return _finish(reason, snap)

        # Admitted batch size: respect single-probe watching, pool size, and remaining budget
        admitted = 1 if watching else size
        if not watching and remaining_needed is not None:
            needed = remaining_needed()
            if needed is not None and needed > 0:
                # Allow modest headroom (min_batch or 2x remaining) without launching 200 browsers for 2 units
                admitted = min(admitted, max(min_batch, needed * 2))
        admitted = max(1, min(admitted, len(pool)))

        batch = [pool.popleft() for _ in range(admitted)]
        batch_offset = total_attempts
        batch_started = clock()

        raw = await run_batch(batch, batch_offset)
        if isinstance(raw, BatchOutcome):
            outcome = raw
        else:
            outcome = BatchOutcome(placed=int(raw))

        batches.append(len(batch))
        total_attempts += len(batch)
        total_placed += outcome.placed
        tried_unique.update(batch)

        # Update live price & stock observations
        last_probe_at = _iso_utc()
        if outcome.seen_prices:
            last_seen_price = outcome.seen_prices[-1]
            batch_low = min(outcome.seen_prices)
            batch_high = max(outcome.seen_prices)
            lowest_seen_price = batch_low if lowest_seen_price is None else min(lowest_seen_price, batch_low)
            highest_seen_price = batch_high if highest_seen_price is None else max(highest_seen_price, batch_high)
        if outcome.in_stock is not None:
            in_stock = outcome.in_stock
        if outcome.last_reason:
            last_reason = outcome.last_reason

        # Recycle non-buying market-blocked accounts back to the tail of the queue
        if pacing.recycle_accounts and outcome.retryable_ids:
            in_pool = set(pool)
            for aid in outcome.retryable_ids:
                if aid not in in_pool:
                    pool.append(aid)
                    in_pool.add(aid)
                    recycled_attempts += 1

        history.append(
            {
                "at": last_probe_at,
                "price": min(outcome.seen_prices) if outcome.seen_prices else None,
                "in_stock": outcome.in_stock,
                "batch_size": len(batch),
                "placed": outcome.placed,
                "failed": max(0, len(batch) - outcome.placed),
                "status": "placed" if outcome.placed > 0 else ("watching" if outcome.market_blocked else "failed"),
                "reason": outcome.last_reason,
            }
        )
        if len(history) > MAX_PRICE_HISTORY:
            history = history[-MAX_PRICE_HISTORY:]

        if not pool:
            break

        # Stop immediately if target reached or cancel requested right after the batch
        reason = should_stop()
        if reason:
            snap = await emit("completed" if "target" in reason else "stopped", stopped_because=reason)
            return _finish(reason, snap)

        # Scale-up vs cooldown / watching decision
        if watching and outcome.placed > 0:
            # The single price-tracker probe placed! Immediately burst into min_batch with 0 wait.
            watching = False
            size = min(max(min_batch, 2), MAX_BATCH)
            await emit("scaling_up")
            continue

        if not watching and outcome.placed >= pacing.threshold(len(batch)):
            # Product is selling at or below target: double immediately, 0 wait!
            size = min(size * 2, MAX_BATCH)
            await emit("scaling_up")
            continue

        # Did not meet scale-up threshold: cool down and wait up to max_interval_s
        if pacing.single_probe and outcome.placed == 0 and outcome.market_blocked:
            watching = True
            size = 1
        else:
            watching = False
            size = min_batch

        deadline = batch_started + pacing.max_interval_s
        wait_left = max(0.0, deadline - clock())
        next_at = _iso_utc(datetime.now(timezone.utc) + timedelta(seconds=wait_left)) if wait_left > 0 else None
        await emit("watching" if (watching or outcome.market_blocked) else "cooling_down", next_probe_at=next_at)

        while clock() < deadline:
            reason = should_stop()
            if reason:
                snap = await emit("stopped", stopped_because=reason)
                return _finish(reason, snap)
            if clock() - started_at >= pacing.run_seconds:
                reason = "the run duration ran out"
                snap = await emit("stopped", stopped_because=reason)
                return _finish(reason, snap)
            await sleep(min(WAIT_STEP_SECONDS, max(0.0, deadline - clock())))

    stopped = "no eligible accounts left" if (pacing.recycle_accounts and not pool) else None
    snap = await emit("completed" if total_placed > 0 else "stopped", stopped_because=stopped)
    return _finish(stopped, snap)
