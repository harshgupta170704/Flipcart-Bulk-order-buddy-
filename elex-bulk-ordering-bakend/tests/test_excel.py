"""Tests for the unified Excel import/export (app.io.excel)."""

import io

from openpyxl import Workbook, load_workbook

from app.io import excel
from app.db.models import Account, AccountStatus, Order, OrderStatus


def _make_legacy_workbook(rows) -> io.BytesIO:
    """Build an in-memory workbook using the legacy column layout.

    rows: list of (email, phone) tuples placed at columns C and D.
    """
    wb = Workbook()
    sheet = wb.active
    sheet.cell(row=1, column=excel.COL_EMAIL, value="Email")
    sheet.cell(row=1, column=excel.COL_PHONE, value="Numbers")
    for i, (email, phone) in enumerate(rows):
        r = excel.FIRST_DATA_ROW + i
        if email is not None:
            sheet.cell(row=r, column=excel.COL_EMAIL, value=email)
        if phone is not None:
            sheet.cell(row=r, column=excel.COL_PHONE, value=phone)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_import_creates_accounts(db):
    src = _make_legacy_workbook(
        [("a@test.com", "9000000001"), ("b@test.com", "9000000002")]
    )
    result = excel.import_accounts(src, db)

    assert result == {"created": 2, "updated": 0, "skipped": 0, "rejected": []}
    emails = {a.email for a in db.query(Account).all()}
    assert emails == {"a@test.com", "b@test.com"}


def test_import_skips_a_row_with_no_email_and_reads_on(db):
    """A gap in the list is not its end: until 11 Nov 2026 the import stopped
    at the first row without an email and said nothing of the rows below."""
    src = _make_legacy_workbook(
        [("a@test.com", "1"), ("b@test.com", "2"), (None, "3"), ("c@test.com", "4")]
    )
    result = excel.import_accounts(src, db)

    assert result["created"] == 3
    assert {a.email for a in db.query(Account).all()} == {"a@test.com", "b@test.com", "c@test.com"}


def _sheet(rows) -> io.BytesIO:
    wb = Workbook()
    sheet = wb.active
    for row in rows:
        sheet.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_import_finds_the_email_column_by_its_heading_wherever_it_is(db):
    """Emails in column A used to import nothing and answer "0 created"."""
    src = _sheet([["Email", "Phone"], ["Buyer1@Example.com", "9876543210"], ["buyer2@example.com", 9876543211.0]])
    result = excel.import_accounts(src, db)

    assert result == {"created": 2, "updated": 0, "skipped": 0, "rejected": []}
    stored = {a.email: a.phone for a in db.query(Account).all()}
    assert stored == {"buyer1@example.com": "9876543210", "buyer2@example.com": "9876543211"}


def test_import_reads_a_sheet_with_title_rows_and_other_headings(db):
    src = _sheet(
        [["Buyer accounts — October"], [None], ["S.No", "Platform Email", "Mobile No"], [1, "t1@x.com", "9876543210"]]
    )
    assert excel.import_accounts(src, db)["created"] == 1
    assert db.query(Account).one().phone == "9876543210"


def test_import_reads_a_sheet_with_no_heading_row_from_its_first_row(db):
    src = _sheet([["Ismail Khan", "ismail@x.com", "9876543210"], ["Ravi", "ravi@x.com", "9876543211"]])
    result = excel.import_accounts(src, db)

    assert result["created"] == 2  # the first row is data, and a name with "mail" in it is no heading
    assert {a.email: a.phone for a in db.query(Account).all()} == {
        "ismail@x.com": "9876543210",
        "ravi@x.com": "9876543211",
    }


def test_import_keeps_an_email_given_twice_once_and_names_the_second_row(db):
    """The same email in different capitals, or with a space after it, failed
    the whole import with a server error."""
    src = _sheet([["Email"], ["one@x.com"], ["two@x.com"], ["ONE@x.com "]])
    result = excel.import_accounts(src, db)

    assert result["created"] == 2
    assert result["rejected"] == [{"row": 4, "reason": "the same email as row 2 — kept once"}]
    assert db.query(Account).count() == 2


def test_import_says_so_when_the_sheet_holds_no_email_at_all(db):
    import pytest

    with pytest.raises(excel.NoAccountsFound):
        excel.import_accounts(_sheet([["Name", "City"], ["Ramesh", "Lucknow"]]), db)
    assert db.query(Account).count() == 0


def test_import_upserts_existing(db):
    db.add(Account(email="a@test.com", phone=None, status=AccountStatus.new))
    db.commit()

    src = _make_legacy_workbook([("a@test.com", "9999999999")])
    result = excel.import_accounts(src, db)

    assert result == {"created": 0, "updated": 1, "skipped": 0, "rejected": []}
    account = db.query(Account).filter_by(email="a@test.com").one()
    assert account.phone == "9999999999"


def test_import_rejects_what_is_not_an_email_and_matches_without_regard_to_case(db):
    """Audit F-26 (25 Oct 2026): a note in the email column must not become an
    account, and "A@x.com" must not become a second row beside "a@x.com"."""
    db.add(Account(email="existing@test.com", phone="1", status=AccountStatus.new))
    db.commit()

    summary = excel.import_accounts(
        _make_legacy_workbook(
            [
                ("Existing@Test.com", "9000000002"),  # the same account, another case
                ("just a name", "9000000003"),  # not an address
                ("New.Person@Test.com", "9000000004"),
            ]
        ),
        db,
    )

    assert (summary["created"], summary["updated"], summary["skipped"]) == (1, 1, 0)
    assert summary["rejected"] == [{"row": excel.FIRST_DATA_ROW + 1, "reason": "not an email address"}]
    emails = sorted(a.email for a in db.query(Account).all())
    assert emails == ["existing@test.com", "new.person@test.com"]
    assert db.query(Account).filter(Account.email == "existing@test.com").one().phone == "9000000002"


def test_import_ignores_none_string(db):
    # Legacy sheets store the literal text "None" for missing phones.
    src = _make_legacy_workbook([("a@test.com", "None")])
    excel.import_accounts(src, db)
    account = db.query(Account).filter_by(email="a@test.com").one()
    assert account.phone is None


def test_export_roundtrip_with_order(db):
    account = Account(email="a@test.com", phone="9000000001", status=AccountStatus.new)
    account.orders.append(
        Order(
            product_link="https://flipkart.com/x",
            qty=2,
            model="Phone X",
            color="Black",
            pin="122016",
            amount=999.99,
            target=1000.0,
            order_id="OD123",
            gst_name="Acme",
            status=OrderStatus.placed,
        )
    )
    db.add(account)
    db.commit()

    data = excel.export_accounts(db)
    wb = load_workbook(io.BytesIO(data))
    sheet = wb.active

    r = excel.FIRST_DATA_ROW
    assert sheet.cell(row=r, column=excel.COL_EMAIL).value == "a@test.com"
    assert sheet.cell(row=r, column=excel.COL_MODEL).value == "Phone X"
    assert sheet.cell(row=r, column=excel.COL_AMOUNT).value == 999.99
    assert sheet.cell(row=r, column=excel.COL_ORDER_ID).value == "OD123"
