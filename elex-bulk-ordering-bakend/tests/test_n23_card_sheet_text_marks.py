"""Virtual-card sheets as HDFC CorpAssist exports them (the owner, 23 Nov 2026).

The export's columns: name, card_number ("C" + 16 digits), expiry_month (two
digits), expiry_year (four digits), cvv, password (with a leading apostrophe,
the spreadsheet's keep-as-text mark), amounts, status and dates. The owner's
rules: the "C" is not part of the number; a keep-as-text mark on ANY column is
not part of the value; the year goes into Flipkart's "MM / YY" box as two
digits. Every card here is made up.
"""

import asyncio
import io

import pytest
from openpyxl import Workbook

from app.automation import ordering as order_flow
from app.automation.ordering import CardInfo
from app.io import cards

HEADER = (
    "name,card_number,expiry_month,expiry_year,cvv,password,card_amount,parent_card_number,"
    "request_id,request_pair_id,status,created_at,validity_days,expiry_date,created_by,"
    "usage_count,usage_amount,remaining_amount,last_used_at\n"
)


def _csv(*rows: str) -> io.BytesIO:
    return io.BytesIO((HEADER + "".join(rows)).encode("utf-8"))


def _row(number="C4111111111111111", month="11", year="2026", cvv="016", password="'0123456", amount="16419",
         remaining="16419", status="completed") -> str:
    return (
        f"CorpAssist demo,{number},{month},{year},{cvv},{password},{amount},C4000000000009999,,,"
        f"{status},23-11-2026 18:28,,,,0,0,{remaining},\n"
    )


def test_the_corpassist_layout_reads_as_the_owner_expects():
    (card,) = cards.parse_cards(_csv(_row()))
    assert card.number == "4111111111111111"  # the "C" is dropped
    assert (card.month, card.year) == ("11", "2026")
    assert card.cvv == "016"  # the leading zero survives
    assert card.password == "0123456"  # the apostrophe is not part of it
    assert card.remaining_amount == 16419.0 and card.is_virtual_pool


def test_every_column_loses_the_keep_as_text_mark():
    """The mark is taken off whatever column it is on — not only the password."""
    sheet = cards.parse_card_sheet(
        _csv(
            _row(
                number="'C4111111111111111",
                month="'11",
                year="'2026",
                cvv="'016",
                password="'0123456",
                amount="'16419",
                remaining="'12000",
                status="'cancelled",
            )
        )
    )
    assert sheet.rejected == []
    (card,) = sheet.records
    assert card.number == "4111111111111111"
    assert (card.month, card.year, card.cvv, card.password) == ("11", "2026", "016", "0123456")
    assert card.remaining_amount == 12000.0  # read, not dropped to "unknown"
    assert card.status == "cancelled"  # so a cancelled card is still recognised


def test_excels_text_formula_form_and_curly_apostrophes_come_off_too():
    # ="0123456" in a cell is written to CSV as "=""0123456""".
    (card,) = cards.parse_cards(_csv(_row(password='"=""0123456"""', cvv='"=""016"""', number="\u2019C4111111111111111")))
    assert (card.number, card.cvv, card.password) == ("4111111111111111", "016", "0123456")


def test_the_mark_comes_off_once_and_nothing_else_is_touched():
    """A second apostrophe, or a "*" anywhere, is part of the password."""
    parsed = cards.parse_cards(
        _csv(
            _row(number="C4111111111111111", password="''pw0"),
            _row(number="C4111111111111112", password="*12*34"),
            _row(number="C4111111111111113", password="pa'ss*"),
            _row(number="C4111111111111114", password="'"),
        )
    )
    assert [card.password for card in parsed] == ["'pw0", "*12*34", "pa'ss*", ""]


def test_a_workbook_text_cell_loses_its_mark_as_a_csv_cell_does():
    wb = Workbook()
    sheet = wb.active
    sheet.append(["Email", "Card Number", "Expiry Month", "Expiry Year", "CVV", "Password"])
    sheet.append(["'a@test.com", "'C4111111111111111", "'09", "'2031", "'045", "'0011223"])
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    (card,) = cards.parse_cards(buffer)
    assert (card.email, card.number, card.month, card.year, card.cvv, card.password) == (
        "a@test.com",
        "4111111111111111",
        "09",
        "2031",
        "045",
        "0011223",
    )


@pytest.mark.parametrize(
    ("month", "year", "typed"),
    [("11", "2026", "11/26"), ("9", "2031", "09/31"), ("'09", "'2031", "09/31"), ("12", "28", "12/28")],
)
def test_a_four_digit_year_goes_into_the_box_as_two_digits(month, year, typed):
    """Flipkart's box reads "MM / YY" (09 / 31 = September 2031)."""
    (card,) = cards.parse_cards(_csv(_row(month=month, year=year)))
    assert order_flow._expiry_for_form(card.month, card.year) == typed


# --- the card form in a real browser -----------------------------------------
#: A made-up page shaped like Flipkart's card form: the number box spaces the
#: digits in fours, the "MM / YY" box keeps digits only and puts its own
#: " / " after the month (so a typed "/" is dropped), like the real one.
_FORM = """
<div id="form">
  <input id="cc-input" maxlength="19" placeholder="XXXX XXXX XXXX XXXX">
  <input placeholder="MM / YY" maxlength="7" id="exp">
  <input id="cvv-input" maxlength="4" placeholder="CVV">
</div>
<script>
  const digits = (v) => v.replace(/\\D/g, "");
  document.getElementById("cc-input").addEventListener("input", (e) => {
    e.target.value = digits(e.target.value).slice(0, 16).replace(/(\\d{4})(?=\\d)/g, "$1 ");
  });
  document.getElementById("exp").addEventListener("input", (e) => {
    const d = digits(e.target.value).slice(0, 4);
    e.target.value = d.length >= 2 ? d.slice(0, 2) + " / " + d.slice(2) : d;
  });
</script>
"""


async def _typed_into_the_form(card: CardInfo) -> tuple[str | None, list[str]]:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium cannot start here: {exc}")
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(_FORM)
            problem = await order_flow._enter_card(page, card)
            shown = [await page.locator(f"#{box}").input_value() for box in ("cc-input", "exp", "cvv-input")]
        finally:
            await browser.close()
    return problem, shown


def test_the_sheets_card_is_typed_into_the_form_as_the_box_shows_it(monkeypatch):
    monkeypatch.setattr(order_flow, "CARD_FORM_SETTLE_MS", 500)
    (record,) = cards.parse_cards(_csv(_row(number="'C4111111111111111", month="11", year="2026", cvv="'016")))
    card = CardInfo(number=record.number, month=record.month, year=record.year, cvv=record.cvv, password=record.password)

    problem, shown = asyncio.run(_typed_into_the_form(card))

    assert problem is None
    assert shown == ["4111 1111 1111 1111", "11 / 26", "016"]
