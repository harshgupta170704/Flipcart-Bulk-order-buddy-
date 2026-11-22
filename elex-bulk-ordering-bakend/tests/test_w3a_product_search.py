"""Searching Flipkart by name on the Place orders page (wave 3, package A).

The result cards come from Flipkart's own search pages, saved on 26 Oct 2026
in a signed-out browser, read only (``fixtures/search``: the phone search
"vivo t5 lite" in its list layout, the cooker search in its grid layout; the
links carry the tracking parameters Flipkart adds). Nothing here reaches
Flipkart: the parser is fed the saved cards, the endpoint a stand-in browser
(or, under the real reader, a stand-in page), and the in-page collector a page
built here, in a browser that may fetch nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import products as products_api
from app.automation import search as search_mod
from app.automation.search import (
    MAX_QUERY_CHARS,
    MAX_RESULTS,
    SearchResult,
    canonical_link,
    parse_search_cards,
    search_url,
)
from app.core.security import create_access_token

FIXTURES = Path(__file__).parent / "fixtures" / "search"
PHONE_CARDS = json.loads((FIXTURES / "phone-cards.json").read_text(encoding="utf-8"))
COOKER_CARDS = json.loads((FIXTURES / "cooker-cards.json").read_text(encoding="utf-8"))


# --- the cards of a saved search page ---------------------------------------------
def test_the_phone_search_reads_as_exact_variants_with_price_and_rating() -> None:
    results = parse_search_cards(PHONE_CARDS)

    assert len(results) == MAX_RESULTS
    first = results[0]
    assert first.title == "vivo T5 Lite 44W 5G (Wave Blue, 128 GB)"
    assert first.link == "https://www.flipkart.com/vivo-t5-lite-44w-5g-wave-blue-128-gb/p/itm5420c578ec2df?pid=MOBHNDTD57UJUK9Q"
    assert (first.pid, first.price, first.rating) == ("MOBHNDTD57UJUK9Q", 22999.0, 4.3)
    assert first.image.startswith("https://rukminim2.flixcart.com/")
    third = results[2]
    assert (third.title, third.price) == ("vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)", 17999.0)
    # Every link is the product page alone: no lid, marketplace, query or tracker.
    for result in results:
        assert "lid=" not in result.link and "otracker" not in result.link and "q=" not in result.link
        assert result.link.endswith(f"?pid={result.pid}")
    assert len({result.pid for result in results}) == len(results)


def test_the_grid_search_takes_the_full_name_from_the_link_and_reads_its_price_line() -> None:
    results = parse_search_cards(COOKER_CARDS)

    first = results[0]
    # The card's own text cuts the name short ("… Induction Bottom ..."); the link's title does not.
    assert first.title == "Pigeon Special Plus and 3 L Inner Lid Induction Bottom Pressure Cooker"
    assert (first.price, first.rating) == (1104.0, 4.2)
    assert next(r for r in results if r.pid == "PRCGQHJD43S4JVV8").rating == 4.0  # "4(57,327)"


def test_a_card_without_a_title_is_named_by_its_first_line_that_is_not_a_label() -> None:
    card = {
        "href": "/some-cooker/p/itmabc123?pid=PRCAAAAAAAAAAAAA&lid=X",
        "title": "",
        "lines": ["Bestseller", "Sponsored", "Some Cooker 3 L", "4.1(1,234)", "₹799₹1,29938% off"],
        "image": "javascript:alert(1)",
    }
    (result,) = parse_search_cards([card])
    assert (result.title, result.price, result.rating, result.image) == ("Some Cooker 3 L", 799.0, 4.1, None)


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        (
            "/vivo-t5/p/itm5420c578ec2df?pid=MOBHNDTD57UJUK9Q&lid=LST&marketplace=FLIPKART&q=vivo&otracker=search",
            ("https://www.flipkart.com/vivo-t5/p/itm5420c578ec2df?pid=MOBHNDTD57UJUK9Q", "MOBHNDTD57UJUK9Q"),
        ),
        (
            "https://www.flipkart.com/vivo-t5/p/itm5420c578ec2df?pid=MOBHNDTD57UJUK9Q",
            ("https://www.flipkart.com/vivo-t5/p/itm5420c578ec2df?pid=MOBHNDTD57UJUK9Q", "MOBHNDTD57UJUK9Q"),
        ),
        ("https://evil.example/vivo-t5/p/itm5420c578ec2df?pid=MOBHNDTD57UJUK9Q", None),  # not Flipkart
        ("https://flipkart.com.evil.example/x/p/itm1?pid=MOBHNDTD57UJUK9Q", None),
        ("javascript:alert(1)//x/p/itm1?pid=MOBHNDTD57UJUK9Q", None),
        ("/vivo-t5/p/itm5420c578ec2df", None),  # no product id
        ("/vivo-t5/p/itm5420c578ec2df?pid=MOB%22%5D", None),  # not letters and digits
        ("/search?q=vivo&pid=MOBHNDTD57UJUK9Q", None),  # not a product page
        ("/a/b/p/itm1?pid=MOBHNDTD57UJUK9Q", None),
        (None, None),
    ],
)
def test_only_a_flipkart_product_link_comes_out_canonical(href, expected) -> None:
    assert canonical_link(href) == expected


def test_the_search_page_address_carries_the_query_encoded() -> None:
    assert search_url("vivo t5 & lite") == "https://www.flipkart.com/search?q=vivo+t5+%26+lite"


# --- the endpoint -------------------------------------------------------------------
@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(products_api.router)
    return TestClient(application, headers={"Authorization": f"Bearer {create_access_token('tester', role='admin')}"})


@pytest.fixture(autouse=True)
def _fresh_cache():
    products_api._SEARCH_CACHE.clear()
    yield
    products_api._SEARCH_CACHE.clear()


class _Browser:
    """Stands in for the app's browser: records how it was opened and what was
    searched, and answers with the saved cards (or ``results``, or raises)."""

    def __init__(self, *, fail: Exception | None = None, results: list[SearchResult] | None = None) -> None:
        self.opened: list[dict] = []
        self.searched: list[str] = []
        self.fail = fail
        self.results = results

    def browser_page(self, **kwargs):
        self.opened.append(kwargs)

        @contextlib.asynccontextmanager
        async def _page():
            yield object(), object()

        return _page()

    async def read(self, page, query: str) -> list[SearchResult]:
        self.searched.append(query)
        if self.fail is not None:
            raise self.fail
        return parse_search_cards(PHONE_CARDS) if self.results is None else self.results


@pytest.fixture
def browser(monkeypatch) -> _Browser:
    fake = _Browser()
    monkeypatch.setattr(products_api, "browser_page", fake.browser_page)
    monkeypatch.setattr(products_api, "read_search_results", fake.read)
    return fake


def test_the_endpoint_answers_up_to_twelve_canonical_results(api, browser) -> None:
    body = api.get("/products/search", params={"q": "  vivo   t5 lite "}).json()

    assert body["query"] == "vivo t5 lite" and body["error"] is None
    assert len(body["results"]) == MAX_RESULTS
    assert set(body["results"][0]) == {"title", "link", "pid", "price", "rating", "image", "mrp", "discount_pct", "highlights"}
    assert all(products_api.is_flipkart_link(result["link"]) for result in body["results"])
    assert browser.searched == ["vivo t5 lite"]


def test_it_searches_signed_out_never_with_an_accounts_cookies(api, browser) -> None:
    api.get("/products/search", params={"q": "vivo t5 lite"})
    (opened,) = browser.opened
    assert "cookies" not in opened or opened["cookies"] is None


@pytest.mark.parametrize("q", ["", " ", "a", "  a  ", "x" * (MAX_QUERY_CHARS + 1)])
def test_the_query_is_bounded(api, browser, q) -> None:
    response = api.get("/products/search", params={"q": q})
    assert response.status_code == 422
    assert browser.opened == []


def test_the_same_words_are_answered_from_the_cache_for_a_few_minutes(api, browser, monkeypatch) -> None:
    first = api.get("/products/search", params={"q": "vivo t5 lite"}).json()
    again = api.get("/products/search", params={"q": "VIVO  T5 Lite"}).json()

    assert again == first
    assert len(browser.searched) == 1
    # Past the few minutes it is searched again.
    clock = products_api.time.monotonic() + products_api._SEARCH_CACHE_TTL_SECONDS + 1
    monkeypatch.setattr(products_api.time, "monotonic", lambda: clock)
    api.get("/products/search", params={"q": "vivo t5 lite"})
    assert len(browser.searched) == 2


def test_a_failed_search_says_so_in_plain_words_and_is_not_kept(api, monkeypatch) -> None:
    fake = _Browser(fail=RuntimeError("net::ERR_PROXY_CONNECTION_FAILED at http://10.0.0.5:3128"))
    monkeypatch.setattr(products_api, "browser_page", fake.browser_page)
    monkeypatch.setattr(products_api, "read_search_results", fake.read)

    response = api.get("/products/search", params={"q": "vivo t5 lite"})

    assert response.status_code == 200
    body = response.json()
    assert body["results"] == [] and body["error"] == products_api.SEARCH_FAILED
    assert "10.0.0.5" not in response.text and "ERR_PROXY" not in response.text
    api.get("/products/search", params={"q": "vivo t5 lite"})
    assert len(fake.searched) == 2  # tried again, not served the failure


def test_an_answer_with_no_results_is_not_kept(api, monkeypatch) -> None:
    """A page Flipkart did not really answer — a check page served as a normal
    one — reads as no results, as a search that found nothing does; neither
    is kept, so pressing Search again looks again instead of repeating
    "nothing found" for five minutes."""
    fake = _Browser(results=[])
    monkeypatch.setattr(products_api, "browser_page", fake.browser_page)
    monkeypatch.setattr(products_api, "read_search_results", fake.read)

    first = api.get("/products/search", params={"q": "vivo t5 lite"}).json()
    assert first == {"query": "vivo t5 lite", "results": [], "error": None}
    api.get("/products/search", params={"q": "vivo t5 lite"})
    assert len(fake.searched) == 2


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status
        self.ok = 200 <= status < 300


class _Page:
    """Stands in for the browser's page under the real reader
    (``read_search_results``): the search page answers with ``status`` (None:
    no answer at all) and shows ``cards`` — when there are none, no product
    card appears within the wait."""

    def __init__(self, *, status: int | None = 200, cards: list[dict] | None = None) -> None:
        self.status = status
        self.cards = cards or []
        self.visited: list[str] = []

    async def goto(self, url: str, **_kwargs):
        self.visited.append(url)
        return None if self.status is None else _Response(self.status)

    def locator(self, _selector: str):
        page = self

        class _First:
            async def wait_for(self, **_kwargs) -> None:
                if not page.cards:
                    raise TimeoutError("no product card within the wait")

        return SimpleNamespace(first=_First())

    async def evaluate(self, _script: str, _args):
        return self.cards


@pytest.fixture
def page(monkeypatch):
    """The endpoint with the real reader, on a stand-in page."""

    def make(**kwargs) -> _Page:
        stand_in = _Page(**kwargs)

        @contextlib.asynccontextmanager
        async def browser_page(**_kwargs):
            yield object(), stand_in

        monkeypatch.setattr(products_api, "browser_page", browser_page)
        return stand_in

    return make


def test_the_real_reader_answers_a_page_of_cards_and_keeps_it(api, page) -> None:
    stand_in = page(cards=PHONE_CARDS)

    body = api.get("/products/search", params={"q": "vivo t5 lite"}).json()
    again = api.get("/products/search", params={"q": "vivo t5 lite"}).json()

    assert body["error"] is None and len(body["results"]) == MAX_RESULTS
    assert again == body and len(stand_in.visited) == 1


@pytest.mark.parametrize("status", [403, 503, None])
def test_an_error_page_is_a_failed_search_and_is_not_kept(api, page, status) -> None:
    """The page load does not fail on an error status: an "Access Denied" 403
    or a 503 used to read as "nothing found", kept for five minutes. It is a
    failed search, said in the same plain words, and looked at again."""
    stand_in = page(status=status)

    body = api.get("/products/search", params={"q": "vivo t5 lite"}).json()
    assert body == {"query": "vivo t5 lite", "results": [], "error": products_api.SEARCH_FAILED}
    api.get("/products/search", params={"q": "vivo t5 lite"})
    assert len(stand_in.visited) == 2


def test_a_page_that_shows_no_product_in_time_says_so_and_is_not_kept(api, page) -> None:
    """A slow page, or a check page served with status 200: no product card
    within the wait. The form says so rather than "nothing found", and the
    next press of Search looks again."""
    stand_in = page(status=200, cards=[])

    body = api.get("/products/search", params={"q": "vivo t5 lite"}).json()
    assert body == {"query": "vivo t5 lite", "results": [], "error": products_api.SEARCH_NO_RESULTS_IN_TIME}
    assert body["error"] == "Flipkart did not show any results in time — try again, or paste the product link"
    api.get("/products/search", params={"q": "vivo t5 lite"})
    assert len(stand_in.visited) == 2


def test_the_search_needs_a_sign_in(api) -> None:
    assert TestClient(api.app).get("/products/search", params={"q": "vivo"}).status_code == 401


# --- the in-page collector, on a page built here ----------------------------------------
_GRID_AND_LIST = """<!doctype html><html><body>
<div class="results">
  <div class="row">
    <div class="card"><a href="/pigeon-a/p/itmaaa111?pid=PRCAAAAAAAAAAA01&lid=L1&q=pigeon" title="Pigeon A 3 L Pressure Cooker">
      <img src="https://rukminim2.flixcart.com/image/a.jpeg" alt="Pigeon A"></a>
      <a href="/pigeon-a/p/itmaaa111?pid=PRCAAAAAAAAAAA01&lid=L1&q=pigeon">Pigeon A 3 L Pressure ...</a>
      <div>Aluminium</div><div>4.1(1,60,569)</div><div>₹796₹1,49546% off</div></div>
    <div class="card"><a href="/pigeon-b/p/itmbbb222?pid=PRCBBBBBBBBBBB02&lid=L2" title="Pigeon B 5 L Pressure Cooker">
      <img src="https://rukminim2.flixcart.com/image/b.jpeg" alt="Pigeon B"></a>
      <div>Hard Anodized</div><div>4.2(23,777)</div><div>₹1,249₹1,86533% off</div></div>
  </div>
  <div class="list-card"><a href="/vivo-t5/p/itmccc333?pid=MOBCCCCCCCCCCC03&lid=L3">
    <div>Bestseller</div><div>Add to Compare</div><div>vivo T5 Lite 44W 5G (Wave Blue, 128 GB)</div>
    <div>4.38,153 Ratings &amp; 602 Reviews</div><div>6 GB RAM | 128 GB ROM</div><div>₹22,999</div><div>₹38,49940% off</div></a></div>
  <div class="promo"><a href="/offers/store">Offers</a></div>
</div></body></html>"""


async def _collect(html: str) -> list[dict]:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 — no browser here: nothing to test against
            pytest.skip(f"Chromium cannot start here ({exc.__class__.__name__})")
        try:
            context = await browser.new_context()
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            await page.set_content(html, wait_until="domcontentloaded")
            return await page.evaluate(search_mod._COLLECT_CARDS, [40, 30])
        finally:
            await browser.close()


def test_the_collector_reads_each_card_apart_in_both_layouts() -> None:
    cards = asyncio.run(_collect(_GRID_AND_LIST))
    results = parse_search_cards(cards)

    assert [(r.pid, r.title, r.price, r.rating) for r in results] == [
        ("PRCAAAAAAAAAAA01", "Pigeon A 3 L Pressure Cooker", 796.0, 4.1),
        ("PRCBBBBBBBBBBB02", "Pigeon B 5 L Pressure Cooker", 1249.0, 4.2),
        ("MOBCCCCCCCCCCC03", "vivo T5 Lite 44W 5G (Wave Blue, 128 GB)", 22999.0, 4.3),
    ]
    # One card's text never runs into its neighbour's.
    assert not any("Pigeon B" in line for line in cards[0]["lines"])
    assert results[0].image == "https://rukminim2.flixcart.com/image/a.jpeg"


#: A search that finds one product, laid out as Flipkart lays a page out: the
#: logo and a price filter come first, everything sits in one wrapper under
#: the body, and the product's link has no title and its picture no name.
_ONE_PRODUCT = """<!doctype html><html><body>
<div id="container">
  <div class="header"><img src="https://static-assets-web.flixcart.com/fk-p-linchpin-web/logo.svg" alt="Flipkart">
    <span>Login</span><span>Cart</span></div>
  <div class="page">
    <div class="filters"><div>Filters</div><div>Price</div><div>₹10000+</div></div>
    <div class="results"><div class="row">
      <div class="list-card"><a href="/vivo-t5/p/itmccc333?pid=MOBCCCCCCCCCCC03&lid=L3">
        <div><img src="https://rukminim2.flixcart.com/image/v.jpeg" alt=""></div>
        <div>Sponsored</div><div>vivo T5 Lite 44W 5G (Wave Blue, 128 GB)</div>
        <div>4.38,153 Ratings &amp; 602 Reviews</div><div>6 GB RAM | 128 GB ROM</div>
        <div>₹22,999</div><div>₹38,49940% off</div></a></div>
    </div></div>
  </div>
</div></body></html>"""


def test_the_collector_reads_the_one_product_of_a_page_not_the_page() -> None:
    """With one product there is no neighbour to stop the card at, and the card
    used to be the whole page: "Flipkart ₹10,000.00", with the logo for its
    picture. It is the product's own now."""
    (card,) = asyncio.run(_collect(_ONE_PRODUCT))
    (result,) = parse_search_cards([card])

    # Named by the link's own text: the link has no title, its picture no name.
    assert result.title == "vivo T5 Lite 44W 5G (Wave Blue, 128 GB)"
    assert result.image == "https://rukminim2.flixcart.com/image/v.jpeg"  # not the page's logo
    assert (result.price, result.rating) == (22999.0, 4.3)  # not the price filter's ₹10000
    assert result.link == "https://www.flipkart.com/vivo-t5/p/itmccc333?pid=MOBCCCCCCCCCCC03"


#: The one product's price lines on _ONE_PRODUCT: its price and the struck-out one.
_ONE_PRODUCT_PRICES = "<div>₹22,999</div><div>₹38,49940% off</div>"


def test_the_one_product_with_no_price_stays_unpriced() -> None:
    """A product that shows no price (out of stock, coming soon) has no price
    line of its own to stop at, and its card used to grow to the wrapper that
    also holds the price filter: "Filters ₹10,000.00". A price line before the
    product's link is the page's, never the product's."""
    assert _ONE_PRODUCT.count(_ONE_PRODUCT_PRICES) == 1
    (card,) = asyncio.run(_collect(_ONE_PRODUCT.replace(_ONE_PRODUCT_PRICES, "")))
    (result,) = parse_search_cards([card])

    assert (result.title, result.price, result.rating) == ("vivo T5 Lite 44W 5G (Wave Blue, 128 GB)", None, 4.3)
    assert result.image == "https://rukminim2.flixcart.com/image/v.jpeg"
    assert not any(line == "Filters" or line.startswith("₹") for line in card["lines"])


#: One product in the grid layout (_GRID_AND_LIST's first card): its first
#: link is the picture, with no text of its own, and its price comes after it.
_ONE_GRID_CARD = """<div class="card"><a href="/pigeon-a/p/itmaaa111?pid=PRCAAAAAAAAAAA01&lid=L1&q=pigeon" title="Pigeon A 3 L Pressure Cooker">
        <img src="https://rukminim2.flixcart.com/image/a.jpeg" alt="Pigeon A"></a>
        <a href="/pigeon-a/p/itmaaa111?pid=PRCAAAAAAAAAAA01&lid=L1&q=pigeon">Pigeon A 3 L Pressure ...</a>
        <div>Aluminium</div><div>4.1(1,60,569)</div><div>₹796₹1,49546% off</div></div>"""


def test_the_one_product_of_a_grid_page_keeps_the_price_after_its_picture() -> None:
    """The same page with its one product a grid card. Its first link is the
    picture, with no text, and its price comes after that link: the card
    around it is still the product's own, price and rating included."""
    start = _ONE_PRODUCT.index('<div class="list-card">')
    end = _ONE_PRODUCT.index("</a></div>", start) + len("</a></div>")
    page = _ONE_PRODUCT[:start] + _ONE_GRID_CARD + _ONE_PRODUCT[end:]
    (card,) = asyncio.run(_collect(page))
    (result,) = parse_search_cards([card])

    assert (result.pid, result.title, result.price, result.rating) == (
        "PRCAAAAAAAAAAA01",
        "Pigeon A 3 L Pressure Cooker",
        796.0,
        4.1,
    )
    assert result.image == "https://rukminim2.flixcart.com/image/a.jpeg"
    assert "Filters" not in card["lines"]
