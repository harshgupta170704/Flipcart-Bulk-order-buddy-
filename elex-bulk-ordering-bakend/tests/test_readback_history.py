"""The order's dated history, the two delivery dates and what was paid
(mapped live on an order on its way, 26 Oct 2026).

The order page's own timeline names its steps undated ("Order Confirmed",
"Shipped", "Out For Delivery"), so it cannot say which have happened. Its "See
All Updates" link lays the dated history over the page; the price details say
how the order is paid. The lines below are those the live page showed — no
name, address or number of the owner's in them.
"""

import asyncio
from datetime import datetime

from app.automation import sync
from app.jobs import sync_tasks
from tests.test_sync_scrape import _page as _list_page

TODAY = datetime(2026, 10, 26)

#: What "See All Updates" added to a live order page on its way (26 Oct 2026).
LIVE_HISTORY = [
    "✕",
    "Order ConfirmedSun, 25th Oct \'26",
    "Your Order has been placed.",
    "Sun, 25th Oct \'26 - 10:33pm",
    "Seller has processed your order.",
    "Mon, 26th Oct \'26 - 10:15am",
    "Item waiting to be picked up by delivery partner.",
    "Mon, 26th Oct \'26 - 3:00pm",
    "ShippedExpected By Tue 27th Oct",
    "Item yet to be shipped.",
    "Expected by Tue, 27th Oct",
    "Item yet to reach hub nearest to you.",
    "Item yet to be delivered.",
    "DeliveryExpected By Sun 1st Nov",
    "Item yet to be delivered.",
    "Expected by Sun, 1st Nov",
]
#: The same page's price details.
LIVE_PRICES = "\n".join(
    [
        "Price details", "Listing price", "₹1,599", "Special price", "₹248", "Total fees", "₹7",
        "Other discount", "−₹20", "Total amount", "₹235", "Paid By", "Cash On Delivery", "Offers earned",
        "ABOUT", "Contact Us",
    ]
)


# --- the history ---------------------------------------------------------------------
def test_the_live_history_dates_what_happened_and_leaves_the_rest_to_come():
    steps, promised, delivered = sync.parse_history(LIVE_HISTORY, today=TODAY)

    assert steps == [("Order Confirmed", datetime(2026, 10, 25)), ("Shipped", None), ("Delivery", None)]
    assert promised == datetime(2026, 11, 1)
    assert delivered is None


def test_a_delivered_history_gives_the_day_it_was_delivered():
    lines = [
        "Order ConfirmedSun, 25th Oct \'26",
        "ShippedTue, 27th Oct \'26",
        "Out For DeliveryThu, 29th Oct \'26",
        "DeliveredThu, 29th Oct \'26",
    ]
    steps, promised, delivered = sync.parse_history(lines, today=TODAY)

    assert [name for name, _ in steps] == ["Order Confirmed", "Shipped", "Out For Delivery", "Delivered"]
    assert all(when is not None for _, when in steps)
    assert delivered == datetime(2026, 10, 29)
    assert promised is None


def test_event_lines_are_never_steps():
    steps, _, _ = sync.parse_history(
        ["Item yet to be delivered.", "Your Order has been placed.", "Expected by Sun, 1st Nov", "Delivery Executive details will be available"],
        today=TODAY,
    )
    assert steps == []


def test_a_later_line_naming_a_step_does_not_repeat_it():
    steps, _, _ = sync.parse_history(
        ["ShippedTue, 27th Oct \'26", "Shipped via the courier, Tue 27th Oct"], today=TODAY
    )
    assert steps == [("Shipped", datetime(2026, 10, 27))]


# --- what was paid ----------------------------------------------------------------------
def test_the_live_price_details_give_the_total_and_how_it_is_paid():
    assert sync.parse_price_details(LIVE_PRICES) == (235.0, "Cash On Delivery")


def test_a_page_without_price_details_gives_nothing():
    assert sync.parse_price_details("Order Confirmed\nShipped") == (None, None)
    assert sync.parse_price_details(None) == (None, None)


def test_a_paid_by_with_nothing_after_it_is_not_read_from_the_footer():
    assert sync.parse_price_details("Price details\nTotal amount\n₹235\nPaid By\nOffers earned\nABOUT") == (235.0, None)


# --- the two delivery dates off the list ---------------------------------------------------
def test_the_list_keeps_the_delivered_day_apart_from_the_promised_one():
    orders = asyncio.run(sync.scrape_orders(_list_page(), open_details=False))
    delivered, out_for_delivery, cancelled = orders

    assert delivered.delivered_date == datetime(2018, 10, 31)
    assert delivered.promised_date is None
    assert out_for_delivery.delivered_date is None
    assert cancelled.promised_date is None and cancelled.delivered_date is None


# --- pressing the history open ------------------------------------------------------------------
class _Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    def nth(self, _index):
        return self

    async def count(self):
        if self.selector == "link":
            return 1 if self.page.history is not None else 0
        return 1 if self.selector == "xpath=//body" else 0

    async def inner_text(self, timeout=None):
        if self.selector == "xpath=//body":
            return self.page.body
        raise TimeoutError(self.selector)

    async def wait_for(self, state=None, timeout=None):
        raise TimeoutError(self.selector)

    async def get_attribute(self, name, timeout=None):
        raise TimeoutError(self.selector)

    async def evaluate(self, script):
        assert self.selector == "link", "only the history's link is ever pressed"
        self.page.pressed += 1
        self.page.body += "\n" + "\n".join(self.page.history)


class _OrderPage:
    """An order page whose 'See All Updates' lays ``history`` over it."""

    url = "https://www.flipkart.com/order_details?order_id=OD1"

    def __init__(self, body, history=None):
        self.body, self.history, self.pressed, self.asked_for = body, history, 0, []

    def get_by_text(self, text, exact=False):
        self.asked_for.append((text, exact))
        return _Locator(self, "link")

    def locator(self, selector):
        return _Locator(self, selector)

    async def wait_for_timeout(self, _ms):
        return None


def test_the_history_is_opened_by_its_own_link_and_read():
    page = _OrderPage("Order Confirmed\nShipped\nCancel", LIVE_HISTORY)

    added = asyncio.run(sync._history_lines(page))

    assert page.asked_for == [("See All Updates", True)]  # exactly that link, nothing else
    assert page.pressed == 1
    assert "DeliveryExpected By Sun 1st Nov" in added
    assert "Order Confirmed" not in added  # the page's own lines are not the history's


def test_a_page_without_the_link_is_left_alone():
    page = _OrderPage("Order Confirmed")
    assert asyncio.run(sync._history_lines(page)) == []
    assert page.pressed == 0


def test_a_page_that_cannot_open_it_costs_nothing_but_the_history():
    class _Bare:
        url = _OrderPage.url

    assert asyncio.run(sync._history_lines(_Bare())) == []


def test_the_order_page_reader_records_the_history_dates_and_payment():
    order = sync.ScrapedOrder(order_id="OD1", platform_status="Delivery expected")
    page = _OrderPage("Order Confirmed\nShipped\nOut For Delivery\n" + LIVE_PRICES, LIVE_HISTORY)

    asyncio.run(sync._read_detail(page, order))

    assert order.timeline[0] == {"status": "Order Confirmed", "date": datetime(2026, 10, 25).isoformat()}
    assert [step["date"] for step in order.timeline[1:]] == [None, None]
    assert order.promised_date.month == 11 and order.promised_date.day == 1
    assert order.delivered_date is None
    assert (order.amount_paid, order.payment_method) == (235.0, "Cash On Delivery")


# --- they reach the ledger as dates and a number -----------------------------------------------------
def test_the_new_fields_are_observed_as_days_and_an_amount():
    when = datetime(2026, 11, 1)
    observation = sync.ScrapedOrder(promised_date=when, delivered_date="2026-10-29T00:00:00", amount_paid=235.0, payment_method=" Cash On Delivery ")

    assert sync_tasks._observed(observation, "promised_date") == when
    assert sync_tasks._observed(observation, "delivered_date") == datetime(2026, 10, 29)
    assert sync_tasks._observed(observation, "amount_paid") == 235.0
    assert sync_tasks._observed(observation, "payment_method") == "Cash On Delivery"
    assert sync_tasks._observed(sync.ScrapedOrder(), "amount_paid") is None
