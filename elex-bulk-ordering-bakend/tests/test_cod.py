"""Cash on Delivery: no card anywhere, and the price cap still applies.

COD exists because a run should not need a corporate card at all. The risk it
introduces is the opposite of the card flow's: instead of leaking card data, it
could quietly place an order nobody capped. So these tests pin down both ends —
no card is required or touched, and the §6a target gate still blocks an
over-target total (COD can carry a collection fee that pushes it over).
"""

import asyncio

from app.automation import ordering as order_flow
from app.automation.ordering import (
    PAYMENT_CARD,
    PAYMENT_COD,
    AddressInfo,
    GstInfo,
)
from app.core import selectors as S
from app.db.models import OrderStatus
from tests.fakes import FakePage

ADDR = AddressInfo(name="N", phone="", pin="122016", area="Somewhere")
GST = GstInfo(gstin="", business_name="")


def _run_cod(page, target):
    return asyncio.run(
        order_flow.run_order(
            page,
            product_link="https://flipkart.com/x",
            address=ADDR,
            gst=GST,
            card=None,  # the whole point: no card
            qty=1,
            target=target,
            payment_mode=PAYMENT_COD,
        )
    )


def test_cod_places_an_order_without_any_card():
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹999.00"})
    result = _run_cod(page, target=1000.0)

    assert result["status"] == OrderStatus.placed
    assert result["payment_mode"] == PAYMENT_COD
    # The id comes from Flipkart's own confirmation page.
    assert result["order_id"] == "OD428123456789100"
    # COD was chosen and the order confirmed in the popup...
    assert S.PAY_OPTION_COD in page.clicks
    assert S.COD_CONFIRM_ORDER_BTN in page.clicks
    # ...and no card field was ever touched.
    assert S.CARD_NUMBER not in page.fills
    assert S.CARD_CVV not in page.fills
    assert S.CARD_EXPIRY not in page.fills


def test_cod_still_obeys_the_price_target():
    """A COD collection fee must not sneak past the cap."""
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹1,000.01"})
    result = _run_cod(page, target=1000.0)

    assert result["status"] == OrderStatus.over_target
    assert result["amount"] == 1000.01
    assert result["payment_mode"] == PAYMENT_COD
    assert not page.submitted  # never confirmed


def test_cod_unavailable_fails_rather_than_falling_back_to_a_card():
    """If COD isn't offered, we must NOT silently charge a card instead."""
    # Flipkart greys the option out rather than hiding it — confirmed live
    # 2026-10-23, where COD read "Unavailable" on this handset.
    page = FakePage(
        inner_text_map={S.PAY_OPTION_COD_DISABLED: "Cash on Delivery"}
    )
    result = _run_cod(page, target=1000.0)

    assert result["status"] == OrderStatus.failed
    assert "not available" in result["error"].lower()
    assert not page.submitted
    assert S.CARD_NUMBER not in page.fills


def test_card_mode_without_a_card_fails_instead_of_ordering():
    """The mirror image: a card run with no card must not place an order."""
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹10.00"})
    result = asyncio.run(
        order_flow.run_order(
            page,
            product_link="https://flipkart.com/x",
            address=ADDR,
            gst=GST,
            card=None,
            qty=1,
            target=1000.0,
            payment_mode=PAYMENT_CARD,
        )
    )
    assert result["status"] == OrderStatus.failed
    assert not page.submitted


def test_card_mode_is_unchanged_by_the_cod_work():
    """Regression guard: the original card path still behaves exactly as before."""
    page = FakePage(
        inner_text_map={
            S.ORDER_TOTAL: "₹999.99",
            S.ORDER_ID: "OD1",
            S.ORDER_MODEL: "M",
            S.ORDER_COLOR: "C",
            S.ORDER_AMOUNT: "₹999.99",
        }
    )
    result = asyncio.run(
        order_flow.run_order(
            page,
            product_link="https://flipkart.com/x",
            address=ADDR,
            gst=GST,
            card=order_flow.CardInfo(
                number="4111111111111111", month="12", year="28", cvv="123"
            ),
            qty=1,
            target=1000.0,
            payment_mode=PAYMENT_CARD,
        )
    )
    assert result["status"] == OrderStatus.placed
    assert result["payment_mode"] == PAYMENT_CARD
    assert page.fills[S.CARD_NUMBER] == "4111111111111111"
    assert S.PAY_COD_OPTION not in page.clicks


# --- the real checkout flow, mapped live 2026-10-23 --------------------------
def test_the_run_goes_through_continue_to_the_payment_page():
    """/viewcheckout has no payment controls — they live on /payments.

    The legacy flow looked for payment buttons on the checkout page itself,
    which is why every payment selector timed out. Continue is a required step.
    """
    page = FakePage(
        inner_text_map={
            S.ORDER_TOTAL: "₹999",
            S.ORDER_AMOUNT: "₹999",
            S.ORDER_ID: "OD1",
        }
    )
    result = asyncio.run(
        order_flow.run_order(
            page,
            product_link="https://flipkart.com/x",
            address=ADDR,
            gst=GST,
            card=order_flow.CardInfo(number="4111", month="12", year="28", cvv="123"),
            qty=1,
            target=1000.0,
            payment_mode=PAYMENT_CARD,
        )
    )

    order = page.clicks
    assert S.CHECKOUT_CONTINUE_CTA in order
    assert S.PAY_OPTION_CARD in order
    # Continue happens BEFORE the method is chosen, and both before paying.
    assert order.index(S.CHECKOUT_CONTINUE_CTA) < order.index(S.PAY_OPTION_CARD)
    assert order.index(S.PAY_OPTION_CARD) < order.index(S.CARD_PAY_BTN)
    assert result["status"] == OrderStatus.placed


def test_failing_to_reach_the_payment_page_charges_nothing():
    page = FakePage()
    page.fail_clicks_on = {S.CHECKOUT_CONTINUE_CTA}
    result = asyncio.run(
        order_flow.run_order(
            page,
            product_link="https://flipkart.com/x",
            address=ADDR,
            gst=GST,
            card=order_flow.CardInfo(number="4111", month="12", year="28", cvv="123"),
            qty=1,
            target=1000.0,
        )
    )
    assert result["status"] == OrderStatus.failed
    assert "payment page" in result["error"]
    assert not page.submitted


