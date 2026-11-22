"""Flipkart's seller pages as package A's tests read them (wave 2).

The texts are what the live pages said on 26 Oct 2026, read in a signed-out
browser (the pigeon cooker has three sellers, the vivo phone one), trimmed to
the parts the flow reads: the product page's seller block, and the sellers
page from its headings through the start of the footer.
"""

from __future__ import annotations

from app.automation.sellers import parse_seller_rows, seller_row_xpath
from app.core import selectors as S
from tests.fakes import FakePage

PIGEON_PID = "PRCFNFEBWFFDBVHP"
PIGEON_LINK = f"https://www.flipkart.com/pigeon-special-3-l-inner-lid-pressure-cooker/p/itm0c2cd3b721e22?pid={PIGEON_PID}"
PIGEON_OTHERS_HREF = f"/sellers?pid={PIGEON_PID}"
VIVO_LINK = "https://www.flipkart.com/vivo-t5-lite-44w-5g-twilight-shadow-128-gb/p/itm5d7e2b1f6c3a0?pid=MOBHNDTDQCGAFYXG"

PIGEON_PRODUCT = """Pigeon Special 3 L Pressure Cooker (Aluminium)
4.1
| 75,149
43%
1,305
₹749
Buy at ₹711
Delivery details
Location not set
Select delivery location
Delivery by
Thursday, 29 Oct
Seller: MYTHANGLORYRetail
See other sellers
4.3
•
4 years with Flipkart
7-Day
Return
Cash on
Delivery
Flipkart
Assured
Product highlights"""

PIGEON_SELLERS = """Explore Plus
Login
Become a Seller
More
Cart
Electronics
Offer Zone
All Sellers
Pigeon Special 3 L Inner Lid Pressure Cooker
4.1(75,149)
Seller
Price
Delivery
MYTHANGLORYRetail
4.3
Cash On Delivery available
₹749₹1,30542% off
Get extra ₹50 off
Buy for 500 get 10% off your Next Buy
9 more offers
Usually delivered in 3 days Enter pincode for exact delivery dates/charges
ADD TO CART
 BUY NOW
SAPHEDretail
4.2
Cash On Delivery available
₹749₹1,30542% off
Buy for 500 get 10% off your Next Buy
Extra 10% upto ₹1250
8 more offers
Usually delivered in 3 days Enter pincode for exact delivery dates/charges
ADD TO CART
 BUY NOW
TBL Online
4.4
Cash On Delivery available
₹749₹1,30542% off
Get extra ₹50 off
Buy for 500 get 10% off your Next Buy
9 more offers
Usually delivered in 3 days Enter pincode for exact delivery dates/charges
ADD TO CART
 BUY NOW
ABOUT
Contact Us
About Us
Careers
GROUP COMPANIES
Myntra
Cleartrip
Shopsy
© 2007-2026 Flipkart.com"""

VIVO_PRODUCT = """vivo T5 Lite 44W 5G (Twilight Shadow, 128 GB) (4 GB RAM)
4.3
| 5,442
₹20,499
Delivery details
Location not set
Select delivery location
Delivery by
Thursday, 29 Oct
Seller: HatmanRetails
5 months with flipkart
Shop with peace of mind
Cash on
Delivery
Product highlights
Buy now"""

VIVO_SELLERS = """All Sellers
vivo T5 Lite 44W 5G (Twilight Shadow, 128 GB)
4.3(5,442)
Seller
Price
Delivery
HatmanRetails
(Not Enough Ratings)
Cash On Delivery available
7 Days Replacement Policy
₹20,499₹33,99939% off
EMI From Rs.721?
33 more offers
Usually delivered in 3 days Enter pincode for exact delivery dates/charges
ADD TO CART
 BUY NOW
ABOUT
Contact Us"""

#: The pigeon page as an apparel listing might read: rows offering only ADD
#: TO CART (an assumption — no such page was mapped live).
CART_ONLY_SELLERS = PIGEON_SELLERS.replace("ADD TO CART\n BUY NOW", "ADD TO CART")


def listed(text: str = PIGEON_SELLERS) -> list[str]:
    """The sellers a sellers page lists, as the flow reads them."""
    return [offer.name for offer in parse_seller_rows(text)]


def row_texts(text: str = PIGEON_SELLERS) -> dict[str, str]:
    """Each listed seller's row as its own element's text reads: from its
    name through its last button."""
    lines = [line.strip() for line in text.splitlines()]
    names = listed(text)
    if not names:
        return {}
    starts, at = [], lines.index("Delivery") + 1
    for name in names:
        at = lines.index(name, at)
        starts.append(at)
        at += 1
    rows = {}
    for number, start in enumerate(starts):
        block = lines[start : starts[number + 1] if number + 1 < len(starts) else len(lines)]
        buttons = [index for index, line in enumerate(block) if line.upper() in ("BUY NOW", "ADD TO CART")]
        rows[names[number]] = "\n".join(block[: buttons[-1] + 1] if buttons else block)
    return rows


def seller_page(
    *,
    product: str = PIGEON_PRODUCT,
    others_href: str | None = PIGEON_OTHERS_HREF,
    sellers: str = PIGEON_SELLERS,
    checkout: str | None = None,
    total: str = "₹749",
) -> FakePage:
    """A FakePage that is the product page, its sellers page and the checkout,
    each saying its own text while the browser is on it. ``others_href`` None
    is a product with one seller (no "See other sellers" link)."""
    page = FakePage(inner_text_map={S.ORDER_TOTAL: total})
    on_sellers_page = {S.PAGE_BODY: sellers}
    names = listed(sellers)
    for name, row in row_texts(sellers).items():
        on_sellers_page[seller_row_xpath(name, names)] = row
    page.inner_text_by_url = {
        "/p/": {S.PAGE_BODY: product},
        "/sellers?": on_sellers_page,
    }
    if checkout is not None:
        page.inner_text_by_url["/viewcheckout"] = {S.PAGE_BODY: checkout}
    if others_href is not None:
        page.attrs[(S.OTHER_SELLERS_LINK, "href")] = others_href
    return page
