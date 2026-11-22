"""On PostgreSQL every connection stays in UTC, even after a rollback (27 Oct
2026). Each stamp is UTC held in a plain timestamp column, and PostgreSQL turns
an aware value into the session's wall clock on the way in. The connect hook
set the zone inside a transaction of its own, which the pool's rollback undid,
so on a server set to Asia/Kolkata some connections stored 15:30 for 10:00Z
and ages and durations came out 5½ hours off (two alert tests flipped by a day
between midnight and 05:30 IST)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.base import engine

pytestmark = pytest.mark.skipif(engine.dialect.name != "postgresql", reason="a PostgreSQL session setting")


def test_the_session_zone_stays_utc_after_a_rollback():
    # New connections, whose first use ends in a rollback: the case that lost
    # the setting (a connection the suite already committed on had kept it).
    engine.dispose()
    for _ in range(3):
        with engine.connect() as conn:
            assert conn.execute(text("SHOW TIME ZONE")).scalar() == "UTC"
            conn.rollback()
    engine.dispose()
