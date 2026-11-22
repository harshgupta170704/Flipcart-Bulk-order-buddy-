"""Startup schema sync: indexes declared after a table existed still get built."""

from sqlalchemy import inspect, text

from app.db.base import engine, sync_missing_indexes


def test_a_missing_index_is_created_at_startup(db):
    names = lambda: {ix["name"] for ix in inspect(engine).get_indexes("orders")}  # noqa: E731
    assert "ix_orders_unit_id" in names()  # create_all made it on a fresh database

    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_orders_unit_id"))
    assert "ix_orders_unit_id" not in names()

    created = sync_missing_indexes()

    assert "orders.ix_orders_unit_id" in created
    assert "ix_orders_unit_id" in names()
    assert sync_missing_indexes() == []  # idempotent


def test_the_ledgers_filter_columns_are_indexed(db):
    names = {ix["name"] for ix in inspect(engine).get_indexes("orders")}
    for column in ("order_id", "unit_id", "status", "placed_at", "platform_status", "synced_at", "job_id"):
        assert f"ix_orders_{column}" in names, column
