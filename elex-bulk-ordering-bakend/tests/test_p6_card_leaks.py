"""Card data stays where the owner allowed it (review of 27 Oct 2026, package P6).

The owner's rule (NFR-002, revised 25 Oct 2026): the full card number may be
kept on the order row and shown in the ledger, the dialog and the exports.
Nothing else of a card is shown or kept anywhere — not the expiry, not the
CVV, not the bank's card password, and not the full number anywhere else.
These pin the four places found carrying more than that:

* a card record printed (a log line, a traceback) showed its expiry;
* the diagnostic screenshot's fallback took a plain picture of the payment
  page without checking that the card boxes it had emptied were empty;
* FastAPI's 422 sent a card sheet posted as text straight back, whole;
* the command-line order took the card as flags, which stay in the shell's
  history and show in the process list.

Every card value here is made up, and none is written to disk: the stand-in
screenshots below keep what the boxes held in memory, not in the file.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import re
from pathlib import Path

import pytest

from app import cli
from app.automation import ordering as order_flow
from app.automation.ordering import CardInfo
from app.core import selectors as S
from app.db.models import Account, AccountStatus, Address, Order, OrderStatus, Session as SessionModel
from app.io import cards
from tests.fakes import FakeLocator, FakePage

#: Made-up card values that no id, timestamp or pincode in these tests contains.
NUMBER = "4000001234567899"
MONTH, YEAR = "07", "2031"
CVV = "386"
PASSWORD = "Netsafe-77pw"

PRODUCT = "https://dl.flipkart.com/s/uFy91dNNNN"
EMAIL = "p6-card@test.com"


# --- a card record, printed --------------------------------------------------
@pytest.mark.parametrize("year", [YEAR, YEAR[-2:]])
def test_a_printed_card_record_shows_its_last_four_and_nothing_else_of_the_card(year):
    """repr and str went into the log line or traceback that printed a record,
    and the expiry came out in clear ("exp=07/2031")."""
    record = cards.CardRecord(
        number=NUMBER, month=MONTH, year=year, cvv=CVV, email="a@test.com", password=PASSWORD
    )
    sheet = cards.CardSheet([record], [])

    for text in (repr(record), str(record), f"{record}", f"card={record!r}", repr([record]), repr(sheet)):
        assert NUMBER not in text
        assert year not in text and f"{MONTH}/" not in text  # no expiry, in either form
        assert CVV not in text
        assert PASSWORD not in text
        assert NUMBER[-4:] in text  # the last four tell two cards apart


def test_the_run_side_card_prints_no_expiry_either():
    """What the order start hands the run (``CardInfo``) was already masked;
    held to the same rule as the sheet's record."""
    card = CardInfo(number=NUMBER, month=MONTH, year=YEAR, cvv=CVV, password=PASSWORD)

    for text in (repr(card), str(card), f"{card}"):
        assert NUMBER not in text and YEAR not in text and CVV not in text and PASSWORD not in text


# --- the screenshot's fallback -----------------------------------------------
class _Box(FakeLocator):
    """A card or password box on the stand-in page. One listed in the page's
    ``unreadable`` raises when read, as a box gone between being listed and
    being read does."""

    def nth(self, index: int) -> "_Box":
        return _Box(self.page, f"{self.xpath}[{index}]")

    async def input_value(self, timeout: int | None = None) -> str:
        if self.xpath in self.page.unreadable:
            raise RuntimeError("Element is not attached to the DOM")
        return await super().input_value(timeout=timeout)


class _UnmaskablePage(FakePage):
    """A payment page whose painted-over screenshot fails, as when Playwright
    cannot settle a mask in time, so ``_snapshot`` falls back to emptying the
    boxes. A plain picture is written to disk as a stand-in file; what every
    box held at that moment is kept beside it, in memory."""

    def __init__(self) -> None:
        super().__init__()
        self.unreadable: set[str] = set()
        #: Per plain picture taken: every box that was not empty in it.
        self.pictured: list[dict[str, str]] = []

    def locator(self, selector: str) -> _Box:
        return _Box(self, selector.split("xpath=", 1)[1] if "xpath=" in selector else selector)

    async def screenshot(self, path=None, full_page=False, mask=None, mask_color=None) -> None:
        if mask:
            raise RuntimeError("Timeout 1000ms exceeded")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"\x89PNG stand-in")
        self.pictured.append({xpath: value for xpath, value in self.fills.items() if value})


@pytest.fixture
def diagnostics(monkeypatch, tmp_path) -> Path:
    folder = tmp_path / "diagnostics"
    monkeypatch.setattr(order_flow, "DIAGNOSTICS_DIR", folder)
    return folder


def _card_typed_in() -> _UnmaskablePage:
    """The payment page as a card run leaves it: the three boxes hold the card."""
    page = _UnmaskablePage()
    page.fills.update({S.CARD_NUMBER: "4000 0012 3456 7899", S.CARD_EXPIRY: "07 / 31", S.CARD_CVV: CVV})
    return page


def _pictures(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.png")) if folder.is_dir() else []


def _skipped(caplog) -> bool:
    return any(
        record.levelname == "WARNING" and record.getMessage() == "picture skipped: card boxes could not be cleared"
        for record in caplog.records
    )


def test_a_box_that_keeps_its_value_means_no_picture_at_all(diagnostics, caplog):
    page = _card_typed_in()
    page.fail_clicks_on.add(S.CARD_CVV)  # the CVV box will not take the emptying

    asyncio.run(order_flow._snapshot(page, "unconfirmed"))

    assert _pictures(diagnostics) == []
    assert page.pictured == []
    assert _skipped(caplog)


def test_boxes_that_empty_are_pictured_empty(diagnostics, caplog):
    page = _card_typed_in()

    asyncio.run(order_flow._snapshot(page, "unconfirmed"))

    assert len(_pictures(diagnostics)) == 1
    assert page.pictured == [{}]  # every box was empty in the picture
    assert not _skipped(caplog)


def test_a_page_with_no_card_form_is_pictured(diagnostics, caplog):
    page = _UnmaskablePage()
    for xpath in order_flow._SECRET_BOXES:
        page.counts[xpath] = 0

    asyncio.run(order_flow._snapshot(page, "failed-account7"))

    assert len(_pictures(diagnostics)) == 1
    assert not _skipped(caplog)


def test_every_match_of_a_box_is_emptied_and_read_not_the_first_alone(diagnostics, caplog):
    """A page can hold two CVV boxes (a saved card's beside the new card's).
    The fallback emptied the first of each kind and took the picture."""
    page = _UnmaskablePage()
    page.counts[S.CARD_CVV] = 2
    page.fills.update({f"{S.CARD_CVV}[0]": CVV, f"{S.CARD_CVV}[1]": CVV})

    asyncio.run(order_flow._snapshot(page, "unconfirmed"))

    assert len(_pictures(diagnostics)) == 1
    assert page.pictured == [{}]

    # The second one will not empty: no picture.
    page = _UnmaskablePage()
    page.counts[S.CARD_CVV] = 2
    page.fills.update({f"{S.CARD_CVV}[0]": CVV, f"{S.CARD_CVV}[1]": CVV})
    page.fail_clicks_on.add(f"{S.CARD_CVV}[1]")
    for picture in _pictures(diagnostics):
        picture.unlink()

    asyncio.run(order_flow._snapshot(page, "unconfirmed"))

    assert _pictures(diagnostics) == [] and page.pictured == []
    assert _skipped(caplog)


def test_a_box_that_cannot_be_read_back_means_no_picture(diagnostics, caplog):
    page = _card_typed_in()
    page.unreadable.add(S.CARD_NUMBER)

    asyncio.run(order_flow._snapshot(page, "unconfirmed"))  # never raises

    assert _pictures(diagnostics) == [] and page.pictured == []
    assert _skipped(caplog)


def test_the_bank_page_is_never_typed_on_to_take_a_picture(diagnostics, caplog):
    """After Pay a card run ends on the bank's own 3-D Secure page, which can
    hold a password box a person finishing the step by hand is part-way
    through. The emptying fallback must leave it alone: nothing is typed on the
    bank's page, so no box is emptied and no picture is taken there (the masked
    shot, which types nothing, is what that page normally yields)."""
    page = _UnmaskablePage()
    page.url = "https://securehdfc-acs2ui-b2.hdfcbank.com/mfaRequest.jsp"  # the bank, not Flipkart
    for xpath in (S.CARD_NUMBER, S.CARD_EXPIRY, S.CARD_CVV):
        page.counts[xpath] = 0  # no Flipkart card form on the bank's page
    page.fills["//input[@type='password']"] = PASSWORD  # a person is part-way through

    asyncio.run(order_flow._snapshot(page, "unconfirmed"))

    assert _pictures(diagnostics) == [] and page.pictured == []
    assert _skipped(caplog)
    # Nothing was typed on the bank's page: the box still holds what was entered.
    assert page.fills["//input[@type='password']"] == PASSWORD


# --- a card sheet posted as text ---------------------------------------------
_SHEET_AS_TEXT = f"Email,Card Number,Expiry,CVV,Password\na@test.com,{NUMBER},{MONTH}/{YEAR},{CVV},{PASSWORD}\n"


@pytest.mark.parametrize(
    "route, fields",
    [
        ("/jobs/order/card-preview", {"account_ids": "1"}),
        ("/jobs/order", {"product_link": PRODUCT, "target": "18250", "account_ids": "1"}),
    ],
)
def test_a_card_sheet_posted_as_text_is_not_sent_back_in_the_422(client, auth_headers, route, fields):
    """FastAPI quotes a failing field's value back: the sheet, posted as a text
    field instead of a file, came back whole — number, expiry, CVV, password."""
    response = client.post(route, data={**fields, "cards_file": _SHEET_AS_TEXT}, headers=auth_headers)

    assert response.status_code == 422
    for secret in (NUMBER, f"{MONTH}/{YEAR}", CVV, PASSWORD):
        assert secret not in response.text
    # The answer still says which field was wrong, and why.
    errors = response.json()["detail"]
    assert any(error["loc"][-1] == "cards_file" and "UploadFile" in error["msg"] for error in errors)


def test_every_other_route_keeps_fastapis_own_422(client, auth_headers):
    response = client.post("/jobs/login", json={"account_ids": "not-a-list"}, headers=auth_headers)

    assert response.status_code == 422
    assert response.json()["detail"][0]["input"] == "not-a-list"


# --- the command-line order --------------------------------------------------
@pytest.fixture
def cli_account(db) -> None:
    account = Account(email=EMAIL, status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": "1"}], valid=True))
    db.add(Address(label="lucknow", name="Ayush", line1="Garden Bay", city="Lucknow", pincode="226001", is_default=True))
    db.commit()


@pytest.fixture
def cli_watched(monkeypatch) -> dict:
    """The browsers opened, the card each order run was handed, and the hidden
    prompts asked — answered with the made-up card."""
    seen: dict = {"browsers": 0, "cards": [], "prompts": []}
    answers = {
        "Card number: ": NUMBER,
        "Expiry month (MM): ": MONTH,
        "Expiry year (YY or YYYY): ": YEAR,
        "CVV: ": CVV,
    }

    @contextlib.asynccontextmanager
    async def fake_browser_page(**_):
        seen["browsers"] += 1
        page = FakePage()
        yield page.context, page

    async def fake_run_order(page, **kwargs):
        seen["cards"].append(kwargs.get("card"))
        return {"status": OrderStatus.failed, "amount": None, "payment_mode": order_flow.PAYMENT_CARD}

    def typed_at_the_prompt(prompt: str = "", *_args) -> str:
        seen["prompts"].append(prompt)
        return answers.get(prompt, "")

    monkeypatch.setattr(cli, "browser_page", fake_browser_page)
    monkeypatch.setattr(order_flow, "run_order", fake_run_order)
    monkeypatch.setattr(getpass, "getpass", typed_at_the_prompt)
    return seen


def _order(*flags: str):
    args = cli.build_parser().parse_args(
        ["order", "--product", PRODUCT, "--email", EMAIL, "--target", "18250", *flags]
    )
    asyncio.run(args.func(args))


@pytest.mark.parametrize(
    "flags",
    [
        ["--card", NUMBER, "--month", MONTH, "--year", YEAR, "--cvv", CVV],
        ["--cvv", CVV],  # one alone was taken too, the rest asked for
    ],
)
def test_a_card_on_the_command_line_is_refused_by_flag_name(db, cli_account, cli_watched, caplog, flags):
    _order(*flags)

    assert cli_watched == {"browsers": 0, "cards": [], "prompts": []}
    assert db.query(Order).count() == 0
    refusals = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("never taken on the command line" in message and "--cvv" in message for message in refusals)
    for record in caplog.records:
        assert NUMBER not in record.getMessage() and CVV not in record.getMessage() and YEAR not in record.getMessage()


def test_without_card_flags_the_card_is_typed_at_the_hidden_prompt(db, cli_account, cli_watched):
    _order()

    assert cli_watched["prompts"] == ["Card number: ", "Expiry month (MM): ", "Expiry year (YY or YYYY): ", "CVV: "]
    (card,) = cli_watched["cards"]
    assert (card.number, card.month, card.year, card.cvv) == (NUMBER, MONTH, YEAR, CVV)


def test_the_order_commands_help_offers_no_card_flag(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["order", "--help"])
    shown = capsys.readouterr().out

    for flag in ("--card", "--month", "--year", "--cvv"):
        assert not re.search(rf"(?<![\w-]){flag}\b", shown), flag
    assert re.search(r"hidden\s+prompt", shown)  # argparse may wrap the line
