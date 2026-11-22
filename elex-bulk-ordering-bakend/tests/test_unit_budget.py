"""Tests for ``UnitBudget`` — the thing that stops a run overshooting.

Pure unit tests, no DB and no job plumbing. The headline one is
``test_fifty_concurrent_claims_hand_out_exactly_the_total``: the budget exists
because the accounts in a run are walked concurrently, so "claim before you
order" has to hold when every worker claims at the same moment. Overshooting
means real purchases nobody asked for, against accounts with a per-window cap.
"""

from __future__ import annotations

import asyncio

from app.jobs.budget import UnitBudget


# --- the concurrency guarantee ------------------------------------------------
def test_fifty_concurrent_claims_hand_out_exactly_the_total() -> None:
    """Fifty workers claiming at once must share out 10 units, not 50.

    ``asyncio.sleep(0)`` yields, so every task is live and queued on the loop
    before the first claim runs — the interleaving that a "count what has been
    placed" implementation would get wrong.
    """
    budget = UnitBudget(10, 4)

    async def race() -> list[int | None]:
        async def claimer() -> int | None:
            await asyncio.sleep(0)
            return budget.claim()

        return await asyncio.gather(*(claimer() for _ in range(50)))

    granted = [units for units in asyncio.run(race()) if units is not None]

    assert sum(granted) == 10
    # 4 + 4 + 2: full batches, then whatever is left over. Sorted because which
    # task wins which claim is up to the loop.
    assert sorted(granted) == [2, 4, 4]
    assert budget.placed == 10
    assert budget.remaining == 0


def test_concurrent_single_unit_claims_stop_at_the_total() -> None:
    """Fifty claimers, a total of 12, one unit each: twelve winners."""
    budget = UnitBudget(12, 1)

    async def race() -> list[int | None]:
        async def claimer() -> int | None:
            await asyncio.sleep(0)
            return budget.claim()

        return await asyncio.gather(*(claimer() for _ in range(50)))

    results = asyncio.run(race())
    granted = [units for units in results if units is not None]

    assert granted == [1] * 12
    assert results.count(None) == 38  # the rest were told there is nothing left
    assert (budget.placed, budget.remaining) == (12, 0)


def test_concurrent_claims_and_releases_never_exceed_the_total() -> None:
    """Twenty workers, half of whose orders fail: never more than 6 units.

    Note what this does *not* claim. With every worker claiming at the same
    instant, a release can land after the others have already been told there
    is nothing left, so the freed slot finds no taker and the run ends short of
    its target. That is a property of the batch, not of the budget: the real
    runner gates the accounts behind a semaphore, so later accounts reach their
    claim after earlier ones have settled — see the bounded test below. What
    must hold in *every* interleaving is the one thing that costs money if it
    does not: the run never places more units than it was given.
    """
    budget = UnitBudget(6, 2)
    kept: list[int] = []

    async def race() -> None:
        async def claimer(index: int) -> None:
            await asyncio.sleep(0)
            units = budget.claim()
            if units is None:
                return
            await asyncio.sleep(0)  # stands in for the browser work
            if index % 2:  # every other "order" fails
                budget.release(units)
            else:
                kept.append(units)

        await asyncio.gather(*(claimer(i) for i in range(20)))

    asyncio.run(race())

    assert sum(kept) <= 6
    assert budget.placed == sum(kept)
    assert budget.placed + budget.remaining == 6


def test_a_release_reaches_a_later_worker_when_the_run_is_bounded() -> None:
    """Gated the way ``run_bounded`` gates accounts, the target is still met.

    Every failing "order" gives its units back before the next account claims,
    so all 6 units get placed even though half the attempts failed.
    """
    budget = UnitBudget(6, 2)
    kept: list[int] = []

    async def race() -> None:
        gate = asyncio.Semaphore(1)  # the run's concurrency limit

        async def claimer(index: int) -> None:
            async with gate:
                units = budget.claim()
                if units is None:
                    return
                await asyncio.sleep(0)
                if index % 2:
                    budget.release(units)
                else:
                    kept.append(units)

        await asyncio.gather(*(claimer(i) for i in range(20)))

    asyncio.run(race())

    assert sum(kept) == 6  # every released unit was re-claimed by someone
    assert (budget.placed, budget.remaining) == (6, 0)


# --- claiming -----------------------------------------------------------------
def test_claim_hands_out_per_order_then_the_remainder_then_none() -> None:
    budget = UnitBudget(5, 2)

    assert budget.claim() == 2
    assert budget.claim() == 2
    assert budget.claim() == 1  # never more than what is left
    assert budget.claim() is None
    assert budget.claim() is None  # exhausted stays exhausted
    assert (budget.placed, budget.remaining) == (5, 0)


def test_placed_and_remaining_stay_consistent_through_a_run() -> None:
    """placed + remaining == total at every single step."""
    budget = UnitBudget(7, 3)
    assert (budget.total, budget.per_order) == (7, 3)

    seen: list[tuple[int, int]] = [(budget.placed, budget.remaining)]
    first = budget.claim()
    seen.append((budget.placed, budget.remaining))
    second = budget.claim()
    seen.append((budget.placed, budget.remaining))
    budget.release(first or 0)
    seen.append((budget.placed, budget.remaining))
    third = budget.claim()
    seen.append((budget.placed, budget.remaining))

    assert (first, second, third) == (3, 3, 3)
    assert seen == [(0, 7), (3, 4), (6, 1), (3, 4), (6, 1)]
    assert all(placed + left == 7 for placed, left in seen)


def test_limit_caps_a_single_claim_without_capping_the_run() -> None:
    """An account allowed only 1 more unit orders 1 — the rest stays available.

    This is the per-account platform allowance: a limit smaller than the run's
    batch size must shrink that one order, not the whole run.
    """
    budget = UnitBudget(10, 4)

    assert budget.claim(limit=1) == 1
    assert budget.remaining == 9
    assert budget.claim(limit=99) == 4  # a limit above per_order changes nothing
    assert budget.claim() == 4
    assert budget.claim(limit=3) == 1  # only 1 left, however generous the limit


def test_a_limit_of_zero_or_less_claims_nothing() -> None:
    """A blocked account must not take units it cannot use."""
    budget = UnitBudget(4, 2)

    assert budget.claim(limit=0) is None
    assert budget.claim(limit=-5) is None
    assert (budget.placed, budget.remaining) == (0, 4)


# --- releasing ----------------------------------------------------------------
def test_release_returns_capacity_for_a_later_claim() -> None:
    """A failed order frees its slot, so another account can still order."""
    budget = UnitBudget(4, 4)

    claimed = budget.claim()
    assert claimed == 4
    assert budget.claim() is None  # nothing left while that order is in flight

    budget.release(claimed)
    assert (budget.placed, budget.remaining) == (0, 4)
    assert budget.claim() == 4  # the next account picks the slot up


def test_releasing_part_of_a_claim_frees_only_that_part() -> None:
    """An order that bought 1 of the 3 units claimed gives 2 back."""
    budget = UnitBudget(3, 3)

    assert budget.claim() == 3
    budget.release(2)

    assert (budget.placed, budget.remaining) == (1, 2)


def test_a_release_of_nothing_is_a_no_op() -> None:
    budget = UnitBudget(4, 2)
    budget.claim()

    budget.release(0)
    budget.release(-3)

    assert (budget.placed, budget.remaining) == (2, 2)


def test_releasing_more_than_was_claimed_cannot_inflate_the_budget() -> None:
    """A double release is a caller bug; it must not license an overshoot."""
    budget = UnitBudget(4, 2)
    claimed = budget.claim()

    budget.release(claimed)
    budget.release(claimed)  # the bug: released twice

    assert (budget.placed, budget.remaining) == (0, 4)
    assert budget.claim() == 2
    assert budget.claim() == 2
    assert budget.claim() is None  # still exactly 4 units, not 6


# --- nonsense sizes -----------------------------------------------------------
def test_a_zero_total_hands_out_nothing() -> None:
    budget = UnitBudget(0, 4)

    assert budget.claim() is None
    assert (budget.placed, budget.remaining) == (0, 0)


def test_nonsense_sizes_are_clamped_rather_than_raised_on() -> None:
    """Called mid-run, so a bad value must not take the whole batch down.

    ``POST /jobs/order`` rejects these before a run starts; this is the backstop
    for a direct caller.
    """
    budget = UnitBudget(-5, 0)
    assert (budget.total, budget.per_order) == (0, 1)
    assert budget.claim() is None

    serial = UnitBudget(2, -1)
    assert serial.per_order == 1  # 0 per order would mean orders of no units
    assert serial.claim() == 1
