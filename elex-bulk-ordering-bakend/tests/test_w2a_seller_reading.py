"""Reading who sells a product (wave 2, package A): names, XPaths, pages.

Everything a choice of seller rests on before anything is pressed: a name
compared the way the page compares it, an XPath literal no seller's name can
break out of, the row XPath that never reaches into another seller's row, and
the sellers page and product page read off their real text (26 Oct 2026).
"""

from __future__ import annotations

import re

import pytest

from app.automation import sellers as S2
from app.automation.sellers import (
    SellerOffer,
    choose_pid,
    is_named_seller,
    parse_seller_rows,
    pid_from_url,
    row_problem,
    same_seller,
    seller_add_to_cart_xpath,
    seller_buy_now_xpath,
    seller_key,
    seller_name_xpath,
    sellers_named,
    sellers_page_url,
    xpath_literal,
)
from app.core import selectors as S
from tests.w2a_seller_pages import (
    CART_ONLY_SELLERS,
    PIGEON_LINK,
    PIGEON_OTHERS_HREF,
    PIGEON_PID,
    PIGEON_PRODUCT,
    PIGEON_SELLERS,
    VIVO_PRODUCT,
    VIVO_SELLERS,
    row_texts,
)

# --- an XPath literal for any name ---------------------------------------------
_ONE_LITERAL = re.compile(r"\"([^\"]*)\"|'([^']*)'")


def _value_of(expression: str) -> str:
    """What an XPath 1.0 string literal — or a concat() of literals — evaluates
    to, refusing anything else: a name that ended its literal early would show
    up here as something that is not a literal."""
    expression = expression.strip()
    if expression.startswith("concat(") and expression.endswith(")"):
        inner, parts, position = expression[len("concat(") : -1], [], 0
        while True:
            found = _ONE_LITERAL.match(inner, position)
            assert found, f"not a literal at {inner[position:]!r}"
            parts.append(found.group(1) if found.group(1) is not None else found.group(2))
            position = found.end()
            rest = inner[position:]
            if not rest.strip():
                break
            assert rest.lstrip().startswith(","), f"something other than a literal: {rest!r}"
            position += len(rest) - len(rest.lstrip()) + 1
            position += len(inner[position:]) - len(inner[position:].lstrip())
        assert len(parts) >= 2, "concat() takes two arguments or more"
        return "".join(parts)
    found = _ONE_LITERAL.fullmatch(expression)
    assert found, f"not a literal: {expression!r}"
    return found.group(1) if found.group(1) is not None else found.group(2)


@pytest.mark.parametrize(
    "name",
    [
        "SAPHEDretail",
        "Jo's Mobiles",  # an apostrophe: a double-quoted literal
        'The "Best" Store',  # double quotes: a single-quoted literal
        "Jo's \"Best\" Shop",  # both: concat()
        '"',
        "'\"'",
        '"leading and trailing"\'',
        # A name written to end its literal and add a selector of its own.
        '"] | //button[1] | //*["',
        "x\"]|//*[@id='pay']|//*[\"y",
    ],
)
def test_the_xpath_literal_holds_any_name_exactly(name):
    literal = xpath_literal(name)

    assert _value_of(literal) == name


def test_a_name_with_both_quotes_is_built_with_concat():
    assert xpath_literal("Jo's \"Best\" Shop") == "concat(\"Jo's \", '\"', \"Best\", '\"', \" Shop\")"
    assert xpath_literal("Jo's Mobiles") == '"Jo\'s Mobiles"'
    assert xpath_literal('The "Best" Store') == "'The \"Best\" Store'"


def test_a_quoted_name_reaches_the_row_xpaths_only_as_its_literal():
    name = "Jo's \"Best\" Shop"
    literal = xpath_literal(seller_key(name))

    # The name's own element once; a row control names it twice — from the
    # name's side, and again from the button's (its own row's name).
    for xpath, uses in (
        (seller_name_xpath(name), 1),
        (seller_buy_now_xpath(name), 2),
        (seller_add_to_cart_xpath(name), 2),
    ):
        assert literal in xpath
        # Outside the literal the XPath is the template's own text.
        assert xpath.replace(literal, "{name}").count("{name}") == uses


# --- comparing names -------------------------------------------------------------
def test_names_compare_without_regard_to_case_or_spacing():
    assert same_seller("TBL Online", "tbl   online")
    assert same_seller("SAPHEDretail", " saphedretail\t")
    assert not same_seller("TBL", "TBL Online")  # a shorter name is another seller
    assert not same_seller("", "")  # a blank name is nobody's
    assert not same_seller(None, "SAPHEDretail")
    assert seller_key("  MYTHANGLORY\nRetail  ") == "mythanglory retail"


# --- the row XPath -----------------------------------------------------------------
LISTED = ["MYTHANGLORYRetail", "SAPHEDretail", "TBL Online"]


def test_the_row_is_the_deepest_element_holding_a_row_button_of_either_kind():
    """As first mapped, the row was "the deepest element around the name that
    holds a BUY NOW". A row offering only ADD TO CART holds none, so that was
    the whole list and its BUY NOW another seller's: on a page in the live row
    shape it matched six other rows' buttons, and beside one other seller it
    would have matched exactly one. A button of either kind marks the row."""
    xpath = seller_buy_now_xpath("SAPHEDretail", LISTED)

    assert f"ancestor::*[.//button[{S.SELLER_BUY_NOW_TEXT} or {S.SELLER_ADD_TO_CART_TEXT}]]" in xpath
    assert f")[last()]//button[{S.SELLER_BUY_NOW_TEXT}][ancestor::" in xpath


def test_a_buy_now_is_tied_to_its_own_rows_name_from_the_buttons_side():
    """The nearest element around the button holding a plain rupee figure
    (its row's price) must hold the chosen name: two rows the text runs
    together cannot hand the chosen seller the next seller's BUY NOW."""
    xpath = seller_buy_now_xpath("SAPHEDretail", LISTED)
    chosen = S.SELLER_NAME_TEST.format(name='"saphedretail"')

    assert xpath.endswith(f"[1][.//*[{chosen}]]]")
    assert 'starts-with(normalize-space(text()),"₹")' in xpath
    # Nothing but digits, "," and "." after the "₹": an offer is no price.
    assert '"0123456789,.","")=""' in xpath


def test_a_row_holding_another_listed_sellers_name_is_not_the_row():
    """A row with no button at all would make the deepest element with a
    button the list itself; the other sellers' names rule it out."""
    xpath = seller_buy_now_xpath("SAPHEDretail", LISTED)
    others = [S.SELLER_NAME_TEST.format(name=key) for key in ('"mythangloryretail"', '"tbl online"')]

    assert f"[not(.//*[{' or '.join(others)}])]" in xpath  # the chosen seller is never among the others
    # Named twice: its row from the name's side, and the button's own row.
    assert xpath.count('"saphedretail"') == 2


def test_with_no_list_read_the_row_xpath_has_no_others_clause():
    assert "[not(" not in seller_buy_now_xpath("SAPHEDretail")


def test_the_name_must_have_a_row_button_after_it():
    """The menus above the list and the footer below it are not rows."""
    assert f"[following::button[{S.SELLER_BUY_NOW_TEXT} or {S.SELLER_ADD_TO_CART_TEXT}]]" in seller_name_xpath("Shopsy")


# --- the sellers page, read off its live text -------------------------------------------
def test_the_live_sellers_page_reads_as_three_rows():
    rows = parse_seller_rows(PIGEON_SELLERS)

    assert rows == [
        SellerOffer(name="MYTHANGLORYRetail", price=749.0, rating=4.3, cod=True, buy_now=True),
        SellerOffer(name="SAPHEDretail", price=749.0, rating=4.2, cod=True, buy_now=True),
        SellerOffer(name="TBL Online", price=749.0, rating=4.4, cod=True, buy_now=True),
    ]


def test_a_seller_with_too_few_ratings_has_no_rating():
    (row,) = parse_seller_rows(VIVO_SELLERS)

    assert row == SellerOffer(name="HatmanRetails", price=20499.0, rating=None, cod=True, buy_now=True)


def test_rows_offering_only_add_to_cart_say_so():
    rows = parse_seller_rows(CART_ONLY_SELLERS)

    assert [row.name for row in rows] == ["MYTHANGLORYRetail", "SAPHEDretail", "TBL Online"]
    assert {row.buy_now for row in rows} == {False}


def test_a_row_with_no_button_stays_a_row_of_its_own():
    """Split at the rating line, not only at the buttons: otherwise a seller
    shown without buttons would read as part of the next seller's row, and
    the next seller would go missing from the list."""
    text = PIGEON_SELLERS.replace(
        "MYTHANGLORYRetail\n4.3\nCash On Delivery available",
        "Gone Shop\n4.0\n₹700\nCurrently unavailable\nMYTHANGLORYRetail\n4.3\nCash On Delivery available",
    )

    rows = parse_seller_rows(text)

    assert [row.name for row in rows] == ["Gone Shop", "MYTHANGLORYRetail", "SAPHEDretail", "TBL Online"]
    assert rows[0] == SellerOffer(name="Gone Shop", price=700.0, rating=4.0, cod=False, buy_now=False)


def test_two_rows_run_together_make_the_page_unreadable():
    """SAPHEDretail shown with no buttons and TBL Online with no rating line:
    nothing splits the two, and a list missing TBL Online would leave its
    row's BUY NOW outside the names that keep the chosen row apart."""
    text = PIGEON_SELLERS.replace(
        "Usually delivered in 3 days Enter pincode for exact delivery dates/charges\nADD TO CART\n BUY NOW\nTBL Online\n4.4",
        "Currently unavailable\nTBL Online",
    )

    assert parse_seller_rows(text) == []


def test_a_row_not_starting_with_a_name_makes_the_page_unreadable():
    text = PIGEON_SELLERS.replace("TBL Online\n4.4\nCash On Delivery available\n", "")

    assert parse_seller_rows(text) == []


def test_buttons_before_any_sellers_name_make_the_page_unreadable():
    """A row laid out with its buttons first was never mapped: those buttons
    belong to no seller the text names. Unreadable, never an error."""
    text = PIGEON_SELLERS.replace("Delivery\nMYTHANGLORYRetail", "Delivery\nADD TO CART\n BUY NOW\nMYTHANGLORYRetail")

    assert parse_seller_rows(text) == []


def test_an_offer_starting_with_a_rupee_figure_is_not_a_second_price():
    text = PIGEON_SELLERS.replace("Get extra ₹50 off", "₹100 Cashback on BHIM Payments App. Min Order Value ₹129.")

    assert [(row.name, row.price) for row in parse_seller_rows(text)] == [
        ("MYTHANGLORYRetail", 749.0),
        ("SAPHEDretail", 749.0),
        ("TBL Online", 749.0),
    ]


def test_a_price_split_over_lines_is_one_price():
    """Without the page's styles the price, the MRP and the discount are lines
    of their own."""
    text = PIGEON_SELLERS.replace("₹749₹1,30542% off", "₹749\n₹1,305\n42% off")

    assert [row.price for row in parse_seller_rows(text)] == [749.0, 749.0, 749.0]


# --- the chosen row, read as text before its BUY NOW is pressed ------------------------------
def test_every_live_row_reads_as_its_own_sellers_row():
    rows = row_texts()

    assert set(rows) == set(LISTED)
    for name, text in rows.items():
        assert row_problem(text, name) is None, name
        assert row_problem(text, "Somebody Else") == f"it starts with {name!r}, not the seller's name"
    assert row_problem(row_texts(VIVO_SELLERS)["HatmanRetails"], "hatmanretails") is None


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("", "its text could not be read"),
        ("SAPHEDretail\n4.2\nCash On Delivery available\nADD TO CART\nBUY NOW", "it shows 0 prices"),
        ("SAPHEDretail\n4.2\n₹749\nADD TO CART", "it shows 0 BUY NOW buttons"),
        ("SAPHEDretail\n4.2\n₹749\nADD TO CART\nADD TO CART\nBUY NOW", "it shows 2 ADD TO CART buttons"),
        (
            "SAPHEDretail\n4.2\nCash On Delivery available\n₹749\nNew Shop\nCash On Delivery available\nBUY NOW",
            "it shows 2 cash-on-delivery lines",
        ),
    ],
)
def test_a_row_that_is_not_one_sellers_row_says_why(text, problem):
    assert row_problem(text, "SAPHEDretail") == problem


# --- naming the chosen seller on a checkout ------------------------------------------------
@pytest.mark.parametrize(
    ("found", "chosen", "same"),
    [
        ("SAPHEDretail", "saphedretail", True),
        ("SAPHEDretail (4.2)", "SAPHEDretail", True),
        ("SAPHEDretail | 4.2★", "SAPHEDretail", True),
        ("SAPHEDretail - 4 years with Flipkart", "SAPHEDretail", True),
        ("TBL Online", "TBL", False),  # a word after the name: another seller
        ("Super-Retail", "Super", False),
        ("SAPHEDretail 4.2", "SAPHEDretail", False),
        ("SAPHEDretail", "SAPHEDretail Hub", False),
        ("", "SAPHEDretail", False),
        ("SAPHEDretail", "", False),
        # A label after the name names another seller after it.
        ("SAPHEDretail | Seller: TBL Online", "SAPHEDretail", False),
        ("SAPHEDretail · Sold by TBL Online", "SAPHEDretail", False),
        ("SAPHEDretail | Top Seller", "SAPHEDretail", True),  # a badge, not a label
    ],
)
def test_a_name_on_a_page_is_the_chosen_seller_only_when_nothing_but_punctuation_follows(found, chosen, same):
    assert is_named_seller(found, chosen) is same


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("Seller: SAPHEDretail | Seller: TBL Online", ["SAPHEDretail", "TBL Online"]),
        ("Sold by SAPHEDretail · Sold by TBL Online", ["SAPHEDretail", "TBL Online"]),
        ("Seller: SAPHEDretail (4.2) Seller: TBL Online", ["SAPHEDretail (4.2)", "TBL Online"]),
        ("Seller:SAPHEDretail|Seller:TBL Online", ["SAPHEDretail", "TBL Online"]),
        ("Seller: SAPHEDretail - Seller: TBL Online", ["SAPHEDretail", "TBL Online"]),
        ("Pigeon Special 3 L · Sold by SAPHEDretail · Sold by TBL Online", ["SAPHEDretail", "TBL Online"]),
    ],
)
def test_two_sellers_named_on_one_checkout_line_are_both_read(text, named):
    """A name ends at the next label on its line, its separators trimmed off."""
    assert sellers_named(text, sold_by_anywhere=True) == named


def test_on_a_checkout_sold_by_counts_anywhere_in_a_line():
    text = "Pigeon Special 3 L · Sold by TBL Online\nItems sold by Flipkart are packed with care\nSeller: SAPHEDretail"

    assert sellers_named(text) == ["SAPHEDretail"]
    assert sellers_named(text, sold_by_anywhere=True) == [
        "TBL Online",
        "Flipkart are packed with care",
        "SAPHEDretail",
    ]


def test_without_its_column_headings_a_page_is_not_read_as_a_list():
    assert parse_seller_rows("ABOUT\nContact Us") == []
    assert parse_seller_rows(PIGEON_PRODUCT) == []
    assert parse_seller_rows("") == []


def test_the_footer_is_never_a_row():
    rows = parse_seller_rows(PIGEON_SELLERS + "\nTelephone: 044-45614700\nBecome a Seller")

    assert [row.name for row in rows] == ["MYTHANGLORYRetail", "SAPHEDretail", "TBL Online"]


# --- what a page says about its seller -------------------------------------------------
def test_the_product_pages_seller_line_is_read():
    assert sellers_named(PIGEON_PRODUCT) == ["MYTHANGLORYRetail"]
    assert sellers_named(VIVO_PRODUCT) == ["HatmanRetails"]


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("Pigeon Special 3 L\nSeller: SAPHEDretail\n₹749", ["SAPHEDretail"]),
        ("Pigeon Special 3 L\nseller:\nSAPHEDretail", ["SAPHEDretail"]),
        ("Pigeon Special 3 L\nSold by SAPHEDretail\n₹749", ["SAPHEDretail"]),
        ("Pigeon Special 3 L\nSOLD BY: SAPHEDretail", ["SAPHEDretail"]),
        ("Seller: SAPHEDretail\nSeller: saphedretail", ["SAPHEDretail"]),  # one seller, named twice
        ("Seller: SAPHEDretail\nSold by TBL Online", ["SAPHEDretail", "TBL Online"]),
        # Prose, and words that merely contain "seller", name nobody.
        ("Items sold by Flipkart are packed with care", []),
        ("Bestseller: pressure cookers\nBecome a Seller\nSee other sellers", []),
        ("Order Summary\nPrice details", []),
    ],
)
def test_a_page_names_its_seller_as_seller_or_sold_by(text, named):
    assert sellers_named(text) == named


# --- which product's sellers page ---------------------------------------------------------
def test_the_pid_comes_from_the_link_or_the_see_other_sellers_link():
    assert pid_from_url(PIGEON_LINK) == PIGEON_PID
    assert pid_from_url(PIGEON_OTHERS_HREF) == PIGEON_PID
    assert pid_from_url("https://dl.flipkart.com/s/uFy91dNNNN") is None
    assert pid_from_url("/sellers?pid=PRCF%22%5D") is None  # not letters and digits: not a product id
    assert pid_from_url(None) is None


def test_the_link_names_the_product_unless_the_page_shows_another():
    """A size chosen on the page, or a link Flipkart redirected, shows another
    variant than the link names; the page on screen is what the run read and
    records, so its sellers are the ones to buy from."""
    assert choose_pid(link=PIGEON_LINK, landed=PIGEON_LINK, others_href=PIGEON_OTHERS_HREF) == PIGEON_PID
    short = "https://dl.flipkart.com/s/uFy91dNNNN"
    assert choose_pid(link=short, landed=short, others_href=PIGEON_OTHERS_HREF) == PIGEON_PID
    assert choose_pid(link=PIGEON_LINK, landed=PIGEON_LINK, others_href="/sellers") == PIGEON_PID
    assert choose_pid(link=PIGEON_LINK, landed=PIGEON_LINK, others_href="/sellers?pid=PRCFSIZEXLVARIANT") == (
        "PRCFSIZEXLVARIANT"
    )
    assert choose_pid(link=short, landed=short, others_href="/sellers") is None


def test_the_sellers_page_address():
    assert sellers_page_url(PIGEON_PID) == f"https://www.flipkart.com/sellers?pid={PIGEON_PID}"


def test_a_sellers_name_is_bounded_where_both_doors_read_it():
    assert S2.MAX_SELLER_CHARS == 100
