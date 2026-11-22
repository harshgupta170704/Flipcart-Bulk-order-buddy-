"""Step E: AI mode, an order run paced in batches (app.jobs.pacing).

The engine is driven with a fake clock and a fake batch runner, so the tests
are exact and take no time.
"""

from __future__ import annotations

import asyncio

import pytest

from app.automation.ordering import PAYMENT_COD
from app.db.models import Account, AccountStatus, Address, Gst, Job, JobItem, JobStatus, JobType
from app.db.models import Session as SessionModel
from app.jobs import tasks
from app.jobs.pacing import Pacing, parse_duration, run_paced
from tests.test_x1_mock_mode import _Recorder, _post, api, no_browser, recorder  # noqa: F401 — fixtures


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _engine(ids, placed_per_batch, pacing, *, stop=lambda: None, batch_seconds=5.0):
    clock = FakeClock()
    calls: list[tuple[list[int], int, float]] = []

    async def run_batch(batch, offset):
        calls.append((list(batch), offset, clock.now))
        clock.now += batch_seconds
        return placed_per_batch(len(calls), batch)

    result = asyncio.run(run_paced(ids, run_batch, pacing, should_stop=stop, clock=clock, sleep=clock.sleep))
    return result, calls


PACING = Pacing(max_interval_s=60, min_batch=10, scale_up_pct=20, run_seconds=12 * 3600)


def test_a_selling_product_scales_up_at_once():
    # Every batch places half its accounts: over 20%, so each next batch is
    # twice as big and starts as soon as the last one ends.
    result, calls = _engine(list(range(70)), lambda n, b: len(b) // 2, PACING)
    assert result.batches == [10, 20, 40]
    assert [offset for _b, offset, _t in calls] == [0, 10, 30]
    assert [t for _b, _o, t in calls] == [0.0, 5.0, 10.0]
    assert result.stopped_because is None and result.started == 70


def test_a_slow_product_waits_the_interval_and_resets():
    # 1 of 10 placed is under 20% (2 needed): wait until 60 s after the batch began.
    result, calls = _engine(list(range(30)), lambda n, b: 1, PACING)
    assert result.batches == [10, 10, 10]
    assert [t for _b, _o, t in calls] == [0.0, 60.0, 120.0]


def test_scaling_up_then_slowing_down_goes_back_to_the_minimum():
    placed = {1: 5, 2: 0}
    result, _calls = _engine(list(range(40)), lambda n, b: placed.get(n, 0), PACING)
    assert result.batches == [10, 20, 10]


def test_the_threshold_rounds_up_and_needs_at_least_one():
    assert PACING.threshold(10) == 2
    assert PACING.threshold(1) == 1
    assert Pacing(scale_up_pct=90).threshold(3) == 3


def test_the_run_stops_when_its_duration_runs_out():
    short = Pacing(max_interval_s=60, min_batch=10, scale_up_pct=20, run_seconds=150)
    result, _calls = _engine(list(range(100)), lambda n, b: 0, short)
    assert result.stopped_because == "the run duration ran out"
    assert result.started == 30  # batches at 0, 60 and 120 s; at 180 s time is up


def test_a_stop_reason_ends_the_run_between_batches():
    state = {"n": 0}

    def stop():
        return "the target quantity was reached" if state["n"] >= 2 else None

    def placed(n, batch):
        state["n"] = n
        return 0

    result, _calls = _engine(list(range(100)), placed, PACING, stop=stop)
    assert result.stopped_because == "the target quantity was reached"
    assert result.started == 20


@pytest.mark.parametrize("text, seconds", [("12h", 43200), ("90m", 5400), ("2d", 172800), (" 1H ", 3600)])
def test_durations_are_read(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["30m", "3d", "12", "soon", ""])
def test_durations_outside_1h_to_2d_or_unreadable_are_refused(text):
    with pytest.raises(ValueError):
        parse_duration(text)


# --- a real (mock-outcome) run through the order job ------------------------------
@pytest.fixture
def address_book(db):
    gst = Gst(label="ACME", gstin="29ABCDE1234F1Z5", business_name="Acme", is_default=True)
    db.add(gst)
    db.commit()
    db.add(Address(label="w", name="Acme", line1="1 Road", pincode="560001", gst_id=gst.id, is_default=True))
    db.commit()


def test_an_ai_rehearsal_closes_every_account_and_logs_its_batches(db, address_book, no_browser, monkeypatch):
    ids = []
    for n in range(12):
        account = Account(email=f"ai{n}@t.in", status=AccountStatus.session_valid)
        db.add(account)
        db.commit()
        db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": str(n)}], valid=True))
        db.commit()
        ids.append(account.id)
    job = Job(type=JobType.order, status=JobStatus.queued)
    db.add(job)
    db.commit()
    pacing = Pacing(max_interval_s=0, min_batch=4, scale_up_pct=20, run_seconds=3600)

    asyncio.run(
        tasks.run_order_job(job.id, ids, product_link="https://www.flipkart.com/p/itm1", target=1000.0,
                            payment_mode=PAYMENT_COD, mock=True, pacing=pacing)
    )
    db.expire_all()
    items = db.query(JobItem).filter(JobItem.job_id == job.id).all()
    assert len(items) == 12 and all(item.message for item in items)
    log = db.get(Job, job.id).log or ""
    assert "AI pacing:" in log and "AI batches:" in log


# --- the API --------------------------------------------------------------------
def test_an_ai_request_reaches_the_run_with_its_pacing(api, db, recorder):
    response = _post(api, mode="ai", ai_max_interval_minutes="2", ai_min_attempts="5", ai_scale_up_pct="30", ai_run_duration="6h")
    assert response.status_code == 201, response.text
    call = recorder.calls[0]
    assert call["mock"] is False
    assert call["pacing"] == Pacing(max_interval_s=120, min_batch=5, scale_up_pct=30, run_seconds=21600)
    payload = response.json()["payload"]
    assert payload["mode"] == "ai" and payload["pacing"]["min_batch"] == 5


def test_an_ai_rehearsal_buys_nothing(api, db, recorder):
    response = _post(api, mode="ai", ai_rehearsal="true")
    assert response.status_code == 201, response.text
    assert recorder.calls[0]["mock"] is True
    assert response.json()["payload"]["rehearsal"] is True


@pytest.mark.parametrize(
    "field, value",
    [("ai_min_attempts", "31"), ("ai_scale_up_pct", "95"), ("ai_max_interval_minutes", "0"), ("ai_run_duration", "3d")],
)
def test_pacing_out_of_range_starts_nothing(api, db, recorder, field, value):
    assert _post(api, mode="ai", **{field: value}).status_code == 422
    assert recorder.calls == []


def test_a_real_or_mock_run_has_no_pacing(api, db, recorder):
    _post(api, mode="mock")
    assert recorder.calls[0]["pacing"] is None


# --- BOB AI Price Tracker: 1-probe watch, account recycling, fixed threshold ---
def test_price_tracker_probes_one_account_recycles_over_target_then_bursts_on_drop():
    from app.jobs.pacing import BatchOutcome

    clock = FakeClock()
    states: list[dict] = []
    # Only 2 accounts, but price stays above target (26905) for 3 intervals,
    # then drops to 26899 on probe 4 and stays low!
    calls: list[tuple[list[int], float]] = []

    async def run_batch(batch: list[int], _offset: int) -> BatchOutcome:
        calls.append((list(batch), clock.now))
        clock.now += 5.0
        n = len(calls)
        if n <= 3:
            # Price still high: over target -> recycle every account in batch
            seen = 27249.0 - (n - 1) * 50.0  # 27249, 27199, 27149
            return BatchOutcome(
                placed=0,
                retryable_ids=list(batch),
                seen_prices=[seen],
                in_stock=True,
                market_blocked=True,
                last_reason=f"₹{seen:,.2f} is over cap ₹26,905.00",
            )
        # Price dropped! Every account in batch places at ₹26,899!
        return BatchOutcome(
            placed=len(batch),
            retryable_ids=[],
            seen_prices=[26899.0] * len(batch),
            in_stock=True,
            market_blocked=False,
            last_reason="placed at ₹26,899.00",
        )

    pacing = Pacing(
        max_interval_s=60,
        min_batch=4,
        scale_up_pct=20,
        run_seconds=12 * 3600,
        single_probe=True,
        recycle_accounts=True,
    )
    result = asyncio.run(
        run_paced(
            [101, 102, 103, 104, 105],
            run_batch,
            pacing,
            should_stop=lambda: None,
            clock=clock,
            sleep=clock.sleep,
            target_price=26905.0,
            on_state=lambda snap: states.append(snap),
        )
    )
    # Probes 1..3 used 1 account each at t=0, 60, 120 (recycling 101,102,103)
    # Probe 4 (t=180) hit the drop and placed 1 -> IMMEDIATELY (t=185, 0s wait!) launched min_batch=4
    assert [len(b) for b, _t in calls] == [1, 1, 1, 1, 4]
    assert [t for _b, t in calls] == [0.0, 60.0, 120.0, 180.0, 185.0]
    assert result.total_placed == 5
    assert result.recycled_attempts == 3
    assert result.tracker is not None
    assert result.tracker["lowest_seen_price"] == 26899.0
    assert result.tracker["highest_seen_price"] == 27249.0
    assert any(s["status"] == "watching" for s in states)
    assert any(s["status"] == "scaling_up" for s in states)


def test_fixed_count_scale_up_mode_and_api_wire(api, db, recorder):
    p = Pacing(min_batch=10, scale_up_mode="count", scale_up_count=2)
    assert p.threshold(10) == 2
    assert p.threshold(1) == 1

    response = _post(
        api,
        mode="ai",
        ai_max_interval_minutes="1",
        ai_min_attempts="10",
        ai_scale_up_mode="count",
        ai_scale_up_count="3",
        ai_run_duration="12h",
        ai_single_probe="true",
        ai_recycle_accounts="true",
    )
    assert response.status_code == 201, response.text
    pacing = recorder.calls[0]["pacing"]
    assert pacing.scale_up_mode == "count"
    assert pacing.scale_up_count == 3
    assert pacing.single_probe is True
    assert pacing.recycle_accounts is True

    bad = _post(api, mode="ai", ai_scale_up_mode="count")
    assert bad.status_code == 422
    bad_mode = _post(api, mode="ai", ai_scale_up_mode="magic")
    assert bad_mode.status_code == 422

