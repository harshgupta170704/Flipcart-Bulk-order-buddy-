"""Read a product's details for the order preview.

Deliberately reads the page's STRUCTURED DATA (JSON-LD ``Product``, falling
back to Open Graph meta tags) rather than scraping CSS classes. Confirmed live
2026-10-23: Flipkart publishes a full ``Product`` block carrying name, brand,
category, image, sku, price, availability and rating.

That choice matters here more than anywhere else in this codebase. Every
class-based selector we inherited had rotted; structured data is contractual —
search engines consume it, so Flipkart has a reason to keep it stable. The
preview is also the one place a wrong value is obvious to the operator, which
makes it worth getting from the most durable source available.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field

from playwright.async_api import Page

log = logging.getLogger("ordering.product")

#: schema.org availability values that mean "can be bought right now".
IN_STOCK = {
    "https://schema.org/InStock",
    "http://schema.org/InStock",
    "InStock",
    "https://schema.org/LimitedAvailability",
    "LimitedAvailability",
}


@dataclass
class ProductPreview:
    """What the operator sees before committing a run to a product."""

    url: str
    name: str | None = None
    brand: str | None = None
    category: str | None = None
    color: str | None = None
    image: str | None = None
    sku: str | None = None
    price: float | None = None
    currency: str = "INR"
    availability: str | None = None
    in_stock: bool | None = None
    rating: float | None = None
    rating_count: int | None = None
    seller: str | None = None
    error: str | None = None
    #: Who an order buys from when no seller is chosen: the product page's
    #: "Seller: X". Filled by the preview endpoint, never by the order run.
    default_seller: str | None = None
    #: Every seller the product's sellers page lists, as
    #: ``app.automation.sellers.SellerOffer.as_dict()`` — name, price, rating,
    #: cash on delivery, BUY NOW. One entry for a product with one seller.
    sellers: list[dict] = field(default_factory=list)
    #: Why the sellers could not be read, when they could not; the rest of
    #: the preview stands either way.
    sellers_error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _first(value):
    """JSON-LD fields are routinely either a value or a list of them."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _name_of(value):
    """brand/seller arrive as either a string or an object with a name."""
    value = _first(value)
    if isinstance(value, dict):
        return value.get("name")
    return value


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_product_jsonld(blocks: list) -> dict | None:
    """Pick the ``Product`` object out of the page's JSON-LD blocks."""
    for block in blocks:
        for item in block if isinstance(block, list) else [block]:
            if isinstance(item, dict) and item.get("@type") == "Product":
                return item
    return None


def preview_from_jsonld(product: dict, url: str) -> ProductPreview:
    offers = _first(product.get("offers")) or {}
    availability = offers.get("availability")
    return ProductPreview(
        url=url,
        name=product.get("name"),
        brand=_name_of(product.get("brand")),
        category=_first(product.get("category")),
        color=_first(product.get("color")),
        image=_first(product.get("image")),
        sku=product.get("sku"),
        price=_as_float(offers.get("price") or offers.get("lowPrice")),
        currency=offers.get("priceCurrency") or "INR",
        availability=availability,
        # None, not False, when the page said nothing: "we don't know" and
        # "out of stock" are different, and only one should block a run.
        in_stock=(availability in IN_STOCK) if availability else None,
        rating=_as_float((product.get("aggregateRating") or {}).get("ratingValue")),
        rating_count=int(
            _as_float((product.get("aggregateRating") or {}).get("ratingCount")) or 0
        )
        or None,
        seller=_name_of(offers.get("seller")),
    )


_PRICE_IN_TEXT = re.compile(r"₹\s*([\d,]+)")


async def fetch_product_preview(page: Page, url: str) -> ProductPreview:
    """Open a product page and read its details. Never raises."""
    try:
        await page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        return ProductPreview(url=url, error=f"could not open the product page: {exc}")
    return await read_product_preview(page, url)


async def read_product_preview(page: Page, url: str) -> ProductPreview:
    """Read the details of the product page already open. Never raises.

    The order flow uses this to record WHAT it bought from the product itself,
    rather than from the account's order list afterwards — reading the newest
    row there is how a run that placed nothing once reported a months-old
    backpack as its order.
    """
    try:
        blocks = await page.evaluate(
            """() => [...document.querySelectorAll('script[type="application/ld+json"]')]
                 .map(s => { try { return JSON.parse(s.textContent); } catch (e) { return null; } })
                 .filter(Boolean)"""
        )
    except Exception as exc:  # noqa: BLE001
        blocks = []
        log.warning("could not read structured data: %s", exc)

    product = parse_product_jsonld(blocks or [])
    if product:
        preview = preview_from_jsonld(product, url)
        if preview.name:
            return preview

    # Fallback: Open Graph tags, which carry at least a title and an image.
    log.info("no JSON-LD Product on %s; falling back to meta tags", url[:60])
    try:
        meta = await page.evaluate(
            """() => {
                 const get = (sel) => { const m = document.querySelector(sel); return m ? m.content : null; };
                 return {
                   title: get('meta[property="og:title"]'),
                   image: get('meta[property="og:image"]'),
                   text: (document.body.innerText || '').slice(0, 4000),
                 };
               }"""
        )
    except Exception as exc:  # noqa: BLE001
        return ProductPreview(url=url, error=f"could not read the product page: {exc}")

    price = None
    found = _PRICE_IN_TEXT.search((meta or {}).get("text") or "")
    if found:
        price = _as_float(found.group(1))

    return ProductPreview(
        url=url,
        name=(meta or {}).get("title"),
        image=(meta or {}).get("image"),
        price=price,
        error=None if (meta or {}).get("title") else "product details not found",
    )
