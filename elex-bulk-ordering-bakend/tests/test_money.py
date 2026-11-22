"""Money in the server's own messages (spec NFR-010): rupees with the ₹ sign,
Indian digit grouping and two decimals, the way every screen shows it
(frontend lib/format.formatCurrency) — "₹1,29,900.00", never "₹129,900" or a
bare "129900.00"."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.money import rupees


@pytest.mark.parametrize(
    ("amount", "shown"),
    [
        (129900, "₹1,29,900.00"),
        (18207.0, "₹18,207.00"),
        (0.5, "₹0.50"),
        (0, "₹0.00"),
        (999, "₹999.00"),
        (1000, "₹1,000.00"),
        (100000, "₹1,00,000.00"),
        (12345678.9, "₹1,23,45,678.90"),
        (1000000000, "₹1,00,00,00,000.00"),
        (43219.5, "₹43,219.50"),
        (Decimal("89447"), "₹89,447.00"),
    ],
)
def test_rupees_are_grouped_the_indian_way_with_two_decimals(amount, shown):
    assert rupees(amount) == shown


def test_a_half_paisa_rounds_away_from_zero_like_the_screens():
    """The browser rounds a float's shortest spelling half away from zero, so
    1.005 shows as ₹1.01 there; here too, not ₹1.00 from the binary
    1.00499999…."""
    assert rupees(1.005) == "₹1.01"
    assert rupees(0.125) == "₹0.13"
    assert rupees(18206.994) == "₹18,206.99"
    assert rupees(18206.995) == "₹18,207.00"


def test_a_negative_amount_carries_its_sign_before_the_rupee():
    assert rupees(-129900) == "-₹1,29,900.00"
    assert rupees(-0.001) == "₹0.00"  # rounds to nothing: no minus on a zero


def test_a_figure_that_is_not_a_number_never_raises():
    assert rupees(float("nan")) == "₹nan"
    assert rupees(float("inf")) == "₹inf"
