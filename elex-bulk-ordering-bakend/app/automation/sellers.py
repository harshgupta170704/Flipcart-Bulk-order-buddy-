"""Who sells a product, read off Flipkart's pages (wave 2, mapped 26 Oct 2026).

Every account bought from whichever seller the product page shows by default,
and the orders the tool placed were all cancelled "as per seller policy
restrictions applicable to this brand" — a seller's decision. Picking another
seller is the operator's main lever, so a run may name one.

What Flipkart shows, mapped live in a signed-out browser, read only:

* The product page prints "Seller: <name>" and, when other sellers offer the
  product, a "See other sellers" link to ``/sellers?pid=<PID>``. The PID is
  the product id; it is also the ``pid=`` of most product links.
* That page lists every seller, one row each, under "All Sellers" with the
  columns Seller / Price / Delivery. A row reads: the name, its rating, "Cash
  On Delivery available", the price, offers, "Usually delivered in 3 days",
  then one ADD TO CART and one BUY NOW button. The rows carry obfuscated
  classes only, so nothing here relies on a class.

This module only READS: what the product page says about its seller, and the
sellers page's rows. Pressing anything is the order flow's job
(``app.automation.ordering.choose_seller``), which counts the chosen row's
BUY NOW first and presses it only when it matches exactly once.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.async_api import Page

from app.core import selectors as S

log = logging.getLogger("ordering.sellers")

#: The longest seller name a run accepts, from the portal and the command line
#: alike. Flipkart's are short ("SAPHEDretail", "TBL Online"); the bound keeps
#: a pasted page out of the XPath a run builds from the name.
MAX_SELLER_CHARS = 100

#: How long the product page's seller block ("Seller: X", "See other sellers")
#: gets to render after the page itself: it arrives with the delivery details.
PRODUCT_SELLER_WAIT_MS = 6_000
#: The line and the link render together; once the line is there, the link
#: gets this long to follow before the product is taken to have one seller.
SELLER_LINK_SETTLE_MS = 1_000
#: How long the sellers page's rows get to arrive after the page has loaded.
SELLERS_PAGE_WAIT_MS = 10_000
#: How long one read of a page's text may wait.
READ_TIMEOUT_MS = 4_000


# --- Comparing names ----------------------------------------------------------
#: The only characters XPath's normalize-space() collapses.
_XML_SPACE = re.compile(r"[ \t\r\n]+")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def seller_key(name: str | None) -> str:
    """A seller's name as compared: spaces collapsed, letters lower-cased.

    The same folding the page-side XPath does (normalize-space, and an A–Z
    translate), so a name typed "tbl  online" finds the row reading "TBL
    Online", and the two sides can never disagree about a match. Only A–Z is
    folded because XPath 1.0 has no wider lower-casing.
    """
    return _XML_SPACE.sub(" ", name or "").strip(" ").translate(_ASCII_LOWER)


def same_seller(first: str | None, second: str | None) -> bool:
    """Whether two names are one seller. A blank name is nobody's."""
    key = seller_key(first)
    return bool(key) and key == seller_key(second)


def xpath_literal(text: str) -> str:
    """``text`` as an XPath 1.0 string literal, whatever quotes it holds.

    XPath 1.0 has no escape inside a literal, so a name holding both kinds of
    quote is built with concat(): ``Jo's "Shop"`` becomes
    ``concat("Jo's ", '"', "Shop", '"', "")``. A seller's name is the
    operator's text, and it must never be able to end the literal early and
    change what the XPath selects.
    """
    if '"' not in text:
        return f'"{text}"'
    if "'" not in text:
        return f"'{text}'"
    return "concat(" + ", '\"', ".join(f'"{part}"' for part in text.split('"')) + ")"


def seller_name_xpath(name: str) -> str:
    """The named seller's name in its row on the sellers page."""
    return S.SELLER_NAME.format(name=xpath_literal(seller_key(name)))


def _others_clause(name: str, others: list[str] | tuple[str, ...]) -> str:
    """``[not(.//*[…])]`` naming every other listed seller, so a candidate
    row holding one of them — the whole list, not a row — is never taken for
    the chosen seller's row."""
    tests: dict[str, str] = {}
    for other in others:
        key = seller_key(other)
        if key and key != seller_key(name):
            tests.setdefault(key, S.SELLER_NAME_TEST.format(name=xpath_literal(key)))
    return f"[not(.//*[{' or '.join(tests.values())}])]" if tests else ""


def seller_row_xpath(name: str, others: list[str] | tuple[str, ...] = ()) -> str:
    """The named seller's row itself, the element its controls are looked
    for in — read as text before its BUY NOW is pressed (``row_problem``)."""
    return S.SELLER_ROW.format(name=xpath_literal(seller_key(name)), others=_others_clause(name, others))


def seller_row_control_xpath(name: str, control: str, others: list[str] | tuple[str, ...] = ()) -> str:
    """One control of the named seller's row — and only of that row.

    ``others`` are the other sellers the page lists; a candidate row holding
    any of their names is the list itself and matches nothing.
    """
    return S.SELLER_ROW_CONTROL.format(
        name=xpath_literal(seller_key(name)), others=_others_clause(name, others), control=control
    )


def seller_buy_now_xpath(name: str, others: list[str] | tuple[str, ...] = ()) -> str:
    """The BUY NOW button of the named seller's row."""
    return seller_row_control_xpath(name, S.SELLER_BUY_NOW_TEXT, others)


def seller_add_to_cart_xpath(name: str, others: list[str] | tuple[str, ...] = ()) -> str:
    """The ADD TO CART button of the named seller's row."""
    return seller_row_control_xpath(name, S.SELLER_ADD_TO_CART_TEXT, others)


# --- Which product's sellers ---------------------------------------------------
#: A product id as Flipkart writes one ("PRCFNFEBWFFDBVHP"): letters and digits.
_PID_RE = re.compile(r"[A-Za-z0-9]{6,40}")


def pid_from_url(url: str | None) -> str | None:
    """The ``pid=`` a link carries, or None. Anything but letters and digits
    is not a product id, and is never put into the sellers page's address."""
    try:
        values = parse_qs(urlsplit(url or "").query).get("pid", [])
    except ValueError:
        return None
    for value in values:
        value = value.strip()
        if _PID_RE.fullmatch(value):
            return value
    return None


def choose_pid(*, link: str | None, landed: str | None, others_href: str | None) -> str | None:
    """The product id whose sellers page to open.

    The link's ``pid=``, else the product page's own "See other sellers" link
    (or the address the link landed on — a short link carries no pid). When
    the link and the page name different products, the page's wins: a size
    chosen on the page, or a link Flipkart redirected, shows another variant
    than the link names, and the product page is what the run read and
    records. Buying the link's variant from its sellers page would buy what
    nobody looked at.
    """
    from_link = pid_from_url(link)
    from_page = pid_from_url(others_href) or pid_from_url(landed)
    if from_link and from_page and from_link != from_page:
        log.warning(
            "the link names product %s but the page shows %s (a size chosen, or a redirect); using the page's",
            from_link,
            from_page,
        )
        return from_page
    return from_link or from_page


def sellers_page_url(pid: str) -> str:
    """Flipkart's page listing every seller of one product."""
    return f"{S.SELLERS_URL}?{urlencode({'pid': pid})}"


# --- What a page says --------------------------------------------------------
#: A label naming a seller: "Seller:" (the colon makes it specific) or "Sold by".
_LABEL = r"\bseller\s*:|\bsold[ \t]+by\b"
_LABEL_RE = re.compile(_LABEL, re.IGNORECASE)
#: A seller's name after its label: the rest of the line, up to the next label
#: on it. "Seller: X | Seller: Y" names two sellers, never one called
#: "X | Seller: Y" — which would read as the chosen X and hide Y.
_NAME = rf"((?:(?!{_LABEL})[^\n])+)"
#: What may sit between a name and the next label on its line, trimmed off
#: the name: spaces, a bar, a dot, a star, a dash, a comma, a semicolon, a
#: slash, an opening bracket.
_TRAILING_SEPARATORS = " \t|·•★-–—,;/(["
#: "Seller: X" anywhere (the colon makes it specific), or "Sold by X" at the
#: start of a line — in the middle of a sentence "sold by" is prose ("items
#: sold by Flipkart …"), not a page naming its seller.
_SELLER_LINE = re.compile(
    rf"\bseller\s*:\s*{_NAME}|(?:^|\n)[ \t]*sold[ \t]+by\b[ \t]*:?\s*{_NAME}",
    re.IGNORECASE,
)


#: "Sold by X" anywhere in a line, for a checkout: there a seller named in the
#: middle of a line ("Pigeon Special 3 L · Sold by X") must not go unseen.
_SOLD_BY_ANYWHERE = re.compile(rf"\bsold[ \t]+by\b[ \t]*:?\s*{_NAME}", re.IGNORECASE)
#: What may follow a seller's name on its line without being part of it: a
#: bracket, a bar, a dot or a star ("SAPHEDretail (4.2)"), or a dash between
#: spaces. Never a letter, a digit, or a "-", "&", "'" or "." run on: "TBL
#: Online" is not "TBL", nor "Super-Retail" "Super".
_AFTER_NAME = re.compile(r"\s*[(\[|·•★]|\s+[-–—]\s")


def sellers_named(text: str | None, *, sold_by_anywhere: bool = False) -> list[str]:
    """Every seller a page's text names, in page order, each once.

    The product page names its own in a line of its own ("Seller:
    MYTHANGLORYRetail", seen live); a checkout may name the seller of the item
    it holds the same way, or as "Sold by X". ``sold_by_anywhere`` (the
    checkout) also takes a "sold by" in the middle of a line, with the rest of
    that line as the name: prose then reads as a seller nobody chose, which
    refuses the order — a refusal costs a run, a seller missed could cost an
    order from the very seller the run was steered away from.

    A name ends at the next label on its line, so a line naming two sellers
    ("Seller: X | Seller: Y", "Sold by X · Sold by Y") reads as both.
    """
    found: list[str] = []
    seen: set[str] = set()
    matches = [(m.start(), m.group(1) or m.group(2) or "") for m in _SELLER_LINE.finditer(text or "")]
    if sold_by_anywhere:
        matches += [(m.start(), m.group(1)) for m in _SOLD_BY_ANYWHERE.finditer(text or "")]
    for _start, raw in sorted(matches, key=lambda match: match[0]):
        name = " ".join(raw.split()).rstrip(_TRAILING_SEPARATORS)
        key = seller_key(name)
        if key and key not in seen:
            seen.add(key)
            found.append(name)
    return found


def is_named_seller(found: str | None, chosen: str | None) -> bool:
    """Whether a name read off a page is the chosen seller: the same name, or
    the same name with a bracket, bar or star after it on its line, as a page
    may run the rating on ("SAPHEDretail (4.2)"). A word after the name makes
    it another seller's ("TBL Online" is not "TBL"), and so does a label
    after it ("SAPHEDretail | Seller: TBL Online" names TBL Online too)."""
    found_key, chosen_key = seller_key(found), seller_key(chosen)
    if not chosen_key or not found_key.startswith(chosen_key):
        return False
    rest = found_key[len(chosen_key) :]
    if _LABEL_RE.search(rest):
        return False
    return not rest or bool(_AFTER_NAME.match(rest))


@dataclass
class SellerOffer:
    """One seller of a product, as the sellers page lists it."""

    name: str
    price: float | None = None
    rating: float | None = None
    #: "Cash On Delivery available" on the row. None when unknown: a product
    #: with one seller is described by its product page, which does not say.
    cod: bool | None = None
    #: Whether the row offers BUY NOW, the button a chosen seller is bought
    #: through. None when unknown, as for ``cod``.
    buy_now: bool | None = None

    def as_dict(self) -> dict:
        return asdict(self)


#: The column headings the rows follow, as the page's text reads them.
_HEADINGS = ("seller", "price", "delivery")
_BUY_NOW = "buy now"
_ADD_TO_CART = "add to cart"
_BUTTONS = (_BUY_NOW, _ADD_TO_CART)
#: "4.3" — a seller with few ratings reads "(Not Enough Ratings)" instead.
_RATING_RE = re.compile(r"\d(?:\.\d{1,2})?")
_NO_RATING_RE = re.compile(r"\(\s*not enough ratings?\s*\)", re.IGNORECASE)
#: A row's price line, and nothing but: "₹749₹1,30542% off" is the price, then
#: the struck-out MRP run into the discount. The price is its first figure.
#: Without the page's styles the three are lines of their own ("₹749",
#: "₹1,305", "42% off"), and lines in a row running together are one price.
#: An offer that starts with a rupee figure ("₹100 Cashback on …") says more
#: than a price, so it is not one.
_PRICE_LINE = re.compile(r"₹\s*([\d,]+(?:\.\d{1,2})?)(?:\s*₹\s*[\d,]+(?:\.\d{1,2})?)?(?:\s*\d{1,3}\s*%\s*off)?", re.IGNORECASE)
_COD_RE = re.compile(r"cash on delivery available", re.IGNORECASE)


def _is_rating_line(line: str) -> bool:
    return bool(_RATING_RE.fullmatch(line) or _NO_RATING_RE.fullmatch(line))


def _prices(lines: list[str]) -> list[float]:
    """The prices among a row's lines, one per run of price lines."""
    prices: list[float] = []
    in_run = False
    for line in lines:
        found = _PRICE_LINE.fullmatch(line)
        if found and not in_run:
            prices.append(float(found.group(1).replace(",", "")))
        in_run = bool(found)
    return prices


def parse_seller_rows(text: str | None) -> list[SellerOffer]:
    """The rows of a sellers page, from its text.

    The rows follow the "Seller / Price / Delivery" headings. A row's first
    line is the seller's name and its second the rating ("4.3", or "(Not
    Enough Ratings)"); then the cash-on-delivery line, the price, the offers,
    and its buttons ("ADD TO CART", "BUY NOW"). A row starts after the
    previous row's buttons, or at a line followed by a rating line — so a row
    with no button of its own still reads as a row of its own when the next
    row shows its rating line. What follows the last row is the footer.

    A row that does not start with a name, or shows two prices, is two rows
    run together or a layout never mapped: a seller in it would be missing
    from the list, and the list is what keeps the chosen row's BUY NOW from
    being another seller's. So the whole page then reads as unreadable, and
    so does a page whose buttons come before any seller's name.

    Not every two rows run together show it. A row with no buttons, no price
    and no cash-on-delivery line, followed by one whose rating line is
    missing or in a form never mapped, reads as ONE row: the first seller's,
    with the second's price and buttons, and the second seller unlisted. The
    text alone cannot tell, so the BUY NOW XPath does not rest on the list
    alone: it also ties a button to its own row's name
    (``S.SELLER_ROW_CONTROL``).

    An empty list means the page could not be read as a list of sellers —
    never that a product has none.
    """
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    lowered = [line.lower() for line in lines]
    start = next(
        (index + len(_HEADINGS) for index in range(len(lines)) if tuple(lowered[index : index + 3]) == _HEADINGS),
        None,
    )
    if start is None:
        return []
    body = lines[start:]

    rows: list[SellerOffer] = []
    current: list[str] = []
    buttons: set[str] = set()
    #: Whether the group being read began as a row does: a name, then a rating.
    rated = False
    for index, line in enumerate(body):
        low = line.lower()
        if low in _BUTTONS:
            buttons.add(low)
            continue
        rating_next = index + 1 < len(body) and _is_rating_line(body[index + 1])
        if buttons or (current and rating_next):
            # A group is kept when it began as a row or ended with buttons;
            # anything else (the footer, a stray line) is not a seller.
            if (buttons or rated) and not _keep_row(rows, current, buttons):
                return []
            current, buttons = [], set()
        if not current:
            rated = rating_next
        current.append(line)
    if current and (buttons or rated) and not _keep_row(rows, current, buttons):
        return []
    return rows


def _keep_row(rows: list[SellerOffer], lines: list[str], buttons: set[str]) -> bool:
    """Add one row's offer; False when the lines cannot be one seller's row."""
    if not lines:
        # Buttons with no name before them: a layout never mapped, whose
        # buttons belong to no seller the text can name.
        log.warning("buttons on the sellers page come before any seller's name; the list is not read")
        return False
    name = lines[0]
    if name.startswith("₹") or _is_rating_line(name):
        log.warning("a row on the sellers page does not start with a name (%r); the list is not read", name[:40])
        return False
    prices = _prices(lines[1:])
    if len(prices) > 1:
        log.warning("%r's row on the sellers page shows %d prices — two rows run together?; the list is not read",
                    name[:40], len(prices))
        return False
    rating = next((float(line) for line in lines[1:4] if _RATING_RE.fullmatch(line)), None)
    rows.append(
        SellerOffer(
            name=name,
            price=prices[0] if prices else None,
            rating=rating,
            cod=any(_COD_RE.fullmatch(line) for line in lines[1:]),
            buy_now=_BUY_NOW in buttons,
        )
    )
    return True


def row_problem(text: str | None, name: str) -> str | None:
    """Why ``text``, the text of the element whose BUY NOW is about to be
    pressed, is not one row of the seller ``name`` — or None when it is.

    The row XPath leaves out every row holding another seller the list names;
    this reads the row itself, so a seller missing from the list is caught
    too when its part of the text shows a second price, rating,
    cash-on-delivery line or button. One seller's row starts with its name
    and shows one price, one BUY NOW, and at most one rating, one
    cash-on-delivery line and one ADD TO CART; two rows run together show two
    of most of them. Not when the chosen seller's own row shows none of those
    (no buttons, no price, no cash-on-delivery line): the two then read as one
    clean row, and the BUY NOW XPath's tie to its own row's name is what keeps
    the other seller's button out (``S.SELLER_ROW_CONTROL``).
    """
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return "its text could not be read"
    if not same_seller(lines[0], name):
        return f"it starts with {lines[0][:40]!r}, not the seller's name"
    lowered = [line.lower() for line in lines]
    counts = {
        "BUY NOW buttons": (lowered.count(_BUY_NOW), 1, 1),
        "prices": (len(_prices(lines[1:])), 1, 1),
        "ratings": (sum(1 for line in lines[1:] if _is_rating_line(line)), 0, 1),
        "cash-on-delivery lines": (sum(1 for line in lines if _COD_RE.fullmatch(line)), 0, 1),
        "ADD TO CART buttons": (lowered.count(_ADD_TO_CART), 0, 1),
    }
    for what, (found, fewest, most) in counts.items():
        if not fewest <= found <= most:
            return f"it shows {found} {what}"
    return None


# --- Reading the pages -----------------------------------------------------------
async def element_text(page: Page, xpath: str) -> str:
    """The text of the first element ``xpath`` matches; empty when there is
    none or it cannot be read — which names nobody and passes no check."""
    try:
        return await page.locator(f"xpath={xpath}").first.inner_text(timeout=READ_TIMEOUT_MS) or ""
    except Exception:  # noqa: BLE001
        return ""


async def _page_text(page: Page) -> str:
    """The text of the page the browser is on; empty when it cannot be read."""
    return await element_text(page, S.PAGE_BODY)


async def count_matches(page: Page, xpath: str) -> int:
    """How many elements match now; 0 when the page cannot say — which every
    caller reads as "not there", never as permission."""
    try:
        return await page.locator(f"xpath={xpath}").count()
    except Exception:  # noqa: BLE001
        return 0


async def _other_sellers_href(page: Page) -> str | None:
    """Where the product page's "See other sellers" link points, or None."""
    link = page.locator(f"xpath={S.OTHER_SELLERS_LINK}")
    try:
        # Counted first: reading an attribute of an element that is not there
        # waits out the whole timeout, and one seller means no link at all.
        if not await link.count():
            return None
        href = await link.first.get_attribute("href", timeout=READ_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — no link read is no link
        return None
    return (href or "").strip() or None


@dataclass
class ProductSellers:
    """What the product page says about who sells it."""

    #: Its "Seller: X": who an order buys from when no seller is chosen.
    default: str | None = None
    #: The "See other sellers" link — None when the product has one seller.
    others_href: str | None = None


async def read_product_sellers(page: Page, *, wait_ms: int = PRODUCT_SELLER_WAIT_MS) -> ProductSellers:
    """The open product page's seller and its "See other sellers" link.

    The block renders a moment after the page, so it is waited for, bounded:
    a check made too early would take a product with many sellers for one
    with a single seller. Never raises.
    """
    waited = 0
    while True:
        href = await _other_sellers_href(page)
        named = sellers_named(await _page_text(page))
        if href or named or waited >= wait_ms:
            break
        await page.wait_for_timeout(500)
        waited += 500
    if named and not href:
        # The line came first; the link beside it gets a beat to follow.
        await page.wait_for_timeout(SELLER_LINK_SETTLE_MS)
        href = await _other_sellers_href(page)
    return ProductSellers(default=named[0] if named else None, others_href=href)


async def read_sellers_page(page: Page, pid: str, *, wait_ms: int = SELLERS_PAGE_WAIT_MS) -> list[SellerOffer]:
    """Open the sellers page of ``pid`` and read its rows (see ``parse_seller_rows``).

    The rows arrive after the page has loaded; a row's button showing is the
    sign they are there, waited for up to ``wait_ms``.
    """
    await page.goto(sellers_page_url(pid), wait_until="domcontentloaded")
    waited = 0
    while waited < wait_ms and not await count_matches(page, S.SELLER_ROW_BUTTON):
        await page.wait_for_timeout(500)
        waited += 500
    return parse_seller_rows(await _page_text(page))


async def read_seller_offers(
    page: Page, product_url: str, *, price: float | None = None
) -> tuple[str | None, list[SellerOffer]]:
    """For the product preview: the product's default seller and every seller
    listed, from the open product page (and its sellers page, when it has
    other sellers).

    A product with one seller has no sellers page: its product page is the
    whole list, with the product page's ``price``. Raises when the sellers
    page cannot be found; the caller decides what a failure means.
    """
    on_page = await read_product_sellers(page)
    if not on_page.others_href:
        return on_page.default, ([SellerOffer(name=on_page.default, price=price)] if on_page.default else [])
    pid = choose_pid(link=product_url, landed=page.url, others_href=on_page.others_href)
    if not pid:
        raise LookupError("the product page links to its other sellers without a product id")
    return on_page.default, await read_sellers_page(page, pid)
