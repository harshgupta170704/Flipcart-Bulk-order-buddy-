"""Minimal duck-typed fakes for a Playwright Page, for flow unit tests.

These let us drive the async flows without a real browser: locators record
clicks/fills and return canned text, so we can assert on behaviour — crucially,
that the price-target gate never clicks PAY_CONFIRM when over target.
"""

from __future__ import annotations


class FakeLocator:
    def __init__(self, page: "FakePage", xpath: str) -> None:
        self.page = page
        self.xpath = xpath

    def nth(self, index: int) -> "FakeLocator":
        # Distinct xpath per index so a test can tell the six OTP boxes apart.
        return FakeLocator(self.page, f"{self.xpath}[{index}]")

    async def count(self) -> int:
        return self.page.counts.get(self.xpath, 1)

    async def all(self) -> list["FakeLocator"]:
        """Every match, as Playwright's ``Locator.all`` lists them. One match
        is the locator itself, as ``first`` and ``last`` are; several are told
        apart by index, as ``nth`` tells them."""
        count = await self.count()
        return [self] if count == 1 else [self.nth(index) for index in range(count)]

    @property
    def first(self) -> "FakeLocator":
        return self

    @property
    def last(self) -> "FakeLocator":
        return self

    async def fill(self, value: str, timeout: int | None = None) -> None:
        if self.xpath in self.page.fail_clicks_on:
            raise RuntimeError(f"element not found: {self.xpath}")
        self.page.fills[self.xpath] = value

    async def press_sequentially(self, text: str, delay: int | None = None, timeout: int | None = None) -> None:
        """Typing key by key lands in the same box as a fill would, unless the
        test made the box keep fewer characters (``max_chars``), as a box with
        room for "MM / YY" does."""
        if self.xpath in self.page.fail_clicks_on:
            raise RuntimeError(f"element not found: {self.xpath}")
        typed = self.page.fills.get(self.xpath) or ""
        key = self.page.key_rules.get(self.xpath)
        for char in text:
            typed = key(typed, char) if key else typed + char
        limit = self.page.max_chars.get(self.xpath)
        self.page.fills[self.xpath] = typed if limit is None else typed[:limit]

    async def press(
        self, key: str, delay: int | None = None, timeout: int | None = None, no_wait_after: bool | None = None
    ) -> None:
        """A key pressed in this box (Enter to submit a form)."""
        if f"key:{key}@{self.xpath}" in self.page.fail_clicks_on:
            raise RuntimeError(f"key not pressed: {key}")
        self.page.clicks.append(f"key:{key}@{self.xpath}")

    async def click(self, timeout: int | None = None, no_wait_after: bool | None = None) -> None:
        # Lets a test simulate an element that isn't on the page (e.g. Flipkart
        # not offering Cash on Delivery for a product/pincode).
        if self.xpath in self.page.fail_clicks_on:
            raise RuntimeError(f"element not found: {self.xpath}")
        self.page.clicks.append(self.xpath)
        self.page.clicked(self.xpath)

    async def evaluate(self, script: str):
        """Stands in for the RN-Web DOM-click fallback."""
        if "click()" in script:
            if self.xpath in self.page.fail_clicks_on:
                raise RuntimeError(f"element not found: {self.xpath}")
            self.page.clicks.append(self.xpath)
        return None

    async def is_visible(self) -> bool:
        return self.xpath not in self.page.hidden

    async def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        return self.page.attrs.get((self.xpath, name))

    def locator(self, selector: str) -> "FakeLocator":
        """Row-relative lookup: './/x' resolves under this locator."""
        xpath = selector.split("xpath=", 1)[1] if "xpath=" in selector else selector
        return FakeLocator(self.page, xpath)

    async def input_value(self, timeout: int | None = None) -> str:
        return self.page.fills.get(self.xpath, "")

    async def wait_for(self, state: str | None = None, timeout: int | None = None) -> None:
        # An xpath counted as 0 never appears, e.g. a popup that never opens.
        if self.page.counts.get(self.xpath, 1) == 0:
            raise RuntimeError(f"timed out waiting for {self.xpath}")

    async def inner_text(self, timeout: int | None = None) -> str:
        # Text that belongs to one page (the product page, the sellers page,
        # the checkout) answers while the browser is on that page.
        for fragment, texts in self.page.inner_text_by_url.items():
            if fragment in (self.page.url or "") and self.xpath in texts:
                return self._read(texts[self.xpath])
        if self.xpath in self.page.inner_text_map:
            return self._read(self.page.inner_text_map[self.xpath])
        raise RuntimeError(f"no text for {self.xpath}")

    @staticmethod
    def _read(value):
        if isinstance(value, list):
            # A list is a value that CHANGES, one entry per read: it models
            # a checkout still re-pricing itself. The final entry is the
            # settled figure and repeats for every subsequent read.
            return value.pop(0) if len(value) > 1 else value[0]
        return value


#: Flipkart's order-creating request, as the real page sends it.
ORDER_REQUEST_URL = "https://1.payments.flipkart.com/fkpay/api/v3/payments/pay?instrument=COD"


class FakeRequest:
    def __init__(self, method: str, url: str) -> None:
        self.method = method
        self.url = url


class FakeKeyboard:
    """Records typed text, so OTP entry can be asserted without a browser."""

    def __init__(self, page: "FakePage") -> None:
        self.page = page

    async def type(self, text: str, delay: int | None = None) -> None:
        self.page.typed += text
        # Mirror the real widget: each character lands in the next box.
        for index, char in enumerate(text):
            self.page.fills[f"{self.page.otp_xpath}[{index}]"] = char

    async def press(self, key: str, delay: int | None = None) -> None:
        self.page.clicks.append(f"key:{key}")


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    async def body(self) -> bytes:
        return self._body


class FakeRequestContext:
    """The context's HTTP client (Playwright's ``context.request``): answers
    from ``responses`` (url -> (status, body)); 404 for anything else."""

    def __init__(self) -> None:
        self.responses: dict[str, tuple[int, bytes]] = {}
        self.calls: list[str] = []

    async def get(self, url: str, timeout: float | None = None) -> FakeResponse:
        self.calls.append(url)
        status, body = self.responses.get(url, (404, b""))
        return FakeResponse(status, body)


class FakeContext:
    def __init__(self, cookies: list[dict] | None = None) -> None:
        self._cookies = cookies if cookies is not None else [{"name": "SN", "value": "1"}]
        self.request = FakeRequestContext()

    async def cookies(self) -> list[dict]:
        return self._cookies

    async def add_cookies(self, cookies: list[dict]) -> None:
        self._cookies = cookies


class FakePage:
    def __init__(self, inner_text_map: dict[str, str] | None = None) -> None:
        self.inner_text_map = inner_text_map or {}
        #: Text that depends on which page the browser is on: address fragment
        #: -> {xpath: text}. The first fragment found in the current ``url``
        #: that has the xpath answers, before ``inner_text_map``. Empty by
        #: default, so a test that does not set it reads what it always did.
        self.inner_text_by_url: dict[str, dict] = {}
        #: What the map search suggests, and what the pin page's "Deliver To"
        #: block says, for the add-address flow.
        self.map_suggestions: list[str] = []
        self.pin_block: str = ""
        #: Link addresses on the confirmation page carrying an order id.
        self.order_links: list[str] = []
        #: Link addresses on an order's own page carrying a unit id.
        self.unit_links: list[str] = []
        # What Flipkart's confirmation page says by default: it names the order
        # (the id extends the address bar's reference, see confirmation_url).
        # A test that wants a page saying something else sets PAGE_BODY itself.
        from app.core import selectors as S

        self.inner_text_map.setdefault(
            S.PAGE_BODY, "Order placed successfully. Order ID OD428123456789100"
        )
        self.clicks: list[str] = []
        self.fills: dict[str, str] = {}
        #: xpaths whose click should raise, standing in for a missing element.
        self.fail_clicks_on: set[str] = set()
        #: xpath -> how many elements it matches (for .count()). Flipkart's
        #: "secure your card" dialog after Pay is absent unless a test shows it.
        self.counts: dict[str, int] = {S.CARD_SAVE_PROMPT: 0}
        #: (xpath, attribute) -> value, for get_attribute().
        self.attrs: dict[tuple[str, str], str] = {}
        self.goto_urls: list[str] = []
        self.typed = ""
        #: Current page URL; goto() updates it.
        self.url = "https://www.flipkart.com/"
        #: Where a BUY NOW / Place Order lands. The one-page checkout by
        #: default; a test sets /checkout/init to simulate the step-by-step one
        #: a seller's BUY NOW sometimes opens.
        self.checkout_url = "https://www.flipkart.com/viewcheckout"
        #: When set, every goto lands here instead (simulates a redirect).
        self.redirect_to: str | None = None
        #: xpath whose indexed children FakeKeyboard.type should populate.
        self.otp_xpath = ""
        self.keyboard = FakeKeyboard(self)
        self.context = FakeContext()
        #: Where submitting an order lands: Flipkart's confirmation page. None
        #: simulates a confirmation that never arrives. Like the real page, the
        #: reference is the order id minus its last two digits (OD428123456789100).
        self.confirmation_url: str | None = (
            "https://www.flipkart.com/orderresponse?reference_id=OD4281234567891"
        )
        #: The product page's JSON-LD blocks, as page.evaluate would return them.
        self.jsonld: list = [
            {"@type": "Product", "name": "Phone X", "color": "Black",
             "offers": {"price": 999, "availability": "https://schema.org/InStock"}}
        ]
        #: Paths passed to screenshot(), which writes nothing.
        self.screenshots: list[str] = []
        #: Handlers registered with on(), e.g. the order-request watcher.
        self.handlers: dict[str, list] = {}
        #: Simulates a checkout where Place Order sends the order itself, with
        #: no confirmation popup (not Flipkart's behaviour today, but its popup
        #: is server-sent and could change).
        self.place_order_sends = False
        #: Order requests "sent" so far.
        self.order_requests: list[str] = []
        #: Frames inside the page (a bank's authentication page can load in one).
        self.frames: list = []
        #: xpath -> how many characters typing can leave in that box.
        self.max_chars: dict[str, int] = {}
        #: xpath -> what one typed key does to the box's text, for a box that
        #: formats as it goes: ``rule(text_so_far, key) -> new_text``.
        self.key_rules: dict = {}
        #: xpaths that are on the page but not shown (is_visible() is False).
        self.hidden: set[str] = set()
        #: The boxes each screenshot painted over (their xpaths), per screenshot.
        self.screenshot_masks: list[list[str]] = []

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler) -> None:
        handlers = self.handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def clicked(self, xpath: str) -> None:
        """A click on a control that sends the order fires the order request,
        as the real page does, to whoever is listening."""
        from app.core import selectors as S

        sends = xpath in (S.COD_CONFIRM_ORDER_BTN, S.CARD_PAY_BTN) or (
            self.place_order_sends and xpath == S.COD_PLACE_ORDER_BTN
        )
        if not sends:
            return
        request = FakeRequest("POST", ORDER_REQUEST_URL)
        self.order_requests.append(request.url)
        for handler in list(self.handlers.get("request", [])):
            handler(request)

    @property
    def submitted(self) -> bool:
        """Whether the order request was sent.

        Sent by the COD popup's Confirm order or a card's Pay — never by COD's
        Place Order, which only opens the popup (unless ``place_order_sends``).
        """
        return bool(self.order_requests)

    async def evaluate(self, script: str, *args):
        """The few page scripts the flows run, by marker."""
        if "ld+json" in script:
            return self.jsonld
        if "MAP_SUGGESTIONS" in script:
            return list(self.map_suggestions)
        if "CLICK_SUGGESTION" in script:
            wanted = args[0] if args else None
            if wanted in self.map_suggestions:
                self.clicks.append(f"suggestion:{wanted}")
                return True
            return False
        if "PIN_BLOCK" in script:
            return self.pin_block
        if "ORDER_LINKS" in script:
            return list(self.order_links)
        if "UNIT_LINKS" in script:
            return list(self.unit_links)
        raise RuntimeError("FakePage.evaluate: script not modelled")

    async def screenshot(
        self,
        path: str | None = None,
        full_page: bool = False,
        mask: list | None = None,
        mask_color: str | None = None,
    ) -> None:
        self.screenshots.append(path or "")
        self.screenshot_masks.append([getattr(box, "xpath", "") for box in mask or []])

    async def goto(self, url: str, wait_until: str | None = None) -> None:
        self.goto_urls.append(url)
        # redirect_to simulates Flipkart bouncing an unauthenticated browser
        # back to the login page.
        self.url = self.redirect_to or url

    async def wait_for_url(
        self, pattern, timeout: int | None = None, wait_until: str | None = None
    ) -> None:
        """Pretend the navigation the click triggered has completed.

        Only moves when the control that causes it was actually clicked, so a
        test that blocks a click still sees the page stay put.
        """
        if callable(pattern) and not self.submitted and self.checkout_url and pattern(self.checkout_url):
            # Buy now's wait also passes a predicate now (the checkout, or the
            # first-address form of an account with no address): it moves only
            # when a control that leads to the checkout was clicked, as below.
            if not any(
                "Buy now" in c or "BUY NOW" in c or "Place Order" in c or "Save address" in c for c in self.clicks
            ):
                raise RuntimeError("no navigation")
            self.url = self.checkout_url
            return
        if callable(pattern):
            # The order confirmation wait passes a predicate. Only a control
            # that actually sends an order leads there.
            target = self.confirmation_url
            if not self.submitted or target is None or not pattern(target):
                raise RuntimeError("no navigation")
            self.url = target
        elif "viewcheckout" in pattern:
            # Buy now / the cart's Place Order lead here, and so does saving a
            # new address from the checkout's own add-address page — and a
            # sellers-page row's BUY NOW (its xpath carries the upper-cased words).
            if not any(
                "Buy now" in c or "BUY NOW" in c or "Place Order" in c or "Save address" in c for c in self.clicks
            ):
                raise RuntimeError("no navigation")
            # The BUY NOW navigated; where to is checkout_url. When that is
            # not the awaited one-page checkout (the step-by-step /checkout/init
            # a seller's BUY NOW can open), the browser still moves, but the
            # await for "viewcheckout" fails — as it does live.
            self.url = self.checkout_url
            if "viewcheckout" not in self.checkout_url:
                raise RuntimeError("navigated to a different checkout")
        elif "payments" in pattern:
            if not any("Continue" in c for c in self.clicks):
                raise RuntimeError("no navigation")
            self.url = "https://www.flipkart.com/payments"

    async def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        return None

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    def locator(self, selector: str) -> FakeLocator:
        xpath = selector.split("xpath=", 1)[1] if "xpath=" in selector else selector
        return FakeLocator(self, xpath)
