"""The invoice-data CSV, streamed (audit F-39, 26 Oct 2026).

The download used to load every matching invoice and its order before the
first byte went out. Now the invoices that go in — as ids, in the file's
order — and the rows they make are settled first, and the rows are read a
batch at a time as the text is written. The file must not change by a byte:
the old code is kept below word for word and both are run over a few hundred
generated rows built to stress the order (shared dates, orders with no date,
invoices with no date, ids out of date order, several invoices and several
lines each, unreadable invoices, text a spreadsheet would misread), over
every batch size from one up.

Also: memory holds a batch, not the file; and no query is left open while
the browser downloads, so SQLite's writers — the order runs — never wait on
a download.
"""

from __future__ import annotations

import csv
import hashlib
import io
import random
import sqlite3
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.api import invoices as invoices_api
from app.api.orders import build_filter
from app.core import invoices, order_filter
from app.core.security import create_access_token
from app.db.base import SessionLocal, engine
from app.db.models import Account, AccountStatus, Invoice, Order, OrderStatus
from app.io import invoice_data_csv
from app.io.orders_csv import safe_row
from app.io.invoice_pdf import PARSER_VERSION

TYPES = ["Product", "Secure Packaging Fee", "Offer Handling Fee", 'Odd, "quoted" Fee', "Credit Note"]
MODELS = ["Apple iPhone 15 (Blue, 128 GB)", 'Phone "Pro", 5G', "=SUM(A1:A2)", "vivo T5 — Twilight", "", None]


# --- the code as it was before 26 Oct 2026, word for word --------------------------------------
def _old_data_rows(db, flt, types):
    """app.core.invoices.data_rows as it was: every row loaded at once."""
    wanted = tuple(dict.fromkeys(t for t in types if t))
    if not wanted:
        return []
    orders = order_filter.matching_orders(flt).subquery()
    stmt = (
        select(Invoice, Order)
        .join(Order, Order.id == Invoice.order_pk)
        .where(Invoice.order_pk.in_(select(orders.c.id)), Invoice.invoice_type.in_(wanted))
        .order_by(
            func.coalesce(Order.placed_at, Order.created_at).desc().nulls_last(),
            Order.id.desc(),
            Invoice.invoice_date.asc().nulls_last(),
            Invoice.id.asc(),
        )
    )
    return [(invoice, order) for invoice, order in db.execute(stmt).all()]


def _old_stream(pairs):
    """app.io.invoice_data_csv.stream as it was: one piece per invoice. Each
    line passes the same last formula guard the real one gives every line
    (orders_csv.safe_row, 27 Oct 2026): this test is about the streaming,
    which must leave the file byte for byte as it was."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(invoice_data_csv.HEADERS)
    yield "﻿" + buffer.getvalue()
    for invoice, order in pairs:
        buffer.seek(0)
        buffer.truncate()
        writer.writerows(safe_row(row) for row in invoice_data_csv.rows_for(invoice, order))
        yield buffer.getvalue()


def _old_csv(flt, types) -> tuple[str, int]:
    """The old file and its X-Invoice-Rows, from a session of their own."""
    with SessionLocal() as session:
        pairs = _old_data_rows(session, flt, types)
        return "".join(_old_stream(pairs)), invoice_data_csv.row_count(pairs)


def _new_csv(flt, types, *, chunk: int | None = None) -> tuple[str, int]:
    with SessionLocal() as session:
        selection = invoices.data_selection(session, flt, types, chunk=chunk)
        text = "".join(invoice_data_csv.stream(invoices.data_pairs(session, selection, chunk=chunk)))
        return text, selection.rows


def _data_rows(text: str) -> list[list[str]]:
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:])))[1:]


# --- a few hundred rows ------------------------------------------------------------------------
def _lines(rng: random.Random, count: int) -> list[dict] | None:
    if count < 0:
        return None
    out = []
    for n in range(count):
        line = {
            "title": rng.choice(["Apple iPhone 15", "Shipping, packing", 'Fee "A"', "=cmd", None]),
            "fsn": rng.choice(["MOBGTAGPAQNVFZZY", None, "123456789012"]),
            "hsn_sac": rng.choice(["85171300", "998599", None]),
            "imei": rng.choice(["358107262645105", "111, 222", None]),
            "qty": rng.choice([1, 2, 1.5, None, "3"]),
            "taxable": rng.choice([43219.5, 0.0, None, "83.9", "n/a"]),
            "cgst": rng.choice([3889.75, None]),
            "sgst": rng.choice([3889.75, None]),
            "igst": rng.choice([7.47, None]),
            "total": rng.choice([50999.0, 99.0, None]),
        }
        if n == 1 and rng.random() < 0.3:
            del line["total"]  # a key the parser never wrote
        out.append(line)
    return out


@pytest.fixture
def many_rows(db) -> dict:
    rng = random.Random(26_10_2026)
    accounts = [Account(email=f"stream{n}@test.com", status=AccountStatus.new) for n in range(3)]
    db.add_all(accounts)
    db.commit()
    # Few distinct moments, so many orders share one and the order id decides.
    moments = [datetime(2026, 10, day, hour, 30) for day in (1, 2, 3) for hour in (4, 23)]
    orders = [
        Order(
            account_id=accounts[n % 3].id,
            status=rng.choice([OrderStatus.placed] * 4 + [OrderStatus.failed, OrderStatus.unconfirmed]),
            qty=rng.choice([1, 1, 2, 3]),
            model=rng.choice(MODELS),
            order_id=None if n % 11 == 0 else f"OD{330000000000000000 + n * 7919}",
            placed_at=None if n % 5 == 0 else rng.choice(moments),
            created_at=rng.choice(moments),
        )
        for n in range(120)
    ]
    rng.shuffle(orders)  # ids out of date order
    db.add_all(orders)
    db.commit()
    # A few orders with neither date: they sort last, by id.
    undated = [o.id for o in orders if o.placed_at is None][:4]
    db.execute(update(Order).where(Order.id.in_(undated)).values(created_at=None))
    db.commit()

    rows: list[Invoice] = []
    for order in orders:
        for k in range(rng.choice([0, 1, 2, 3, 4, 5])):
            number = f"INV{order.id:04d}{k}"
            rows.append(
                Invoice(
                    order_pk=order.id,
                    order_id=rng.choice([order.order_id, None]),
                    kind="product",
                    invoice_type=rng.choice(TYPES + [None]),  # None: an unreadable PDF
                    invoice_number=rng.choice([number, f"={number}", "1234567890123456"]),
                    invoice_date=rng.choice([datetime(2026, 10, 2), datetime(2026, 10, 3), None]),
                    order_date=rng.choice([datetime(2026, 10, 1), None]),
                    seller_name=rng.choice(["Konde Products, Pvt Ltd", "Flipkart Internet", None]),
                    seller_gstin=rng.choice(["09AAGCK4304E1ZT", None]),
                    buyer_name=rng.choice(["Transparent Deals", 'SK "Enterprises"', None]),
                    buyer_gstin=rng.choice(["09AAUFT2204P1ZI", None]),
                    grand_total=rng.choice([50999.0, 99.0, None]),
                    original_invoice=rng.choice([None, "FASUUT2500113513"]),
                    reason=rng.choice([None, "Return\nto seller"]),
                    lines=_lines(rng, rng.choice([-1, 0, 1, 1, 2, 3])),
                    path=f"{number}.pdf",
                    size=1,
                    sha256=hashlib.sha256(number.encode()).hexdigest(),
                    parsed_at=datetime(2026, 10, 4),
                    parser_version=PARSER_VERSION,
                )
            )
    rng.shuffle(rows)  # invoice ids out of date order too
    db.add_all(rows)
    db.commit()
    return {"accounts": accounts, "orders": orders}


def test_the_generated_rows_stress_what_they_should(many_rows):
    """Guard the guard: the data really has the shapes the comparison needs."""
    text, rows = _old_csv(order_filter.OrderFilter(), TYPES)
    assert rows >= 300
    assert len(_data_rows(text)) == rows
    with SessionLocal() as session:
        undated = session.scalar(
            select(func.count()).select_from(Order).where(Order.placed_at.is_(None), Order.created_at.is_(None))
        )
        tied = session.execute(
            select(func.coalesce(Order.placed_at, Order.created_at), func.count())
            .group_by(func.coalesce(Order.placed_at, Order.created_at))
            .having(func.count() > 1)
        ).all()
        no_date = session.scalar(select(func.count()).select_from(Invoice).where(Invoice.invoice_date.is_(None)))
    assert undated >= 1 and len(tied) >= 3 and no_date >= 10


# --- the same file, byte for byte ----------------------------------------------------------------
FILTERS = {
    "everything": {},
    "placed only": {"status": ["placed"]},
    "one product": {"q": "iPhone"},
}
TYPE_SETS = {
    "all types": TYPES,
    "product": ["Product"],
    "odd ones": ['Odd, "quoted" Fee', "Credit Note", "Credit Note", " "],
    "no such type": ["No Such Type"],
}


@pytest.mark.parametrize("chunk", [1, 2, 7, 64, None], ids=lambda c: f"chunk-{c}")
@pytest.mark.parametrize("filter_name", list(FILTERS))
@pytest.mark.parametrize("types_name", list(TYPE_SETS))
def test_the_streamed_file_is_byte_for_byte_the_old_one(many_rows, chunk, filter_name, types_name):
    flt = build_filter(**FILTERS[filter_name])
    types = TYPE_SETS[types_name]

    old_text, old_rows = _old_csv(flt, types)
    new_text, new_rows = _new_csv(flt, types, chunk=chunk)

    assert new_text == old_text
    assert new_rows == old_rows == len(_data_rows(new_text))


@pytest.mark.parametrize("flush_chars", [1, 300, 64 * 1024])
def test_the_pieces_the_text_goes_out_in_do_not_change_it(many_rows, monkeypatch, flush_chars):
    monkeypatch.setattr(invoice_data_csv, "FLUSH_CHARS", flush_chars)
    flt = order_filter.OrderFilter()

    assert _new_csv(flt, TYPES, chunk=7)[0] == _old_csv(flt, TYPES)[0]


def test_the_endpoint_streams_the_old_file_with_the_old_headers(many_rows, monkeypatch):
    monkeypatch.setattr(invoices, "DATA_CHUNK", 7)
    application = FastAPI()
    application.include_router(invoices_api.router)
    api = TestClient(application, headers={"Authorization": f"Bearer {create_access_token('tester', role='admin')}"})
    flt = build_filter(status=["placed"])
    old_text, old_rows = _old_csv(flt, TYPES)
    with SessionLocal() as session:
        _with, left_out = invoices.unit_split(session, flt, TYPES)

    response = api.get("/invoices/data.csv", params=[("status", "placed")] + [("type", t) for t in TYPES])

    assert response.status_code == 200, response.text
    assert response.text == old_text
    assert response.headers["x-invoice-rows"] == str(old_rows)
    assert response.headers["x-units-left-out"] == str(left_out)
    assert response.headers["content-type"].startswith("text/csv")


# --- what it holds, and what it holds open ------------------------------------------------------
def test_memory_holds_a_batch_of_rows_not_the_file(many_rows):
    """The selection holds ids — no invoice or order is loaded by it — and the
    rows come ten at a time: never more than two batches' invoices and orders
    alive at once (the one being written and the one being read), however
    many there are."""
    with SessionLocal() as session:
        selection = invoices.data_selection(session, order_filter.OrderFilter(), TYPES, chunk=10)
        assert len(session.identity_map) == 0
        assert len(selection.invoice_ids) >= 150

        most = written = 0
        for _pair in invoices.data_pairs(session, selection, chunk=10):
            written += 1
            most = max(most, len(session.identity_map))

    assert written == len(selection.invoice_ids)
    assert most <= 2 * (10 + 10)


def test_no_query_is_left_open_while_the_file_downloads(many_rows, monkeypatch):
    """Paused between two pieces — the browser not reading — the download
    holds no read: another connection writes and commits at once. A query
    read lazily as the file went out (yield_per) would hold SQLite's lock
    until the download ended, and this commit would fail as "database is
    locked"."""
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite's file lock is what this guards")
    monkeypatch.setattr(invoice_data_csv, "FLUSH_CHARS", 1)  # a piece per invoice
    with SessionLocal() as session:
        selection = invoices.data_selection(session, order_filter.OrderFilter(), TYPES, chunk=10)
        pieces = invoice_data_csv.stream(invoices.data_pairs(session, selection, chunk=10))
        head, first = next(pieces), next(pieces)  # the header, then one invoice of a batch of ten
        writer = sqlite3.connect(engine.url.database, timeout=0.2)
        try:
            writer.execute("UPDATE accounts SET phone = '9000000009'")
            writer.commit()
        finally:
            writer.close()
        rest = "".join(pieces)
    assert len(_data_rows(head + first + rest)) == selection.rows


def test_an_invoice_gone_since_it_was_selected_is_left_out(db, many_rows):
    flt = order_filter.OrderFilter()
    with SessionLocal() as session:
        selection = invoices.data_selection(session, flt, ["Product"], chunk=5)
        gone = selection.invoice_ids[3]
        session.execute(Invoice.__table__.delete().where(Invoice.id == gone))
        session.commit()
        written = [invoice.id for invoice, _order in invoices.data_pairs(session, selection, chunk=5)]
    assert gone not in written
    assert written == [i for i in selection.invoice_ids if i != gone]


def test_no_type_is_an_empty_selection(many_rows):
    with SessionLocal() as session:
        selection = invoices.data_selection(session, order_filter.OrderFilter(), ["", ""])
        assert (list(selection.invoice_ids), selection.rows) == ([], 0)
        assert list(invoices.data_pairs(session, selection)) == []
