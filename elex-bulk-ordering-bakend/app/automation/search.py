"""Searching Flipkart by name, for the Place orders page (wave 3, 26 Oct 2026).

Pasting a full product link for every run is slow; a search box lets the
operator type "vivo t5 lite" and pick the exact variant instead. What Flipkart
shows, mapped live in a signed-out browser, read only:

* ``https://www.flipkart.com/search?q=<words>`` works without signing in.
* Each result card links to ``/<slug>/p/itm…?pid=<PID>&lid=…&marketplace=…``
  — the product page, plus tracking — and names the exact variant, colour and
  storage included ("vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)"), in the
  link's ``title`` when the card's own text cuts it short.
* Phones come as a list, one card per row: the name, then "4.38,153 Ratings &
  602 Reviews" (the rating 4.3 run into the count), the specs, "₹22,999", then
  the struck-out price and the discount. Most other products come as a grid:
  the name, the variant, "4.2(70,542)", then "₹1,104₹2,38553% off".

The page is read once (:func:`read_search_results`): the browser collects each
card's link, title, text and picture, and :func:`parse_search_cards` — a pure
function of that, tested on the pages saved that day — makes the results.
Only links that come out as a canonical Flipkart product link are kept.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.async_api import Page

log = logging.getLogger("ordering.search")

SEARCH_URL = "https://www.flipkart.com/search"
#: The query's bounds, trimmed: fewer than two characters finds everything,
#: and a longer text is not a product name.
MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 100
#: Results handed back: enough to find the variant, few enough to scan.
MAX_RESULTS = 12
#: How long the result cards get to render after the page has loaded.
RESULTS_WAIT_MS = 10_000
#: The cards read off the page, at most — the page shows 24 to 40.
_CARDS_READ = 40
#: Lines of a card's text kept: the name, rating and price come first.
_LINES_PER_CARD = 30

#: A product page's path: one slug, then ``/p/`` and the item id.
_PRODUCT_PATH = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9\-._~%]*/p/itm[0-9a-zA-Z]+$")
#: A product id as Flipkart writes one: letters and digits.
_PID = re.compile(r"^[A-Za-z0-9]{6,40}$")
#: The price line: the first rupee figure at the start of a line.
_PRICE = re.compile(r"^₹\s*([\d,]+(?:\.\d{1,2})?)")
#: "4.2(70,542)" or "4(57,327)" in the grid; "4.38,153 Ratings & 602 Reviews"
#: in the list, where the one-decimal rating runs into the count.
_RATING = re.compile(r"^([1-5](?:\.\d)?)(?=\(|[\d,]*\d\s*Ratings?\b)", re.IGNORECASE)
#: A card's lines that are labels, never its name.
_NOT_A_NAME = re.compile(
    r"^(?:bestseller|add to compare|sponsored|ad|new|only \d+ left|hot deal|saver deal|top discount of the sale|"
    r"assured|bank offer|trending)$",
    re.IGNORECASE,
)
#: The picture hosts the portal's pages may load from (its CSP's img-src).
_IMAGE_HOSTS = (".flixcart.com", ".flipkart.com")

#: Collects each result card from the open search page: the product link, its
#: title, the card's text lines and its picture. A card is the largest element
#: around a product link that holds links to that one product only, so the
#: text of one card never runs into its neighbour's. A page with one product
#: has no neighbour to stop at, and that climb would end at the page itself —
#: its logo for the picture, a price filter's amount for the price — so there
#: the card is the link, or its nearest ancestor at most four levels up (never
#: the page) whose text has a price line. That search stops at an ancestor
#: with a price line before the link, in page order: such a price is the
#: page's (the price filter beside the results), never the product's, which
#: comes after its link (in the grid, after the link that is its picture). So
#: a product that shows no price (out of stock, coming soon) stays unpriced,
#: rather than taking the filter's amount. The picture is the link's own first.
_COLLECT_CARDS = """
([limit, linesPerCard]) => {
  const pidOf = (href) => {
    const found = (href || "").match(/[?&]pid=([A-Za-z0-9]+)/);
    return found ? found[1] : null;
  };
  const priced = (el) => (el.innerText || "").split("\\n").some((line) => line.trim().startsWith("₹"));
  const pricedBefore = (el, anchor) => {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!(anchor.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_PRECEDING)) return false;
      if ((node.textContent || "").trim().startsWith("₹")) return true;
    }
    return false;
  };
  const cards = [];
  const seen = new Set();
  for (const anchor of document.querySelectorAll('a[href*="/p/itm"]')) {
    const href = anchor.getAttribute("href") || "";
    const pid = pidOf(href);
    if (!pid || seen.has(pid)) continue;
    let card = anchor;
    while (card.parentElement && card.parentElement !== document.body) {
      const pids = new Set();
      for (const link of card.parentElement.querySelectorAll('a[href*="/p/itm"]')) {
        const other = pidOf(link.getAttribute("href"));
        if (other) pids.add(other);
      }
      if (pids.size > 1) break;
      card = card.parentElement;
    }
    if (!card.parentElement || card.parentElement === document.body) {
      card = anchor;
      for (let el = anchor, up = 0; up <= 4 && el.parentElement && el.parentElement !== document.body; up++, el = el.parentElement) {
        if (pricedBefore(el, anchor)) break;
        if (priced(el)) {
          card = el;
          break;
        }
      }
    }
    seen.add(pid);
    const image = anchor.querySelector("img") || card.querySelector("img");
    cards.push({
      href,
      title: anchor.getAttribute("title") || (image && image.getAttribute("alt")) || "",
      lines: (card.innerText || "").split("\\n").map((line) => line.trim()).filter(Boolean).slice(0, linesPerCard),
      image: (image && image.getAttribute("src")) || "",
    });
    if (cards.length >= limit) break;
  }
  return cards;
}
"""


@dataclass
class SearchResult:
    """One product found by a search, ready to be picked."""

    title: str
    #: The canonical product link: ``https://www.flipkart.com/<slug>/p/itm…?pid=PID``.
    link: str
    pid: str
    price: float | None = None
    rating: float | None = None
    image: str | None = None
    #: The struck-out list price, and the discount Flipkart shows off it.
    mrp: float | None = None
    discount_pct: int | None = None
    #: The card's short spec lines ("6 GB RAM | 128 GB ROM"), for the screener.
    highlights: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def query_text(raw: str | None) -> str:
    """The query as searched: trimmed, its spaces collapsed."""
    return " ".join((raw or "").split())


def search_url(query: str) -> str:
    """Flipkart's search page for ``query``."""
    return f"{SEARCH_URL}?{urlencode({'q': query})}"


def canonical_link(href: str | None) -> tuple[str, str] | None:
    """``(link, pid)`` for a result card's link, or None when it is not a
    Flipkart product link: the product page's own path and its product id,
    every tracking parameter dropped (``lid``, ``marketplace``, ``q``, …)."""
    try:
        parts = urlsplit((href or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host and host != "flipkart.com" and not host.endswith(".flipkart.com"):
        return None
    if parts.scheme not in ("", "http", "https") or not _PRODUCT_PATH.match(parts.path):
        return None
    pids = [value.strip() for value in parse_qs(parts.query).get("pid", [])]
    pid = next((value for value in pids if _PID.match(value)), None)
    if pid is None:
        return None
    return f"https://www.flipkart.com{parts.path}?{urlencode({'pid': pid})}", pid


def _image(src: str | None) -> str | None:
    """The card's picture, when it comes from Flipkart's picture hosts."""
    try:
        parts = urlsplit((src or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(_IMAGE_HOSTS):
        return None
    return parts.geturl()


def _title(card: dict, lines: list[str]) -> str | None:
    """The link's own title (the full name), else the card's first line that is
    not a label, a price or a rating."""
    title = " ".join(str(card.get("title") or "").split())
    if title:
        return title
    for line in lines:
        if _NOT_A_NAME.match(line) or _PRICE.match(line) or _RATING.match(line):
            continue
        return line
    return None


def parse_search_cards(cards: list[dict], *, limit: int = MAX_RESULTS) -> list[SearchResult]:
    """The results in the cards read off a search page, in page order, each
    product once, at most ``limit``. A card without a canonical product link
    or a name is left out."""
    results: list[SearchResult] = []
    seen: set[str] = set()
    for card in cards:
        found = canonical_link(card.get("href"))
        if found is None:
            continue
        link, pid = found
        if pid in seen:
            continue
        lines = [" ".join(str(line).split()) for line in card.get("lines") or []]
        lines = [line for line in lines if line]
        title = _title(card, lines)
        if not title:
            continue
        price = next((float(m.group(1).replace(",", "")) for m in map(_PRICE.match, lines) if m), None)
        rating = next((float(m.group(1)) for m in map(_RATING.match, lines) if m), None)
        mrp, discount = parse_mrp(lines, price)
        seen.add(pid)
        results.append(
            SearchResult(
                title=title[:300],
                link=link,
                pid=pid,
                price=price,
                rating=rating,
                image=_image(card.get("image")),
                mrp=mrp,
                discount_pct=discount,
                highlights=_highlights(lines, title),
            )
        )
        if len(results) >= limit:
            break
    return results


#: Every rupee figure in a line: "₹1,104₹2,38553% off" holds two, the second
#: run into the discount.
_RUPEES = re.compile(r"₹\s*([\d,]+(?:\.\d{1,2})?)")
_DISCOUNT = re.compile(r"(\d{1,2})\s*%\s*off", re.IGNORECASE)
#: Card lines that are no highlight: labels, prices, ratings, offers.
_NOT_A_HIGHLIGHT = re.compile(r"₹|%\s*off|ratings?|reviews?|offer|delivery|compare|exchange", re.IGNORECASE)
_HIGHLIGHTS = 4


def split_mrp(price: float | None, glued: str, discount: int | None) -> tuple[float | None, int | None]:
    """``(mrp, discount_pct)`` from the struck-out price's digits as the page
    runs them together: "2,38553" after a "₹1,104" price is ₹2,385 at 53% off.
    The split is the one whose discount agrees with the two prices; with the
    discount on a line of its own (``discount``) the figure is taken whole."""
    digits = glued.replace(",", "")
    if discount is not None:
        try:
            return float(digits), discount
        except ValueError:
            return None, discount
    if price is None or not digits.isdigit():
        return None, None
    for cut in (2, 1):
        if len(digits) <= cut:
            continue
        mrp, pct = int(digits[:-cut]), int(digits[-cut:])
        if mrp > price and abs((mrp - price) * 100 / mrp - pct) <= 1:
            return float(mrp), pct
    return None, None


def parse_mrp(lines: list[str], price: float | None) -> tuple[float | None, int | None]:
    """The list price and discount among a card's (or a page's) lines: the
    first rupee figure after the price that is followed by "% off", glued or
    on the next line."""
    for index, line in enumerate(lines):
        figures = _RUPEES.findall(line)
        if not figures:
            continue
        rest = line[line.rfind("₹") :]
        if _DISCOUNT.search(rest):
            glued = re.match(r"₹\s*([\d,]+)", rest)
            if glued:
                return split_mrp(price, glued.group(1), None)
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        found = _DISCOUNT.match(nxt.strip())
        if found and len(figures) >= 1:
            candidate = figures[-1]
            if price is None or float(candidate.replace(",", "")) > price:
                return split_mrp(price, candidate, int(found.group(1)))
    return None, None


def _highlights(lines: list[str], title: str) -> list[str]:
    kept: list[str] = []
    for line in lines:
        if line == title or title.startswith(line.rstrip(". ")) or _NOT_A_NAME.match(line) or _RATING.match(line):
            continue
        if _NOT_A_HIGHLIGHT.search(line) or len(line) > 90:
            continue
        kept.append(line)
        if len(kept) >= _HIGHLIGHTS:
            break
    return kept


class NoResultsInTime(Exception):
    """Flipkart's search page opened, but no product card appeared on it within
    the wait: a slow page, a check page served as a normal one, or a search
    that found nothing — which a page without cards cannot tell apart."""


async def read_search_results(page: Page, query: str, *, wait_ms: int = RESULTS_WAIT_MS) -> list[SearchResult]:
    """Open Flipkart's search page for ``query`` and read its results.

    Raises when the page cannot be opened or answers with an error status (an
    "Access Denied" 403, a 503: the page load itself does not fail on those),
    and :class:`NoResultsInTime` when no product card appeared within
    ``wait_ms``. An empty list means cards appeared but none was a product the
    parser could read.
    """
    response = await page.goto(search_url(query), wait_until="domcontentloaded")
    if response is None or response.status >= 400:
        raise RuntimeError(f"Flipkart's search page answered {'nothing' if response is None else response.status}")
    appeared = True
    try:
        await page.locator('a[href*="/p/itm"]').first.wait_for(state="attached", timeout=wait_ms)
    except Exception:  # noqa: BLE001 — no result card in time: read what is there
        appeared = False
        log.info("no result card on the search page for %r within %d ms", query[:40], wait_ms)
    cards = await page.evaluate(_COLLECT_CARDS, [_CARDS_READ, _LINES_PER_CARD])
    cards = cards if isinstance(cards, list) else []
    if not appeared and not cards:
        raise NoResultsInTime(query)
    return parse_search_cards(cards)
