"""The unit budget — how a concurrent run places exactly N units (ROADMAP §9).

A run is no longer "one order per account": the operator wants N units of a
product spread across accounts, a few units per order. The accounts are walked
**concurrently** (``app.jobs.runner.run_bounded``), and that is precisely what
makes the obvious implementation wrong — see ``UnitBudget``.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("ordering.jobs.budget")


class UnitBudget:
    """One run's unit target, handed out in order-sized claims.

    **Why claim up front** instead of counting what has already been placed:
    with four workers in flight and two units still wanted, all four would read
    the same "two remaining" and all four would start an order — four real
    purchases for two wanted units, on accounts that have a per-window cap. The
    overshoot costs money *and* burns quota. So a worker claims its units
    *before* it opens a browser (they are spoken for while the order is being
    placed) and **releases** them if the order does not go through, so the slot
    passes to another account instead of being lost.

    **Why this is safe under asyncio concurrency.** ``claim`` and ``release``
    are plain synchronous methods with no ``await`` anywhere inside them. Once a
    coroutine enters one, the event loop cannot switch to another task until it
    returns, so reading what is left and writing the value that spends it are
    one indivisible step — and that read-then-write is the only interleaving
    that could ever hand the same unit to two workers. The lock is held as well,
    so the object stays correct if a caller claims from a worker thread
    (``asyncio.to_thread``); it is never held across an ``await``, so it cannot
    deadlock the loop.
    """

    def __init__(self, total_units: int, per_order: int) -> None:
        # A nonsense size is clamped, not raised on: the API validates these
        # (``POST /jobs/order``), and blowing up here would lose a whole batch
        # mid-run over a value we can interpret safely.
        self._total = max(0, int(total_units))
        self._per_order = max(1, int(per_order))
        self._remaining = self._total
        self._lock = threading.Lock()

    # --- spending -----------------------------------------------------------
    def claim(self, limit: int | None = None) -> int | None:
        """Reserve up to ``per_order`` units; ``None`` once nothing is left.

        Never hands out more than is left, so the run cannot place more than
        ``total_units``. ``limit`` caps this single claim — used for an account
        whose own platform allowance is smaller than the run's batch size, so it
        orders only what it may still buy instead of tripping the cap.
        """
        with self._lock:
            units = min(self._per_order, self._remaining)
            if limit is not None:
                units = min(units, int(limit))
            if units <= 0:
                return None
            self._remaining -= units
            return units

    def release(self, units: int) -> None:
        """Hand unplaced units back, so another account can use the slot.

        Called when an order fails, is blocked by the price target, or buys
        fewer units than were claimed. Releasing more than was ever claimed is a
        caller bug — it would let the run overshoot — so the excess is dropped
        and logged rather than trusted.
        """
        amount = int(units)
        if amount <= 0:
            return
        with self._lock:
            restored = min(amount, self._total - self._remaining)
            if restored != amount:
                log.warning(
                    "released %s unit(s) but only %s were claimed", amount, restored
                )
            self._remaining += restored

    # --- reading ------------------------------------------------------------
    @property
    def placed(self) -> int:
        """Units spoken for: claimed and not released.

        An order still in flight already counts here — that is the point. By the
        time a run ends every claim has either been released or turned into an
        order, so this is the number of units actually placed.
        """
        with self._lock:
            return self._total - self._remaining

    @property
    def remaining(self) -> int:
        """Units still free to claim."""
        with self._lock:
            return self._remaining

    @property
    def total(self) -> int:
        """The target this run was given."""
        return self._total

    @property
    def per_order(self) -> int:
        """Units one order may carry."""
        return self._per_order

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"UnitBudget(total={self._total}, per_order={self._per_order}, "
            f"placed={self.placed}, remaining={self.remaining})"
        )
