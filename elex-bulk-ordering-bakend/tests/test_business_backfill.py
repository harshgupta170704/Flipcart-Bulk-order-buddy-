"""The retired Settings "Business details" are carried into the Address Book once.

The four flat values (GSTIN, business name, address, pin) stopped being settings
when the Address Book became the only place a run ships to. An installation that
still had them saved must not wake up unable to order, or ordering under no GST
where it used to have one — and must not get the old wrong-address fallback
re-created beside the addresses the operator has since entered.
"""

from __future__ import annotations

import pytest

from app.core import app_settings
from app.db import base
from app.db.models import Address, Gst, Setting

BLOCK = {
    "default_gstin": "09abcde1234f1z5",
    "default_business_name": "Aarthbodha Ventures",
    "default_address": "Garden Bay, Sultanpur Road",
    "default_pin": "226001",
}


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    """The environment is one of the two sources; keep the machine's out of it
    — both the process environment and the developer's own ``.env`` file."""
    for key in base.RETIRED_BUSINESS_KEYS:
        monkeypatch.delenv(f"ORDERING_{key.upper()}", raising=False)
    monkeypatch.setattr(base, "RETIRED_ENV_FILE", None)


def _save(db, **extra) -> None:
    db.add(Setting(key=app_settings.SETTINGS_KEY, value={**BLOCK, **extra}))
    db.commit()


def test_an_empty_book_gets_the_saved_gst_and_address_as_defaults(db):
    _save(db, max_concurrency=3)

    created = base.sync_business_block()

    gst = db.query(Gst).one()
    assert (gst.gstin, gst.business_name, gst.is_default) == (
        "09ABCDE1234F1Z5",
        "Aarthbodha Ventures",
        True,
    )
    address = db.query(Address).one()
    assert (address.name, address.line1, address.pincode, address.is_default) == (
        "Aarthbodha Ventures",
        "Garden Bay, Sultanpur Road",
        "226001",
        True,
    )
    assert address.gst_id == gst.id
    assert created == ["GST 09ABCDE1234F1Z5", "address pin 226001"]

    # The retired keys are gone from the saved row; the live ones stay.
    db.expire_all()
    assert db.get(Setting, app_settings.SETTINGS_KEY).value == {"max_concurrency": 3}


def test_existing_addresses_are_left_alone(db):
    """The saved address is the one that shipped to the wrong place."""
    db.add(Address(label="right", name="Right Place", line1="1 Right Road", pincode="110001", is_default=True))
    db.commit()
    _save(db)

    created = base.sync_business_block()

    assert [a.label for a in db.query(Address).all()] == ["right"]
    assert db.query(Gst).one().gstin == "09ABCDE1234F1Z5"  # the GST table was empty
    assert created == ["GST 09ABCDE1234F1Z5"]


def test_other_gsts_stay_and_the_saved_gstin_is_added_beside_them(db):
    """The address must bill under the GST the retired block named — not under
    whichever entity happens to be the default."""
    kept = Gst(label="KEPT", gstin="27KEPTKEPT001Z5", business_name="Kept LLP", is_default=True)
    db.add(kept)
    db.commit()
    _save(db)

    created = base.sync_business_block()

    added = db.query(Gst).filter(Gst.gstin == "09ABCDE1234F1Z5").one()
    assert (added.label, added.business_name, added.is_default) == ("Default", "Aarthbodha Ventures", False)
    assert db.query(Gst).filter(Gst.is_default.is_(True)).one().id == kept.id  # untouched
    address = db.query(Address).one()
    assert address.gst_id == added.id
    assert created == ["GST 09ABCDE1234F1Z5", "address pin 226001"]


def test_a_gst_already_carrying_the_saved_gstin_is_reused(db):
    mine = Gst(label="AARTH", gstin="09abcde1234f1z5", business_name="Aarthbodha", is_default=False)
    other = Gst(label="OTHER", gstin="27OTHEROTHER1Z5", business_name="Other", is_default=True)
    db.add_all([mine, other])
    db.commit()
    _save(db)

    created = base.sync_business_block()

    assert db.query(Gst).count() == 2
    assert db.query(Address).one().gst_id == mine.id
    assert created == ["address pin 226001"]


def test_a_taken_default_label_gets_a_numbered_one(db):
    db.add(Gst(label="Default", gstin="27OTHEROTHER1Z5", business_name="Other", is_default=True))
    db.commit()
    _save(db)

    base.sync_business_block()

    labels = sorted(g.label for g in db.query(Gst).all())
    assert labels == ["Default", "Default-2"]
    assert db.query(Gst).filter(Gst.label == "Default-2").one().gstin == "09ABCDE1234F1Z5"


def test_a_malformed_gstin_creates_no_gst_but_the_keys_are_still_retired(db):
    """And the carried address is linked to NOTHING — not to whichever other
    entity happens to be the default GST."""
    other = Gst(label="OTHER", gstin="27OTHEROTHER1Z5", business_name="Other", is_default=True)
    db.add(other)
    db.commit()
    _save(db, default_gstin="9ABCDE1234F1Z5")  # 14 characters

    created = base.sync_business_block()

    assert [g.label for g in db.query(Gst).all()] == ["OTHER"]
    address = db.query(Address).one()  # the address itself is still carried
    assert address.gst_id is None
    assert created == ["address pin 226001"]
    db.expire_all()
    assert db.get(Setting, app_settings.SETTINGS_KEY).value == {}


def test_a_block_that_names_no_gstin_links_the_address_to_the_default_gst(db):
    other = Gst(label="OTHER", gstin="27OTHEROTHER1Z5", business_name="Other", is_default=True)
    db.add(other)
    db.commit()
    _save(db, default_gstin="")

    base.sync_business_block()

    assert db.query(Address).one().gst_id == other.id


def test_the_environment_block_is_read_once_even_though_it_cannot_be_scrubbed(db, monkeypatch):
    """A deleted default address must not come back at the next start."""
    monkeypatch.setenv("ORDERING_DEFAULT_GSTIN", "27digid1234e1z5")
    monkeypatch.setenv("ORDERING_DEFAULT_BUSINESS_NAME", "Digidrive LLP")
    monkeypatch.setenv("ORDERING_DEFAULT_ADDRESS", "88 Wrong Road")
    monkeypatch.setenv("ORDERING_DEFAULT_PIN", "411001")

    assert base.sync_business_block() == ["GST 27DIGID1234E1Z5", "address pin 411001"]
    db.query(Address).delete()
    db.query(Gst).delete()
    db.commit()

    assert base.sync_business_block() == []  # the environment still holds the block
    assert db.query(Address).count() == 0
    assert db.query(Gst).count() == 0
    mark = db.get(Setting, base.BUSINESS_BLOCK_MARK)
    assert mark is not None
    assert set(mark.value["keys"]) == set(base.RETIRED_BUSINESS_KEYS)


def test_values_in_a_dot_env_file_are_carried_too(db, tmp_path, monkeypatch):
    """The block most installations had lives in backend/.env, which only
    pydantic-settings reads — os.environ never sees it."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ORDERING_DEFAULT_GSTIN=27digid1234e1z5\n"
        "ORDERING_DEFAULT_BUSINESS_NAME=Digidrive LLP\n"
        "ORDERING_DEFAULT_ADDRESS=88 Ring Road\n"
        "ORDERING_DEFAULT_PIN=411001\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(base, "RETIRED_ENV_FILE", str(env_file))

    created = base.sync_business_block()

    assert created == ["GST 27DIGID1234E1Z5", "address pin 411001"]
    assert db.query(Gst).one().business_name == "Digidrive LLP"
    assert db.query(Address).one().line1 == "88 Ring Road"
    assert base.sync_business_block() == []  # still once


def test_it_runs_once(db):
    _save(db)
    base.sync_business_block()

    assert base.sync_business_block() == []
    assert db.query(Gst).count() == 1
    assert db.query(Address).count() == 1


def test_nothing_saved_means_nothing_done(db):
    assert base.sync_business_block() == []
    assert db.query(Gst).count() == 0
    assert db.query(Address).count() == 0
    assert db.get(Setting, app_settings.SETTINGS_KEY) is None
    assert db.get(Setting, base.BUSINESS_BLOCK_MARK) is None  # nothing to remember


def test_the_environment_is_read_when_the_row_has_nothing(db, monkeypatch):
    monkeypatch.setenv("ORDERING_DEFAULT_GSTIN", "27digid1234e1z5")
    monkeypatch.setenv("ORDERING_DEFAULT_BUSINESS_NAME", "Digidrive LLP")

    created = base.sync_business_block()

    assert created == ["GST 27DIGID1234E1Z5"]
    assert db.query(Gst).one().business_name == "Digidrive LLP"
    assert db.query(Address).count() == 0  # no address line or pin in the env
    # Still idempotent although the environment keeps carrying the values.
    assert base.sync_business_block() == []


def test_a_bad_pin_creates_no_address_but_the_keys_are_still_retired(db):
    _save(db, default_pin="22600")

    base.sync_business_block()

    assert db.query(Gst).count() == 1
    assert db.query(Address).count() == 0
    db.expire_all()
    assert db.get(Setting, app_settings.SETTINGS_KEY).value == {}


def test_the_retired_keys_never_come_back_through_the_settings_api_layer(db):
    """Belt and braces: even an un-scrubbed row cannot leak them into effect."""
    _save(db)
    assert app_settings.get_overrides(db) == {}
    assert not any(key in app_settings.get_effective(db) for key in base.RETIRED_BUSINESS_KEYS)
