"""What an order run refuses to download, and what it must never refuse."""

import pytest

from app.core.browser import NO_IMAGES_ARG, is_telemetry


@pytest.mark.parametrize(
    "url",
    [
        "https://1.sonic.fdp.api.flipkart.com/4/data/collector/business",
        "https://sonic.fdp.api.flipkart.com/4/data/collector/business",
        "https://bam.nr-data.net/events/1/NRJS-abc?a=1",
        "https://js-agent.newrelic.com/nr-loader.js",
        "https://www.google-analytics.com/collect",
    ],
)
def test_telemetry_hosts_are_refused(url):
    assert is_telemetry(url) is True


@pytest.mark.parametrize(
    "url",
    [
        # The checkout's own data: never blocked.
        "https://1.rome.api.flipkart.com/api/5/checkout",
        "https://1.payments.flipkart.com/fkpay/api/v3/payments/pay?instrument=COD",
        "https://www.flipkart.com/payments/preload",
        # The page's scripts and styles: never blocked, hydration needs them.
        "https://static-assets-web.flixcart.com/fk-p-linchpin-web/fk-cp-zion/js/app.js",
        # A host that merely CONTAINS a telemetry name is not that host.
        "https://nr-data.net.example.com/x",
        "https://example.com/?next=https://bam.nr-data.net/",
    ],
)
def test_the_page_itself_is_never_refused(url):
    assert is_telemetry(url) is False


def test_images_are_switched_off_in_the_browser_not_per_request():
    """A request route would cost a round trip per image; Chromium's own
    switch costs nothing."""
    assert NO_IMAGES_ARG == "--blink-settings=imagesEnabled=false"
