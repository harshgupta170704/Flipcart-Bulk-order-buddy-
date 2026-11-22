"""A GSTIN is held to its real shape, in ASCII, upper-cased (audit F-24).

The pattern it replaced was written with ``\\d`` and ``(?i)[A-Z]``, which
pydantic-core reads the Unicode way: full-width or Devanagari digits and the
Kelvin sign passed and were saved, to be typed into Flipkart later. These pin
the ASCII pattern, the upper-casing that must happen before it is checked, and
the start-up carry-over of the retired business settings, which must refuse
what the API refuses rather than save it.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import gsts as gsts_api
from app.core import app_settings
from app.core.security import create_access_token
from app.db import base
from app.db.models import Address, Gst, Setting
from app.schemas.address import GstPatch, GstWrite, is_gstin

GOOD = "27AAPFU0939F1ZV"

#: Fifteen characters each, so only the shape can refuse them.
WRONG_SHAPE = {
    "no Z in the fourteenth place": "27AAPFU0939F1AV",
    "0 as the entity number": "27AAPFU0939F0ZV",
    "letters where the state digits go": "AAAAPFU0939F1ZV",
    "digits where the PAN letters go": "2712345093901ZV",
}

#: Fifteen characters each that the old pattern accepted and saved.
NOT_ASCII = {
    "full-width digits": "２７AAPFU０９３９F1ZV",
    "Devanagari digits": "२७AAPFU0939F1ZV",
    "Arabic-Indic digits": "٢٧AAPFU0939F1ZV",
    "the Kelvin sign for K": "27AAPFU0939F1ZK",
    # str.upper() makes this long s an ASCII "S": refused, not quietly turned
    # into a GSTIN nobody typed.
    "a long s in a lower-case GSTIN": "27aapſu0939f1zv",
}


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(gsts_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def _body(gstin: str, label: str = "AARTH") -> dict:
    return {"label": label, "gstin": gstin, "business_name": "Aarthbodha Pvt Ltd"}


# --- the schema ---------------------------------------------------------------
def test_a_lower_case_padded_gstin_is_upper_cased_before_its_shape_is_checked():
    """pydantic-core checks a pattern BEFORE its own to_upper, so the strict
    upper-case pattern only works because the value is upper-cased first."""
    assert GstWrite(**_body("  27aapfu0939f1zv\n")).gstin == GOOD
    assert GstPatch(gstin="27aaPFu0939f1Zv").gstin == GOOD


@pytest.mark.parametrize("gstin", list(WRONG_SHAPE.values()), ids=list(WRONG_SHAPE))
def test_a_fifteen_character_value_of_the_wrong_shape_is_refused(gstin):
    with pytest.raises(ValidationError):
        GstWrite(**_body(gstin))
    with pytest.raises(ValidationError):
        GstPatch(gstin=gstin)
    assert not is_gstin(gstin)


@pytest.mark.parametrize("gstin", list(NOT_ASCII.values()), ids=list(NOT_ASCII))
def test_non_ascii_digits_and_letters_are_refused(gstin):
    assert len(gstin) == 15  # the length check alone would let it through
    with pytest.raises(ValidationError):
        GstWrite(**_body(gstin))
    assert not is_gstin(gstin)


def test_a_patch_may_still_leave_the_gstin_out_but_not_send_null():
    assert "gstin" not in GstPatch(label="NEW").model_fields_set
    with pytest.raises(ValidationError):
        GstPatch(gstin=None)


# --- the API ------------------------------------------------------------------
def test_the_api_stores_a_lower_case_gstin_upper_cased(api, db):
    response = api.post("/gsts", json=_body("27aapfu0939f1zv"))
    assert response.status_code == 201, response.text
    assert response.json()["gstin"] == GOOD
    assert db.query(Gst).one().gstin == GOOD


def test_the_api_refuses_full_width_digits_and_a_malformed_gstin(api, db):
    assert api.post("/gsts", json=_body(NOT_ASCII["full-width digits"])).status_code == 422
    assert api.post("/gsts", json=_body(WRONG_SHAPE["no Z in the fourteenth place"])).status_code == 422
    assert db.query(Gst).count() == 0


def test_an_edit_cannot_slip_a_non_ascii_gstin_in(api, db):
    created = api.post("/gsts", json=_body(GOOD)).json()

    response = api.patch(f"/gsts/{created['id']}", json={"gstin": NOT_ASCII["Devanagari digits"]})

    assert response.status_code == 422
    db.expire_all()
    assert db.get(Gst, created["id"]).gstin == GOOD


# --- the start-up carry-over --------------------------------------------------
@pytest.fixture
def _no_env(monkeypatch):
    for key in base.RETIRED_BUSINESS_KEYS:
        monkeypatch.delenv(f"ORDERING_{key.upper()}", raising=False)
    monkeypatch.setattr(base, "RETIRED_ENV_FILE", None)


def _save_block(db, gstin: str) -> None:
    db.add(
        Setting(
            key=app_settings.SETTINGS_KEY,
            value={
                "default_gstin": gstin,
                "default_business_name": "Aarthbodha Ventures",
                "default_address": "Garden Bay, Sultanpur Road",
                "default_pin": "226001",
            },
        )
    )
    db.commit()


@pytest.mark.parametrize(
    "gstin",
    [WRONG_SHAPE["no Z in the fourteenth place"], NOT_ASCII["full-width digits"], "27DIGIDRIVE01Z5"],
    ids=["malformed", "full-width digits", "letters where digits go"],
)
def test_the_carry_over_skips_a_gstin_of_the_wrong_shape_with_a_warning(db, _no_env, caplog, gstin):
    """It used to check the length only, so a fifteen-character typo became a
    GST row — and the default one, on an empty table — that no form would
    have accepted."""
    _save_block(db, gstin)

    with caplog.at_level(logging.WARNING, logger="ordering.db"):
        created = base.sync_business_block()

    assert db.query(Gst).count() == 0
    # The address is still carried, linked to no GST rather than to a wrong one.
    address = db.query(Address).one()
    assert address.gst_id is None
    assert created == ["address pin 226001"]
    assert any(
        record.levelname == "WARNING" and "not a valid GSTIN" in record.getMessage() for record in caplog.records
    )
    # The retired keys are gone all the same: the warning is the one notice.
    db.expire_all()
    assert db.get(Setting, app_settings.SETTINGS_KEY).value == {}


@pytest.mark.parametrize("pin", ["５６０００１", "५६०००१", "56000१"], ids=["full-width", "Devanagari", "one-devanagari-digit"])
def test_a_pincode_must_be_ascii_digits(pin):
    """\\d in pydantic-core's regex and in Python's is any Unicode digit; a
    pincode is typed into Flipkart as it is, so it must be 0 to 9."""
    from app.schemas.address import AddressWrite

    with pytest.raises(ValidationError):
        AddressWrite(label="hq", name="HQ", line1="1 Main Road", pincode=pin)
    assert AddressWrite(label="hq", name="HQ", line1="1 Main Road", pincode="560001").pincode == "560001"


def test_the_carry_over_skips_an_address_whose_pincode_is_not_ascii_digits(db, _no_env):
    db.add(
        Setting(
            key=app_settings.SETTINGS_KEY,
            value={"default_address": "Garden Bay, Sultanpur Road", "default_pin": "२२६००१"},
        )
    )
    db.commit()

    base.sync_business_block()

    assert db.query(Address).count() == 0


def test_the_carry_over_upper_cases_a_good_gstin_the_way_the_api_does(db, _no_env):
    _save_block(db, " 27aapfu0939f1zv ")

    created = base.sync_business_block()

    assert db.query(Gst).one().gstin == GOOD
    assert created == [f"GST {GOOD}", "address pin 226001"]
