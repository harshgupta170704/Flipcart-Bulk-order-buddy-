"""A sheet of order ids to track: read by header, imported as orders, refused row by row."""

import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.api import orders as orders_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderUnit
from app.io import order_ids


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


# --- the reader --------------------------------------------------------------------
def test_the_reader_finds_columns_by_name_whatever_the_spelling():
    sheet = order_ids.read_id_sheet(
        _csv("Platform-Email,ORDER ID,Quantity,Remark\nA@X.com, od338967210239333100 ,2,hello\n"),
        "ids.csv",
    )
    assert not sheet.rejected
    row = sheet.rows[0]
    assert (row.email, row.order_id, row.qty, row.note) == ("a@x.com", "OD338967210239333100", 2, "hello")


def test_the_reader_refuses_a_row_with_the_reason_and_keeps_the_rest():
    text = (
        "Email,Order ID,Qty\n"
        "not-an-email,OD1234567890,1\n"
        "a@x.com,ABC,1\n"
        "a@x.com,OD1234567890,x\n"
        "a@x.com,OD1234567890,1\n"
        "\n"
        "a@x.com,OD1234567890,1\n"
    )
    sheet = order_ids.read_id_sheet(_csv(text), "ids.csv")
    assert [r.order_id for r in sheet.rows] == ["OD1234567890"]
    reasons = {r.row_number: r.reason for r in sheet.rejected}
    assert "not an email" in reasons[2]
    assert "not a Flipkart order id" in reasons[3]
    assert "quantity" in reasons[4]
    assert "repeats" in reasons[7]  # the blank line 6 is skipped, not refused


def test_the_reader_needs_the_two_required_columns():
    with pytest.raises(ValueError, match="order id"):
        order_ids.read_id_sheet(_csv("Email,Notes\na@x.com,hi\n"), "ids.csv")


def test_an_xlsx_sheet_reads_the_same_way():
    wb = Workbook()
    ws = wb.active
    ws.append(["Email", "Order ID", "Qty"])
    ws.append(["a@x.com", "OD1234567890", 3.0])  # a spreadsheet's 3.0 is the operator's 3
    buffer = io.BytesIO()
    wb.save(buffer)
    sheet = order_ids.read_id_sheet(buffer.getvalue(), "ids.xlsx")
    assert [(r.order_id, r.qty) for r in sheet.rows] == [("OD1234567890", 3)]


# --- an id a spreadsheet turned into a number (FR-060) -----------------------------------
def _number_reason(label: str, shown: str) -> str:
    return (
        f"{label} {shown!r} was saved as a number, which keeps only 15 digits, so the id may be "
        f"wrong; format the {label} column as Text and type the ids again"
    )


def test_a_csv_id_in_scientific_notation_is_refused_by_row_and_the_rest_kept():
    """Excel shows a 21-digit unit id as 4.39E+20 or 4.39123E+20, and a CSV
    saved from it keeps exactly that: the digits are gone for good."""
    text = (
        "Email,Order ID,Unit ID\n"
        "a@x.com,OD1111111111,4.39E+20\n"
        "a@x.com,OD2222222222,4.39123E+20\n"
        "a@x.com,OD3333333333,4.39123e+20\n"
        "a@x.com,3.38967E+17,439123456789012345678\n"
        "a@x.com,OD5555555555,439123456789012345678\n"
        "a@x.com,OD6666666666,\n"
    )

    sheet = order_ids.read_id_sheet(_csv(text), "ids.csv")

    assert [(r.row_number, r.order_id, r.unit_id) for r in sheet.rows] == [
        (6, "OD5555555555", "439123456789012345678"),
        (7, "OD6666666666", None),
    ]
    reasons = {r.row_number: r.reason for r in sheet.rejected}
    assert reasons == {
        2: _number_reason("unit id", "4.39E+20"),
        3: _number_reason("unit id", "4.39123E+20"),
        4: _number_reason("unit id", "4.39123e+20"),
        5: _number_reason("order id", "3.38967E+17"),
    }


def test_an_xlsx_id_in_a_long_number_cell_is_refused_by_row_and_the_rest_kept():
    """A number cell holds 15 digits at most: a longer id comes back as a float
    (its tail lost) or, at 16 digits, as a whole number Excel already rounded."""
    wb = Workbook()
    ws = wb.active
    ws.append(["Email", "Order ID", "Unit ID"])
    ws.append(["a@x.com", "OD1111111111", 4.39123456789012e20])  # a 21-digit unit id, as Excel keeps it
    ws.append(["a@x.com", "OD2222222222", 1234567890123450])  # 16 digits: the last one already lost
    ws.append(["a@x.com", 3.38967210239333e17, "439123456789012345678"])  # the order id as a number
    ws.append(["a@x.com", "OD4444444444", 12345.5])  # a fraction is never an id
    ws.append(["a@x.com", "OD5555555555", "439123456789012345678"])  # typed as text: kept
    ws.append(["a@x.com", "OD6666666666", 123456789012345])  # 15 digits fit a number cell exactly
    buffer = io.BytesIO()
    wb.save(buffer)

    sheet = order_ids.read_id_sheet(buffer.getvalue(), "ids.xlsx")

    assert [(r.row_number, r.order_id, r.unit_id) for r in sheet.rows] == [
        (6, "OD5555555555", "439123456789012345678"),
        (7, "OD6666666666", "123456789012345"),
    ]
    reasons = {r.row_number: r.reason for r in sheet.rejected}
    assert reasons == {
        2: _number_reason("unit id", "4.39123456789012E+20"),
        3: _number_reason("unit id", "1234567890123450"),
        4: _number_reason("order id", "3.38967210239333E+17"),
        5: _number_reason("unit id", "12345.5"),
    }


# --- the endpoint ------------------------------------------------------------------
@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def account(db) -> Account:
    row = Account(email="ids@test.com", status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    return row


def _upload(api, text: str, name: str = "ids.csv"):
    return api.post("/orders/import-ids", files={"file": (name, text.encode("utf-8"), "text/csv")})


def test_uploaded_ids_become_orders_to_track(api, db, account):
    out = _upload(
        api,
        "Email,Order ID,Qty\n"
        "ids@test.com,OD1111111111,2\n"
        "ids@test.com,OD2222222222,1\n"
        "nobody@test.com,OD3333333333,1\n",
    ).json()

    assert (out["created"], out["existing"]) == (2, 0)
    assert out["account_ids"] == [account.id]
    assert out["order_ids"] == ["OD1111111111", "OD2222222222"]
    assert out["rejected"] == [{"row": 4, "reason": "no account with the email nobody@test.com"}]

    db.expire_all()
    order = db.query(Order).filter(Order.order_id == "OD1111111111").one()
    assert (order.qty, order.status.value, order.placed_at, order.payment_mode) == (2, "placed", None, "unknown")
    assert [(u.position, u.source) for u in order.units] == [(1, "upload"), (2, "upload")]
    # Visible in the ledger, with its source.
    rows = api.get("/orders", params={"order_id": "OD1111111111"}).json()["items"]
    assert len(rows) == 2 and {r["source"] for r in rows} == {"upload"}

    # The same sheet again creates nothing new.
    again = _upload(api, "Email,Order ID\nids@test.com,OD1111111111\n").json()
    assert (again["created"], again["existing"]) == (0, 1)


def test_the_upload_refuses_the_wrong_kind_of_file(api, account):
    assert _upload(api, "x", name="ids.pdf").status_code == 400
    assert _upload(api, "Email,Notes\n").status_code == 422


def test_the_readers_own_refusals_reach_the_operator_whole(api, account):
    """A missing column and too many rows are the reader's own words
    (IdSheetError), written for the operator, and answered as they are."""
    missing = _upload(api, "Email,Notes\nids@test.com,hello\n")
    too_long = _upload(api, "Email,Order ID\n" + "ids@test.com,OD1234567890\n" * (order_ids.MAX_ROWS + 1))

    assert missing.status_code == 422
    assert missing.json()["detail"] == (
        "the header row needs an email column and an order id column (missing: order_id); found: Email, Notes"
    )
    assert too_long.status_code == 422
    assert too_long.json()["detail"] == f"the sheet has {order_ids.MAX_ROWS + 1:,} rows; at most {order_ids.MAX_ROWS:,} are accepted"
    with pytest.raises(order_ids.IdSheetError):
        order_ids.read_id_sheet(_csv("Email,Notes\n"), "ids.csv")


@pytest.mark.parametrize(
    ("kind", "value"),
    [("n", "4111 1111 1111 4242"), ("d", "4111-1111-1111-4242")],
    ids=["spaced-number", "number-as-date"],
)
def test_an_unreadable_sheet_is_refused_without_quoting_a_cell(api, db, account, caplog, kind, value) -> None:
    """The legacy workbook is a natural upload here: the reader finds Email
    and Order ID by name and leaves its card columns (T to W) unread. But
    openpyxl reads every cell, and its error for a badly typed one quotes the
    cell's text. Neither the answer nor the log may hold it, even with its
    spaces and dashes taken out; and nothing is imported."""
    import re
    import zipfile

    wb = Workbook()
    ws = wb.active
    ws["C1"], ws["K1"], ws["T1"] = "Email", "Order ID", "Card Number"
    ws["C2"], ws["K2"] = account.email, "OD1234567890"
    buffer = io.BytesIO()
    wb.save(buffer)
    source = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                cell = f'<c r="T2" t="{kind}"><v>{value}</v></c>'.encode()
                # Written into the row the order is in, after its last cell.
                start = data.index(b'<row r="2"')
                end = data.index(b"</row>", start)
                data = data[:end] + cell + data[end:]
            target.writestr(item, data)
    files = {"file": ("orders.xlsx", out.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}

    response = api.post("/orders/import-ids", files=files)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == orders_api.UNREADABLE_ID_SHEET
    bare = lambda text: re.sub(r"[\W_]+", "", text)  # noqa: E731
    logged = "\n".join(record.getMessage() for record in caplog.records)
    # The log names the error's type, and nothing of the cell.
    assert "id sheet unreadable: ValueError" in logged
    for text in (detail, logged):
        assert value not in text and bare(value) not in bare(text)
    db.expire_all()
    assert db.query(Order).count() == 0


def test_the_upload_answers_a_mangled_id_row_by_row_and_imports_the_rest(api, db, account):
    out = _upload(
        api,
        "Email,Order ID,Unit ID\n"
        "ids@test.com,OD1111111111,4.39123E+20\n"
        "ids@test.com,OD2222222222,439123456789012345678\n",
    ).json()

    assert (out["created"], out["order_ids"]) == (1, ["OD2222222222"])
    assert out["rejected"] == [{"row": 2, "reason": _number_reason("unit id", "4.39123E+20")}]
    db.expire_all()
    assert [o.order_id for o in db.query(Order).all()] == ["OD2222222222"]


# --- a unit id the ledger already holds (FR-024) -------------------------------------
def _holder_of(db, unit_id: str) -> list[str]:
    """The order ids whose units carry this unit id, read fresh."""
    db.expire_all()
    return [unit.order.order_id for unit in db.query(OrderUnit).filter(OrderUnit.unit_id == unit_id)]


def test_a_unit_id_another_order_holds_is_refused_by_row_not_given_a_blank_order(api, db, account):
    first = _upload(
        api,
        "Email,Order ID,Unit ID\n"
        "ids@test.com,OD1111111111,439123456789012345671\n"
        "ids@test.com,OD2222222222,439123456789012345672\n",
    ).json()
    assert first["created"] == 2

    out = _upload(
        api,
        "Email,Order ID,Unit ID\n"
        "ids@test.com,OD3333333333,439123456789012345671\n"  # a new order naming OD1111111111's unit
        "ids@test.com,OD1111111111,439123456789012345672\n"  # a known order naming OD2222222222's unit
        "ids@test.com,OD2222222222,439123456789012345672\n"  # a known order naming its own unit
        "ids@test.com,OD4444444444,439123456789012345674\n",  # a new order with a unit nobody holds
    ).json()

    assert (out["created"], out["existing"]) == (1, 1)
    assert out["rejected"] == [
        {"row": 2, "reason": "unit id 439123456789012345671 is already in the ledger under OD1111111111"},
        {"row": 3, "reason": "unit id 439123456789012345672 is already in the ledger under OD2222222222"},
    ]
    assert out["order_ids"] == ["OD2222222222", "OD4444444444"]
    db.expire_all()
    assert db.query(Order).filter(Order.order_id == "OD3333333333").count() == 0  # no order with a blank unit
    assert _holder_of(db, "439123456789012345671") == ["OD1111111111"]
    assert _holder_of(db, "439123456789012345672") == ["OD2222222222"]
    assert _holder_of(db, "439123456789012345674") == ["OD4444444444"]


def test_two_rows_of_one_sheet_cannot_give_one_unit_id_to_two_orders(db, account):
    sheet = order_ids.read_id_sheet(
        b"Email,Order ID,Unit ID\n"
        b"ids@test.com,OD1111111111,439123456789012345671\n"
        b"ids@test.com,OD2222222222,439123456789012345671\n",
        "ids.csv",
    )

    out = order_ids.import_sheet(db, sheet)

    assert out.created == 1
    assert [(r.row_number, r.reason) for r in out.rejected] == [
        (3, "unit id 439123456789012345671 is already in the ledger under OD1111111111")
    ]
    assert _holder_of(db, "439123456789012345671") == ["OD1111111111"]
    assert db.query(Order).filter(Order.order_id == "OD2222222222").count() == 0


def test_a_unit_id_held_by_an_order_with_no_id_yet_is_named_by_its_row(db, account):
    """An order still waiting to be settled has no order id to name."""
    waiting = Order(account_id=account.id, order_id=None, qty=1)
    db.add(waiting)
    db.flush()
    waiting.units[0].unit_id = "439123456789012345671"
    other = Account(email="other@test.com", status=AccountStatus.session_valid)
    db.add(other)
    db.commit()

    out = order_ids.import_sheet(
        db,
        order_ids.read_id_sheet(b"Email,Order ID,Unit ID\nother@test.com,OD1111111111,439123456789012345671\n", "ids.csv"),
    )

    assert out.created == 0
    assert [r.reason for r in out.rejected] == [
        f"unit id 439123456789012345671 is already in the ledger under order #{waiting.id}"
    ]
