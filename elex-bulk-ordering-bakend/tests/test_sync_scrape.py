"""Tests for the Phase 10 order-state scrape, over a fake page (no browser).

The scrape exists because two things went wrong live, and both are pinned here:

* the detail selectors were read on the orders LIST, where they match nothing,
  so every order recorded blanks;
* the email reader once returned a six-digit run from unrelated text as an OTP.

So: detail fields must only come from an order's own page, and digits only count
as a delivery OTP when they sit beside OTP wording.
"""

import asyncio
from datetime import datetime
from urllib.parse import urljoin

from app.automation import sync
from app.core import selectors as S
from tests.fakes import FakeLocator, FakePage

LOGIN_PAGE = "https://www.flipkart.com/login?ret=%2Faccount%2Forders"


# --- fake page ----------------------------------------------------------------
class _ScopedLocator(FakeLocator):
    """FakeLocator whose relative lookups stay scoped to their row.

    ``tests.fakes.FakeLocator.locator()`` drops the parent xpath, which is fine
    for a single-row flow but would make every listed order read the same text.
    The sync reads several rows in one pass, so the xpaths are composed here.
    """

    def nth(self, index: int) -> "_ScopedLocator":
        return _ScopedLocator(self.page, f"{self.xpath}[{index}]")

    def locator(self, selector: str) -> "_ScopedLocator":
        xpath = selector.split("xpath=", 1)[1] if "xpath=" in selector else selector
        return _ScopedLocator(self.page, f"{self.xpath}{xpath}")


class _SyncPage(FakePage):
    """FakePage that serves a different text map per URL.

    The scrape reads the list page and then each order's own page, and the whole
    point is that those two pages carry different things — one shared text map
    could not tell them apart.
    """

    def __init__(self, pages: dict[str, dict[str, str]]) -> None:
        super().__init__(inner_text_map=pages.get(S.ORDERS_URL, {}))
        self.pages = pages
        #: URLs whose navigation should blow up (a detail page that fails).
        self.fail_goto_on: set[str] = set()
        #: requested URL -> where we actually land (a redirect racing us).
        self.redirect_on: dict[str, str] = {}

    async def goto(self, url: str, wait_until: str | None = None) -> None:
        self.goto_urls.append(url)
        if url in self.fail_goto_on:
            raise RuntimeError(f"net::ERR_ABORTED at {url}")
        self.url = self.redirect_on.get(url, self.redirect_to or url)
        self.inner_text_map = self.pages.get(self.url, {})

    def locator(self, selector: str) -> _ScopedLocator:
        xpath = selector.split("xpath=", 1)[1] if "xpath=" in selector else selector
        return _ScopedLocator(self, xpath)


def _row_key(index: int, relative_xpath: str) -> str:
    """Text-map key for one field of one row, as _ScopedLocator composes it."""
    return f"{S.ORDER_ROW}[{index}]{relative_xpath}"


def _detail_url(href: str) -> str:
    return urljoin(S.ORDERS_URL, href)


# Three orders, deliberately uneven: a delivered one, one still out for
# delivery (the only state that shows a delivery OTP), and a cancelled one.
HREF_0 = "/order_details?order_id=OD111&item_id=IT111&unit_id=UN111&sellerId=XYZ"
HREF_1 = "order_details?unit_id=UN222&order_id=OD222"  # relative, reordered, no item_id
HREF_2 = "/order_details?order_id=OD333&item_id=IT333&unit_id=UN333"

LIST_TEXT = {
    _row_key(0, S.ORDER_MODEL_IN_ROW): "Realme 2 (Diamond Blue, 64 GB)",
    _row_key(0, S.ORDER_AMOUNT_IN_ROW): "₹9,891",
    _row_key(0, S.ORDER_STATUS_IN_ROW): "Delivered on Oct 31, 2018",
    _row_key(0, S.ORDER_COLOR_IN_ROW): "Diamond Blue",
    # Row 1 shows no model: the detail page has to fill that gap.
    _row_key(1, S.ORDER_AMOUNT_IN_ROW): "₹1,299",
    _row_key(1, S.ORDER_STATUS_IN_ROW): "Out for Delivery",
    _row_key(2, S.ORDER_MODEL_IN_ROW): "Mi Power Bank 3i",
    _row_key(2, S.ORDER_AMOUNT_IN_ROW): "₹1,099",
    _row_key(2, S.ORDER_STATUS_IN_ROW): "Cancelled",
    # Detail selectors DO sit in this map, with wrong values, because on the
    # real list page they match zero elements. Anything reading them here is
    # the original blanks-everywhere bug, and would pick these up.
    S.ORDER_ID: "OD-READ-OFF-THE-LIST",
    S.ORDER_SELLER: "Seller: Wrong Seller From The List",
    S.ORDER_AMOUNT: "₹1",
    S.ORDER_MODEL: "Wrong Model From The List",
}

DETAIL_0 = {
    S.ORDER_ID: "OD113741449375753000",  # href wins over this
    S.ORDER_MODEL: "Realme 2 (Diamond Blue, 64 GB)",
    S.ORDER_COLOR: "Diamond Blue",
    S.ORDER_AMOUNT: "₹9,891",
    S.ORDER_SELLER: "Seller: Flashtech Retail",
    # A delivered order shows no OTP. The six-digit policy number must not be
    # mistaken for one.
    "//body": (
        "Order OD113741449375753000 Delivered on Oct 31, 2018 "
        "Total ₹9,891 Return policy 568901 Seller: Flashtech Retail"
    ),
}

DETAIL_1 = {
    S.ORDER_MODEL: "boAt Rockerz 255 Pro+",
    S.ORDER_COLOR: "Active Black",
    # Higher than the row: the detail page carries the Protect Promise Fee, and
    # that is the figure the operator reconciles against.
    S.ORDER_AMOUNT: "₹1,349",
    S.ORDER_SELLER: "Seller: RetailNet",
    f"{S.ORDER_TIMELINE_STEPS}[0]": "Order Confirmed, Oct 20, 2018",
    f"{S.ORDER_TIMELINE_STEPS}[1]": "Shipped on Oct 22, 2018",
    # Undated => has NOT happened yet. Must never become the status.
    f"{S.ORDER_TIMELINE_STEPS}[2]": "Delivered",
    "//body": (
        "Out for Delivery Delivery OTP 4827 Share this code with the "
        "delivery agent. Total ₹1,349"
    ),
}


def _page(*, counts: int = 3) -> _SyncPage:
    page = _SyncPage(
        {
            S.ORDERS_URL: LIST_TEXT,
            _detail_url(HREF_0): DETAIL_0,
            _detail_url(HREF_1): DETAIL_1,
            # A cancelled order: its own page shows the seller and nothing else.
            _detail_url(HREF_2): {S.ORDER_SELLER: "Seller: Cloudtail India"},
        }
    )
    page.counts[S.ORDER_ROW] = counts
    page.counts[S.ORDER_TIMELINE_STEPS] = 3
    for index, href in enumerate((HREF_0, HREF_1, HREF_2)):
        page.attrs[(f"{S.ORDER_ROW}[{index}]", "href")] = href
    return page


def _scrape(page: _SyncPage, **kwargs) -> list[sync.ScrapedOrder]:
    return asyncio.run(sync.scrape_orders(page, **kwargs))


# --- parse_order_ids ----------------------------------------------------------
def test_parse_order_ids_reads_all_three_ids_from_the_href():
    """The link carries the same three ids the operator's portal reports."""
    ids = sync.parse_order_ids(HREF_0)
    assert ids == {"order_id": "OD111", "item_id": "IT111", "unit_id": "UN111"}


def test_parse_order_ids_tolerates_order_and_extra_params():
    ids = sync.parse_order_ids(
        "/order_details?unit_id=UN9&foo=bar&order_id=OD9&utm_source=email&item_id=IT9"
    )
    assert ids == {"order_id": "OD9", "item_id": "IT9", "unit_id": "UN9"}


def test_parse_order_ids_leaves_missing_ids_as_none():
    ids = sync.parse_order_ids(HREF_1)  # no item_id
    assert ids == {"order_id": "OD222", "item_id": None, "unit_id": "UN222"}

    # A blank value is missing, not an empty-string id.
    assert sync.parse_order_ids("/order_details?order_id=&item_id=IT1")["order_id"] is None


def test_parse_order_ids_on_junk_returns_all_none():
    for junk in ("", "not a url at all", "https://www.flipkart.com/account/orders"):
        assert sync.parse_order_ids(junk) == {
            "order_id": None,
            "item_id": None,
            "unit_id": None,
        }


# --- parse_status_line --------------------------------------------------------
TODAY = datetime(2026, 10, 23)


def test_parse_status_line_month_first_with_year():
    assert sync.parse_status_line("Delivered on Oct 31, 2018", today=TODAY) == (
        "Delivered",
        datetime(2018, 10, 31),
    )


def test_parse_status_line_day_first_with_year():
    assert sync.parse_status_line("Delivered on 31 Oct 2018", today=TODAY) == (
        "Delivered",
        datetime(2018, 10, 31),
    )


def test_parse_status_line_cancelled_today_is_cancelled():
    """Seen live 2026-10-24: "Cancelled Today, Oct 24" synced as the status
    "Cancelled Today", which is not "Cancelled", so two cancelled orders stayed
    recorded as placed. The relative day is a tail word, not part of the status."""
    status, when = sync.parse_status_line("Cancelled Today, Oct 24", today=TODAY)
    assert status == "Cancelled"
    assert sync.is_cancelled(status) is True
    assert (when.month, when.day) == (10, 24)


def test_parse_status_line_reads_a_relative_day_as_the_date():
    from datetime import timedelta

    midnight = TODAY.replace(hour=0, minute=0, second=0, microsecond=0)
    assert sync.parse_status_line("Arriving Tomorrow", today=TODAY) == (
        "Arriving",
        midnight + timedelta(days=1),
    )
    assert sync.parse_status_line("Delivered Yesterday", today=TODAY) == (
        "Delivered",
        midnight - timedelta(days=1),
    )
    assert sync.parse_status_line("Cancelled Today", today=TODAY) == ("Cancelled", midnight)


def test_parse_status_line_drops_the_weekday_from_an_eta():
    """"Arriving by Mon Oct 28" -> status "Arriving", with that date."""
    assert sync.parse_status_line("Arriving by Mon Oct 28", today=TODAY) == (
        "Arriving",
        datetime(2026, 10, 28),
    )


def test_parse_status_line_year_less_date_falls_back_a_year():
    """A year-less date landing far in the future belongs to last year.

    Flipkart drops the year on nearby dates, so "Dec 28" read in early January
    is last December's delivery — not one eleven months away.
    """
    assert sync.parse_status_line(
        "Delivered on Dec 28", today=datetime(2026, 1, 5)
    ) == ("Delivered", datetime(2025, 12, 28))


def test_parse_status_line_keeps_a_near_future_eta_in_this_year():
    assert sync.parse_status_line("Arriving by 2 Nov", today=TODAY) == (
        "Arriving",
        datetime(2026, 11, 2),
    )


def test_parse_status_line_without_a_date_keeps_the_status():
    assert sync.parse_status_line("Out for Delivery", today=TODAY) == (
        "Out for Delivery",
        None,
    )
    assert sync.parse_status_line("Cancelled", today=TODAY) == ("Cancelled", None)


def test_parse_status_line_unparseable_date_never_raises():
    # An impossible date, a year mistaken for a day, and nothing.
    assert sync.parse_status_line("Delivered on Feb 30, 2018") == ("Delivered", None)
    assert sync.parse_status_line("Delivered in May 2018")[1] is None
    assert sync.parse_status_line("") == (None, None)
    # A relative day is a date now, not part of the status (it once left
    # "Cancelled Today" unrecognised as cancelled). Case does not matter.
    from datetime import timedelta

    assert sync.parse_status_line("Arriving tomorrow", today=TODAY) == (
        "Arriving",
        TODAY.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
    )


def test_parse_status_line_date_only_line_has_no_status():
    assert sync.parse_status_line("Oct 31, 2018") == (None, datetime(2018, 10, 31))


# --- delivery OTP -------------------------------------------------------------
def test_a_bare_number_is_not_taken_as_a_delivery_otp():
    """The email reader was burnt by exactly this: digits with no OTP label.

    A detail page is full of numbers — amounts, pincodes, policy numbers, order
    ids — and none of them is a delivery code.
    """
    assert sync.parse_delivery_otp("Delivered on Oct 31, 2018 Return policy 568901") is None
    assert sync.parse_delivery_otp("Total ₹9891 Seller: Flashtech Retail") is None
    assert sync.parse_delivery_otp("Delivery address 560103 Bengaluru") is None
    assert sync.parse_delivery_otp("") is None


def test_a_labelled_code_is_read():
    assert sync.parse_delivery_otp("Delivery OTP 4827 Share with the agent") == "4827"
    assert sync.parse_delivery_otp("OTP: 123456") == "123456"
    assert sync.parse_delivery_otp("Delivery code 9981") == "9981"
    # Code ahead of its label.
    assert sync.parse_delivery_otp("483910 is your one-time password") == "483910"


def test_an_amount_beside_otp_wording_is_not_a_code():
    assert sync.parse_delivery_otp("OTP sent. Amount payable ₹1349") is None
    # ...but a plain number near the label still reads as the code.
    assert sync.parse_delivery_otp("OTP sent. 1349") == "1349"


def test_a_long_id_near_otp_wording_is_not_a_code():
    """An order id is one long digit run, not a 4-8 digit code."""
    assert sync.parse_delivery_otp("Share OTP OD113741449375753000") is None


# --- platform states ----------------------------------------------------------
def test_delivered_and_cancelled_states_are_consulted_not_mapped():
    assert sync.is_delivered("Delivered") is True
    assert sync.is_delivered("delivered") is True
    assert sync.is_cancelled("Cancelled") is True
    # A state nobody has seen before stays unrecognised rather than guessed.
    assert sync.is_delivered("Out for Delivery") is False
    assert sync.is_delivered("Partially Delivered") is False
    assert sync.is_cancelled("Cancellation Requested") is False
    assert sync.is_delivered(None) is False
    assert "delivered" in sync.DELIVERED_STATES
    assert "cancelled" in sync.CANCELLED_STATES


# --- scrape_orders ------------------------------------------------------------
def test_scrape_orders_lands_each_field_on_the_right_order():
    page = _page()
    orders = _scrape(page)

    assert [o.order_id for o in orders] == ["OD111", "OD222", "OD333"]
    assert [o.unit_id for o in orders] == ["UN111", "UN222", "UN333"]
    assert [o.item_id for o in orders] == ["IT111", None, "IT333"]

    first, second, third = orders

    # Row-level fields stay with their own row.
    assert first.model == "Realme 2 (Diamond Blue, 64 GB)"
    assert first.color == "Diamond Blue"
    assert first.platform_status == "Delivered"
    assert first.delivery_date == datetime(2018, 10, 31)
    assert first.amount == 9891.0
    assert first.detail_href == HREF_0
    # Detail-only fields come from the order's own page.
    assert first.seller == "Flashtech Retail"
    assert first.delivery_otp is None  # delivered orders show no code

    # The detail page fills what the row did not show...
    assert second.model == "boAt Rockerz 255 Pro+"
    assert second.color == "Active Black"
    assert second.seller == "RetailNet"
    # ...and wins on amount, because it includes the Protect Promise Fee.
    assert second.amount == 1349.0
    assert second.delivery_otp == "4827"

    assert third.platform_status == "Cancelled"
    assert third.delivery_date is None
    assert third.seller == "Cloudtail India"
    # Its own page shows no amount, so the row's figure stands.
    assert third.amount == 1099.0


def test_scrape_orders_opens_each_order_on_its_own_page():
    page = _page()
    _scrape(page)

    assert page.goto_urls[0] == S.ORDERS_URL
    for href in (HREF_0, HREF_1, HREF_2):
        assert _detail_url(href) in page.goto_urls
    # ...and leaves the browser back on the list it started from.
    assert page.goto_urls[-1] == S.ORDERS_URL


def test_detail_fields_are_never_read_off_the_list_page():
    """The live bug: ORDER_ID/MODEL/COLOR/AMOUNT match 0 elements on the list.

    The list text map here answers those selectors with obvious junk, so any
    read that happens before navigating to the order's own page shows up.
    """
    page = _page()
    orders = _scrape(page)

    for order in orders:
        assert order.order_id != "OD-READ-OFF-THE-LIST"
        assert order.seller != "Wrong Seller From The List"
        assert order.model != "Wrong Model From The List"
        assert order.amount != 1.0


def test_an_undated_timeline_step_is_not_reported_as_the_status():
    """Flipkart dates the steps that happened and leaves future ones blank.

    Order 2 is "Out for Delivery" with an undated "Delivered" step on its
    timeline. Taking the last step would mark it delivered and close it.
    """
    page = _page()
    second = _scrape(page)[1]

    assert second.platform_status == "Out for Delivery"  # raw, not flattened
    # The date comes from the last step that actually carries one.
    assert second.delivery_date == datetime(2018, 10, 22)


def test_the_order_pages_timeline_is_kept_step_by_step():
    """The owner's ask of 26 Oct 2026: not just the latest state but the order's
    history — every step the page shows, dated when it has happened."""
    orders = _scrape(_page())
    assert orders[1].timeline == [
        {"status": "Order Confirmed", "date": "2018-10-20T00:00:00"},
        {"status": "Shipped", "date": "2018-10-22T00:00:00"},
        {"status": "Delivered", "date": None},
    ]
    # A page with no timeline leaves the field alone rather than writing an empty list.
    assert orders[2].timeline is None


def test_the_timeline_fallback_uses_the_last_DATED_step():
    """With no status on the row, the timeline fills it — but carefully.

    The last step on the page reads "Delivered" and carries no date, so it has
    not happened yet. Reporting it would close an order still in transit.
    """
    href = "/order_details?order_id=OD444&item_id=IT444&unit_id=UN444"
    page = _SyncPage(
        {
            S.ORDERS_URL: {_row_key(0, S.ORDER_MODEL_IN_ROW): "Nokia 105"},
            _detail_url(href): {
                f"{S.ORDER_TIMELINE_STEPS}[0]": "Order Confirmed on Oct 20, 2018",
                f"{S.ORDER_TIMELINE_STEPS}[1]": "Shipped on Oct 22, 2018",
                f"{S.ORDER_TIMELINE_STEPS}[2]": "Delivered",
            },
        }
    )
    page.counts[S.ORDER_ROW] = 1
    page.counts[S.ORDER_TIMELINE_STEPS] = 3
    page.attrs[(f"{S.ORDER_ROW}[0]", "href")] = href

    order = _scrape(page)[0]
    assert order.platform_status == "Shipped"
    assert order.delivery_date == datetime(2018, 10, 22)


def test_an_invoice_link_is_absolute_when_present_and_none_when_not():
    page = _page()
    page.attrs[(sync._INVOICE_LINK, "href")] = "/invoice/OD111.pdf"

    assert _scrape(page)[0].invoice_url == "https://www.flipkart.com/invoice/OD111.pdf"
    # No link at all (none was visible on a 2017-era order) -> left as None
    # rather than guessed at.
    assert all(o.invoice_url is None for o in _scrape(_page()))


def test_an_amount_with_extra_digits_is_not_concatenated():
    """"₹9,891 · 2 items" is 9891, not 98912."""
    assert sync._amount_or_none("₹9,891 · 2 items") == 9891.0
    assert sync._amount_or_none("₹1,349.50") == 1349.5
    # Unreadable is None, never 0.0 — a real order is not free.
    assert sync._amount_or_none("No price shown") is None
    assert sync._amount_or_none("") is None


def test_a_failing_detail_page_still_leaves_the_other_rows_scraped():
    page = _page()
    page.fail_goto_on = {_detail_url(HREF_1)}
    orders = _scrape(page)

    assert len(orders) == 3
    # The broken one keeps everything its row showed.
    broken = orders[1]
    assert broken.order_id == "OD222"
    assert broken.amount == 1299.0  # row value, since the detail never loaded
    assert broken.platform_status == "Out for Delivery"
    assert broken.seller is None
    assert broken.delivery_otp is None
    # Its neighbours are complete.
    assert orders[0].seller == "Flashtech Retail"
    assert orders[2].order_id == "OD333"


def test_a_redirect_racing_the_detail_page_keeps_row_level_data():
    """A redirect can land us somewhere that is not an order page.

    Reading the detail selectors on whatever page we arrive at is how blanks
    got recorded, so the landing URL is checked before anything is read.
    """
    page = _page()
    page.redirect_on = {_detail_url(HREF_0): "https://www.flipkart.com/"}
    # The page we get bounced to answers the detail selectors, so a read that
    # skips the landing-URL check picks this up instead of failing quietly.
    page.pages["https://www.flipkart.com/"] = {
        S.ORDER_SELLER: "Seller: Wrong Page Entirely",
        S.ORDER_MODEL: "Wrong Model From The Home Page",
    }
    first = _scrape(page)[0]

    assert first.seller is None  # nothing was read off the wrong page
    assert first.model == "Realme 2 (Diamond Blue, 64 GB)"  # row data survives
    assert first.platform_status == "Delivered"


def test_open_details_false_reads_rows_only_and_never_navigates():
    page = _page()
    orders = _scrape(page, open_details=False)

    assert page.goto_urls == [S.ORDERS_URL]  # the list, and nowhere else
    assert [o.order_id for o in orders] == ["OD111", "OD222", "OD333"]
    assert orders[0].model == "Realme 2 (Diamond Blue, 64 GB)"
    assert orders[0].platform_status == "Delivered"
    # Detail-only fields are simply absent.
    assert all(o.seller is None for o in orders)
    assert all(o.delivery_otp is None for o in orders)
    assert orders[1].model is None


def test_limit_is_respected():
    page = _page()
    orders = _scrape(page, limit=2)

    assert [o.order_id for o in orders] == ["OD111", "OD222"]
    # The third order is never opened.
    assert _detail_url(HREF_2) not in page.goto_urls

    assert _scrape(_page(), limit=0) == []


def test_a_login_bounce_is_a_sign_out_not_an_empty_account():
    """An expired session is redirected to login, where no selector matches.
    That is reported as "not signed in", never as a clean account with no
    orders (audit F-8, 25 Oct 2026): a dead session silently stopping every
    read-back while the screens stay green is the worse outcome."""
    page = _page()
    page.redirect_to = LOGIN_PAGE

    try:
        _scrape(page)
    except sync.NotSignedIn as exc:
        assert "not signed in" in str(exc)
    else:
        raise AssertionError("a login bounce must be reported as not signed in")
    assert page.goto_urls == [S.ORDERS_URL]  # gave up instead of reading junk


def test_an_empty_orders_list_is_not_an_error():
    page = _page(counts=0)
    assert _scrape(page) == []


# --- the delivery address (spec FR-044/045) ------------------------------------
#: A delivered order's page on 25 Oct 2026, once "Delivery details" was opened:
#: the type, the address on one line, the name and the phone, then the next block.
ADDRESS_PAGE = (
    "Order #OD113741449375753000\n"
    "Delivery details\n"
    "Work\n"
    "A-39, Nucleus Software Sector 62, Near Jaypee College Noida Uttar Pradesh 201307\n"
    "Akansha Agarwal\n"
    "8957768866\n"
    "Price details\n"
    "Paid ₹9,891 by Debit Card\n"
)


def test_parse_delivery_address_reads_the_block_and_its_pincode():
    address, pincode = sync.parse_delivery_address(ADDRESS_PAGE)
    assert address == (
        "Work · A-39, Nucleus Software Sector 62, Near Jaypee College Noida Uttar Pradesh 201307"
        " · Akansha Agarwal · 8957768866"
    )
    assert pincode == "201307"


def test_parse_delivery_address_says_nothing_without_the_block():
    """No heading, or a heading with nothing under it: no address, no guess."""
    assert sync.parse_delivery_address(None) == (None, None)
    assert sync.parse_delivery_address("Order OD1 Delivered on Oct 31, 2018 Total ₹9,891") == (None, None)
    assert sync.parse_delivery_address("Delivery details\nPrice details\nPaid ₹1") == (None, None)
    # A phone's digits are never mistaken for a pincode, and a block with no
    # six-digit run has none.
    address, pincode = sync.parse_delivery_address("Delivery details\nHome\nSomewhere\n8957768866\nABOUT")
    assert address == "Home · Somewhere · 8957768866"
    assert pincode is None


def test_the_detail_page_opens_the_address_block_and_keeps_what_it_shows():
    page = _page()
    page.pages[_detail_url(HREF_0)] = {**DETAIL_0, "//body": DETAIL_0["//body"] + "\n" + ADDRESS_PAGE}
    orders = _scrape(page)
    first = next(o for o in orders if o.order_id == "OD111")
    assert first.delivery_pincode == "201307"
    assert first.delivery_address.startswith("Work · A-39, Nucleus Software")
    assert first.delivery_otp is None  # the policy number is still not an OTP
    # The other pages show no block: nothing is invented for them.
    assert all(o.delivery_address is None for o in orders if o.order_id != "OD111")
    # A heading whose toggle says nothing about its state is clicked open.
    assert S.DELIVERY_DETAILS_HEADING in page.clicks


def test_an_open_address_block_is_not_folded_shut_by_the_reveal():
    """Ayush's cancelled orders (25 Oct 2026): the section header is a toggle
    that starts open (``aria-expanded="true"``), with nothing under it. A click
    there would fold the block shut on a page where it holds the address."""
    page = _page()
    page.pages[_detail_url(HREF_0)] = {**DETAIL_0, "//body": DETAIL_0["//body"] + "\n" + ADDRESS_PAGE}
    page.attrs[(sync.toggle_of(S.DELIVERY_DETAILS_HEADING), "aria-expanded")] = "true"
    orders = _scrape(page)
    first = next(o for o in orders if o.order_id == "OD111")
    assert first.delivery_pincode == "201307"
    assert S.DELIVERY_DETAILS_HEADING not in page.clicks

    # A toggle that says it is closed is opened, like one that says nothing.
    page = _page()
    page.pages[_detail_url(HREF_0)] = {**DETAIL_0, "//body": DETAIL_0["//body"] + "\n" + ADDRESS_PAGE}
    page.attrs[(sync.toggle_of(S.DELIVERY_DETAILS_HEADING), "aria-expanded")] = "false"
    _scrape(page)
    assert S.DELIVERY_DETAILS_HEADING in page.clicks


def test_a_cancelled_orders_empty_address_block_records_nothing():
    """What both of Ayush's cancelled orders showed live on 25 Oct 2026: the
    heading, then straight on to the price block. No address, no pincode."""
    cancelled_page = (
        "Order #OD338967210239333100\nDelivery details\nPrice details\n"
        "Listing price\n₹30,999\nTotal amount\n₹18,207\nPaid By\nCash On Delivery\n"
    )
    assert sync.parse_delivery_address(cancelled_page) == (None, None)
    assert sync.parse_delivery_otp(cancelled_page) is None


# --- the GST the order was billed under (the owner's addition, 26 Oct 2026) ------
#: Ayush's cancelled order page on 25 Oct 2026, as the browser's text read it:
#: the label and the number share a line, the business name has its own.
GST_PAGE = (
    "Send Order Details\n"
    "GST details\n"
    "GST Number09AAUFT2204P1ZI\n"
    "TRANSPARENT DEALS\n"
    "Order #OD338967106334306100\n"
    "Delivery details\n"
    "Price details\n"
)


def test_parse_gst_details_reads_the_gstin_and_the_business_name():
    assert sync.parse_gst_details(GST_PAGE) == ("09AAUFT2204P1ZI", "TRANSPARENT DEALS")
    # The number on its own line, and a name followed straight by the next block.
    assert sync.parse_gst_details("GST details\nGST Number\n27AAPFU0939F1ZV\nACME TRADERS\nPrice details\n") == (
        "27AAPFU0939F1ZV",
        "ACME TRADERS",
    )
    assert sync.parse_gst_details("GST details\n09AAUFT2204P1ZI\nOrder #OD1\n") == ("09AAUFT2204P1ZI", None)


def test_parse_gst_details_says_nothing_without_the_block_or_a_gstin_in_it():
    """An order placed without GST shows no block — and a GSTIN elsewhere on the
    page (a seller's, in a footer) is never taken for the buyer's."""
    assert sync.parse_gst_details(None) == (None, None)
    assert sync.parse_gst_details("Order #OD1\nDelivery details\nPrice details\nSeller GSTIN 29AABCF1234A1Z5") == (None, None)
    assert sync.parse_gst_details("GST details\nOrder #OD1\n29AABCF1234A1Z5\n") == (None, None)  # the block was empty
    assert sync.parse_gst_details("GST details\nGST Number 123456\nSomething\n") == (None, None)  # not a GSTIN


def test_the_detail_page_keeps_the_gst_the_order_was_billed_under():
    page = _page()
    page.pages[_detail_url(HREF_0)] = {**DETAIL_0, "//body": DETAIL_0["//body"] + "\n" + GST_PAGE}
    orders = _scrape(page)
    first = next(o for o in orders if o.order_id == "OD111")
    assert (first.billed_gstin, first.billed_business_name) == ("09AAUFT2204P1ZI", "TRANSPARENT DEALS")
    # Pages without the block leave both unset: nothing is invented.
    assert all(o.billed_gstin is None for o in orders if o.order_id != "OD111")


def test_the_pincode_is_the_last_six_digit_run_not_an_earlier_plot_number():
    """An Indian address ends with its pincode; a plot or society number
    earlier in the line can be six digits too and must not win."""
    page = (
        "Delivery details\n"
        "Home\n"
        "Plot 110092, Sector 12 Dwarka New Delhi Delhi 110075\n"
        "Akansha Agarwal\n"
        "8957768866\n"
        "Price details\n"
    )
    address, pincode = sync.parse_delivery_address(page)
    assert address == (
        "Home · Plot 110092, Sector 12 Dwarka New Delhi Delhi 110075 · Akansha Agarwal · 8957768866"
    )
    assert pincode == "110075"
    # The page seen live still reads the same.
    assert sync.parse_delivery_address(ADDRESS_PAGE)[1] == "201307"
