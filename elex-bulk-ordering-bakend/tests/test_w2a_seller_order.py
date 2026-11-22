"""Choosing which seller an order buys from (wave 2, package A).

Every account bought from whichever seller the product page showed by
default, and the orders the tool placed were all cancelled "as per seller
policy restrictions applicable to this brand" — a seller's decision. So a run
may name the seller: the order is then placed from that seller's row on the
product's sellers page. These pin what that means for money: only the chosen
row's BUY NOW is ever pressed, a seller the page does not list is refused
with nothing pressed, and a checkout naming another seller never reaches a
payment step.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.automation import ordering as order_flow
from app.automation.ordering import PAYMENT_COD, SELLER_NEEDS_BUY_NOW, AddressInfo, GstInfo
from app.automation.sellers import (
    seller_add_to_cart_xpath,
    seller_buy_now_xpath,
    seller_name_xpath,
    seller_row_xpath,
    sellers_page_url,
)
from app.automation.sync import ScrapedOrder
from app.core import selectors as S
from app.db.base import SessionLocal
from app.db.models import Account, AccountStatus, Job, JobStatus, JobType, Order, OrderStatus
from app.db.models import Session as SessionModel
from app.jobs import sync_tasks, tasks
from tests.fakes import FakePage
from tests.w2a_seller_pages import (
    CART_ONLY_SELLERS,
    PIGEON_LINK,
    PIGEON_OTHERS_HREF,
    PIGEON_PID,
    PIGEON_PRODUCT,
    PIGEON_SELLERS,
    VIVO_LINK,
    VIVO_PRODUCT,
    listed,
    row_texts,
    seller_page,
)

ADDR = AddressInfo(name="N", phone="", pin="226013", area="Lucknow")
GST = GstInfo(gstin="", business_name="")
CHECKOUT_FROM_SAPHED = "Deliver to: N, Lucknow 226013\nPigeon Special 3 L Pressure Cooker\nSeller: SAPHEDretail\n₹749"


def _run(page, *, seller=None, link=PIGEON_LINK, reserve=None, size=None):
    return asyncio.run(
        order_flow.run_order(
            page,
            product_link=link,
            address=ADDR,
            gst=GST,
            card=None,
            qty=1,
            target=1000.0,
            payment_mode=PAYMENT_COD,
            seller=seller,
            reserve=reserve,
            size=size,
        )
    )


def _buy_controls(page) -> list[str]:
    """Every buy control pressed: the product page's Buy now and Add to cart,
    and any sellers-page row's BUY NOW or ADD TO CART."""
    return [
        click
        for click in page.clicks
        if click in (S.BUY_NOW_BTN, S.ADD_TO_CART_BTN) or "BUY NOW" in click or "ADD TO CART" in click
    ]


# --- the chosen row's BUY NOW, and nothing else ---------------------------------------
def test_the_chosen_rows_buy_now_is_pressed_and_no_other_buy_control():
    page = seller_page(checkout=CHECKOUT_FROM_SAPHED)

    result = _run(page, seller="SAPHEDretail")

    chosen = seller_buy_now_xpath("SAPHEDretail", listed())
    assert result["status"] == OrderStatus.placed
    assert _buy_controls(page) == [chosen]  # pressed once, and nothing else that buys
    for other in ("MYTHANGLORYRetail", "TBL Online"):
        assert seller_buy_now_xpath(other, listed()) not in page.clicks
    assert sellers_page_url(PIGEON_PID) in page.goto_urls
    assert result["seller"] == "SAPHEDretail"
    assert result["seller_verified"] is True


def test_the_seller_is_found_in_any_case_and_recorded_as_the_page_spells_it():
    page = seller_page(checkout=CHECKOUT_FROM_SAPHED)

    result = _run(page, seller="  saphedretail ")

    assert result["status"] == OrderStatus.placed
    assert result["seller"] == "SAPHEDretail"
    assert seller_buy_now_xpath("saphedretail", listed()) in page.clicks


def test_a_product_page_without_buy_now_still_buys_from_the_rows_buy_now():
    """The pigeon page reads "Buy at ₹711", not "Buy now", so without a seller
    the run takes the cart path. With one, the sellers page's row decides —
    and the cart is neither emptied nor used."""
    page = seller_page(checkout=CHECKOUT_FROM_SAPHED)  # S.BUY_NOW_BTN has no text: no "Buy now"
    page.inner_text_map[S.ADD_TO_CART_BTN] = "Add to cart"

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.placed
    assert _buy_controls(page) == [seller_buy_now_xpath("SAPHEDretail", listed())]
    assert S.CART_URL not in page.goto_urls


# --- refused before anything is pressed --------------------------------------------------
def test_a_seller_not_listed_is_refused_with_nothing_pressed_naming_those_listed():
    page = seller_page()

    result = _run(page, seller="Nobody Retail")

    assert result["status"] == OrderStatus.failed
    assert "'Nobody Retail' is not among the sellers" in result["error"]
    assert "(MYTHANGLORYRetail, SAPHEDretail, TBL Online)" in result["error"]
    assert "nothing was clicked, nothing was charged" in result["error"]
    assert page.clicks == []
    assert not page.submitted


@pytest.mark.parametrize("names_on_page", [0, 1])
def test_a_sellers_list_that_cannot_be_read_is_refused_with_nothing_pressed(names_on_page):
    """Even with the chosen name on the page once: without the list there are
    no other names to keep the row XPath out of the whole list, whose one BUY
    NOW could be another seller's."""
    page = seller_page(sellers="Something went wrong")  # no list could be read
    page.counts[seller_name_xpath("SAPHEDretail")] = names_on_page

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.failed
    assert "could not read the list of sellers on the sellers page" in result["error"]
    assert "nothing was clicked, nothing was charged" in result["error"]
    assert page.clicks == []
    assert not page.submitted


def test_a_sellers_page_with_buttons_before_any_name_is_refused_as_unreadable():
    """A layout never mapped: its first buttons belong to no seller the text
    names. Said as a list that could not be read — the page did open — and
    refused with nothing pressed."""
    sellers = PIGEON_SELLERS.replace("Delivery\nMYTHANGLORYRetail", "Delivery\nADD TO CART\n BUY NOW\nMYTHANGLORYRetail")
    # Built here, not with seller_page(): that one reads the list to find each row.
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹749"})
    page.inner_text_by_url = {"/p/": {S.PAGE_BODY: PIGEON_PRODUCT}, "/sellers?": {S.PAGE_BODY: sellers}}
    page.attrs[(S.OTHER_SELLERS_LINK, "href")] = PIGEON_OTHERS_HREF

    result = _run(page, seller="SAPHEDretail")

    assert sellers_page_url(PIGEON_PID) in page.goto_urls
    assert result["status"] == OrderStatus.failed
    assert result["error"] == (
        "could not read the list of sellers on the sellers page — nothing was clicked, nothing was charged"
    )
    assert page.clicks == []
    assert not page.submitted


#: SAPHEDretail's row with no button of its own, run together with a row the
#: list missed — the element the row XPath would then land on.
_TWO_ROWS = (
    "SAPHEDretail\n4.2\nCash On Delivery available\n₹749₹1,30542% off\nCurrently unavailable\n"
    "Quick Mart\n(Not Enough Ratings)\nCash On Delivery available\n₹699₹1,30546% off\nADD TO CART\nBUY NOW"
)


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        (_TWO_ROWS, "it shows 2 prices"),
        (_TWO_ROWS.replace("₹749₹1,30542% off\n", ""), "it shows 2 ratings"),
        ("Sponsored\n" + row_texts()["SAPHEDretail"], "it starts with 'Sponsored', not the seller's name"),
        (row_texts()["SAPHEDretail"].replace("\nBUY NOW", "\nBUY NOW\nBUY NOW"), "it shows 2 BUY NOW buttons"),
        (None, "its text could not be read"),
    ],
)
def test_a_row_that_does_not_read_as_one_sellers_row_is_refused_before_buy_now(row, problem):
    page = seller_page()
    texts = page.inner_text_by_url["/sellers?"]
    row_xpath = seller_row_xpath("SAPHEDretail", listed())
    if row is None:
        del texts[row_xpath]
    else:
        texts[row_xpath] = row

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.failed
    assert f"SAPHEDretail's row on the sellers page does not read as one seller's row ({problem})" in result["error"]
    assert "nothing was clicked, nothing was charged" in result["error"]
    assert page.clicks == []
    assert not page.submitted


def test_a_listed_seller_whose_row_cannot_be_found_is_refused():
    page = seller_page()
    page.counts[seller_name_xpath("SAPHEDretail")] = 0  # the text lists it; no element holds the name

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.failed
    assert "its row could not be found" in result["error"]
    assert page.clicks == []


def test_a_seller_listed_twice_is_not_guessed_between():
    page = seller_page(sellers=PIGEON_SELLERS.replace("TBL Online", "SAPHEDretail"))

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.failed
    assert "listed more than once" in result["error"]
    assert page.clicks == []


def test_a_row_with_two_buy_now_buttons_is_refused():
    page = seller_page()
    page.counts[seller_buy_now_xpath("SAPHEDretail", listed())] = 2

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.failed
    assert "expected one BUY NOW in SAPHEDretail's row on the sellers page, found 2" in result["error"]
    assert "nothing was charged" in result["error"]
    assert page.clicks == []
    assert not page.submitted


def test_a_row_with_no_buy_now_is_refused():
    page = seller_page()
    page.counts[seller_buy_now_xpath("SAPHEDretail", listed())] = 0
    page.counts[seller_add_to_cart_xpath("SAPHEDretail", listed())] = 0

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.failed
    assert "no BUY NOW was found in SAPHEDretail's row" in result["error"]
    assert page.clicks == []


def test_a_sellers_page_that_will_not_open_is_a_reason_not_a_crash():
    page = seller_page()
    goto = page.goto

    async def unreachable_sellers_page(url, wait_until=None):
        if "/sellers?" in url:
            raise RuntimeError("net::ERR_TIMED_OUT")
        await goto(url, wait_until)

    page.goto = unreachable_sellers_page

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.failed
    assert "could not open this product's sellers page (RuntimeError)" in result["error"]
    assert "nothing was clicked, nothing was charged" in result["error"]
    assert page.clicks == []


def test_a_buy_now_that_never_responds_charges_nothing():
    page = seller_page()
    page.fail_clicks_on = {seller_buy_now_xpath("SAPHEDretail", listed())}

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.failed
    assert "did not respond" in result["error"] and "nothing was charged" in result["error"]
    assert not page.submitted


def test_a_buy_now_that_opens_the_step_by_step_checkout_is_refused_clearly():
    """A seller's BUY NOW sometimes opens Flipkart's older step-by-step
    checkout (/checkout/init), which the flow cannot place through yet (seen
    live 27 Oct 2026). It is refused with an accurate, actionable message, not
    the misleading "did not open the checkout", and nothing is ordered."""
    page = seller_page()
    page.checkout_url = "https://www.flipkart.com/checkout/init"

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.failed
    assert "step-by-step checkout" in result["error"]
    assert "leave Seller blank" in result["error"]
    assert "Nothing was charged" in result["error"]
    assert not page.submitted


def test_a_size_that_cannot_be_picked_is_refused_before_the_sellers_page_is_opened():
    """The sellers page would be opened for whichever variant the product page
    shows, and its BUY NOW would buy that variant — not the size asked for."""
    page = seller_page(checkout=CHECKOUT_FROM_SAPHED)
    page.fail_clicks_on = {S.SIZE_CHIP % '"XL"'}  # no XL chip on the page

    result = _run(page, seller="SAPHEDretail", size="XL")

    assert result["status"] == OrderStatus.failed
    assert result["error"] == "could not pick size 'XL' on the product page — nothing was clicked, nothing was charged"
    assert page.clicks == []
    assert not any("/sellers?" in url for url in page.goto_urls)
    assert not page.submitted


# --- apparel: rows that offer only Add to cart --------------------------------------------
def test_a_chosen_seller_offering_only_add_to_cart_is_refused_before_the_cart_is_touched():
    """Pressing a sellers-page ADD TO CART was never seen live, so this is
    refused for now — before anything is pressed, and before the cart is
    emptied (the cart path empties it first)."""
    page = seller_page(sellers=CART_ONLY_SELLERS)
    page.inner_text_map[S.ADD_TO_CART_BTN] = "Add to cart"

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.failed
    assert result["error"].startswith(SELLER_NEEDS_BUY_NOW)
    assert SELLER_NEEDS_BUY_NOW == "choosing a seller works only for products with Buy now for now"
    assert "nothing was clicked, nothing was charged" in result["error"]
    assert page.clicks == []
    assert S.CART_URL not in page.goto_urls


def test_a_row_whose_only_button_is_add_to_cart_on_the_page_is_refused_the_same_way():
    """The page's elements say so even when its text did not."""
    page = seller_page()
    page.counts[seller_buy_now_xpath("SAPHEDretail", listed())] = 0  # the add-to-cart button is there

    result = _run(page, seller="SAPHEDretail")

    assert result["error"].startswith(SELLER_NEEDS_BUY_NOW)
    assert page.clicks == []


def test_a_one_seller_product_with_only_add_to_cart_is_bought_through_its_cart_as_today():
    page = seller_page(
        product=PIGEON_PRODUCT.replace("See other sellers\n", "").replace("Buy at ₹711\n", ""),
        others_href=None,
        checkout="Seller: MYTHANGLORYRetail",
    )
    page.inner_text_map[S.ADD_TO_CART_BTN] = "Add to cart"
    page.counts[S.CART_REMOVE_ITEM] = 0  # an empty cart to start with

    result = _run(page, seller="MYTHANGLORYRetail")

    assert result["status"] == OrderStatus.placed
    assert S.ADD_TO_CART_BTN in page.clicks and S.PLACE_ORDER_BTN in page.clicks
    assert result["seller"] == "MYTHANGLORYRetail" and result["seller_verified"] is True


# --- a product with one seller ---------------------------------------------------------------
def test_a_one_seller_product_whose_seller_line_matches_is_bought_as_today():
    page = seller_page(product=VIVO_PRODUCT, others_href=None)
    page.inner_text_map[S.BUY_NOW_BTN] = "Buy now"

    result = _run(page, seller="hatmanretails", link=VIVO_LINK)

    assert result["status"] == OrderStatus.placed
    assert _buy_controls(page) == [S.BUY_NOW_BTN]
    assert not any("/sellers?" in url for url in page.goto_urls)
    assert result["seller"] == "HatmanRetails"
    assert result["seller_verified"] is False  # the checkout named no seller


def test_a_one_seller_product_whose_seller_line_differs_is_refused_with_nothing_pressed():
    page = seller_page(product=VIVO_PRODUCT, others_href=None)
    page.inner_text_map[S.BUY_NOW_BTN] = "Buy now"

    result = _run(page, seller="SAPHEDretail", link=VIVO_LINK)

    assert result["status"] == OrderStatus.failed
    assert "sold by 'HatmanRetails' alone" in result["error"] and "'SAPHEDretail'" in result["error"]
    assert "nothing was clicked, nothing was charged" in result["error"]
    assert page.clicks == []


def test_a_product_page_naming_no_seller_is_refused():
    page = seller_page(product="vivo T5 Lite 44W 5G\n₹20,499\nBuy now", others_href=None)
    page.inner_text_map[S.BUY_NOW_BTN] = "Buy now"

    result = _run(page, seller="HatmanRetails", link=VIVO_LINK)

    assert result["status"] == OrderStatus.failed
    assert "names no seller" in result["error"]
    assert page.clicks == []


# --- the checkout must not name another seller -------------------------------------------------
def test_a_checkout_naming_another_seller_is_refused_before_payment():
    page = seller_page(checkout="Deliver to: N\nPigeon Special 3 L\nSeller: MYTHANGLORYRetail\n₹749")

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.failed
    assert "the checkout names the seller MYTHANGLORYRetail, not the chosen SAPHEDretail" in result["error"]
    assert "nothing was charged" in result["error"]
    # No payment step: not Continue, not a payment method, not Place Order.
    assert S.CHECKOUT_CONTINUE_CTA not in page.clicks
    assert S.PAY_OPTION_COD not in page.clicks
    assert S.COD_PLACE_ORDER_BTN not in page.clicks
    assert not page.submitted


def test_a_checkout_sold_by_line_counts_too():
    page = seller_page(checkout="Order Summary\nSold by TBL Online\n₹749")

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.failed
    assert "TBL Online" in result["error"]
    assert not page.submitted


def test_a_checkout_naming_another_seller_in_the_middle_of_a_line_is_refused():
    page = seller_page(checkout="Pigeon Special 3 L Pressure Cooker · sold by: TBL Online\n₹749")

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.failed
    assert "the checkout names the seller TBL Online, not the chosen SAPHEDretail" in result["error"]
    assert S.CHECKOUT_CONTINUE_CTA not in page.clicks
    assert not page.submitted


def test_a_checkout_naming_the_chosen_seller_with_its_rating_run_on_is_verified():
    page = seller_page(checkout="Pigeon Special 3 L Pressure Cooker · Sold by SAPHEDretail (4.2)\n₹749")

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.placed
    assert result["seller_verified"] is True


@pytest.mark.parametrize(
    "line",
    [
        "Seller: SAPHEDretail | Seller: TBL Online",
        "Sold by SAPHEDretail · Sold by TBL Online",
        "Seller: SAPHEDretail (4.2) Seller: TBL Online",
        "Seller:SAPHEDretail|Seller:TBL Online",
        "Seller: SAPHEDretail - Seller: TBL Online",
    ],
)
def test_a_checkout_line_naming_another_seller_after_the_chosen_one_is_refused_before_payment(line):
    """Each name ends at the next label on its line, so the second seller is
    read as a seller of its own — never as the rest of the chosen one's name."""
    page = seller_page(checkout=f"Deliver to: N, Lucknow 226013\nPigeon Special 3 L Pressure Cooker\n{line}\n₹749")

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.failed
    assert result["error"] == (
        "the checkout names the seller TBL Online, not the chosen SAPHEDretail — not ordering, nothing was charged"
    )
    assert S.CHECKOUT_CONTINUE_CTA not in page.clicks
    assert S.PAY_OPTION_COD not in page.clicks
    assert S.COD_PLACE_ORDER_BTN not in page.clicks
    assert not page.submitted


@pytest.mark.parametrize(
    "line",
    [
        "Seller: SAPHEDretail",
        "Sold by: SAPHEDretail",
        "Pigeon Special 3 L Pressure Cooker · Sold by SAPHEDretail (4.2)",
        "Seller: SAPHEDretail | 4.2★",
        "Seller: SAPHEDretail ★",
        "Seller: SAPHEDretail - 4 years with Flipkart",
        "Seller: SAPHEDretail\nSeller: saphedretail",
    ],
)
def test_a_checkout_naming_only_the_chosen_seller_goes_on_verified(line):
    page = seller_page(checkout=f"Deliver to: N, Lucknow 226013\nPigeon Special 3 L Pressure Cooker\n{line}\n₹749")

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.placed
    assert result["seller_verified"] is True


def test_a_checkout_naming_no_seller_goes_on_unverified():
    page = seller_page(checkout="Deliver to: N\nPigeon Special 3 L Pressure Cooker\n₹749")

    result = _run(page, seller="SAPHEDretail")

    assert result["status"] == OrderStatus.placed
    assert result["seller"] == "SAPHEDretail"
    assert result["seller_verified"] is False


# --- without a chosen seller nothing changes -------------------------------------------------
def test_without_a_seller_the_product_pages_buy_now_is_used_as_before():
    page = seller_page()
    page.inner_text_map[S.BUY_NOW_BTN] = "Buy now"

    result = _run(page)

    assert result["status"] == OrderStatus.placed
    assert _buy_controls(page) == [S.BUY_NOW_BTN]
    assert not any("/sellers?" in url for url in page.goto_urls)
    assert result["seller"] is None and result["seller_verified"] is False


# --- the order row ------------------------------------------------------------------------------
def _account(db) -> Account:
    account = Account(email="w2a-seller@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    return account


COLUMNS = dict(product_link=PIGEON_LINK, qty=1, target=1000.0, address=ADDR, gst=GST)


def test_the_order_row_carries_the_chosen_seller_from_the_moment_it_is_reserved(db):
    account = _account(db)
    at_reservation: dict = {}

    def reserve(row: dict) -> int:
        row_id = tasks.reserve_order(account.id, row, **COLUMNS)
        with SessionLocal() as session:
            at_reservation["seller"] = session.get(Order, row_id).seller
        return row_id

    result = _run(seller_page(checkout=CHECKOUT_FROM_SAPHED), seller="saphedretail", reserve=reserve)
    tasks.record_order_result(account.id, result, **COLUMNS)

    assert at_reservation["seller"] == "SAPHEDretail"
    db.expire_all()
    order = db.query(Order).one()
    assert (order.status, order.seller) == (OrderStatus.placed, "SAPHEDretail")


def test_a_run_that_chose_no_seller_never_wipes_the_seller_a_read_back_filled_in(db):
    """A read-back running beside the order may fill the row's seller while
    the run waits for Flipkart's confirmation; settling a run that left the
    choice to Flipkart writes no seller over it."""
    account = _account(db)
    row_id = tasks.reserve_order(account.id, {"amount": 749.0, "payment_mode": PAYMENT_COD}, **COLUMNS)
    with SessionLocal() as session:
        session.get(Order, row_id).seller = "MYTHANGLORYRetail"
        session.commit()

    tasks.record_order_result(
        account.id,
        {"status": OrderStatus.placed, "order_id": "OD428123456789100", "amount": 749.0, "seller": None,
         "payment_mode": PAYMENT_COD, "reservation": row_id},
        **COLUMNS,
    )

    db.expire_all()
    assert db.get(Order, row_id).seller == "MYTHANGLORYRetail"


def _read_back(monkeypatch, account_id: int, *observations: ScrapedOrder) -> None:
    """A real read-back of the account's orders (``sync_tasks.run_sync_job``),
    with only the browser and the scraper stood in for: the scraper hands over
    ``observations`` as the account's orders page would."""

    @contextlib.asynccontextmanager
    async def browser_page(**_kwargs):
        page = FakePage()
        yield page.context, page

    async def scrape_orders(_page, **_kwargs):
        return list(observations)

    monkeypatch.setattr(sync_tasks, "browser_page", browser_page)
    monkeypatch.setattr(sync_tasks, "scrape_orders", scrape_orders)
    with SessionLocal() as session:
        job = Job(type=JobType.sync, status=JobStatus.queued)
        session.add(job)
        session.commit()
        job_id = job.id
    asyncio.run(sync_tasks.run_sync_job(job_id, [account_id]))


@pytest.mark.parametrize("confirmed", [False, True], ids=["run-unconfirmed", "run-placed"])
def test_a_chosen_seller_is_never_written_over_the_seller_a_read_back_read(db, monkeypatch, tmp_path, confirmed):
    """A read-back running beside the order adopts its reserved row and reads
    the seller off the order's own page: Flipkart sold from MYTHANGLORYRetail,
    whatever the run chose. The run settles after — never confirmed, or
    confirmed — and the chosen seller is who it asked for, not who sold, so
    the order row keeps the read-back's reading."""
    monkeypatch.setattr(order_flow, "DIAGNOSTICS_DIR", tmp_path)  # an unconfirmed run's screenshot
    account = _account(db)
    db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": "1"}], valid=True))
    db.commit()
    page = seller_page(checkout="Deliver to: N, Lucknow 226013\nPigeon Special 3 L Pressure Cooker\n₹749")  # no seller named
    if not confirmed:
        page.confirmation_url = None  # Flipkart never confirms

    result = _run(page, seller="SAPHEDretail", reserve=lambda row: tasks.reserve_order(account.id, row, **COLUMNS))
    assert result["status"] == (OrderStatus.placed if confirmed else OrderStatus.unconfirmed)
    assert result["seller"] == "SAPHEDretail"
    _read_back(
        monkeypatch,
        account.id,
        ScrapedOrder(
            order_id="OD428123456789100",
            item_id="ITM428123456789100",
            unit_id="UNIT428123456789100",
            amount=749.0,
            seller="MYTHANGLORYRetail",
            platform_status="Order Confirmed",
        ),
    )
    db.expire_all()
    order = db.query(Order).one()
    assert (order.order_id, order.seller) == ("OD428123456789100", "MYTHANGLORYRetail")  # adopted, and read
    assert order.synced_at is not None

    tasks.record_order_result(account.id, result, **COLUMNS)

    db.expire_all()
    order = db.query(Order).one()
    assert (order.order_id, order.status) == ("OD428123456789100", OrderStatus.placed)
    assert order.seller == "MYTHANGLORYRetail"
    assert [unit.seller for unit in order.units] == ["MYTHANGLORYRetail"]


def test_a_refused_seller_leaves_a_failed_row_with_no_seller(db):
    """Nothing was bought from anybody, so the row names no seller."""
    account = _account(db)

    result = _run(seller_page(), seller="Nobody Retail")
    tasks.record_order_result(account.id, result, **COLUMNS)

    order = db.query(Order).one()
    assert (order.status, order.seller) == (OrderStatus.failed, None)
