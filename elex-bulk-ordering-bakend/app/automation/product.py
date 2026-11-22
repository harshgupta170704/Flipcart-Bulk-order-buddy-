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
from urllib.parse import urlsplit

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
    #: For the Product Screener — best effort, read off the page itself:
    #: the struck-out list price and the discount off it,
    mrp: float | None = None
    discount_pct: int | None = None
    #: every picture the page publishes (``image`` is the first),
    images: list[str] = field(default_factory=list)
    #: and the other variants it links to (colour, storage, size), each
    #: ``{"label", "link", "pid"}``.
    variants: list[dict] = field(default_factory=list)

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
    pictures = product.get("image")
    pictures = [p for p in (pictures if isinstance(pictures, list) else [pictures]) if isinstance(p, str) and p]
    return ProductPreview(
        images=pictures[:_MAX_IMAGES],
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
#: The pictures a preview carries, at most.
_MAX_IMAGES = 12


#: Collects, from the open product page, its first lines of text (where the
#: price block is) and every product link with its own text. Which links are
#: variants, and what the price block says, is decided in Python
#: (:func:`parse_price_block`, :func:`parse_variants`) so it can be tested on
#: saved pages.
_COLLECT_EXTRAS = r"""
([maxLines, maxLinks]) => {
  const lines = (document.body.innerText || '').split('\n').map(l => l.trim()).filter(Boolean).slice(0, maxLines);
  const links = [];
  for (const a of document.querySelectorAll('a[href*="/p/itm"]')) {
    links.push({ href: a.getAttribute('href') || '', text: (a.innerText || '').trim().slice(0, 200) });
    if (links.length >= maxLinks) break;
  }
  return { lines, links };
}
"""
_EXTRA_LINES = 120
_EXTRA_LINKS = 120
_MAX_VARIANTS = 24

#: "40%" or "↓40%": the discount line of a price block.
_PCT_LINE = re.compile(r"^↓?\s*(\d{1,2})\s*%$")
#: "38,499": the list price, a bare figure with no rupee sign.
_BARE_FIGURE = re.compile(r"^[\d,]+(?:\.\d{1,2})?$")
#: "₹22,999": a price line on its own.
_RUPEE_LINE = re.compile(r"^₹\s*([\d,]+(?:\.\d{1,2})?)$")
#: Every rupee figure within a line.
_RUPEES_IN_LINE = re.compile(r"₹\s*([\d,]+(?:\.\d{1,2})?)")
#: The item id in a product path: ``/<slug>/p/itm…``.
_ITEM_PATH = re.compile(r"^/?(?:https?://[^/]+/)?([^/?#]+)/p/(itm[0-9a-zA-Z]+)")


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text.split(".")[0])


def parse_price_block(lines: list[str], price: float | None) -> tuple[float | None, int | None]:
    """``(mrp, discount_pct)`` from the product's own price block.

    Mapped live on 11 Nov 2026: the block reads ``40%`` / ``38,499`` /
    ``₹22,999`` — the discount, the list price as a bare figure, then the
    price. Ad carousels above it use the same shape for other products, so the
    block is the one whose price line matches the product's own price. Falls
    back to the search card's shapes (:func:`app.automation.search.parse_mrp`).
    """
    from app.automation.search import parse_mrp

    if price is None:
        return None, None
    wanted = _digits(f"{price:.0f}")
    for index, line in enumerate(lines):
        found = _RUPEE_LINE.match(line)
        if not found or _digits(found.group(1)) != wanted or index < 2:
            continue
        figure, pct = lines[index - 1], _PCT_LINE.match(lines[index - 2])
        if pct and _BARE_FIGURE.match(figure):
            mrp = float(figure.replace(",", ""))
            if mrp > price:
                return mrp, int(pct.group(1))
    # The first line holding the price itself as one of its rupee figures —
    # not merely its digits inside another figure (₹999 within "₹19,999").
    start = next(
        (i for i, line in enumerate(lines) if any(_digits(figure) == wanted for figure in _RUPEES_IN_LINE.findall(line))),
        0,
    )
    return parse_mrp(lines[start:], price)


def _item(href: str | None) -> tuple[str, str] | None:
    found = _ITEM_PATH.match((href or "").strip())
    return (found.group(1), found.group(2)) if found else None


def _slug_label(slug: str, base: str) -> str:
    """What a variant's slug says that the product's own does not: from
    ``vivo-t5-lite-44w-5g-twilight-shadow-128-gb`` against
    ``…-wave-blue-128-gb``, "Twilight Shadow"."""
    mine, theirs = base.split("-"), slug.split("-")
    head = 0
    while head < min(len(mine), len(theirs)) and mine[head] == theirs[head]:
        head += 1
    tail = 0
    while tail < min(len(mine), len(theirs)) - head and mine[-1 - tail] == theirs[-1 - tail]:
        tail += 1
    words = theirs[head : len(theirs) - tail] or theirs[head:]
    return " ".join(word.capitalize() if not word.isdigit() else word for word in words)


def parse_variants(url: str, links: list[dict], *, limit: int = _MAX_VARIANTS) -> list[dict]:
    """The other variants of the product at ``url`` among the page's links.

    Mapped live on 11 Nov 2026: a product's variants (colour, storage) share
    its item id (``/p/itm…``) and differ by ``pid``. A storage swatch's text
    is ``128 GB + 4 GB`` then its discount, list price and price (or "Out of
    stock"); a colour swatch is a picture with no text, so its name is taken
    from its slug. Each variant: ``label``, ``link``, ``pid``, and ``price``,
    ``mrp``, ``discount_pct``, ``in_stock`` when its swatch shows them.
    """
    from app.automation.search import canonical_link

    here = _item(_path_of(url))
    here_pid = (canonical_link(url) or (None, None))[1]
    if here is None:
        return []
    base_slug, item_id = here
    variants: list[dict] = []
    seen = {here_pid} if here_pid else set()
    for link in links:
        found = canonical_link(link.get("href"))
        item = _item(link.get("href"))
        if found is None or item is None:
            continue
        canonical, pid = found
        lines = [line.strip() for line in str(link.get("text") or "").split("\n") if line.strip()]
        # Same item, another pid (the phone's colours and storage); or a
        # swatch that is a product of its own (the cooker's "5 L", "With
        # Induction Base"): one short line, no price, never an ad card, and
        # the same brand (the slug's first word) — a short-named related
        # product of another brand is no swatch of this one.
        if item[1] != item_id and not (_is_swatch(lines) and _same_brand(item[0], base_slug)):
            continue
        if pid in seen:
            continue
        seen.add(pid)
        label = lines[0] if lines and not _PCT_LINE.match(lines[0]) and "₹" not in lines[0] else _slug_label(item[0], base_slug)
        variant: dict = {"label": label[:40], "link": canonical, "pid": pid}
        prices = [_RUPEE_LINE.match(line) for line in lines]
        price = next((float(m.group(1).replace(",", "")) for m in prices if m), None)
        if price is not None:
            variant["price"] = price
            mrp, pct = parse_price_block(lines, price)
            variant["mrp"], variant["discount_pct"] = mrp, pct
        if any(line.lower() == "out of stock" for line in lines):
            variant["in_stock"] = False
        elif price is not None:
            variant["in_stock"] = True
        variants.append(variant)
        if len(variants) >= limit:
            break
    return variants


#: A swatch's label: short, one line, no price, not a rating.
_SWATCH_MAX = 30


def _is_swatch(lines: list[str]) -> bool:
    if len(lines) != 1:
        return False
    text = lines[0]
    return len(text) <= _SWATCH_MAX and "₹" not in text and text.upper() != "AD" and not _PCT_LINE.match(text)


def _same_brand(slug: str, base: str) -> bool:
    return slug.split("-", 1)[0].lower() == base.split("-", 1)[0].lower()


def _path_of(url: str) -> str:
    try:
        return urlsplit(url).path
    except ValueError:
        return ""


async def add_screener_details(page: Page, preview: ProductPreview) -> None:
    """Fill the preview's list price, discount and variants from the open
    product page. Never raises; what cannot be read stays empty."""
    try:
        extras = await page.evaluate(_COLLECT_EXTRAS, [_EXTRA_LINES, _EXTRA_LINKS])
    except Exception as exc:  # noqa: BLE001
        log.info("could not read the screener details: %s", exc)
        return
    lines = [str(line) for line in (extras or {}).get("lines") or []]
    preview.mrp, preview.discount_pct = parse_price_block(lines, preview.price)
    preview.variants = parse_variants(page.url or preview.url, (extras or {}).get("links") or [])
    if preview.image and preview.image not in preview.images:
        preview.images.insert(0, preview.image)
        del preview.images[_MAX_IMAGES:]


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
