"""The dashboard's "Sessions valid" tile counts what the rest of the app calls
a valid session: an account whose status says so (review of 27 Oct 2026).

A read-back that finds an account signed out marks the ACCOUNT
session_invalid (app.jobs.sync_tasks) and leaves its cookie set as the login
saved it, ``valid=True``. The tile counted cookie sets, so it went on calling
that account valid while the Sessions page, the read-back timer and the
"session invalid" alert all said otherwise.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import accounts as accounts_api
from app.api import alerts as alerts_api
from app.api import stats as stats_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus
from app.db.models import Session as SessionModel


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(stats_api.router)
    application.include_router(accounts_api.router)
    application.include_router(alerts_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def test_an_account_found_signed_out_is_not_counted_valid(api, db):
    signed_in = Account(email="signed-in@test.com", status=AccountStatus.session_valid)
    signed_out = Account(email="signed-out@test.com", status=AccountStatus.session_invalid)
    never = Account(email="never@test.com", status=AccountStatus.new)
    db.add_all([signed_in, signed_out, never])
    db.commit()
    # Both logged in once; the read-back then found the second signed out and
    # marked only the account, so its cookie set still reads valid.
    db.add_all(
        [
            SessionModel(account_id=signed_in.id, cookies=[{"name": "SN"}], valid=True),
            SessionModel(account_id=signed_out.id, cookies=[{"name": "SN"}], valid=True),
        ]
    )
    db.commit()

    tile = api.get("/stats").json()["sessions_valid"]

    assert tile == 1
    # The Sessions page counts the accounts whose status is session_valid.
    sessions_page = api.get("/accounts", params={"status": "session_valid"}).json()["total"]
    assert tile == sessions_page
    # And the alert names the very account the tile no longer counts.
    api.post("/alerts/evaluate")
    open_alerts = api.get("/alerts", params={"kind": "session_invalid"}).json()
    assert [row["account_id"] for row in open_alerts] == [signed_out.id]
