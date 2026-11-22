"""The ledger CSV goes out in pieces of about 64 KB, not one per line, and a
SQLite statement waits its turn for the lock (27 Oct 2026).

A year's ledger (40,000 units) took 13 s to export, most of it spent handing
forty thousand one-line pieces between the server's threads; and while that
one long read held SQLite's lock, the invoice backfill's write gave up after
Python's default 5 s with "database is locked". The bytes of the file are the
same as before: only how they are handed on changed.
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import insert, select

from app.core import order_filter
from app.db.models import Account, AccountStatus, Order, OrderStatus, OrderUnit
from app.io import orders_csv


@pytest.fixture
def ledger(db):
    """1,500 one-unit orders: about 1.4 MB of CSV, so many pieces."""
    account = Account(email="pieces@example.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    base = datetime(2026, 9, 1)
    db.execute(
        insert(Order),
        [
            {
                "account_id": account.id, "status": OrderStatus.placed, "model": f"Item {i} =SUM(A1)" if i % 50 == 0 else f"Item {i}",
                "order_id": f"OD{341000000000000000 + i:018d}", "qty": 1, "amount": 100.0 + i,
                "payment_mode": "cod", "placed_at": base + timedelta(minutes=i), "created_at": base,
            }
            for i in range(1_500)
        ],
    )
    db.commit()
    pks = db.execute(select(Order.id).where(Order.model.like("Item %"))).scalars().all()
    db.execute(insert(OrderUnit), [
        {"order_pk": pk, "platform": "flipkart", "position": 1, "source": "run", "unit_id": f"43{pk:019d}"} for pk in pks
    ])
    db.commit()
    return account


def _one_line_at_a_time(db) -> str:
    """The file as the old stream wrote it, a line at a time."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(orders_csv.FULL_LEDGER.headers)
    for row in order_filter.export_rows(db, order_filter.OrderFilter()):
        writer.writerow(orders_csv.safe_row(orders_csv.FULL_LEDGER.line(row)))
    return "﻿" + buffer.getvalue()


def test_the_file_goes_out_in_pieces_of_about_64_kb_with_the_same_bytes(db, ledger):
    pieces = list(orders_csv.stream(order_filter.export_rows(db, order_filter.OrderFilter())))
    whole = "".join(pieces)

    assert whole == _one_line_at_a_time(db)
    assert len(list(csv.reader(io.StringIO(whole.lstrip("﻿"))))) == 1 + 1_500
    # The header alone first, so the download starts at once; then a few
    # big pieces, not 1,500 small ones.
    assert pieces[0].startswith("﻿") and pieces[0].count("\r\n") == 1
    assert len(pieces) <= 2 + len(whole) // orders_csv.FLUSH_CHARS
    assert all(len(piece) < orders_csv.FLUSH_CHARS + 4_096 for piece in pieces[1:])
    # The formula guard still runs on every line.
    assert ", =SUM" not in whole and " =SUM(A1)" in whole


def test_the_owners_sheets_come_in_pieces_too(db, ledger):
    sheet = orders_csv.sheet_named("order_data")
    pieces = list(orders_csv.stream(order_filter.export_rows(db, order_filter.OrderFilter()), sheet=sheet))
    lines = "".join(pieces).lstrip("﻿").split("\r\n")
    assert lines[0].split(",") == sheet.headers
    assert len([line for line in lines[1:] if line]) == 1_500
    assert len(pieces) < 1_500 // 10


def test_an_empty_ledger_is_the_header_alone(db):
    pieces = list(orders_csv.stream(order_filter.export_rows(db, order_filter.OrderFilter())))
    assert len(pieces) == 1 and pieces[0].startswith("﻿") and pieces[0].count("\r\n") == 1


def test_sqlite_statements_wait_their_turn_for_the_lock(db):
    """Another connection holds SQLite's write lock for longer than Python's
    default 5 s wait: the app's own write waits for it instead of failing."""
    from app.db import base

    if base.engine.url.get_backend_name() != "sqlite":
        pytest.skip("SQLite only: PostgreSQL never blocks a write behind a read")

    holder = sqlite3.connect(base.engine.url.database, timeout=0, check_same_thread=False)
    holder.execute("BEGIN EXCLUSIVE")
    released = threading.Event()

    def release_later():
        time.sleep(float(os.environ.get("ORDERING_TEST_LOCK_HOLD", "6")))
        holder.execute("ROLLBACK")
        holder.close()
        released.set()

    threading.Thread(target=release_later, daemon=True).start()
    started = time.perf_counter()
    db.add(Account(email="waited@example.com", status=AccountStatus.new))
    db.commit()  # waits for the holder, more than 5 s, then goes through
    waited = time.perf_counter() - started
    assert released.wait(5)
    assert waited >= 5.5
    assert db.scalar(select(Account.id).where(Account.email == "waited@example.com")) is not None
