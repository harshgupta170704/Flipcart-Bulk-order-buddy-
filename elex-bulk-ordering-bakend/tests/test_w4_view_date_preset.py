"""A saved ledger view keeps a relative date preset (spec FR-011).

"Last 7 days" saved on one day must reopen a week later as the seven days
before that later day, not the week it was saved in. Quick Save sends the
preset beside the dates, and the page works the preset's dates out again when
it restores the view (frontend/src/lib/orderView.ts restoredFilters, tested in
orderView.test.ts with a faked clock). The server's part is to accept the
preset, keep it, and refuse one the page does not offer: a view holding a
preset the page cannot read would come back as nobody's range.
"""

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import orders as orders_api
from app.core.security import create_access_token

#: Where the page declares the presets it can save (``DatePreset``).
from tests import layout



@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def test_a_view_keeps_its_relative_preset_beside_the_dates(api):
    filters = {"date_preset": "7d", "date_from": "2026-10-18", "date_to": "2026-10-24"}

    saved = api.put("/orders/view", json={"columns": ["model"], "filters": filters})

    assert saved.status_code == 200, saved.text
    assert saved.json()["filters"] == filters
    assert api.get("/orders/view").json()["filters"] == filters
    # The other two the Range select offers.
    for preset in ("30d", "month"):
        response = api.put("/orders/view", json={"columns": [], "filters": {"date_preset": preset}})
        assert response.status_code == 200, response.text
        assert api.get("/orders/view").json()["filters"] == {"date_preset": preset}


def test_a_range_typed_by_hand_is_kept_as_its_dates_alone(api):
    filters = {"date_preset": None, "date_from": "2026-10-02", "date_to": "2026-10-24"}

    assert api.put("/orders/view", json={"columns": [], "filters": filters}).status_code == 200

    assert api.get("/orders/view").json()["filters"] == {"date_from": "2026-10-02", "date_to": "2026-10-24"}


@pytest.mark.parametrize("preset", ["yesterday", "7D", "custom", "any", 7, ["7d"], True])
def test_a_preset_the_page_does_not_offer_is_refused(api, preset):
    response = api.put("/orders/view", json={"columns": [], "filters": {"date_preset": preset}})

    assert response.status_code == 422
    assert "date_preset" in response.text
    assert api.get("/orders/view").json() is None  # nothing was saved


def test_the_server_takes_exactly_the_presets_the_page_saves():
    """A preset the page saves and the server refuses would make every Quick
    Save under it a 422, as a column the server did not know once did
    (tests/test_view_columns.py)."""
    source = layout.web_or_skip("src", "lib", "types.ts").read_text(encoding="utf-8")
    declared = re.search(r"export type DatePreset = ([^;]+);", source)
    assert declared, "DatePreset is no longer declared in frontend/src/lib/types.ts"

    assert tuple(re.findall(r'"([^"]+)"', declared.group(1))) == orders_api.DATE_PRESETS
