"""The seller row XPaths run against the real sellers page (wave 2, package A).

``fixtures/sellers/pigeon-sellers.html`` is the pigeon cooker's sellers page
as Flipkart served it on 26 Oct 2026 (signed out, read only), with its
scripts, frames, links and images taken out. It is loaded into a real
Chromium with JavaScript off and every request refused, so nothing reaches
Flipkart and nothing on the page runs: these test the XPaths and the row
check against the page's own elements, which the fakes elsewhere cannot.
The one run of the order flow's own ``choose_seller`` opens its sellers page
at a ``.invalid`` address the test answers itself from the saved page, and
refuses every other request. Skipped when Chromium cannot start here.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.automation import ordering
from app.automation.sellers import (
    element_text,
    parse_seller_rows,
    row_problem,
    seller_add_to_cart_xpath,
    seller_buy_now_xpath,
    seller_row_xpath,
)
from app.core import selectors as S

PAGE = (Path(__file__).parent / "fixtures" / "sellers" / "pigeon-sellers.html").read_text(encoding="utf-8")
LISTED = ["MYTHANGLORYRetail", "SAPHEDretail", "TBL Online"]
_BUTTON = re.compile(r"<button\b[^>]*>.*?</button>", re.S)


def _without_buttons(seller: str, keep_add_to_cart: bool = False) -> str:
    """The page with ``seller``'s row's buttons taken out (all of them, or
    BUY NOW alone): the rows that matter most, never seen live."""
    start = PAGE.index(f">{seller}<")
    later = [PAGE.find(f">{name}<", start + 1) for name in LISTED if name != seller]
    end = min([index for index in later if index > start] or [len(PAGE)])
    row = PAGE[start:end]
    if keep_add_to_cart:
        row = _BUTTON.sub(lambda button: "" if "Buy Now" in button.group(0) else button.group(0), row)
    else:
        row = _BUTTON.sub("", row)
    return PAGE[:start] + row + PAGE[end:]


async def _read(html: str, xpaths: dict[str, str]) -> tuple[list[str], dict[str, int], dict[str, str]]:
    """Load ``html`` offline and return the sellers its text lists, how many
    elements each of ``xpaths`` matches, and the text of each one's first."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 — no browser here: nothing to test against
            pytest.skip(f"Chromium cannot start here ({exc.__class__.__name__})")
        try:
            context = await browser.new_context(java_script_enabled=False)
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            await page.set_content(html, wait_until="domcontentloaded")
            listed = [offer.name for offer in parse_seller_rows(await element_text(page, S.PAGE_BODY))]
            counts = {key: await page.locator(f"xpath={xpath}").count() for key, xpath in xpaths.items()}
            texts = {key: await element_text(page, xpath) for key, xpath in xpaths.items()}
        finally:
            await browser.close()
    return listed, counts, texts


def test_each_rows_buy_now_and_add_to_cart_match_once_and_the_row_reads_as_its_own():
    xpaths = {}
    for name in LISTED:
        xpaths[f"buy {name}"] = seller_buy_now_xpath(name, LISTED)
        xpaths[f"cart {name}"] = seller_add_to_cart_xpath(name, LISTED)
        xpaths[f"row {name}"] = seller_row_xpath(name, LISTED)

    listed, counts, texts = asyncio.run(_read(PAGE, xpaths))

    assert listed == LISTED
    for name in LISTED:
        assert counts[f"buy {name}"] == 1, name
        assert counts[f"cart {name}"] == 1, name
        assert counts[f"row {name}"] == 1, name
        assert row_problem(texts[f"row {name}"], name) is None, texts[f"row {name}"]
        assert texts[f"buy {name}"].strip().upper() == "BUY NOW"
    # Three rows, three different BUY NOW buttons: no row reaches into another.
    assert len({texts[f"row {name}"] for name in LISTED}) == 3


def test_a_row_with_no_buttons_matches_no_buy_now_at_all():
    """Without the other names the row would be the whole list, whose BUY
    NOWs are other sellers'. A button is tied to its own row's name as well,
    so even an XPath built with no list read reaches none of them."""
    xpaths = {
        "buy": seller_buy_now_xpath("SAPHEDretail", LISTED),
        "cart": seller_add_to_cart_xpath("SAPHEDretail", LISTED),
        "buy, no list read": seller_buy_now_xpath("SAPHEDretail"),
    }

    listed, counts, _texts = asyncio.run(_read(_without_buttons("SAPHEDretail"), xpaths))

    assert listed == LISTED  # the row still reads as a row of its own
    assert counts["buy"] == 0
    assert counts["cart"] == 0
    assert counts["buy, no list read"] == 0  # each BUY NOW sits beside another seller's name


def test_a_row_offering_only_add_to_cart_matches_its_add_to_cart_and_no_buy_now():
    xpaths = {
        "buy": seller_buy_now_xpath("SAPHEDretail", LISTED),
        "cart": seller_add_to_cart_xpath("SAPHEDretail", LISTED),
        "row": seller_row_xpath("SAPHEDretail", LISTED),
    }

    _listed, counts, texts = asyncio.run(_read(_without_buttons("SAPHEDretail", keep_add_to_cart=True), xpaths))

    assert counts["buy"] == 0
    assert counts["cart"] == 1
    assert row_problem(texts["row"], "SAPHEDretail") == "it shows 0 BUY NOW buttons"


# --- two rows the page's text reads as one ------------------------------------------
_ROW_START = '<div class="QGdlvi">'
_RATING_LINE = re.compile(r'<div class="MKiFS6 BJrjER">[^<]*</div>')
_COD_LINE = '<div class="tX1Zab"><div class="sVC6vR"><span>Cash On Delivery available</span></div></div>'


def _with_rating(row: str, rating: str | None) -> str:
    """``row`` with its rating line reading ``rating`` instead, or with none."""
    return _RATING_LINE.sub("" if rating is None else f'<div class="MKiFS6 BJrjER">{rating}</div>', row, count=1)


def _run_together(chosen_rating: str | None, next_rating: str | None) -> str:
    """The page with two sellers left: SAPHEDretail shown but not selling —
    its name, its rating line if any, "Currently unavailable", and no
    cash-on-delivery line, price, offers or buttons — then TBL Online's row
    as it was but for its rating line. Never seen live. Nothing in the text
    starts a row at TBL Online unless a known rating line follows its name,
    so the two read as one row: SAPHEDretail's, with TBL Online's price and
    BUY NOW."""
    before, _mythanglory, saphed, tbl_and_the_rest = PAGE.split(_ROW_START)
    not_selling = saphed[: saphed.index('<div class="FFsNw9')].replace(_COD_LINE, "<div>Currently unavailable</div>")
    return (
        before
        + _ROW_START
        + _with_rating(not_selling, chosen_rating)
        + "</div>"
        + _ROW_START
        + _with_rating(tbl_and_the_rest, next_rating)
    )


#: (SAPHEDretail's rating line, TBL Online's): the text starts a row only at
#: a rating line it knows ("4.3", "(Not Enough Ratings)").
_RUN_TOGETHER = {
    "A: the next row shows no rating line": ("4.2", None),
    "G: ratings in a form never mapped": ("4.2 ★", "4.4 ★"),
    "H: no rating lines at all": (None, None),
}


@pytest.mark.parametrize("case", list(_RUN_TOGETHER))
def test_two_rows_read_as_one_never_reach_the_next_sellers_buy_now(case):
    """The list then names SAPHEDretail alone, with TBL Online's price and
    BUY NOW. Found from the name's side, the row is the whole list, whose one
    BUY NOW is TBL Online's; tied to its own row's name, no button is
    SAPHEDretail's. (The live page still matches one per seller: the first
    test above.)"""
    listed = ["SAPHEDretail"]  # what the text reads, checked below
    xpaths = {
        "buy": seller_buy_now_xpath("SAPHEDretail", listed),
        "cart": seller_add_to_cart_xpath("SAPHEDretail", listed),
        "from the name's side alone": f"{seller_row_xpath('SAPHEDretail', listed)}//button[{S.SELLER_BUY_NOW_TEXT}]",
    }

    read, counts, _texts = asyncio.run(_read(_run_together(*_RUN_TOGETHER[case]), xpaths))

    assert read == listed  # TBL Online went missing from the list
    assert counts["from the name's side alone"] == 1  # TBL Online's BUY NOW: the gap
    assert counts["buy"] == 0
    assert counts["cart"] == 0


def test_an_offer_starting_with_a_rupee_figure_beside_the_buttons_is_no_price():
    """Only a plain rupee figure is a row's price. An EMI offer beside each
    row's buttons ("₹721/month EMI") would otherwise be the nearest "price"
    around every button, hold no seller's name, and refuse every good row."""
    emi = '<div class="KZcexD col-4-12"><div>₹721/month EMI</div>'
    page = PAGE.replace('<div class="KZcexD col-4-12">', emi)
    assert page.count(emi) == len(LISTED)
    xpaths = {name: seller_buy_now_xpath(name, LISTED) for name in LISTED}

    listed, counts, texts = asyncio.run(_read(page, xpaths))

    assert listed == LISTED
    for name in LISTED:
        assert counts[name] == 1, name
        assert texts[name].strip().upper() == "BUY NOW"


#: Where the order flow below opens the sellers page: a host that can never
#: resolve, answered by the test itself.
_OFFLINE_SELLERS = "http://sellers.invalid/sellers"
_PRODUCT_LINK = "http://product.invalid/pigeon-special-3-l-inner-lid-pressure-cooker/p/itm0c2cd3b721e22?pid=PRCFNFEBWFFDBVHP"
#: The product page's seller block, as the live page reads it.
_PRODUCT_PAGE = (
    "<html><body><h1>Pigeon Special 3 L Pressure Cooker</h1><div>₹749</div>"
    "<div>Seller: MYTHANGLORYRetail</div><a href='/sellers?pid=PRCFNFEBWFFDBVHP'>See other sellers</a>"
    "</body></html>"
)


async def _choose_offline(sellers_html: str, seller: str, requested: list[str]) -> ordering.SellerChoice:
    """The real ``choose_seller`` on the product page above, with its sellers
    page answered from ``sellers_html``. Every other request is refused;
    every request asked for is recorded in ``requested``."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 — no browser here: nothing to test against
            pytest.skip(f"Chromium cannot start here ({exc.__class__.__name__})")
        try:
            context = await browser.new_context(java_script_enabled=False)

            async def answer(route):
                requested.append(route.request.url)
                if route.request.url.startswith(f"{_OFFLINE_SELLERS}?"):
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=sellers_html)
                else:
                    await route.abort()

            await context.route("**/*", answer)
            page = await context.new_page()
            await page.set_content(_PRODUCT_PAGE, wait_until="domcontentloaded")
            return await ordering.choose_seller(page, product_link=_PRODUCT_LINK, seller=seller)
        finally:
            await browser.close()


def test_the_order_flow_refuses_two_rows_read_as_one_with_nothing_pressed(monkeypatch):
    """Case A through the order flow itself. Before a button was tied to its
    own row's name, every check passed here — the name found once, one BUY
    NOW, a row reading as one seller's — and TBL Online's BUY NOW was pressed
    for SAPHEDretail."""
    pressed: list[str] = []
    requested: list[str] = []

    async def record_the_press(page, xpath, url_fragment, **_kwargs):
        pressed.append(xpath)  # recorded, never clicked
        return True

    monkeypatch.setattr(S, "SELLERS_URL", _OFFLINE_SELLERS)
    monkeypatch.setattr(ordering, "click_until_navigated", record_the_press)

    choice = asyncio.run(_choose_offline(_run_together("4.2", None), "SAPHEDretail", requested))

    assert choice.why_not == (
        "no BUY NOW was found in SAPHEDretail's row on the sellers page — nothing was clicked, nothing was charged"
    )
    assert choice.route == ""
    assert pressed == []
    # The sellers page was opened, and only ever at the address that cannot resolve.
    assert f"{_OFFLINE_SELLERS}?pid=PRCFNFEBWFFDBVHP" in requested
    assert all(urlsplit(url).hostname.endswith(".invalid") for url in requested)
