"""Hidden or shown browser: each machine's own .env decides (24 Nov 2026).

Until then the Settings page had a "Run the browser headless" switch, saved in
the database and so shared by every machine on it, and it beat the .env's
ORDERING_BROWSER_HEADLESS: the owner set that line to false on a Mac and saw
no window. Now the line decides, on its own machine only. The switch is gone
from the page, one saved in the database before is ignored (and dropped at the
next save), and a page from before the change, which still sends it, can save
the rest.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.api import corpassist as corpassist_api
from app.api import products as products_api
from app.api import screener as screener_api
from app.automation import hdfc_portal
from app.config import get_settings
from app.core import app_settings
from app.db.models import ScreenerItem, Setting
from app.jobs import tasks


@pytest.fixture
def old_switch_saved_hidden(db):
    """The Settings row as the old switch left it: hidden, for every machine."""
    db.add(Setting(key=app_settings.SETTINGS_KEY, value={"browser_headless": True, "max_concurrency": 3}))
    db.commit()
    return db


@pytest.fixture
def shows(monkeypatch):
    """This machine's .env says false: show the browser."""
    monkeypatch.setattr(get_settings(), "browser_headless", False)


@pytest.fixture
def launches(monkeypatch):
    """Every browser the products and screener pages open, by how it was asked
    for; the fake fails at once, which those pages answer without a 500."""
    seen: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        seen.append(kwargs)
        raise RuntimeError("no browser in the tests")
        yield  # pragma: no cover — never reached

    monkeypatch.setattr(products_api, "browser_page", fake_browser_page)
    monkeypatch.setattr(screener_api, "browser_page", fake_browser_page)
    products_api._CACHE.clear()
    products_api._SEARCH_CACHE.clear()
    yield seen
    products_api._CACHE.clear()
    products_api._SEARCH_CACHE.clear()


# --- runs -------------------------------------------------------------------------------
def test_the_env_line_decides_though_the_old_switch_said_hidden(old_switch_saved_hidden, shows):
    db = old_switch_saved_hidden

    assert "browser_headless" not in app_settings.get_overrides(db)
    assert "browser_headless" not in app_settings.get_effective(db)
    # Order, sign-in and read-back runs read the settings here.
    assert tasks._effective_settings().browser_headless is False
    # The HDFC portal's browser too: the settings its pages build, and the
    # portal that opens the browser for test sign-in, sync and create.
    assert hdfc_portal.config_from(corpassist_api._effective_settings(db)).headless is False
    assert corpassist_api._portal().config.headless is False
    # The rest of the saved row still counts.
    assert app_settings.get_effective(db)["max_concurrency"] == 3


def test_hidden_in_the_env_stays_hidden(db, monkeypatch):
    db.add(Setting(key=app_settings.SETTINGS_KEY, value={"browser_headless": False}))
    db.commit()
    monkeypatch.setattr(get_settings(), "browser_headless", True)

    assert tasks._effective_settings().browser_headless is True
    assert hdfc_portal.config_from(corpassist_api._effective_settings(db)).headless is True
    assert corpassist_api._portal().config.headless is True


# --- the product preview, the search and the screener ----------------------------------
def test_the_product_preview_and_search_open_the_browser_the_env_says(
    old_switch_saved_hidden, shows, launches, client, auth_headers
):
    preview = client.post("/products/preview", json={"url": "https://www.flipkart.com/x/p/itm1"}, headers=auth_headers)
    search = client.get("/products/search", params={"q": "pressure cooker"}, headers=auth_headers)

    assert preview.status_code == 200, preview.text
    assert search.status_code == 200, search.text
    assert [launch["headless"] for launch in launches] == [False, False]


def test_the_screener_opens_the_browser_the_env_says(old_switch_saved_hidden, shows, launches):
    db = old_switch_saved_hidden
    item = ScreenerItem(product_url="https://www.flipkart.com/triggr/p/itm123")
    db.add(item)
    db.commit()

    asyncio.run(screener_api._check_one(db, item, {"banks": [], "supercoin_offers": True, "coupon_offers": True}))

    assert [launch["headless"] for launch in launches] == [False]


# --- the Settings page --------------------------------------------------------------------
def test_the_switch_can_no_longer_be_saved(db):
    assert "browser_headless" not in app_settings.EDITABLE_FIELDS
    with pytest.raises(app_settings.SettingsValidationError) as excinfo:
        app_settings.set_overrides(db, {"browser_headless": False})
    assert excinfo.value.key == "browser_headless"


def test_the_page_shows_this_machines_value_read_only(old_switch_saved_hidden, shows, client, auth_headers):
    shown = client.get("/settings", headers=auth_headers)

    assert shown.status_code == 200, shown.text
    assert shown.json()["browser_headless"] is False


def test_a_page_from_before_still_saves_the_rest(old_switch_saved_hidden, shows, client, auth_headers):
    """It sends its old switch with every save of the Performance card."""
    saved = client.put("/settings", json={"browser_headless": True, "max_concurrency": 4}, headers=auth_headers)

    assert saved.status_code == 200, saved.text
    assert saved.json()["max_concurrency"] == 4
    assert saved.json()["browser_headless"] is False  # still this machine's .env
    db = old_switch_saved_hidden
    db.expire_all()
    row = db.get(Setting, app_settings.SETTINGS_KEY)
    assert row.value["max_concurrency"] == 4
    assert "browser_headless" not in row.value  # the old saved switch went with that save
