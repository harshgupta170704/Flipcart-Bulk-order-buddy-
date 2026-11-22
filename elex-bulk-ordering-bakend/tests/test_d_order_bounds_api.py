"""The order run's numbers are bounded at the door (audit F-23, 25 Oct 2026).

``POST /jobs/order`` declares the bounds on its form fields: a quantity of at
least 1, and a target and a cart amount limit that are finite and above zero.
Without them a quantity of 0 ordered one unit, an infinite cap let every price
through and a NaN cap refused every one — runs that looked configured and were
not. These pin each bound, and that a refused request starts nothing: no job
row, no run.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import jobs as jobs_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Address, Job

PRODUCT = "https://www.flipkart.com/vivo-t5-lite/p/itm0000000000000"

#: The four ways a price cap goes wrong, as a form sends them.
BAD_CAPS = ["inf", "nan", "0", "-1"]


class _Recorder:
    """Stands in for ``run_order_job``; records the call synchronously."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})

        async def _noop() -> None:
            return None

        return _noop()


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(jobs_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def run(db, monkeypatch) -> _Recorder:
    """One account and a default address, so a good request would start a run."""
    db.add(Account(email="bounds@test.com", phone="9000000001", status=AccountStatus.session_valid))
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.commit()
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    return recorder


def _form(**fields) -> dict:
    """A Cash on Delivery run (no card file needed), plus the fields under test."""
    return {"product_link": PRODUCT, "payment_mode": "cod", **fields}


def _refused(api: TestClient, db, run: _Recorder, **fields) -> None:
    (field,) = fields
    response = api.post("/jobs/order", data=_form(**fields))
    assert response.status_code == 422, response.text
    # Refused for that field, not for anything else in the form.
    assert [error["loc"][-1] for error in response.json()["detail"]] == [field]
    assert run.calls == []  # no run started
    assert db.query(Job).count() == 0  # and no job row written


def test_a_good_request_starts_a_run(api, db, run):
    """The control: the same form with sound numbers is accepted."""
    response = api.post("/jobs/order", data=_form(qty="1", target="18250", cart_amount_limit="18250"))
    assert response.status_code == 201, response.text
    assert len(run.calls) == 1
    kwargs = run.calls[0]["kwargs"]
    assert (kwargs["qty"], kwargs["target"], kwargs["cart_amount_limit"]) == (1, 18250.0, 18250.0)


@pytest.mark.parametrize("qty", ["0", "-1"])
def test_a_quantity_below_one_is_a_422(api, db, run, qty):
    _refused(api, db, run, qty=qty)


@pytest.mark.parametrize("target", BAD_CAPS)
def test_a_target_that_is_not_a_finite_price_above_zero_is_a_422(api, db, run, target):
    _refused(api, db, run, target=target)


@pytest.mark.parametrize("limit", BAD_CAPS)
def test_a_cart_amount_limit_that_is_not_a_finite_price_above_zero_is_a_422(api, db, run, limit):
    _refused(api, db, run, cart_amount_limit=limit)


def test_leaving_the_caps_out_is_still_allowed(api, db, run):
    """No cart limit is a run without that cap, as before — the bounds apply to
    a value that is given. No price cap must now be said on purpose
    (``no_cap``, audit F-23); a blank target alone is refused."""
    assert api.post("/jobs/order", data=_form()).status_code == 422
    response = api.post("/jobs/order", data=_form(no_cap="true"))
    assert response.status_code == 201, response.text
    kwargs = run.calls[0]["kwargs"]
    assert (kwargs["qty"], kwargs["target"], kwargs["cart_amount_limit"]) == (1, None, None)
