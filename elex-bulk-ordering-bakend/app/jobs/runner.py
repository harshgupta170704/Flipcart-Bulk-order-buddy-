"""Bounded-concurrency runner — the "speed" knob from PLAN.md §2/§3.

Runs up to ``limit`` accounts in parallel inside one event loop. The limit is
what keeps us from tripping Flipkart's rate limiting (PLAN.md §3 caveat), so it
is a hard cap, not a hint.

Two invariants the rest of the engine depends on:

* **No item can abort the batch.** A worker that raises is captured as a failed
  ``ItemResult``; its siblings keep going. This is what makes the price-target
  rule in PLAN.md §6a safe — one over-target order never stops the run.
* **Results come back in input order**, however completion interleaves, so a
  caller can zip them back against its own input list.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("ordering.jobs.runner")

CANCELLED = "cancelled"


@dataclass
class ItemResult:
    """Outcome of one item. ``value`` is whatever the worker returned."""

    index: int
    item: Any
    ok: bool
    value: Any | None = None
    error: str | None = None
    skipped: bool = False


async def run_bounded(
    items: Iterable[Any],
    worker: Callable[[Any, int], Any],
    *,
    limit: int,
    should_cancel: Callable[[], bool] | None = None,
    on_result: Callable[[ItemResult], Any] | None = None,
) -> list[ItemResult]:
    """Run ``worker(item, index)`` over ``items``, at most ``limit`` at a time.

    ``should_cancel`` is polled (synchronously) just before an item starts; once
    it returns True the remaining items are reported as skipped. Items already
    in flight are left alone — we never interrupt a browser mid-payment.

    ``on_result`` is invoked after each item settles and may be sync or async.
    """
    materialised = list(items)
    results: list[ItemResult | None] = [None] * len(materialised)
    semaphore = asyncio.Semaphore(_coerce_limit(limit))

    async def _run_one(index: int, item: Any) -> None:
        if _cancel_requested(should_cancel):
            # Checked before queueing too, so a cancelled batch drains fast
            # instead of waiting for semaphore slots it will not use.
            result = _skipped(index, item)
        else:
            async with semaphore:
                if _cancel_requested(should_cancel):
                    result = _skipped(index, item)
                else:
                    result = await _invoke(worker, index, item)
        results[index] = result
        await _emit(on_result, result)

    await asyncio.gather(
        *(_run_one(i, item) for i, item in enumerate(materialised))
    )
    # Every slot is filled: _run_one assigns on all paths.
    return [r for r in results if r is not None]


def _coerce_limit(limit: int) -> int:
    """A limit below 1 would mean "never run"; treat it as serial execution."""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = 1
    return max(1, value)


def _skipped(index: int, item: Any) -> ItemResult:
    return ItemResult(index=index, item=item, ok=False, skipped=True, error=CANCELLED)


async def _invoke(
    worker: Callable[[Any, int], Any], index: int, item: Any
) -> ItemResult:
    try:
        value = await worker(item, index)
    except asyncio.CancelledError:
        raise  # genuine task cancellation must stay cancellation
    except Exception as exc:  # noqa: BLE001 - one item's failure is data, not a crash
        log.exception("item %s failed", index)
        return ItemResult(
            index=index,
            item=item,
            ok=False,
            error=str(exc) or exc.__class__.__name__,
        )
    return ItemResult(index=index, item=item, ok=True, value=value)


def _cancel_requested(should_cancel: Callable[[], bool] | None) -> bool:
    if should_cancel is None:
        return False
    try:
        return bool(should_cancel())
    except Exception:  # noqa: BLE001 - an unreadable cancel flag must not kill the run
        log.exception("should_cancel raised; treating the job as not cancelled")
        return False


async def _emit(on_result: Callable[[ItemResult], Any] | None, result: ItemResult) -> None:
    """Call the per-item callback, awaiting it when it is a coroutine."""
    if on_result is None:
        return
    try:
        returned = on_result(result)
        if inspect.isawaitable(returned):
            await returned
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - progress reporting must not abort the batch
        log.exception("on_result callback failed for item %s", result.index)
