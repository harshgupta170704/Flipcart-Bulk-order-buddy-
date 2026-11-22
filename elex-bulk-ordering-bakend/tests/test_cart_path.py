"""Products with no "Buy now" (found live 2026-10-23).

A women's top offered ONLY "Add to cart", and offered nothing at all until a
size was picked. A flow built purely on Buy now times out on whole categories.

The cart path carries a hazard the Buy-now path does not: "Place Order" buys
the WHOLE cart. Observed live, a cart holding one unrelated phone totalled
₹18,511 against a ₹212 top — so the cart must be emptied first.
"""

import asyncio

from app.automation import ordering as order_flow
from app.automation.ordering import AddressInfo, CardInfo, GstInfo
from app.core import selectors as S
from app.db.models import OrderStatus
from tests.fakes import FakePage

ADDR = AddressInfo(name="N", phone="", pin="122016", area="Somewhere")
GST = GstInfo(gstin="", business_name="")
CARD = CardInfo(number="4111111111111111", month="12", year="28", cvv="123")

PLACED = {
    S.ORDER_TOTAL: "₹212",
    S.ORDER_AMOUNT: "₹212",
    S.ORDER_ID: "OD1",
}


def _run(page, **kw):
    return asyncio.run(
        order_flow.run_order(
            page,
            product_link="https://flipkart.com/top",
            address=ADDR,
            gst=GST,
            card=CARD,
            qty=1,
            target=1000.0,
            **kw,
        )
    )


def test_buy_now_is_used_when_the_product_offers_it():
    page = FakePage(inner_text_map={**PLACED, S.BUY_NOW_BTN: "Buy now"})
    result = _run(page)

    assert S.BUY_NOW_BTN in page.clicks
    assert S.ADD_TO_CART_BTN not in page.clicks
    assert result["status"] == OrderStatus.placed


def test_a_product_without_buy_now_falls_back_to_the_cart():
    """The top had no Buy now at all — only Add to cart."""
    page = FakePage(inner_text_map={**PLACED, S.ADD_TO_CART_BTN: "Add to cart"})
    result = _run(page)

    assert S.ADD_TO_CART_BTN in page.clicks
    assert S.PLACE_ORDER_BTN in page.clicks
    assert result["status"] == OrderStatus.placed


def test_the_cart_is_emptied_before_adding_anything():
    """Place Order buys the WHOLE cart, so a leftover item would be bought too."""
    page = FakePage(
        inner_text_map={
            **PLACED,
            S.ADD_TO_CART_BTN: "Add to cart",
            S.CART_REMOVE_ITEM: "Remove",
        }
    )
    _run(page)

    assert S.CART_REMOVE_ITEM in page.clicks
    # ...and the clearing happened BEFORE the product was added.
    assert page.clicks.index(S.CART_REMOVE_ITEM) < page.clicks.index(S.ADD_TO_CART_BTN)


def test_a_size_is_chosen_before_looking_for_a_buy_control():
    """Apparel shows no buy control until a size is picked."""
    size_xpath = S.SIZE_CHIP % '"M"'
    page = FakePage(
        inner_text_map={**PLACED, size_xpath: "M", S.ADD_TO_CART_BTN: "Add to cart"}
    )
    _run(page, size="M")

    assert size_xpath in page.clicks
    assert page.clicks.index(size_xpath) < page.clicks.index(S.ADD_TO_CART_BTN)


def test_a_size_that_cannot_be_picked_stops_the_order_before_anything_is_pressed():
    """Even on a page that offers Buy now without one: going on would buy
    whichever variant the page shows, not the size the run asked for."""
    size_xpath = S.SIZE_CHIP % '"XL"'
    page = FakePage(inner_text_map={**PLACED, S.BUY_NOW_BTN: "Buy now"})
    page.fail_clicks_on = {size_xpath}  # no XL chip on the page

    result = _run(page, size="XL")

    assert result["status"] == OrderStatus.failed
    assert result["error"] == "could not pick size 'XL' on the product page — nothing was clicked, nothing was charged"
    assert page.clicks == []
    assert S.CART_URL not in page.goto_urls
    assert not page.submitted


def test_a_size_gone_when_the_product_page_is_reopened_stops_with_the_cart_emptied():
    """The cart path empties the cart, opens the product page again and picks
    the size again. Gone by then (sold out meanwhile), Add to cart would put
    another variant in the cart; the run stops instead, and says the cart was
    emptied."""
    size_xpath = S.SIZE_CHIP % '"XL"'
    page = FakePage(inner_text_map={**PLACED, size_xpath: "XL", S.ADD_TO_CART_BTN: "Add to cart"})
    goto = page.goto

    async def the_size_sells_out_while_the_cart_is_emptied(url, wait_until=None):
        await goto(url, wait_until)
        if url == S.CART_URL:
            page.fail_clicks_on.add(size_xpath)

    page.goto = the_size_sells_out_while_the_cart_is_emptied

    result = _run(page, size="XL")

    assert result["status"] == OrderStatus.failed
    assert result["error"] == "could not pick size 'XL' on the product page — the cart was emptied, nothing was charged"
    assert page.clicks.count(size_xpath) == 1  # the first pick, before the cart was emptied
    assert S.CART_REMOVE_ITEM in page.clicks
    assert S.ADD_TO_CART_BTN not in page.clicks
    assert not page.submitted


def test_failing_to_add_to_the_cart_charges_nothing():
    page = FakePage(inner_text_map={S.ADD_TO_CART_BTN: "Add to cart"})
    page.fail_clicks_on = {S.ADD_TO_CART_BTN}
    result = _run(page)

    assert result["status"] == OrderStatus.failed
    assert "cart" in result["error"].lower()
    assert not page.submitted


def test_the_price_cap_still_applies_on_the_cart_path():
    """A cart total over target must be refused like any other."""
    page = FakePage(
        inner_text_map={
            S.ADD_TO_CART_BTN: "Add to cart",
            S.ORDER_TOTAL: "₹18,511",  # the whole-cart total seen live
        }
    )
    result = _run(page)

    assert result["status"] == OrderStatus.over_target
    assert result["amount"] == 18511.0
    assert not page.submitted


# --- the cart path, proven at every step (26 Oct 2026) ------------------------------
# Live, the account offered only "Add to cart" reached Place Order on an empty
# cart twice, and its cart page carried 25 "Saved For Later" rows with a
# "Remove" link each — the first "Remove" anywhere was a saved item's.


def test_emptying_the_cart_never_presses_a_saved_for_later_remove():
    assert "saved for later" in S.CART_REMOVE_ITEM and "preceding::" in S.CART_REMOVE_ITEM
    assert "saved for later" in S.CART_REMOVE_CONFIRM


def test_a_removal_is_confirmed_in_flipkarts_box():
    page = FakePage(inner_text_map={**PLACED, S.ADD_TO_CART_BTN: "Add to cart", S.CART_REMOVE_ITEM: "Remove"})
    _run(page)

    first = page.clicks.index(S.CART_REMOVE_ITEM)
    assert S.CART_REMOVE_CONFIRM in page.clicks[first:]


def test_add_to_cart_is_pressed_again_until_flipkart_shows_go_to_cart():
    page = FakePage(inner_text_map={**PLACED, S.ADD_TO_CART_BTN: "Add to cart"})
    page.counts[S.GO_TO_CART_BTN] = 0  # the press never takes
    result = _run(page)

    assert result["status"] == OrderStatus.failed
    assert "did not put the product in the cart" in result["error"]
    assert page.clicks.count(S.ADD_TO_CART_BTN) == 3  # a pointer press, then two DOM clicks
    assert S.PLACE_ORDER_BTN not in page.clicks
    assert not page.submitted


def test_the_product_pages_own_reason_is_given():
    page = FakePage(
        inner_text_map={**PLACED, S.ADD_TO_CART_BTN: "Add to cart", S.PAGE_BODY: "Sa Rasa Kurta Not deliverable at 241001 Add to cart"}
    )
    page.counts[S.GO_TO_CART_BTN] = 0
    result = _run(page)

    assert "Not deliverable at 241001" in result["error"]


def test_an_empty_cart_is_never_ordered_from():
    page = FakePage(inner_text_map={**PLACED, S.ADD_TO_CART_BTN: "Add to cart", S.CART_EMPTY_TEXT: "Your cart is empty!"})
    result = _run(page)

    assert result["status"] == OrderStatus.failed
    assert "cart was empty" in result["error"]
    assert S.PLACE_ORDER_BTN not in page.clicks
    assert not page.submitted


def test_a_cart_holding_more_than_this_product_is_refused():
    page = FakePage(inner_text_map={**PLACED, S.ADD_TO_CART_BTN: "Add to cart", S.CART_ITEM_COUNT: "Flipkart (2)"})
    result = _run(page)

    assert result["status"] == OrderStatus.failed
    assert "holds 2 items" in result["error"]
    assert not page.submitted
