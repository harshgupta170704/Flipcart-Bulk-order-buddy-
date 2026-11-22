"""Only Flipkart links may aim the server's browser (audit F-14 / F-44)."""

import pytest

from app.api.products import is_flipkart_link


@pytest.mark.parametrize(
    "url",
    [
        "https://www.flipkart.com/vivo-t5-lite-44w-5g/p/itm5420c578ec2df?pid=MOBHQSFF6MZK4YNP",
        "https://dl.flipkart.com/s/uFy91dNNNN",
        "http://flipkart.com/anything",
        "HTTPS://WWW.FLIPKART.COM/x",
    ],
)
def test_flipkart_links_are_accepted(url):
    assert is_flipkart_link(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://db:5432",
        "http://127.0.0.1:8000/health",
        "https://flipkart.com.evil.example/p/1",
        "https://notflipkart.com/p/1",
        "https://example.com/?u=https://www.flipkart.com/p/1",
        "not a link",
        "",
    ],
)
def test_anything_else_is_refused(url):
    assert is_flipkart_link(url) is False
