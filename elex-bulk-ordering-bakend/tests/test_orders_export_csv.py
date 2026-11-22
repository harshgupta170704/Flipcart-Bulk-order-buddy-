"""The ledger's CSV export: the same rows as the screen, readable in Excel.
And the workbook beside it, streamed from a private temporary file that is
gone once sent (C-9)."""

import csv
import io
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api import orders as orders_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Job, JobStatus, JobType, Order, OrderStatus
from app.io import orders_csv


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def seeded(db):
    account = Account(email="csv@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    placed = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        model="vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)",
        order_id="OD338967210239333100",
        unit_id="439123456789012345678",
        amount=18207.0,
        target=18207.0,
        payment_mode="cod",
        platform_status="Cancelled Today",
        placed_at=datetime(2026, 10, 24, 4, 40, 15),  # 10:10:15 IST
    )
    failed = Order(account_id=account.id, status=OrderStatus.failed, model="HP Backpack")
    db.add_all([placed, failed])
    db.commit()
    return {"account": account, "placed": placed, "failed": failed}


def _rows(response) -> list[dict]:
    text = response.text.lstrip("﻿")
    return list(csv.DictReader(io.StringIO(text)))


def test_the_csv_holds_the_filtered_rows_with_ist_dates_and_safe_ids(api, seeded):
    response = api.get("/orders/export.csv", params={"status": "placed"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"].startswith('attachment; filename="order_units_')
    assert response.headers["content-disposition"].endswith('.csv"')
    assert response.text.startswith("﻿")  # Excel reads UTF-8 only with the BOM

    rows = _rows(response)
    assert len(rows) == 1  # the filter applied, unlike the old export
    row = rows[0]
    assert row["Order Date"] == "24-10-2026"
    assert row["Order Time"] == "10:10:15"  # IST, not the stored UTC 04:40:15
    assert row["Unit ID"] == '="439123456789012345678"'  # every digit survives Excel
    assert row["Order ID"] == "OD338967210239333100"  # letters keep it text already
    assert row["Status"] == "Cancelled Today"
    assert row["Delivery State"] == "cancelled"
    assert row["Cancelled"] == "Yes"
    assert row["Cash on Delivery"] == "Yes"
    assert row["Unit Amount"] == "18207.00"
    assert row["Total Order Amount"] == "18207.00"
    assert row["Platform Email"] == "csv@test.com"
    assert row["Source"] == "run"


def test_the_columns_are_the_specs_in_its_order_then_the_portals_own(api, seeded):
    """FR-017: the Order Unit columns of ui-evidence X1 come first and in that
    order, blank where the ledger holds nothing yet; what this portal knows
    beyond them follows, so nothing is lost and nothing moves."""
    from app.io import orders_csv

    text = api.get("/orders/export.csv").text.lstrip("﻿")
    header = next(csv.reader(io.StringIO(text)))
    assert header == orders_csv.HEADERS
    assert header[: len(orders_csv.SPEC_HEADERS)] == orders_csv.SPEC_HEADERS
    assert orders_csv.SPEC_HEADERS[:8] == [
        "Platform", "Order Date", "Order Time", "Platform Email", "Status", "Product", "Order ID", "Unit ID",
    ]
    assert orders_csv.SPEC_HEADERS[-3:] == ["Seller Name", "IMEI", "Received"]
    assert len(set(header)) == len(header)  # no two columns share a name

    rows = _rows(api.get("/orders/export.csv", params={"status": "placed"}))
    row = rows[0]
    for blank in ("PO Number", "Estimated Delivery", "Billing Phone", "Card Number", "IMEI", "Received"):
        assert row[blank] == ""
    assert row["Outcome"] == "placed"  # the portal's own column survives, after the spec's


def test_a_multi_unit_order_splits_its_amount_per_unit(api, db, seeded):
    order = seeded["placed"]
    order.qty = 3
    order.amount = 54621.0
    db.commit()

    row = _rows(api.get("/orders/export.csv", params={"status": "placed"}))[0]
    assert row["Quantity"] == "3"
    assert row["Unit Amount"] == "18207.00"
    assert row["Total Order Amount"] == "54621.00"


def test_identifier_columns_stay_text_at_any_length():
    from app.io.orders_csv import identifier_cell, text_guard

    assert identifier_cell("8957768866") == '="8957768866"'  # a 10-digit phone
    assert identifier_cell("356938035643809") == '="356938035643809"'  # a 15-digit IMEI
    assert identifier_cell(None) == ""
    assert identifier_cell("n/a") == "n/a"
    assert text_guard("8957768866") == "8957768866"  # the generic guard leaves short runs alone


def test_an_unfiltered_export_holds_every_order(api, seeded):
    rows = _rows(api.get("/orders/export.csv"))
    assert len(rows) == 2
    assert {r["Outcome"] for r in rows} == {"placed", "failed"}


def test_a_large_export_is_allowed_and_holds_every_row(api, db, seeded):
    """The owner's decision of 25 Oct 2026 on C-9: no cap. More rows than the
    old 5,000 limit stream out whole, in the CSV and in the workbook."""
    from sqlalchemy import insert

    from app.db.models import OrderUnit

    account = seeded["account"]
    base = datetime(2026, 9, 1)
    db.execute(
        insert(Order),
        [
            {
                "account_id": account.id, "status": OrderStatus.placed, "model": f"Bulk item {i}",
                "order_id": f"OD{340000000000000000 + i:018d}", "qty": 1, "amount": 100.0,
                "payment_mode": "card", "placed_at": base + timedelta(minutes=i), "created_at": base,
            }
            for i in range(5_200)
        ],
    )
    db.commit()
    pks = db.execute(select(Order.id).where(Order.model.like("Bulk item %"))).scalars().all()
    db.execute(insert(OrderUnit), [{"order_pk": pk, "platform": "flipkart", "position": 1, "source": "run"} for pk in pks])
    db.commit()

    response = api.get("/orders/export.csv")
    assert response.status_code == 200
    assert len(_rows(response)) == 5_202
    workbook = api.get("/orders/export")
    assert workbook.status_code == 200
    assert not hasattr(orders_api, "EXPORT_CAP")


def test_the_card_number_is_written_whole_and_filters_the_ledger(api, db, seeded):
    """FR-004's card column, as the owner decided on 25 Oct 2026: the full
    number, kept as text for Excel, and a filter on any part of it."""
    placed = seeded["placed"]
    placed.payment_mode = "card"
    placed.card_number = "4111111111111111"
    db.commit()

    rows = _rows(api.get("/orders/export.csv", params={"status": "placed"}))
    assert rows[0]["Card Number"] == '="4111111111111111"'
    assert rows[0]["Cash on Delivery"] == "No"
    failed = _rows(api.get("/orders/export.csv", params={"status": "failed"}))
    assert failed[0]["Card Number"] == ""

    listed = api.get("/orders", params={"card": "1111"}).json()
    assert [o["card_number"] for o in listed["items"]] == ["4111111111111111"]
    assert api.get("/orders", params={"card": "9999"}).json()["total"] == 0
    assert api.get("/orders", params={"card": "4111 1111"}).json()["total"] == 1  # spaces ignored
    saved = api.put("/orders/view", json={"columns": ["order_id"], "filters": {"card": "1111"}})
    assert saved.status_code == 200, saved.text


def test_the_owners_two_sheets_have_exactly_their_columns(api, db, seeded):
    """The out-for-delivery and order-data sheets the owner keeps by hand
    (25 Oct 2026): short dates, the OTP, the tracking id, the phone's last six."""
    account = seeded["account"]
    account.phone = "08957768866"
    placed = seeded["placed"]
    placed.platform_status = "Out for delivery"
    unit = placed.units[0]
    unit.platform_status = "Out for delivery"
    unit.delivery_otp = "349605"
    unit.tracking_id = "FMPP4385018544"
    unit.delivery_date = datetime(2026, 10, 25, 6, 0)
    db.commit()

    out = api.get("/orders/export.csv", params={"sheet": "out_for_delivery", "status": "placed"})
    assert out.status_code == 200
    assert out.headers["content-disposition"].startswith('attachment; filename="out_for_delivery_')
    rows = _rows(out)
    assert list(rows[0]) == ["Order Date", "Status", "Product", "Order ID", "Delivery OTP", "Tracking ID", "Last 6 Digits Of Phone"]
    assert rows[0]["Order Date"] == "24/10/26"
    assert rows[0]["Status"] == "Out for delivery"
    assert rows[0]["Delivery OTP"] == '="349605"'
    assert rows[0]["Tracking ID"] == '="FMPP4385018544"' or rows[0]["Tracking ID"] == "FMPP4385018544"
    assert rows[0]["Last 6 Digits Of Phone"] == '="768866"'

    data = _rows(api.get("/orders/export.csv", params={"sheet": "order_data"}))
    assert list(data[0]) == [
        "Platform", "Order Date", "Status", "Product", "Order ID",
        "Estimated Delivery Date", "Delivery OTP", "Tracking ID", "Phone Last 6 Numbers",
    ]
    by_product = {r["Product"]: r for r in data}
    assert by_product[placed.model]["Estimated Delivery Date"] == "25/10/26"  # not delivered yet: a promise
    assert by_product["HP Backpack"]["Status"] == "Failed"  # nothing read back: the tool's own word
    assert by_product["HP Backpack"]["Order Date"] == ""

    assert api.get("/orders/export.csv", params={"sheet": "nonsense"}).status_code == 422


def test_a_cancelled_unit_shows_no_estimated_delivery_date(api, db, seeded):
    """Flipkart's timeline for a cancelled order ends "Cancelled, Oct 24" and
    that date is what the read-back keeps (seen live on 25 Oct 2026). It is
    not a delivery promise, so the order-data sheet leaves the column blank."""
    placed = seeded["placed"]
    unit = placed.units[0]
    unit.platform_status = "Cancelled"
    unit.delivery_state = None
    unit.delivery_date = datetime(2026, 10, 24, 6, 0)
    db.commit()

    data = _rows(api.get("/orders/export.csv", params={"sheet": "order_data"}))
    row = next(r for r in data if r["Product"] == placed.model)
    assert row["Status"] == "Cancelled"
    assert row["Estimated Delivery Date"] == ""

    full = _rows(api.get("/orders/export.csv", params={"status": "placed"}))
    assert full[0]["Estimated Delivery"] == ""


def test_the_xlsx_export_follows_the_same_filter(api, seeded):
    from openpyxl import load_workbook

    response = api.get("/orders/export", params={"status": "failed"})
    assert response.status_code == 200
    sheet = load_workbook(io.BytesIO(response.content)).active
    values = [[c.value for c in r] for r in sheet.iter_rows()]
    assert len(values) == 2  # header + the one failed row
    assert "HP Backpack" in values[1]


def test_text_guard_only_touches_long_digit_runs():
    assert orders_csv.text_guard("439123456789012345678") == '="439123456789012345678"'
    assert orders_csv.text_guard("9999999999") == "9999999999"  # 10 digits: a phone, left alone
    assert orders_csv.text_guard("OD3389") == "OD3389"
    assert orders_csv.text_guard(None) == ""


def test_the_filename_is_the_specs_pattern_in_ist():
    stamp = datetime(2026, 10, 21, 9, 46, tzinfo=orders_csv.UTC_TZ)  # 15:16 IST
    assert orders_csv.export_filename(stamp) == "order_units_2026-10-21_1516.csv"


def test_date_range_query_params_read_as_ist_days(api, seeded):
    # 24-10-2026 in IST includes 04:40 UTC; the day before does not.
    assert api.get("/orders", params={"date_from": "2026-10-24", "date_to": "2026-10-24"}).json()["total"] == 1
    assert api.get("/orders", params={"date_from": "2026-10-23", "date_to": "2026-10-23"}).json()["total"] == 0
    assert api.get("/orders", params={"date_from": "not-a-date"}).status_code == 422
    assert api.get("/orders", params={"date_field": "nope"}).status_code == 422


def test_filter_options_and_saved_view(api, seeded):
    opts = api.get("/orders/filter-options").json()
    assert opts["total"] == 2
    assert {"value": "Cancelled Today", "count": 1, "state": "cancelled"} in opts["platform_statuses"]

    assert api.get("/orders/view").json() is None
    saved = api.put("/orders/view", json={"columns": ["model", "amount"], "filters": {"status": ["placed"]}})
    assert saved.status_code == 200
    assert api.get("/orders/view").json()["columns"] == ["model", "amount"]
    assert api.delete("/orders/view").json() == {"cleared": True}
    assert api.get("/orders/view").json() is None


# --- what the review of this page found -----------------------------------------
def test_a_multi_word_platform_wording_matches_as_one_value(api, db, seeded):
    """The picker sends each ticked wording as its own parameter: "Out for
    delivery" is one value, not three words to match separately — and the
    page, the count and both exports must all say so."""
    moving = Order(
        account_id=seeded["account"].id,
        status=OrderStatus.placed,
        model="Moving",
        order_id="OD1",
        platform_status="Out for delivery",
        placed_at=datetime(2026, 10, 23, 4, 0),
    )
    db.add(moving)
    db.commit()
    params = {"platform_status": "Out for delivery"}

    assert api.get("/orders", params=params).json()["total"] == 1
    assert [r["Status"] for r in _rows(api.get("/orders/export.csv", params=params))] == ["Out for delivery"]
    from openpyxl import load_workbook

    sheet = load_workbook(io.BytesIO(api.get("/orders/export", params=params).content)).active
    assert sheet.max_row == 2  # header + the one row
    # Two ticked wordings: both rows, each counted once.
    both = [("platform_status", "Out for delivery"), ("platform_status", "Cancelled Today")]
    assert api.get("/orders", params=both).json()["total"] == 2


def test_job_id_and_cased_order_ids_filter_through_the_router(api, db, seeded):
    job = Job(type=JobType.order, status=JobStatus.completed)
    db.add(job)
    db.commit()
    seeded["placed"].job_id = job.id
    db.commit()

    assert api.get("/orders", params={"job_id": job.id}).json()["total"] == 1
    assert api.get("/orders", params={"job_id": job.id + 1}).json()["total"] == 0
    # An order id matches however it was typed.
    assert api.get("/orders", params={"order_id": "  od338967210239333100 "}).json()["total"] == 1


def test_rows_never_read_back_count_as_unknown_even_when_the_column_is_null(api, db, seeded):
    """A row from before the column existed holds NULL, not 'unknown'."""
    db.execute(text("UPDATE order_units SET delivery_state = NULL"))
    db.commit()
    assert api.get("/orders", params={"delivery_state": "unknown"}).json()["total"] == 2
    assert api.get("/orders", params={"delivery_state": "nope"}).status_code == 422


def test_an_oversized_number_is_refused_not_crashed(api, seeded):
    huge = "100000000000000000000"
    assert api.get("/orders", params={"page": huge}).status_code == 422
    assert api.get("/orders", params={"job_id": huge}).status_code == 422
    assert api.get("/orders", params={"account_id": huge}).status_code == 422
    assert api.get("/orders/export.csv", params={"job_id": huge}).status_code == 422


def test_datetime_bounds_are_ist_unless_an_offset_says_otherwise(api, seeded):
    # The placed order is 10:10:15 IST on the 24th (04:40:15 UTC).
    assert api.get("/orders", params={"date_from": "2026-10-24T00:00:00"}).json()["total"] == 1
    assert api.get("/orders", params={"date_to": "2026-10-24T10:00:00"}).json()["total"] == 0  # IST, exclusive
    assert api.get("/orders", params={"date_to": "2026-10-24T10:30:00+05:30"}).json()["total"] == 1
    assert api.get("/orders", params={"date_to": "2026-10-24T04:30:00Z"}).json()["total"] == 0
    assert api.get("/orders", params={"date_to": "20261024"}).status_code == 422


def test_tracking_ids_match_whatever_their_case(api, db, seeded):
    seeded["placed"].units[0].tracking_id = "FMPC123"  # the unit's, where the filter looks
    db.commit()
    assert api.get("/orders", params={"tracking_id": "fmpc123"}).json()["total"] == 1


def test_the_workbook_writes_the_ist_wall_clock_like_the_csv(api, seeded):
    from openpyxl import load_workbook

    sheet = load_workbook(io.BytesIO(api.get("/orders/export", params={"status": "placed"}).content)).active
    header = [c.value for c in sheet[1]]
    row = [c.value for c in sheet[2]]
    assert header[-2:] == ["Placed At (IST)", "Card Number"]
    assert row[-2] == datetime(2026, 10, 24, 10, 10, 15)
    assert row[-1] is None  # a Cash-on-Delivery order paid with no card


def test_an_attempt_that_never_became_an_order_has_no_order_date_anywhere(api, seeded):
    """The page shows a dash and the Order-date filter leaves it out; the CSV
    must agree rather than quietly dating it by when it was recorded."""
    rows = {r["Outcome"]: r for r in _rows(api.get("/orders/export.csv"))}
    assert rows["failed"]["Order Date"] == "" and rows["failed"]["Order Time"] == ""
    window = {"date_from": "2026-01-01", "date_to": "2099-12-31"}
    assert api.get("/orders", params=window).json()["total"] == 1
    assert len(_rows(api.get("/orders/export.csv", params=window))) == 1
    # By when it was recorded, it is there.
    assert api.get("/orders", params={**window, "date_field": "created_at"}).json()["total"] == 2


def test_a_saved_view_must_be_one_the_page_can_apply(api, seeded):
    assert api.put("/orders/view", json={"columns": ["bogus"], "filters": {}}).status_code == 422
    assert api.put("/orders/view", json={"columns": [], "filters": {"date_from": "garbage"}}).status_code == 422
    assert api.put("/orders/view", json={"columns": [], "filters": {"status": ["nope"]}}).status_code == 422
    assert api.put("/orders/view", json={"columns": [], "filters": {"mystery": 1}}).status_code == 422
    # A run's deep link and empty values are not part of a view.
    saved = api.put(
        "/orders/view",
        json={"columns": ["model"], "filters": {"status": "placed", "job_id": 42, "q": "", "order_ids": []}},
    )
    assert saved.status_code == 200
    assert api.get("/orders/view").json()["filters"] == {"status": "placed"}


def test_a_saved_card_filter_obeys_the_list_endpoints_limit(api, seeded):
    """A view's filters are restored verbatim into the list's query string, so
    a card the list endpoint would refuse must not save: it would break every
    ledger load until Default View."""
    from app.core.order_filter import CARD_MAX_LENGTH

    too_long = "4" * (CARD_MAX_LENGTH + 1)
    assert api.get("/orders", params={"card": too_long}).status_code == 422  # what every load would hit
    assert api.put("/orders/view", json={"columns": [], "filters": {"card": too_long}}).status_code == 422
    assert api.put("/orders/view", json={"columns": [], "filters": {"card": ["1111"]}}).status_code == 422

    longest = "4" * CARD_MAX_LENGTH
    assert api.put("/orders/view", json={"columns": [], "filters": {"card": longest}}).status_code == 200
    restored = api.get("/orders/view").json()["filters"]["card"]
    assert api.get("/orders", params={"card": restored}).status_code == 200


def test_a_card_filter_with_no_digits_empties_the_page_and_both_exports(api, db, seeded):
    """"abcd" in the card box is a filter the screen shows as applied: the
    page, its count and both exports must narrow to nothing, not quietly to
    everything."""
    seeded["placed"].card_number = "4111111111111111"
    db.commit()
    params = {"card": "abcd"}

    page = api.get("/orders", params=params).json()
    assert (page["total"], page["items"]) == (0, [])
    assert _rows(api.get("/orders/export.csv", params=params)) == []
    from openpyxl import load_workbook

    sheet = load_workbook(io.BytesIO(api.get("/orders/export", params=params).content)).active
    assert sheet.max_row == 1  # the header alone


# --- the workbook, streamed from a temporary file (C-9) -------------------------------------
@pytest.fixture
def temp_folder(tmp_path):
    """The system's temp folder, for this test alone: where the export and
    openpyxl's own sheet file are written, so what is left there can be seen.
    Put back by this fixture itself, before the test database is dropped —
    which is allowed only under the real temp folder."""
    import tempfile

    folder = tmp_path / "tmp"
    folder.mkdir()
    before = tempfile.tempdir
    tempfile.tempdir = str(folder)
    try:
        yield folder
    finally:
        tempfile.tempdir = before


def test_the_workbook_is_streamed_from_a_private_temporary_file_never_built_in_memory(api, seeded, temp_folder, monkeypatch):
    """C-9: no cap, and nothing held in memory. The finished workbook is not
    an in-memory buffer — no BytesIO in the export or the route — but a file
    in the temp folder that only the server's user can read, streamed to the
    client and gone once the response is read."""
    import os
    import stat
    import types

    from openpyxl import load_workbook

    from app.io import excel

    def no_buffer(*_args, **_kwargs):
        raise AssertionError("the workbook was built in memory")

    monkeypatch.setattr(excel, "io", types.SimpleNamespace(BytesIO=no_buffer))
    monkeypatch.setattr(orders_api, "io", types.SimpleNamespace(BytesIO=no_buffer))
    real = excel.export_orders
    written: list[tuple] = []

    def spy(db, flt=None):
        path = real(db, flt)
        written.append((path, stat.S_IMODE(os.stat(path).st_mode)))
        return path

    monkeypatch.setattr(excel, "export_orders", spy)

    response = api.get("/orders/export", params={"status": "placed"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert response.headers["content-disposition"] == 'attachment; filename="orders.xlsx"'
    sheet = load_workbook(io.BytesIO(response.content)).active
    rows = [[cell.value for cell in row] for row in sheet.iter_rows()]
    assert len(rows) == 2 and "OD338967210239333100" in rows[1]
    [(path, mode)] = written
    assert path.parent == temp_folder and path.suffix == ".xlsx"
    assert mode == 0o600  # the card numbers in it are the server's alone
    assert not path.exists()  # deleted once sent
    assert list(temp_folder.iterdir()) == []  # openpyxl's own sheet file too


def test_the_route_answers_with_the_file_not_a_body_built_beforehand(db, seeded, temp_folder):
    from pathlib import Path

    from fastapi.responses import FileResponse
    from starlette.responses import StreamingResponse

    from app.core.order_filter import OrderFilter

    response = orders_api.export_orders(flt=OrderFilter(), db=db)
    try:
        assert isinstance(response, FileResponse) and not isinstance(response, StreamingResponse)
        assert Path(response.path).parent == temp_folder
        assert response.background is not None  # deletes the file once sent
    finally:
        Path(response.path).unlink(missing_ok=True)


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
def test_the_workbook_file_is_deleted_when_the_client_goes_away(db, seeded, temp_folder, spec_version):
    """Mid-download the client is gone. Before ASGI 2.4 Starlette hears the
    disconnect, stops sending and runs the background task; from 2.4 on the
    send itself fails and background tasks are skipped — the file must go
    either way, not sit in the temp folder with every card number in it."""
    import asyncio
    from pathlib import Path

    from app.core.order_filter import OrderFilter

    response = orders_api.export_orders(flt=OrderFilter(), db=db)
    path = Path(response.path)
    assert path.exists()
    scope = {"type": "http", "method": "GET", "headers": [], "asgi": {"version": "3.0", "spec_version": spec_version}}

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body":
            raise OSError("the client went away")
        await asyncio.sleep(0)

    async def scenario() -> None:
        try:
            await response(scope, receive, send)
        except OSError:
            pass  # the server's to log; the file is what matters here

    asyncio.run(scenario())

    assert not path.exists()
    assert list(temp_folder.iterdir()) == []


def test_an_export_that_fails_part_way_leaves_no_file_behind(db, seeded, temp_folder, monkeypatch):
    """The database gone after the first row: nothing of the rows written so
    far — card numbers included — is left in the temp folder, neither the
    workbook nor openpyxl's sheet file."""
    from sqlalchemy.exc import OperationalError

    from app.core import order_filter
    from app.io import excel

    real = order_filter.export_rows

    def failing(db, flt, **kwargs):
        for row in real(db, flt, **kwargs):
            yield row
            raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setattr(order_filter, "export_rows", failing)

    with pytest.raises(OperationalError):
        excel.export_orders(db)

    assert list(temp_folder.iterdir()) == []
