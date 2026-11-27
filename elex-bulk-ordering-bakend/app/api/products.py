"""Product preview — what the operator sees before committing to a run.

A browser is opened per request, so this is deliberately rate-limited by a
small cache: the Ordering form calls it whenever a link is pasted, and a user
editing a URL should not spawn a browser per keystroke.

The preview also says who sells the product — its default seller and every
seller its sellers page lists — so the form can offer them to choose from.
That part has a bounded wait of its own and never fails the preview.

``GET /products/search`` finds products by name on Flipkart's own search page,
so a link need not be pasted (app.automation.search): signed out, through the
same browser, proxy and bounded concurrency as the preview, with a short cache
of its own keyed on the query.
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from sqlalchemy.orm import Session as DbSession

from app.automation.product import ProductPreview, add_screener_details, fetch_product_preview
from app.automation.search import MAX_QUERY_CHARS, MIN_QUERY_CHARS, NoResultsInTime, query_text, read_search_results
from app.automation.sellers import read_seller_offers
from app.config import get_settings
from app.core import app_settings
from app.core.browser import browser_page
from app.core.security import require_user
from app.db.base import get_db

log = logging.getLogger("ordering.api.products")

router = APIRouter(prefix="/products", tags=["products"], dependencies=[Depends(require_user)])

#: url -> (fetched_at, payload). Small and in-process on purpose: a preview is
#: cheap to redo and must never serve a stale price into an ordering decision.
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_SECONDS = 120
_CACHE_MAX = 64


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=10, max_length=2048)


def _cached(url: str) -> dict | None:
    hit = _CACHE.get(url)
    if not hit:
        return None
    fetched_at, payload = hit
    if time.monotonic() - fetched_at > _CACHE_TTL_SECONDS:
        _CACHE.pop(url, None)
        return None
    return payload


def _remember(url: str, payload: dict) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    _CACHE[url] = (time.monotonic(), payload)


#: How many previews may drive a browser at the same time.
_PREVIEWS_AT_ONCE = asyncio.Semaphore(2)

#: How long a preview may spend on who sells the product, after the product
#: page itself has been read: the seller block, then the sellers page.
_SELLERS_READ_SECONDS = 15


async def _add_sellers(page, preview: ProductPreview) -> None:
    """Fill in the preview's sellers from the open product page and its
    sellers page. Never raises: what went wrong is noted on the preview, and
    the rest of it stands — a choice of seller is a help, not a requirement."""
    try:
        default, offers = await asyncio.wait_for(
            read_seller_offers(page, preview.url, price=preview.price), timeout=_SELLERS_READ_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 — a timeout included
        log.warning("could not read the sellers of %s: %s", preview.url[:60], exc)
        preview.sellers_error = "the sellers of this product could not be read"
        return
    preview.default_seller = default or preview.seller
    preview.sellers = [offer.as_dict() for offer in offers]
    if not offers:
        preview.sellers_error = "no seller could be read off this product's pages"


def is_flipkart_link(url: str) -> bool:
    """Whether the link points at Flipkart (``www.flipkart.com``, the short
    ``dl.flipkart.com``, any ``*.flipkart.com``) — the only host the ordering
    browser and the preview may be aimed at."""
    try:
        host = (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host == "flipkart.com" or host.endswith(".flipkart.com")


@router.post("/preview")
async def preview_product(payload: PreviewRequest, db: DbSession = Depends(get_db)) -> dict:
    """Read a product's name, image, price and stock for the order form, and
    who sells it: ``default_seller`` and ``sellers`` (name, price, rating,
    cash on delivery, BUY NOW), or ``sellers_error`` when they could not be
    read."""
    url = payload.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="Enter a full product URL.")
    if not is_flipkart_link(url):
        # The preview drives a real browser on the server: it opens Flipkart
        # product pages and nothing else — not an internal address, not a
        # stranger's site (audit F-14 / F-44, 25 Oct 2026).
        raise HTTPException(status_code=422, detail="Enter a Flipkart product link (flipkart.com).")

    cached = _cached(url)
    if cached is not None:
        return cached

    effective = app_settings.get_effective(db)
    try:
        # A few previews at a time: each is a browser launch, and a page that
        # fires one per keystroke must not start fifty of them.
        async with _PREVIEWS_AT_ONCE:
            async with browser_page(
                headless=get_settings().browser_headless,
                proxy=str(effective.get("proxy_server") or "") or None,
            ) as (_context, page):
                preview: ProductPreview = await fetch_product_preview(page, url)
                if not preview.error:
                    await add_screener_details(page, preview)
                    await _add_sellers(page, preview)
    except Exception as exc:  # noqa: BLE001 — a preview must never 500 the form
        log.error("preview failed for %s: %s", url[:60], exc)
        # The browser's own error text is for the log, not the form: it can
        # describe the server's network.
        return ProductPreview(url=url, error="could not load a preview of this link").as_dict()

    result = preview.as_dict()
    if not result.get("error"):
        _remember(url, result)
    return result


#: query -> (fetched_at, payload): Flipkart's results move slowly, and a person
#: refining a search types the same words again; a few minutes spares Flipkart
#: the repeated loads. Only an answer with results is kept: a failed search is
#: never kept, and nor is one that found nothing — a check page Flipkart serves
#: as a normal one also shows no product, and pressing Search again must look
#: again rather than repeat "nothing found" for five minutes.
_SEARCH_CACHE: dict[str, tuple[float, dict]] = {}
_SEARCH_CACHE_TTL_SECONDS = 300
_SEARCH_CACHE_MAX = 64
#: The whole search, page load and reading included, at most.
_SEARCH_SECONDS = 30
#: What a failed search says. The browser's own error is for the log only: it
#: can describe the server's network (audit F-14/F-44).
SEARCH_FAILED = "could not search Flipkart just now — try again, or paste the product link"
#: What a search says when Flipkart's page opened but showed no product within
#: the wait (app.automation.search.NoResultsInTime).
SEARCH_NO_RESULTS_IN_TIME = "Flipkart did not show any results in time — try again, or paste the product link"


def _search_cached(key: str) -> dict | None:
    hit = _SEARCH_CACHE.get(key)
    if not hit:
        return None
    fetched_at, payload = hit
    if time.monotonic() - fetched_at > _SEARCH_CACHE_TTL_SECONDS:
        _SEARCH_CACHE.pop(key, None)
        return None
    return payload


def _search_remember(key: str, payload: dict) -> None:
    if len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX:
        oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
        _SEARCH_CACHE.pop(oldest, None)
    _SEARCH_CACHE[key] = (time.monotonic(), payload)


@router.get("/search")
async def search_products(q: str = Query(..., max_length=1000), db: DbSession = Depends(get_db)) -> dict:
    """Flipkart's search results for ``q``, for the order form to pick from.

    Up to 12 results, each ``title`` (the exact variant), ``link`` (canonical:
    ``https://www.flipkart.com/<slug>/p/itm…?pid=PID``, tracking dropped),
    ``pid``, ``price``, ``rating`` and ``image``. Read in the app's own browser,
    signed out — never with an account's cookies. ``error`` says, in plain
    words, when the search could not be made, or when Flipkart's page showed no
    product in time; the browser's own message never reaches the form. Only an
    answer with results is kept for the next few minutes.
    """
    query = query_text(q)
    if not MIN_QUERY_CHARS <= len(query) <= MAX_QUERY_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"Type {MIN_QUERY_CHARS} to {MAX_QUERY_CHARS} characters to search Flipkart.",
        )
    key = query.lower()
    cached = _search_cached(key)
    if cached is not None:
        return cached

    effective = app_settings.get_effective(db)
    try:
        async with _PREVIEWS_AT_ONCE:
            async with browser_page(
                headless=get_settings().browser_headless,
                proxy=str(effective.get("proxy_server") or "") or None,
            ) as (_context, page):
                results = await asyncio.wait_for(read_search_results(page, query), timeout=_SEARCH_SECONDS)
    except NoResultsInTime:
        return {"query": query, "results": [], "error": SEARCH_NO_RESULTS_IN_TIME}
    except Exception as exc:  # noqa: BLE001 — a search must never 500 the form
        log.error("search failed for %r: %s", query[:60], exc)
        return {"query": query, "results": [], "error": SEARCH_FAILED}

    links = [result for result in results if is_flipkart_link(result.link)]
    payload = {"query": query, "results": [result.as_dict() for result in links], "error": None}
    if links:
        _search_remember(key, payload)
    return payload
