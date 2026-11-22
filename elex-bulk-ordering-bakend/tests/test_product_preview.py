"""Product preview parsing.

Reads STRUCTURED DATA rather than CSS classes: every class-based selector this
project inherited had rotted, while JSON-LD is contractual (search engines
consume it) and carried name, brand, category, image, price and availability
on the live page.
"""

from app.automation.product import (
    IN_STOCK,
    parse_product_jsonld,
    preview_from_jsonld,
)

URL = "https://www.flipkart.com/vivo-t4-lite-5g/p/itm49fb53d099f3c"

# Shaped exactly like the live page returned on 2026-10-23.
LIVE = {
    "@type": "Product",
    "name": "Vivo T4 Lite 5G (Prism Blue 2026) ( 4GB 64GB)",
    "brand": {"name": "vivo"},
    "category": "Mobiles",
    "color": "Prism Blue",
    "image": "https://rukmini1.flixcart.com/image/1500/1500/x.jpeg?q=70",
    "sku": "MOBHNCRKA4XQFSRM",
    "aggregateRating": {"ratingValue": 4.3, "ratingCount": 47544},
    "offers": {
        "price": 16499,
        "priceCurrency": "INR",
        "availability": "https://schema.org/InStock",
    },
}


def test_the_live_shape_parses_completely():
    preview = preview_from_jsonld(LIVE, URL)

    assert preview.name.startswith("Vivo T4 Lite")
    assert preview.brand == "vivo"
    assert preview.category == "Mobiles"
    assert preview.price == 16499.0
    assert preview.sku == "MOBHNCRKA4XQFSRM"
    assert preview.in_stock is True
    assert preview.rating == 4.3
    assert preview.rating_count == 47544


def test_brand_and_category_come_through_for_the_unit_limits():
    """These feed the per-model/brand caps, so the form can prefill them."""
    preview = preview_from_jsonld(LIVE, URL)
    assert preview.brand and preview.category


def test_brand_may_be_a_plain_string():
    product = {**LIVE, "brand": "realme"}
    assert preview_from_jsonld(product, URL).brand == "realme"


def test_list_valued_fields_take_the_first():
    product = {**LIVE, "image": ["first.jpg", "second.jpg"], "category": ["Mobiles", "X"]}
    preview = preview_from_jsonld(product, URL)
    assert preview.image == "first.jpg"
    assert preview.category == "Mobiles"


def test_out_of_stock_is_recognised():
    product = {**LIVE, "offers": {**LIVE["offers"], "availability": "https://schema.org/OutOfStock"}}
    assert preview_from_jsonld(product, URL).in_stock is False


def test_unknown_availability_is_none_not_false():
    """"We don't know" and "out of stock" are different; only one blocks a run."""
    product = {**LIVE, "offers": {"price": 10}}
    assert preview_from_jsonld(product, URL).in_stock is None


def test_a_price_with_separators_parses():
    product = {**LIVE, "offers": {**LIVE["offers"], "price": "1,16,499"}}
    assert preview_from_jsonld(product, URL).price == 116499.0


def test_the_product_block_is_found_among_other_blocks():
    blocks = [
        {"@type": "BreadcrumbList"},
        [{"@type": "WebPage"}, LIVE],
    ]
    assert parse_product_jsonld(blocks)["sku"] == "MOBHNCRKA4XQFSRM"


def test_no_product_block_returns_none():
    assert parse_product_jsonld([{"@type": "Organization"}]) is None
    assert parse_product_jsonld([]) is None


def test_limited_availability_still_counts_as_buyable():
    assert "https://schema.org/LimitedAvailability" in IN_STOCK
