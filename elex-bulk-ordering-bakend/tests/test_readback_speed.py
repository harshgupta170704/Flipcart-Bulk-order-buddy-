"""An order page read fast, and the cancellation reason (26 Oct 2026).

Measured live on account 2: an order page took 31.4 s, 30.0 s of it waiting
for an invoice link that an order still on its way does not have — the
browser's own 30 s default, as that one read carried no budget of its own —
and one account's read-back of 20 pages took ten minutes.
"""

import asyncio

import pytest

from app.automation import sync
from app.core.invoices import invoice_api_url
from tests.fakes import FakePage


# --- the invoice ---------------------------------------------------------------------
# Since 27 Nov 2026 the page is not searched for an invoice link at all (it has
# none: "Download Invoice" is a div calling Flipkart's API), so no page read
# waits on one. Every order is pointed at the API: one still "Arriving" already
# holds its product and fee invoices (seen live), a cancelled one credit notes.
@pytest.mark.parametrize(
    "status",
    ["Delivery expected by Nov 01", "Shipped", "Out For Delivery", "Cancelled", "Delivered on Oct 31", None],
)
def test_every_order_is_pointed_at_the_invoice_api(status):
    order = sync.ScrapedOrder(order_id="OD1", platform_status=status)
    asyncio.run(sync._read_detail(FakePage(), order))

    assert order.invoice_url == invoice_api_url("OD1")


# --- why it was cancelled ----------------------------------------------------------------
REASON = "Sorry, your order was cancelled as per seller policy restrictions applicable to this brand."


def _page(*status_lines: str) -> str:
    """An order page's text in the shape seen live on 24–25 Oct 2026 — made-up values."""
    return "\n".join(
        [
            "Explore Plus",
            "My Orders",
            "OD100000000000000001",
            "Some Phone (Black, 64 GB)",
            "Seller: Some Retail",
            "₹10,000",
            "",
            *status_lines,
            "Chat with us",
            "Send Order Details",
            "GST details",
            "GST Number00AAAAA0000A1Z0",
            "Order #OD100000000000000001",
            "Delivery details",
            "Work",
            "1 Example Road, Cancel Street",
            "Price details",
            "Cancellation & Returns",
        ]
    )


def test_the_reason_is_the_line_after_the_cancelled_step():
    assert sync.parse_cancel_reason(_page("Order Received, Oct 24", "Cancelled, Oct 24", REASON)) == REASON
    assert sync.parse_cancel_reason(_page("Order Received, Today, Oct 25", "Cancelled, Today, Oct 25", REASON)) == REASON


def test_an_order_that_was_not_cancelled_has_no_reason():
    # Not the footer's "Cancellation & Returns", nor an address that says "Cancel".
    assert sync.parse_cancel_reason(_page("Order Confirmed, Oct 24", "Shipped, Oct 25")) is None
    assert sync.parse_cancel_reason(None) is None


def test_nothing_is_taken_from_below_the_status_block():
    assert sync.parse_cancel_reason(_page("Order Received, Oct 24", "Cancelled, Oct 24")) is None


def test_a_reason_that_opens_with_cancelled_is_not_taken_for_the_step():
    reason = "Cancelled as per seller policy restrictions applicable to this brand."
    assert sync.parse_cancel_reason(_page("Cancelled, Oct 24", reason)) == reason


def test_cancelled_by_user_is_read_too():
    assert sync.parse_cancel_reason(_page("Order Received, Oct 24", "Cancelled by user - Order shipped")) == (
        "Cancelled by user - Order shipped"
    )


def test_a_long_reason_is_cut_to_what_the_row_keeps():
    long = "Your order was cancelled because " + "x" * 400
    assert len(sync.parse_cancel_reason(_page("Cancelled, Oct 24", long))) == sync.CANCEL_REASON_MAX


def test_a_line_after_the_step_that_is_not_a_reason_is_not_taken():
    assert sync.parse_cancel_reason(_page("Cancelled, Oct 24", "Refund completed", "Chat with us")) is None


def test_help_text_below_the_status_block_is_never_a_reason():
    page = _page("Order Confirmed, Oct 24", "Shipped, Oct 25") + "\nOrders cancelled by user before shipping are refunded"
    assert sync.parse_cancel_reason(page) is None


def test_the_cancelled_step_is_a_short_line():
    assert sync._CANCELLED_STEP_RE.match("Cancelled")
    assert sync._CANCELLED_STEP_RE.match("Cancelled, Oct 24")
    assert sync._CANCELLED_STEP_RE.match("Cancelled, Today, Oct 25")
    assert not sync._CANCELLED_STEP_RE.match(REASON.replace("Sorry, your order was cancelled", "Cancelled"))
    assert not sync._CANCELLED_STEP_RE.match("Cancellation & Returns")


def test_the_order_page_reader_records_the_reason():
    order = sync.ScrapedOrder(order_id="OD1", platform_status="Cancelled")
    page = FakePage(inner_text_map={sync._DETAIL_TEXT: _page("Order Received, Oct 24", "Cancelled, Oct 24", REASON)})

    asyncio.run(sync._read_detail(page, order))

    assert order.cancel_reason == REASON
