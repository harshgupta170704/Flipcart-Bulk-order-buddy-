"""The bank's step finished by hand (27 Oct 2026).

On 27 Oct 2026 HDFC's 3-D Secure page answered the tool's own card run with
"Request Blocked". ``POST /jobs/order`` takes ``bank_step_by_hand`` for a
person at the computer the tool runs on: the run shows its browser window,
takes the accounts one at a time and hands ``run_order`` each card without its
bank password, so nothing is typed on the bank's page. The run records how
long each bank page waits, for the run page to tell the person. A
Cash-on-Delivery run and a server with no screen are refused before anything
starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.api import jobs as jobs_api
from app.automation.ordering import ORDER_CONFIRMATION_TIMEOUT_MS, CardInfo
from app.config import get_settings
from app.core.security import create_access_token
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    Job,
    JobStatus,
    JobType,
    OrderStatus,
    Session as SessionModel,
)
from app.jobs import tasks
from tests.fakes import FakeContext, FakePage

PRODUCT = "https://www.flipkart.com/pigeon-special-3-l-outer-lid-pressure-cooker/p/itm0000000000001"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PASSWORD = "pw-of-the-test"


class _Recorder:
    """Stands in for ``run_order_job``; records the call synchronously."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})

        async def _noop() -> None:
            return None

        return _noop()


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(jobs_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def run(db, monkeypatch) -> _Recorder:
    """One account and a default address, so a good request starts a run."""
    db.add(Account(email="hand@test.com", phone="9000000001", status=AccountStatus.session_valid))
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.commit()
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    return recorder


def _cards_xlsx(email: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Email", "Card Number", "Exp Month", "Exp Year", "CVV", "Password"])
    sheet.append([email, "4111111111111111", "12", "28", "321", PASSWORD])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _card_run(api: TestClient, **fields):
    return api.post(
        "/jobs/order",
        data={"product_link": PRODUCT, "payment_mode": "card", "target": "700", **fields},
        files={"cards_file": ("cards.xlsx", _cards_xlsx("hand@test.com"), XLSX)},
    )


# --- POST /jobs/order ---------------------------------------------------------------
def test_a_card_run_finished_by_hand_reaches_the_run_and_is_recorded(api, db, run, monkeypatch):
    monkeypatch.setattr(jobs_api, "_has_screen", lambda: True)
    monkeypatch.setattr(get_settings(), "bank_otp_fill_wait", 300)

    response = _card_run(api, bank_step_by_hand="true")

    assert response.status_code == 201, response.text
    assert run.calls[0]["kwargs"]["bank_step_by_hand"] is True
    # How long each bank page waits, as run_order reckons it: its usual wait
    # for Flipkart's confirmation plus the bank's allowance.
    wait = ORDER_CONFIRMATION_TIMEOUT_MS // 1000 + 300
    assert {key: response.json()["payload"][key] for key in ("bank_step_by_hand", "bank_wait_seconds")} == {
        "bank_step_by_hand": True,
        "bank_wait_seconds": wait,
    }
    assert db.get(Job, response.json()["id"]).payload["bank_wait_seconds"] == wait


def test_a_card_run_without_it_leaves_the_bank_step_to_the_tool_as_before(api, db, run):
    response = _card_run(api)

    assert response.status_code == 201, response.text
    assert run.calls[0]["kwargs"]["bank_step_by_hand"] is False
    assert "bank_step_by_hand" not in response.json()["payload"]
    assert "bank_wait_seconds" not in response.json()["payload"]


def test_cash_on_delivery_has_no_bank_step_to_finish_by_hand(api, db, run):
    response = api.post(
        "/jobs/order",
        data={"product_link": PRODUCT, "payment_mode": "cod", "target": "700", "bank_step_by_hand": "true"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "A Cash-on-Delivery run has no bank step to finish by hand."
    assert run.calls == []
    assert db.query(Job).count() == 0


def test_without_a_screen_the_bank_step_cannot_be_finished_by_hand(api, db, run, monkeypatch):
    monkeypatch.setattr(jobs_api, "_has_screen", lambda: False)

    response = _card_run(api, bank_step_by_hand="true")

    assert response.status_code == 422, response.text
    assert "without a screen" in response.json()["detail"]
    assert run.calls == []
    assert db.query(Job).count() == 0


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("win32", {}, True),
        ("linux", {}, False),
        ("linux", {"DISPLAY": ":0"}, True),
        ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, True),
    ],
)
def test_off_a_mac_a_screen_is_a_windows_desktop_or_a_display_server(monkeypatch, platform, env, expected):
    monkeypatch.setattr(jobs_api, "sys", SimpleNamespace(platform=platform))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert jobs_api._has_screen() is expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("Aqua\n", True), ("Background\n", False), ("System\n", False), ("", False)],
)
def test_on_a_mac_a_screen_is_the_logged_in_desktop_session(monkeypatch, answer, expected):
    """A server started from the desktop's Terminal is in the "Aqua" session;
    one started over SSH or as a background service is not, and cannot open
    a window."""
    monkeypatch.setattr(jobs_api, "sys", SimpleNamespace(platform="darwin"))
    asked: list[list[str]] = []

    def fake_run(command, **_kwargs):
        asked.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=answer, stderr="")

    monkeypatch.setattr(jobs_api.subprocess, "run", fake_run)

    assert jobs_api._has_screen() is expected
    assert asked == [["launchctl", "managername"]]


@pytest.mark.parametrize(
    "failure",
    [FileNotFoundError("launchctl"), subprocess.TimeoutExpired(["launchctl", "managername"], 2)],
)
def test_a_mac_whose_session_cannot_be_named_has_no_screen(monkeypatch, failure):
    monkeypatch.setattr(jobs_api, "sys", SimpleNamespace(platform="darwin"))

    def fake_run(command, **_kwargs):
        raise failure

    monkeypatch.setattr(jobs_api.subprocess, "run", fake_run)

    assert jobs_api._has_screen() is False


# --- the worker -----------------------------------------------------------------------
@pytest.fixture
def two_accounts(db, monkeypatch) -> list[int]:
    """Two signed-in accounts, and the settings saying "headless", four
    accounts at once and a five-minute bank allowance, whatever this
    machine's own settings say. Set where every run reads them from, so a
    run that changed them for the next one would show."""
    ids = []
    for n in (1, 2):
        account = Account(email=f"hand{n}@test.com", phone=f"900000000{n}", status=AccountStatus.session_valid)
        db.add(account)
        db.commit()
        db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": str(n)}], valid=True))
        ids.append(account.id)
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.commit()

    settings = get_settings()
    monkeypatch.setattr(settings, "browser_headless", True)
    monkeypatch.setattr(settings, "max_concurrency", 4)
    monkeypatch.setattr(settings, "bank_otp_fill_wait", 300)
    return ids


def _order_run(db, monkeypatch, account_ids: list[int], *, by_hand: bool):
    """One card run over the accounts, with the browser and the order flow
    stood in for: what each window was opened with, the card each order was
    handed and how many orders were under way at once."""
    job = Job(type=JobType.order, status=JobStatus.queued)
    db.add(job)
    db.commit()
    cards = {
        account_id: CardInfo(number="4111111111111111", month="12", year="28", cvv="321", password=PASSWORD)
        for account_id in account_ids
    }
    seen: dict = {"windows": [], "cards": [], "at_once": 0, "most_at_once": 0}

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        pool = kwargs["pool"]
        seen["windows"].append({"headless": kwargs.get("headless"), "pool": pool.headless, "pictures": not pool.lean})
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    async def fake_run_order(page, *, product_link, address, gst, card=None, reserve=None, **kwargs):
        seen["cards"].append(card)
        seen["at_once"] += 1
        seen["most_at_once"] = max(seen["most_at_once"], seen["at_once"])
        await asyncio.sleep(0.1)
        seen["at_once"] -= 1
        # As the real flow does: the row is written before the order is sent.
        row = {"amount": 638.0, "payment_mode": tasks.PAYMENT_CARD, "qty": 1}
        reservation = reserve(row)
        # Unique across runs: the 18 digits end in the job's id and the order's place in it.
        order_id = f"OD33899668929659{job.id:02d}{len(seen['cards']):02d}"
        return {**row, "status": OrderStatus.placed, "order_id": order_id, "reservation": reservation}

    monkeypatch.setattr(tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(tasks, "run_order", fake_run_order)

    asyncio.run(
        tasks.run_order_job(
            job.id, account_ids, product_link=PRODUCT, target=700.0, cards=cards, bank_step_by_hand=by_hand
        )
    )
    db.expire_all()
    return seen, cards, db.get(Job, job.id)


def test_by_hand_the_window_shows_and_the_accounts_go_one_at_a_time(db, monkeypatch, two_accounts):
    seen, _cards, job = _order_run(db, monkeypatch, two_accounts, by_hand=True)

    # Shown, and with its pictures on: a person looks at it.
    assert seen["windows"] == [{"headless": False, "pool": False, "pictures": True}] * 2
    assert seen["most_at_once"] == 1
    assert job.status == JobStatus.completed


def test_by_hand_no_password_is_handed_to_the_order_flow(db, monkeypatch, two_accounts):
    seen, cards, _job = _order_run(db, monkeypatch, two_accounts, by_hand=True)

    assert [card.password for card in seen["cards"]] == ["", ""]
    assert [card.number for card in seen["cards"]] == ["4111111111111111"] * 2
    # Copies: the cards the run was handed are left as they were.
    assert [card.password for card in cards.values()] == [PASSWORD, PASSWORD]


def test_otherwise_a_card_run_is_as_before(db, monkeypatch, two_accounts):
    seen, _cards, job = _order_run(db, monkeypatch, two_accounts, by_hand=False)

    assert seen["windows"] == [{"headless": True, "pool": True, "pictures": False}] * 2
    assert seen["most_at_once"] == 2
    assert [card.password for card in seen["cards"]] == [PASSWORD, PASSWORD]
    assert job.status == JobStatus.completed


def test_the_window_is_shown_for_that_run_only(db, monkeypatch, two_accounts):
    _order_run(db, monkeypatch, two_accounts[:1], by_hand=True)

    # Neither the settings nor what a run reads of them kept the choice.
    assert get_settings().browser_headless is True
    assert tasks._effective_settings().browser_headless is True
    seen, _cards, _job = _order_run(db, monkeypatch, two_accounts[1:], by_hand=False)

    assert seen["windows"] == [{"headless": True, "pool": True, "pictures": False}]
