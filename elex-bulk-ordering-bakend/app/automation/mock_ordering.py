"""Mock mode: an order run that never opens a browser and buys nothing.

A Mock run goes through everything a real run does before the browser —
account selection, sessions, cards, the unit budget, unit limits — and then,
in place of Flipkart, asks ``mock_order`` how the order "went". Nothing is
written to the orders ledger: a made-up order in the ledger would count
against unit limits, raise alerts and be read back from Flipkart.

The outcome is drawn from a random generator seeded with the run and the
account, so the same run of the same accounts always ends the same way (and a
test can say exactly what it expects), while different accounts get different
outcomes, as they would for real.

Roughly: 10% fail for one of the reasons real runs fail for; of the rest,
about 15% come out over the price cap and are not placed, and the others are
placed. Without a price cap nothing can be over it, so those are placed too.
"""

from __future__ import annotations

import asyncio
import random

from app.db.models import OrderStatus

#: Share of mock orders that fail outright, before any price is read.
FAIL_SHARE = 0.10
#: Final amount as a fraction of the price cap: drawn from this range, so the
#: part above 1.0 is the share that comes out over the cap.
PRICE_RANGE = (0.90, 1.0175)  # 15% of the range lies above the cap
#: A price for a run with no cap, which has nothing to scale from.
NO_CAP_PRICE = (500.0, 1500.0)

#: Reasons real runs fail for, so a mock run reads like one.
FAIL_REASONS = (
    "product out of stock",
    "the session had expired — sign the account in again",
    "the delivery address could not be selected",
    "Flipkart did not offer Cash on Delivery for this pincode",
    "the payment page did not load in time",
)


def _rng(job_id: int, account_id: int) -> random.Random:
    # A string seed: stable across processes and Python versions (hash() is not).
    return random.Random(f"mock:{job_id}:{account_id}")


async def mock_order(
    *,
    job_id: int,
    account_id: int,
    qty: int,
    target: float | None,
    payment_mode: str,
    delay_seconds: float = 0.0,
) -> dict:
    """How a mock order for this account ended, shaped like ``run_order``'s result.

    ``delay_seconds`` is the typical time one mock order takes, so the live
    progress view has something to show; each order takes between half and
    one and a half times it. 0 makes the run instant (the tests).
    """
    rng = _rng(job_id, account_id)
    roll = rng.random()
    price_roll = rng.random()
    pause = delay_seconds * (0.5 + rng.random()) if delay_seconds > 0 else 0.0
    if pause:
        await asyncio.sleep(pause)

    if roll < FAIL_SHARE:
        return {
            "status": OrderStatus.failed,
            "error": rng.choice(FAIL_REASONS),
            "qty": 0,
            "payment_mode": payment_mode,
        }

    if target is None:
        low, high = NO_CAP_PRICE
        amount = round(low + (high - low) * price_roll, 2)
    else:
        low, high = PRICE_RANGE
        amount = round(target * (low + (high - low) * price_roll), 2)

    if target is not None and amount > target:
        return {
            "status": OrderStatus.over_target,
            "amount": amount,
            "target": target,
            "qty": 0,
            "payment_mode": payment_mode,
        }

    return {
        "status": OrderStatus.placed,
        "amount": amount,
        "target": target,
        "qty": qty,
        "order_id": f"MOCK-{job_id}-{account_id}",
        "payment_mode": payment_mode,
    }


def mock_message(result: dict) -> str:
    """The line a mock order leaves on its account's row in the run."""
    status = result.get("status")
    if status == OrderStatus.placed:
        return (
            f"[mock] placed {result.get('qty')} unit(s) at ₹{result['amount']:,.2f} "
            f"— fake order {result['order_id']}, nothing bought"
        )
    if status == OrderStatus.over_target:
        return (
            f"[mock] not placed: ₹{result['amount']:,.2f} is over the cap "
            f"of ₹{result['target']:,.2f}"
        )
    return f"[mock] failed: {result.get('error') or 'no reason given'}"
