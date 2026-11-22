"""Product Screener figures: list price, discount, highlights, the product
page's price block and its variants."""

import json
from pathlib import Path

from app.automation.search import parse_mrp, parse_search_cards, split_mrp

FIXTURES = Path(__file__).parent / "fixtures" / "search"


def _cards(name):
    return json.loads((FIXTURES / f"{name}-cards.json").read_text(encoding="utf-8"))


def test_split_mrp_reads_the_glued_list_price_and_discount():
    assert split_mrp(1104, "2,38553", None) == (2385.0, 53)
    assert split_mrp(22999, "38,49940", None) == (38499.0, 40)
    assert split_mrp(649, "1,07039", None) == (1070.0, 39)


def test_split_mrp_gives_up_rather_than_guess():
    assert split_mrp(None, "2,38553", None) == (None, None)
    assert split_mrp(5000, "1234", None) == (None, None)


def test_parse_mrp_with_the_discount_on_its_own_line():
    assert parse_mrp(["₹999", "₹1,499", "33% off"], 999) == (1499.0, 33)


def test_parse_mrp_without_a_list_price():
    assert parse_mrp(["₹999", "Free delivery"], 999) == (None, None)


def test_search_results_carry_list_price_discount_and_highlights():
    phone = parse_search_cards(_cards("phone"))[0]
    assert (phone.price, phone.mrp, phone.discount_pct) == (22999.0, 38499.0, 40)
    assert phone.highlights[0] == "6 GB RAM | 128 GB ROM"
    assert all("₹" not in line for line in phone.highlights)
    cooker = parse_search_cards(_cards("cooker"))[0]
    assert (cooker.price, cooker.mrp, cooker.discount_pct) == (1104.0, 2385.0, 53)
    assert "highlights" in cooker.as_dict()


# --- Product page, as saved live on 11 Nov 2026 -------------------------------
PAGE = json.loads((Path(__file__).parent / "fixtures" / "product" / "vivo-t5-lite-page.json").read_text(encoding="utf-8"))


def test_price_block_is_the_products_own_not_an_ad_above_it():
    from app.automation.product import parse_price_block

    # The ad carousel above it reads "33%" / "23,999" / "₹15,999" — not ours.
    assert parse_price_block(PAGE["lines"], PAGE["price"]) == (38499.0, 40)


def test_price_block_with_no_list_price():
    from app.automation.product import parse_price_block

    assert parse_price_block(["Phone", "₹999", "Buy now"], 999) == (None, None)
    assert parse_price_block(PAGE["lines"], None) == (None, None)


def test_variants_share_the_item_id_and_carry_their_own_prices():
    from app.automation.product import parse_variants

    variants = parse_variants(PAGE["url"], PAGE["links"])
    by_label = {v["label"]: v for v in variants}
    # The colour swatch has no text: named from its slug.
    assert "Twilight Shadow" in by_label
    assert by_label["64 GB + 4 GB"]["price"] == 17999.0
    assert (by_label["64 GB + 4 GB"]["mrp"], by_label["64 GB + 4 GB"]["discount_pct"]) == (30999.0, 42)
    # Never the product itself, and never another product (the ads).
    assert all(v["pid"] != "MOBHNDTD57UJUK9Q" for v in variants)
    assert all("itm5420c578ec2df" in v["link"] for v in variants)
    assert len(variants) == 4


def test_slug_label():
    from app.automation.product import _slug_label

    assert _slug_label("vivo-t5-lite-44w-5g-twilight-shadow-128-gb", "vivo-t5-lite-44w-5g-wave-blue-128-gb") == "Twilight Shadow"


COOKER = json.loads((Path(__file__).parent / "fixtures" / "product" / "pigeon-cooker-page.json").read_text(encoding="utf-8"))


def test_swatches_that_are_products_of_their_own():
    """The cooker's sizes are separate products (their own item ids), shown as
    one-line swatches; the ad cards beside them are not variants."""
    from app.automation.product import parse_price_block, parse_variants

    labels = [v["label"] for v in parse_variants(COOKER["url"], COOKER["links"])]
    assert labels == ["5 L", "6 L", "7.5 L", "12 L", "With Induction Base"]
    assert parse_price_block(COOKER["lines"], 799) == (1395.0, 43)


# --- Review fixes ---------------------------------------------------------------


def test_a_short_named_product_of_another_brand_is_no_swatch():
    from app.automation.product import parse_variants

    links = [*COOKER["links"], {"href": "/prestlee-mini-cooker/p/itm00000000000aa?pid=PRCXXXXXXXXXXXXX", "text": "Mini Cooker"}]
    labels = [v["label"] for v in parse_variants(COOKER["url"], links)]
    assert labels == ["5 L", "6 L", "7.5 L", "12 L", "With Induction Base"]


def test_price_block_fallback_needs_the_price_itself_not_its_digits_inside_another():
    from app.automation.product import parse_price_block

    # "₹19,999" holds 999's digits; the block starts at the ₹999 line.
    lines = ["Ad", "₹19,999", "₹24,999", "20% off", "Phone", "₹999", "₹1,499", "33% off"]
    assert parse_price_block(lines, 999) == (1499.0, 33)


def test_preview_images_stay_capped_with_the_main_image_added():
    import asyncio

    from app.automation.product import ProductPreview, add_screener_details

    class Page:
        url = COOKER["url"]

        async def evaluate(self, _script, _args):
            return {"lines": [], "links": []}

    preview = ProductPreview(url=COOKER["url"], image="main.jpg", images=[f"{n}.jpg" for n in range(12)])
    asyncio.run(add_screener_details(Page(), preview))
    assert preview.images[0] == "main.jpg" and len(preview.images) == 12
