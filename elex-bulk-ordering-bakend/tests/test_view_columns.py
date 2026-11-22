"""The ledger's saved view and the page's column registry must name the same
columns.

Quick Save sends the columns the page shows; the server refuses any id it
does not know. When the registry gained the card number (on by default) and
the delivery address and this list did not, every Quick Save of the default
columns answered 422. The registry is read out of the frontend source here,
so the two cannot drift apart again without this file saying so.
"""

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import orders as orders_api
from app.core.security import create_access_token

from tests import layout

#: Both tests read the page's own column registry out of the web app's source.
pytestmark = layout.needs_web
REGISTRY_FILE = layout.web("src", "components", "OrderTable.tsx")
#: The first line of one registry entry: ``id: "qty", label: "Qty", defaultOn: true,``
#: — all three keys, in that order, on one line. Nothing else in the file reads so.
_ENTRY = re.compile(
    r'^\s*id:\s*"([a-z][a-z0-9_]*)",\s*label:\s*"[^"]*",\s*defaultOn:\s*(true|false),?\s*$',
    re.MULTILINE,
)


def registry() -> dict[str, bool]:
    """``{column id: shown by default}`` from the page's REGISTRY array alone."""
    source = REGISTRY_FILE.read_text(encoding="utf-8")
    start = source.index("const REGISTRY")
    end = source.index("\n];", start)
    found = {column: flag == "true" for column, flag in _ENTRY.findall(source[start:end])}
    # A pattern that stopped matching would make the comparison below vacuous.
    assert len(found) >= 20, f"the registry pattern no longer fits OrderTable.tsx: {found}"
    assert {"order_id", "actions"} <= set(found)
    return found


def test_the_view_accepts_exactly_the_columns_the_page_can_show():
    ids = set(registry())
    assert ids - orders_api.VIEW_COLUMNS == set(), "in the page's registry, refused by VIEW_COLUMNS"
    assert orders_api.VIEW_COLUMNS - ids == set(), "in VIEW_COLUMNS, no longer in the page's registry"


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def test_quick_save_of_the_default_columns_is_accepted(api):
    """What every operator who never touched the column picker sends."""
    columns = registry()
    defaults = [column for column, on in columns.items() if on]
    # The column that once broke every save. It is no longer shown by default
    # (the default view holds what fits a laptop screen, 11 Nov 2026), so the
    # save of every column below is what still covers it.
    assert "card_number" in columns and defaults

    saved = api.put("/orders/view", json={"columns": defaults, "filters": {}})
    assert saved.status_code == 200, saved.text
    assert api.get("/orders/view").json()["columns"] == defaults
    # Every column ticked at once saves too.
    assert api.put("/orders/view", json={"columns": list(columns), "filters": {}}).status_code == 200
