"""Money as the operator reads it, in every message the server writes.

The spec's rule (NFR-010): rupees with the ₹ sign, Indian digit grouping and
two decimals — "₹1,29,900.00", "₹18,207.00", "₹0.50". The screens follow it
with the frontend's ``lib/format.formatCurrency`` (the browser's own en-IN
rupee format); this is the same rule for the text the server writes itself —
the alerts, the run messages, the order flow's refusals — so an amount reads
the same in a message as in the table beside it. Log lines are for whoever
reads the server's log and keep their plain figures.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_PAISA = Decimal("0.01")


def _decimal(amount: float | int | Decimal) -> Decimal:
    """The amount as an exact decimal. A float goes through its shortest
    spelling ("1.005", not the binary 1.00499999…), which is what the
    browser rounds too, so the two never disagree by a paisa."""
    if isinstance(amount, Decimal):
        return amount
    if isinstance(amount, int):
        return Decimal(amount)
    return Decimal(repr(float(amount)))


def rupees(amount: float | int | Decimal) -> str:
    """``amount`` as "₹1,29,900.00": the ₹ sign; the last three digits of the
    rupees together and every two before them (thousand, lakh, crore); exactly
    two decimals, a half paisa rounded away from zero like the screens. A
    negative amount reads "-₹500.00".

    Never raises, since a message must not fail over its figure: an amount
    that is not a number at all ("nan"), or past what a decimal holds, is
    written as it is, after the sign.
    """
    try:
        value = _decimal(amount)
        if not value.is_finite():
            return f"₹{amount}"
        value = value.quantize(_PAISA, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return f"₹{amount}"
    whole, paise = f"{abs(value):.2f}".split(".")
    groups = [whole[-3:]]
    rest = whole[:-3]
    while rest:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    sign = "-" if value < 0 else ""
    return f"{sign}₹{','.join(groups)}.{paise}"
