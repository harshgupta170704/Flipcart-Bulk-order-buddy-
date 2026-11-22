"""A chosen seller through every door (wave 2, package A).

``POST /jobs/order`` takes an optional ``seller`` (at most 100 characters),
records it on the job and hands it to the run; the worker hands it to
``run_order``; the command line takes ``--seller``; and the product preview
lists who sells the product so the form can offer them — without a failure to
read them ever failing the preview.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import cli
from app.api import jobs as jobs_api
from app.api import products as products_api
from app.automation import ordering as order_flow
from app.automation.sellers import read_seller_offers
from app.core.security import create_access_token
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    Job,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    Session as SessionModel,
)
from app.jobs import tasks
from tests.fakes import FakeContext, FakePage
from tests.w2a_seller_pages import PIGEON_LINK, PIGEON_OTHERS_HREF, VIVO_LINK, VIVO_PRODUCT, seller_page

PRODUCT = PIGEON_LINK


class _Recorder:
    """Stands in for ``run_order_job``; records the call synchronously."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})

        async def _noop() -> None:
            return None

        return _noop()


def _client(*routers) -> TestClient:
    application = FastAPI()
    for router in routers:
        application.include_router(router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def api() -> TestClient:
    return _client(jobs_api.router)


@pytest.fixture
def run(db, monkeypatch) -> _Recorder:
    """One account and a default address, so a good request starts a run."""
    db.add(Account(email="seller@test.com", phone="9000000001", status=AccountStatus.session_valid))
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.commit()
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    return recorder


def _form(**fields) -> dict:
    """A Cash on Delivery run (no card file), plus the fields under test."""
    return {"product_link": PRODUCT, "payment_mode": "cod", "target": "18250", **fields}


# --- POST /jobs/order ---------------------------------------------------------------
def test_a_chosen_seller_reaches_the_run_and_is_recorded_on_the_job(api, db, run):
    response = api.post("/jobs/order", data=_form(seller="  SAPHEDretail "))

    assert response.status_code == 201, response.text
    assert run.calls[0]["kwargs"]["seller"] == "SAPHEDretail"
    assert response.json()["payload"]["seller"] == "SAPHEDretail"
    assert db.get(Job, response.json()["id"]).payload["seller"] == "SAPHEDretail"


def test_a_seller_of_exactly_100_characters_is_accepted(api, db, run):
    name = "S" * 100

    response = api.post("/jobs/order", data=_form(seller=name))

    assert response.status_code == 201, response.text
    assert run.calls[0]["kwargs"]["seller"] == name


def test_a_seller_over_100_characters_is_a_422_and_starts_nothing(api, db, run):
    response = api.post("/jobs/order", data=_form(seller="S" * 101))

    assert response.status_code == 422, response.text
    assert [error["loc"][-1] for error in response.json()["detail"]] == ["seller"]
    assert run.calls == []
    assert db.query(Job).count() == 0


def test_a_blank_seller_leaves_the_choice_to_flipkart_and_records_none(api, db, run):
    response = api.post("/jobs/order", data=_form(seller="   "))

    assert response.status_code == 201, response.text
    assert run.calls[0]["kwargs"]["seller"] is None
    assert "seller" not in response.json()["payload"]


def test_a_run_without_the_field_records_no_seller(api, db, run):
    response = api.post("/jobs/order", data=_form())

    assert response.status_code == 201, response.text
    assert run.calls[0]["kwargs"]["seller"] is None
    assert "seller" not in response.json()["payload"]


def _cards_xlsx(email: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Email", "Card Number", "Exp Month", "Exp Year", "CVV"])
    sheet.append([email, "4111111111111111", "12", "28", "321"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_a_card_run_hands_the_seller_on_too(api, db, run):
    response = api.post(
        "/jobs/order",
        data={**_form(seller="TBL Online"), "payment_mode": "card"},
        files={
            "cards_file": (
                "cards.xlsx",
                _cards_xlsx("seller@test.com"),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 201, response.text
    assert run.calls[0]["kwargs"]["seller"] == "TBL Online"
    assert response.json()["payload"]["seller"] == "TBL Online"


# --- the worker -----------------------------------------------------------------------
def test_the_order_job_hands_the_seller_to_run_order_and_the_row_keeps_it(db, monkeypatch):
    account = Account(email="worker@test.com", phone="9000000002", status=AccountStatus.session_valid)
    db.add(account)
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.commit()
    db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": "1"}], valid=True))
    job = Job(type=JobType.order, status=JobStatus.queued)
    db.add(job)
    db.commit()

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    handed: list[dict] = []

    async def fake_run_order(page, *, product_link, address, gst, reserve=None, **kwargs):
        handed.append(kwargs)
        # As the real flow does: the row is written before the order is sent.
        row = {"amount": 749.0, "payment_mode": tasks.PAYMENT_COD, "qty": 1, "seller": kwargs.get("seller")}
        reservation = reserve(row)
        return {**row, "status": OrderStatus.placed, "order_id": "OD428123456789100", "reservation": reservation}

    monkeypatch.setattr(tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(tasks, "run_order", fake_run_order)

    asyncio.run(
        tasks.run_order_job(
            job.id, [account.id], product_link=PRODUCT, payment_mode=tasks.PAYMENT_COD, seller="SAPHEDretail"
        )
    )

    assert handed[0]["seller"] == "SAPHEDretail"
    db.expire_all()
    order = db.query(Order).one()
    assert (order.status, order.seller) == (OrderStatus.placed, "SAPHEDretail")


# --- the command line -------------------------------------------------------------------
@pytest.fixture
def cli_account(db) -> None:
    account = Account(email="ayush@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": "1"}], valid=True))
    db.add(Address(label="lucknow", name="Ayush", line1="Garden Bay", city="Lucknow", pincode="226001", is_default=True))
    db.commit()


@pytest.fixture
def cli_runs(monkeypatch) -> list[dict]:
    @contextlib.asynccontextmanager
    async def fake_browser_page(**_):
        page = FakePage()
        yield page.context, page

    calls: list[dict] = []

    async def fake_run_order(page, **kwargs):
        calls.append(kwargs)
        return {"status": OrderStatus.failed, "amount": None, "target": 18000.0, "payment_mode": order_flow.PAYMENT_COD}

    monkeypatch.setattr(cli, "browser_page", fake_browser_page)
    monkeypatch.setattr(order_flow, "run_order", fake_run_order)
    return calls


def test_the_order_command_takes_seller_and_hands_it_to_the_run(db, cli_account, cli_runs):
    args = cli.build_parser().parse_args(
        ["order", "--product", PRODUCT, "--email", "ayush@test.com", "--target", "18000",
         "--payment-mode", "cod", "--seller", "SAPHEDretail"]
    )

    assert args.seller == "SAPHEDretail"
    asyncio.run(cli.cmd_order(args))
    assert cli_runs[0]["seller"] == "SAPHEDretail"


def test_without_seller_the_command_leaves_the_choice_to_flipkart(db, cli_account, cli_runs):
    args = cli.build_parser().parse_args(
        ["order", "--product", PRODUCT, "--email", "ayush@test.com", "--target", "18000", "--payment-mode", "cod"]
    )

    asyncio.run(cli.cmd_order(args))
    assert cli_runs[0]["seller"] is None


def test_a_command_line_seller_over_100_characters_orders_nothing(db, cli_account, cli_runs, caplog):
    args = SimpleNamespace(
        product=PRODUCT, email="ayush@test.com", address=None, address_id=None, gst_id=None, target=18000.0,
        qty=1, payment_mode=order_flow.PAYMENT_COD, card=None, month=None, year=None, cvv=None, seller="S" * 101,
    )

    asyncio.run(cli.cmd_order(args))

    assert cli_runs == []
    assert db.query(Order).count() == 0
    assert any(record.levelname == "ERROR" and "--seller" in record.getMessage() for record in caplog.records)


# --- the product preview ----------------------------------------------------------------
@pytest.fixture
def preview(monkeypatch):
    """The preview endpoint on a fake browser; returns a setter for its page."""
    products_api._CACHE.clear()
    holder: dict = {}

    @contextlib.asynccontextmanager
    async def fake_browser_page(**_):
        yield holder["page"].context, holder["page"]

    monkeypatch.setattr(products_api, "browser_page", fake_browser_page)
    client = _client(products_api.router)

    def post(page: FakePage, url: str = PIGEON_LINK) -> dict:
        holder["page"] = page
        response = client.post("/products/preview", json={"url": url})
        assert response.status_code == 200, response.text
        return response.json()

    yield post
    products_api._CACHE.clear()


def test_the_preview_lists_every_seller_and_the_default(preview):
    body = preview(seller_page())

    assert body["error"] is None and body["name"] == "Phone X"
    assert body["default_seller"] == "MYTHANGLORYRetail"
    assert body["sellers"] == [
        {"name": "MYTHANGLORYRetail", "price": 749.0, "rating": 4.3, "cod": True, "buy_now": True},
        {"name": "SAPHEDretail", "price": 749.0, "rating": 4.2, "cod": True, "buy_now": True},
        {"name": "TBL Online", "price": 749.0, "rating": 4.4, "cod": True, "buy_now": True},
    ]
    assert body["sellers_error"] is None


def test_a_one_seller_product_lists_its_one_seller_at_the_product_pages_price(preview):
    body = preview(seller_page(product=VIVO_PRODUCT, others_href=None), url=VIVO_LINK)

    assert body["default_seller"] == "HatmanRetails"
    assert body["sellers"] == [{"name": "HatmanRetails", "price": 999.0, "rating": None, "cod": None, "buy_now": None}]


def test_a_failure_to_read_the_sellers_never_fails_the_preview(preview, monkeypatch):
    async def broken(page, url, *, price=None):
        raise RuntimeError("net::ERR_ABORTED")

    monkeypatch.setattr(products_api, "read_seller_offers", broken)

    body = preview(seller_page())

    assert body["error"] is None and body["name"] == "Phone X" and body["price"] == 999.0
    assert body["sellers"] == [] and body["default_seller"] is None
    assert body["sellers_error"] == "the sellers of this product could not be read"
    assert "ERR_ABORTED" not in str(body)  # the browser's words stay in the log


def test_reading_the_sellers_has_a_bounded_wait(preview, monkeypatch):
    async def slow(page, url, *, price=None):
        await asyncio.sleep(30)

    monkeypatch.setattr(products_api, "read_seller_offers", slow)
    monkeypatch.setattr(products_api, "_SELLERS_READ_SECONDS", 0.05)

    body = preview(seller_page())

    assert body["error"] is None and body["name"] == "Phone X"
    assert body["sellers_error"] == "the sellers of this product could not be read"


def test_a_sellers_page_with_no_readable_rows_says_so(preview):
    body = preview(seller_page(sellers="Something went wrong"))

    assert body["sellers"] == [] and body["default_seller"] == "MYTHANGLORYRetail"
    assert body["sellers_error"] == "no seller could be read off this product's pages"


def test_reading_the_offers_opens_the_sellers_page_of_the_product_on_screen():
    page = seller_page()
    page.url = PIGEON_LINK

    default, offers = asyncio.run(read_seller_offers(page, PIGEON_LINK, price=749.0))

    assert default == "MYTHANGLORYRetail"
    assert [offer.name for offer in offers] == ["MYTHANGLORYRetail", "SAPHEDretail", "TBL Online"]
    assert page.goto_urls[-1].endswith(PIGEON_OTHERS_HREF.split("/sellers")[1])
    assert page.clicks == []  # reading presses nothing


def test_a_link_to_other_sellers_without_a_product_id_is_a_failure_to_read():
    page = seller_page(others_href="/sellers")
    page.url = "https://dl.flipkart.com/s/uFy91dNNNN"

    with pytest.raises(LookupError):
        asyncio.run(read_seller_offers(page, "https://dl.flipkart.com/s/uFy91dNNNN"))
