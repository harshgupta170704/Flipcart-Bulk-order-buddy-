"""Filing a large order report without holding SQLite's one writer's lock for
the whole of it, and the answer when a unit's id meets another write
(review of 27 Oct 2026).

A report of 20,000 new units filed in one transaction held SQLite's write lock
for 18 s, measured: a run recording an order, or a read-back, waited behind it,
and one that waited longer than the database's lock wait failed. On SQLite a
report is now committed in chunks of whole orders, the answer still counting
the whole report; on PostgreSQL it stays one transaction.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api import orders as orders_api
from app.db.base import engine
from app.db.models import Order, OrderUnit
from app.io import order_report
from tests.w7_report_rows import api, buyer, line, report, upload  # noqa: F401 — fixtures

ORDERS = ("OD338967210239333111", "OD338967210239333112", "OD338967210239333113")


def _rows() -> str:
    """Three orders: the first of three units, the second of one, the third of
    two — its rows apart, with the first order's last row after them."""
    unit = iter(f"4391234567890123456{n:02d}" for n in range(10, 30))
    return report(
        line(order_id=ORDERS[0], unit_id=next(unit), quantity="3"),
        line(order_id=ORDERS[0], unit_id=next(unit), quantity="3"),
        line(order_id=ORDERS[1], unit_id=next(unit)),
        line(order_id=ORDERS[0], unit_id=next(unit), quantity="3"),
        line(order_id=ORDERS[2], unit_id=next(unit), quantity=""),
        line(order_id=ORDERS[2], unit_id=next(unit), quantity=""),
    )


def test_a_report_is_committed_by_chunks_of_whole_orders(db, buyer, monkeypatch):
    seen: list[list[tuple[str, int, int]]] = []
    real_commit = db.commit

    def commit() -> None:
        # What each commit makes durable (a commit flushes first): every order
        # whole, its quantity settled.
        db.flush()
        held = db.execute(
            select(Order.order_id, Order.qty, func.count(OrderUnit.unit_id))
            .join(OrderUnit, OrderUnit.order_pk == Order.id)
            .group_by(Order.id)
            .order_by(Order.order_id)
        ).all()
        seen.append([tuple(row) for row in held])
        real_commit()

    monkeypatch.setattr(db, "commit", commit)
    parsed = order_report.read_report(_rows().encode("utf-8"), "report.csv")

    out = order_report.import_report(db, parsed, uploaded_by="ops@test.com", commit_every=2)

    # Not after the second or third row: the first order has a row further
    # down. After the fourth, the first two orders whole; after the sixth,
    # the third — its quantity settled from its units.
    assert seen == [
        [(ORDERS[0], 3, 3), (ORDERS[1], 1, 1)],
        [(ORDERS[0], 3, 3), (ORDERS[1], 1, 1), (ORDERS[2], 2, 2)],
    ]
    # The answer counts the whole report.
    assert (out.created, out.updated, out.unchanged, out.rejected) == (6, 0, 0, [])


def test_without_chunks_a_report_is_one_transaction(db, buyer, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(db, "commit", lambda: calls.append(1))
    parsed = order_report.read_report(_rows().encode("utf-8"), "report.csv")

    out = order_report.import_report(db, parsed, uploaded_by="ops@test.com")

    assert calls == [] and out.created == 6
    db.rollback()


def test_the_upload_files_by_chunks_on_sqlite_and_whole_on_postgresql(api, buyer, monkeypatch):
    asked: list[int | None] = []
    real = order_report.import_report

    def spy(db, parsed, *, uploaded_by, commit_every=None):
        asked.append(commit_every)
        return real(db, parsed, uploaded_by=uploaded_by, commit_every=commit_every)

    monkeypatch.setattr(order_report, "import_report", spy)

    out = upload(api, _rows()).json()

    assert out["created"] == 6
    assert asked == [orders_api.REPORT_CHUNK_ROWS if engine.dialect.name == "sqlite" else None]


def test_a_unit_id_met_by_another_write_is_a_plain_409(api, buyer, monkeypatch):
    database_words = 'duplicate key value violates unique constraint "uq_order_units_platform_unit_id"'

    def collide(_db, _parsed, **_kwargs):
        raise IntegrityError("INSERT INTO order_units …", {}, Exception(database_words))

    with monkeypatch.context() as patch:
        patch.setattr(order_report, "import_report", collide)
        response = upload(api, report(line()))

    assert response.status_code == 409
    assert response.json()["detail"] == orders_api.REPORT_COLLIDED
    assert "uq_order_units" not in response.text
    # Nothing is left half-done: the next upload goes in.
    assert upload(api, report(line())).json()["created"] == 1
