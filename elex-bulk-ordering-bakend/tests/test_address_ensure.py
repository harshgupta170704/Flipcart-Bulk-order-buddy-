"""Delivering to the address the operator chose (mapped live 2026-10-24).

Two things went wrong before this: the portal's chosen address never reached
the checkout at all (it was only written into the order record, so Flipkart
shipped to whatever the account had selected — Hardoi instead of Lucknow),
and an address not yet on the account could not be added. Flipkart now ties
every address to a map pin, so adding one is: search the map, pick a location,
check its PINCODE against the address, fill the form, save.
"""

import asyncio

from app.automation import ordering as order_flow
from app.automation.ordering import (
    AddressInfo,
    address_matches,
    address_search_queries,
    ensure_delivery_address,
    normalize_address_text,
)
from app.core import selectors as S
from tests.fakes import FakePage

BOOK = AddressInfo(
    name="AARTHBODHA PRIVATE LIMITED",
    phone="8957768866",
    pin="226013",
    area="Tower 5, 1005, LUCKNOW, Uttar Pradesh",
    line1="Tower 5, 1005",
    city="LUCKNOW",
    state="Uttar Pradesh",
)
#: The account's own address in the same building, under a different name.
AYUSH_BLOCK = "Ayush Agarwal Tower5-1005, Garden Bay, Lucknow 226013 9582912283"
BOOK_BLOCK = "AARTHBODHA PRIVATE LIMITED Tower 5, 1005, Gomti Nagar, Lucknow 226013 8957768866"


# --- matching -------------------------------------------------------------------
def test_normalisation_ignores_spacing_and_punctuation():
    assert normalize_address_text("Tower 5, 1005") == normalize_address_text("Tower5-1005")


def test_the_same_flat_under_another_name_is_not_a_match():
    """A business order under a personal name is a different delivery and a
    different invoice; the name is part of the address."""
    assert address_matches(AYUSH_BLOCK, BOOK) is False
    assert address_matches(BOOK_BLOCK, BOOK) is True


def test_a_picker_row_matches_on_name_and_house_line_without_a_pincode():
    row = "AARTHBODHA PRIVATE LIMITED\nTower5-1005, Garden Bay, Lucknow"
    assert address_matches(row, BOOK) is True
    assert address_matches("AARTHBODHA PRIVATE LIMITED\nSome other street, Lucknow", BOOK) is False


def test_the_same_name_and_pincode_at_another_locality_is_not_a_match():
    """What Ayush's account held on 26 Oct 2026 beside the real address: the
    business name, the right pincode, and a wrongly pinned "Madiyanva … Allu
    Nagar Diguria" line. With a house line in the Address Book, the pincode
    alone must not settle it — on the checkout block or in the picker."""
    book = AddressInfo(
        name="AARTHBODHA PRIVATE LIMITED", phone="9582912283", pin="226013",
        area="Tower 5, 1005, Garden Bay, Lucknow, Uttar Pradesh", line1="Tower 5, 1005, Garden Bay",
        city="Lucknow", state="Uttar Pradesh",
    )
    twin = (
        "AARTHBODHA PRIVATE LIMITED 8957768866 Tower 5, 1005, Madiyanva, MOHABBATE Road, "
        "Allu Nagar Diguria, Lucknow, Uttar Pradesh - 226013"
    )
    assert address_matches(twin, book) is False
    assert address_matches("AARTHBODHA PRIVATE LIMITED\nTower 5, 1005, Madiyanva, Allu Nagar Diguria, Lucknow", book) is False
    assert address_matches("AARTHBODHA PRIVATE LIMITED Tower5-1005, Garden Bay, Lucknow, Uttar Pradesh - 226013", book) is True
    assert address_matches("AARTHBODHA PRIVATE LIMITED\nTower5-1005, Garden Bay, Lucknow", book) is True
    # Another name at the right flat is still another delivery.
    assert address_matches("Ayush Agarwal Tower5-1005, Garden Bay, Lucknow 226013", book) is False


def test_a_row_showing_another_pincode_is_not_a_match():
    """The same name and the same flat in another city is another delivery; a
    row that shows no pincode at all is judged on its words."""
    assert address_matches("AARTHBODHA PRIVATE LIMITED\nTower 5, 1005, Gomti Nagar, Lucknow - 226010", BOOK) is False
    assert address_matches("AARTHBODHA PRIVATE LIMITED\nTower 5, 1005, Gomti Nagar, Lucknow - 226013", BOOK) is True
    # A phone number is not a pincode.
    assert address_matches("AARTHBODHA PRIVATE LIMITED 8957768866\nTower 5, 1005, Lucknow", BOOK) is True


def test_a_house_line_does_not_match_inside_a_longer_number():
    """"Tower 5, 1005" is not in "Tower 5, 10050" once the separators are gone."""
    assert address_matches("AARTHBODHA PRIVATE LIMITED\nTower 5, 10050, Lucknow", BOOK) is False
    assert address_matches("AARTHBODHA PRIVATE LIMITED\nTower5-1005, Lucknow", BOOK) is True
    assert address_matches("AARTHBODHA PRIVATE LIMITED\nFlat 1005, Tower 5, Lucknow", BOOK) is False  # another order of the words


def test_the_suggestion_naming_the_locality_is_tried_first():
    """Live, an Address Book line with no locality let a pincode-wide match
    win and the pin landed in the wrong neighbourhood. With the locality in
    the line, its suggestion outranks the wider ones."""
    from app.automation.ordering import rank_suggestions

    with_locality = AddressInfo(
        name=BOOK.name, phone="", pin="226013", area="", line1="Tower 5, 1005, Garden Bay", city="Lucknow"
    )
    offered = [
        "Lucknow - Shahjahanpur Road, Sector 2, Jankipuram Extension, Lucknow, Uttar Pradesh 226013",
        "Shalimar Garden Bay",
        "IIM Road, Lucknow, Alinagar, Uttar Pradesh, India",
    ]
    assert rank_suggestions(offered, with_locality)[0] == "Shalimar Garden Bay"


def test_search_queries_go_from_specific_to_wide():
    assert address_search_queries(BOOK) == [
        "Tower 5, 1005, LUCKNOW 226013",
        "LUCKNOW 226013",
        "226013",
    ]


# --- the flow ---------------------------------------------------------------------
def _checkout_showing(page: FakePage, *texts: str):
    """current_delivery_address returns each text in turn, then repeats the last."""
    queue = list(texts)

    async def reader(_page):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return reader


def test_an_address_already_selected_needs_no_clicks(monkeypatch):
    page = FakePage()
    monkeypatch.setattr(order_flow, "current_delivery_address", _checkout_showing(page, BOOK_BLOCK))
    assert asyncio.run(ensure_delivery_address(page, BOOK)) == (True, "")
    assert page.clicks == []


def test_a_saved_address_is_picked_from_the_list(monkeypatch):
    page = FakePage()
    page.counts[S.ADDRESS_PICKER_ROW] = 2
    page.inner_text_map[f"{S.ADDRESS_PICKER_ROW}[0]"] = "Ayush Agarwal\nTower5-1005, Garden Bay, Lucknow"
    page.inner_text_map[f"{S.ADDRESS_PICKER_ROW}[1]"] = "AARTHBODHA PRIVATE LIMITED\nTower 5, 1005, Lucknow"
    monkeypatch.setattr(
        order_flow, "current_delivery_address", _checkout_showing(page, AYUSH_BLOCK, BOOK_BLOCK)
    )

    assert asyncio.run(ensure_delivery_address(page, BOOK)) == (True, "")
    assert f"{S.ADDRESS_PICKER_ROW}[1]" in page.clicks
    assert S.ADDRESS_ADD_NEW not in page.clicks  # nothing was added


class _CountsThatFillIn(dict):
    """Row and control counts that change from one read to the next, the way
    the live picker fills in: the closed checkout's own leaves on the very first
    read (``before``), a handful of rows at once, the full list and the "Add
    New" control only after several reads."""

    def __init__(self, early: dict, late: dict, *, after_reads: int, before: dict | None = None) -> None:
        super().__init__(early)
        self._late = late
        self._after = after_reads
        self._before = before or {}
        self.reads: dict[str, int] = {}

    def get(self, key, default=None):  # type: ignore[override]
        self.reads[key] = self.reads.get(key, 0) + 1
        if key in self._before and self.reads[key] == 1:
            return self._before[key]
        if key in self._late and self.reads[key] > self._after:
            return self._late[key]
        return super().get(key, default)


def test_a_saved_address_that_loads_late_is_still_found(monkeypatch):
    """Live on 26 Oct 2026 the picker showed 7 rows for 12 s and all 56 — with
    the heading and "Add New" — only after 14 s. The flow must wait for the
    list to complete before calling the address absent, and must not press
    Change again (which would close the picker) because the heading is late."""
    page = FakePage()
    page.counts = _CountsThatFillIn(
        {S.ADDRESS_PICKER_ROW: 7, S.ADDRESS_ADD_NEW: 0, S.ADDRESS_PICKER_HEADING: 0},
        {S.ADDRESS_PICKER_ROW: 56, S.ADDRESS_ADD_NEW: 1, S.ADDRESS_PICKER_HEADING: 1},
        after_reads=6,
        before={S.ADDRESS_PICKER_ROW: 3},  # the closed checkout's own leaves match the row selector too
    )
    for index in range(7):
        page.inner_text_map[f"{S.ADDRESS_PICKER_ROW}[{index}]"] = f"Someone {index}\nSomewhere {index}, Hardoi"
    page.inner_text_map[f"{S.ADDRESS_PICKER_ROW}[40]"] = "AARTHBODHA PRIVATE LIMITED\nTower5-1005, Garden Bay, Lucknow"
    monkeypatch.setattr(order_flow, "current_delivery_address", _checkout_showing(page, AYUSH_BLOCK, BOOK_BLOCK))

    assert asyncio.run(ensure_delivery_address(page, BOOK)) == (True, "")
    assert f"{S.ADDRESS_PICKER_ROW}[40]" in page.clicks
    assert S.ADDRESS_ADD_NEW not in page.clicks
    assert page.clicks.count(S.CHECKOUT_CHANGE_ADDRESS) == 1  # opened once; more rows than before said so


def test_among_several_matching_rows_the_one_naming_the_most_of_the_entry_is_picked(monkeypatch):
    """Two saved rows carry the name and the flat (the account once held a
    wrongly pinned twin beside the real address). The row repeating most of
    the Address Book entry — its locality, city, state — is the entry, wherever
    it sits in the list."""
    page = FakePage()
    page.counts[S.ADDRESS_PICKER_ROW] = 2
    page.inner_text_map[f"{S.ADDRESS_PICKER_ROW}[0]"] = (
        "AARTHBODHA PRIVATE LIMITED\nTower 5, 1005, Madiyanva, Allu Nagar Diguria, Lucknow"
    )
    page.inner_text_map[f"{S.ADDRESS_PICKER_ROW}[1]"] = (
        "AARTHBODHA PRIVATE LIMITED\nTower 5, 1005, Garden Bay, Lucknow, Uttar Pradesh"
    )
    monkeypatch.setattr(order_flow, "current_delivery_address", _checkout_showing(page, AYUSH_BLOCK, BOOK_BLOCK))

    assert asyncio.run(ensure_delivery_address(page, BOOK)) == (True, "")
    assert f"{S.ADDRESS_PICKER_ROW}[1]" in page.clicks
    assert f"{S.ADDRESS_PICKER_ROW}[0]" not in page.clicks


def test_a_change_press_that_did_nothing_is_pressed_again(monkeypatch):
    """The row selector matches the closed checkout's own leaves, so rows alone
    never mean "open": with no heading, no "Add New" and no more rows than
    before, the press is retried (RN-Web swallows the first click before it
    has hydrated) rather than the flow waiting on a picker that is not there."""
    page = FakePage()
    page.counts = {S.ADDRESS_PICKER_ROW: 5, S.ADDRESS_ADD_NEW: 0, S.ADDRESS_PICKER_HEADING: 0}
    for index in range(5):
        page.inner_text_map[f"{S.ADDRESS_PICKER_ROW}[{index}]"] = f"Checkout leaf {index}"
    monkeypatch.setattr(order_flow, "current_delivery_address", _checkout_showing(page, AYUSH_BLOCK))

    ok, why = asyncio.run(ensure_delivery_address(page, BOOK))

    assert ok is False
    assert "picker" in why.lower()
    assert page.clicks.count(S.CHECKOUT_CHANGE_ADDRESS) == 3  # every press tried, none swallowed silently
    assert S.ADDRESS_ADD_NEW not in page.clicks and S.ADDRESS_SAVE not in page.clicks


def test_a_map_search_box_that_cannot_be_typed_into_is_a_refusal_not_a_crash(monkeypatch):
    """On the second query the search box can be gone (the pin view replaced
    it); a bare Playwright error there must not replace the flow's own reason."""
    page = _page_for_adding(monkeypatch, pin_block="", suggestions=["Gomti Nagar, Lucknow"])
    page.fail_clicks_on.add(S.ADDRESS_MAP_SEARCH)

    ok, why = asyncio.run(ensure_delivery_address(page, BOOK))

    assert ok is False and why
    assert S.ADDRESS_SAVE not in page.clicks


def _page_for_adding(monkeypatch, *, pin_block: str, suggestions: list[str]) -> FakePage:
    page = FakePage()
    page.counts[S.ADDRESS_PICKER_ROW] = 1
    page.inner_text_map[f"{S.ADDRESS_PICKER_ROW}[0]"] = "Ayush Agarwal\nTower5-1005, Garden Bay, Lucknow"
    page.counts[S.ADDRESS_FORM_TEXT_INPUTS] = 2
    page.map_suggestions = suggestions
    page.pin_block = pin_block
    monkeypatch.setattr(
        order_flow, "current_delivery_address", _checkout_showing(page, AYUSH_BLOCK, BOOK_BLOCK)
    )
    return page


def test_a_missing_address_is_added_through_the_map_and_form(monkeypatch):
    page = _page_for_adding(
        monkeypatch,
        pin_block="Deliver To Gomti Nagar Change Vibhuti Khand, Lucknow, Uttar Pradesh, 226013",
        suggestions=["Gomti Nagar, Lucknow, Uttar Pradesh, India"],
    )

    ok, why = asyncio.run(ensure_delivery_address(page, BOOK))

    assert (ok, why) == (True, "")
    for step in (S.ADDRESS_ADD_NEW, S.ADDRESS_AWAY_FROM_LOCATION, S.ADDRESS_ADD_DETAILS, S.ADDRESS_SAVE):
        assert step in page.clicks, step
    assert "suggestion:Gomti Nagar, Lucknow, Uttar Pradesh, India" in page.clicks
    assert "Tower 5, 1005, LUCKNOW 226013" in page.typed  # the most specific query first
    assert page.fills[f"{S.ADDRESS_FORM_TEXT_INPUTS}[0]"] == "Tower 5, 1005"  # house/flat
    assert page.fills[f"{S.ADDRESS_FORM_TEXT_INPUTS}[1]"] == "AARTHBODHA PRIVATE LIMITED"
    assert page.fills[f"{S.ADDRESS_FORM_TEL_INPUTS}[0]"] == "8957768866"
    assert S.ADDRESS_TYPE_WORK in page.clicks  # a company gets "Work"
    assert page.clicks.index(S.ADDRESS_ADD_DETAILS) < page.clicks.index(S.ADDRESS_SAVE)


def test_a_pin_in_the_wrong_pincode_is_never_saved(monkeypatch):
    """The whole point of the check: a wrong pin ships to the wrong place while
    the saved address reads correctly."""
    page = _page_for_adding(
        monkeypatch,
        pin_block="Deliver To Hazratganj, Lucknow, Uttar Pradesh, 226001",
        suggestions=["Hazratganj, Lucknow"],
    )

    ok, why = asyncio.run(ensure_delivery_address(page, BOOK))

    assert ok is False
    assert "226013" in why and "not saved" in why
    assert S.ADDRESS_SAVE not in page.clicks
    assert S.ADDRESS_ADD_DETAILS not in page.clicks


def test_no_map_result_at_all_is_refused(monkeypatch):
    page = _page_for_adding(monkeypatch, pin_block="", suggestions=[])
    ok, why = asyncio.run(ensure_delivery_address(page, BOOK))
    assert ok is False
    assert S.ADDRESS_SAVE not in page.clicks


def test_a_person_gets_home_and_a_company_gets_work(monkeypatch):
    # Not already saved on the account (the fake's one saved row is Tower 5).
    person = AddressInfo(
        name="Ayush Agarwal", phone="", pin="226013", area="", line1="Flat 9, Rohini Apartments", city="Lucknow"
    )
    page = _page_for_adding(
        monkeypatch,
        pin_block="Deliver To Lucknow, 226013",
        suggestions=["Lucknow"],
    )
    monkeypatch.setattr(
        order_flow,
        "current_delivery_address",
        _checkout_showing(
            page, "Nobody Nowhere 110001", "Ayush Agarwal Flat 9, Rohini Apartments, Lucknow 226013"
        ),
    )
    assert asyncio.run(ensure_delivery_address(page, person)) == (True, "")
    assert S.ADDRESS_TYPE_HOME in page.clicks
    assert S.ADDRESS_TYPE_WORK not in page.clicks


# --- wired into the order ------------------------------------------------------------
def test_run_order_refuses_when_the_address_cannot_be_set(monkeypatch):
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹999"})
    page.map_suggestions = []

    async def elsewhere(_page):
        return "Someone Else, Hardoi 241001"

    monkeypatch.setattr(order_flow, "current_delivery_address", elsewhere)
    result = asyncio.run(
        order_flow.run_order(
            page,
            product_link="https://flipkart.com/x",
            address=BOOK,
            gst=order_flow.GstInfo(gstin="", business_name=""),
            card=None,
            qty=1,
            target=1000.0,
            payment_mode=order_flow.PAYMENT_COD,
            ensure_address=True,
        )
    )
    assert result["status"].value == "failed"
    assert "different address" in result["error"]
    assert not page.submitted
