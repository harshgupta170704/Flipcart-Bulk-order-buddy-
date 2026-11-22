"""Tests for the bounded-concurrency runner.

No pytest-asyncio in this project, so every async case is driven with
``asyncio.run`` like the other flow tests.
"""

import asyncio

from app.jobs.runner import ItemResult, run_bounded


# --- concurrency cap ---------------------------------------------------------
def test_concurrency_is_capped_at_limit():
    state = {"live": 0, "peak": 0}

    async def worker(item, index):
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        await asyncio.sleep(0.01)
        state["live"] -= 1
        return item * 2

    results = asyncio.run(run_bounded(range(12), worker, limit=3))

    assert state["peak"] <= 3  # the hard cap
    assert state["peak"] == 3  # ...and we really do use it
    assert [r.value for r in results] == [i * 2 for i in range(12)]
    assert all(r.ok for r in results)


def test_limit_below_one_is_coerced_to_serial():
    state = {"live": 0, "peak": 0}

    async def worker(item, index):
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        await asyncio.sleep(0.005)
        state["live"] -= 1
        return item

    results = asyncio.run(run_bounded([1, 2, 3], worker, limit=0))

    assert state["peak"] == 1
    assert [r.value for r in results] == [1, 2, 3]


# --- ordering ----------------------------------------------------------------
def test_results_are_in_input_order_not_completion_order():
    items = ["a", "b", "c", "d"]
    completion: list[str] = []

    async def worker(item, index):
        # Item 0 sleeps longest, so completion order is the reverse of input.
        await asyncio.sleep(0.02 * (len(items) - index))
        completion.append(item)
        return item.upper()

    results = asyncio.run(run_bounded(items, worker, limit=len(items)))

    assert completion == ["d", "c", "b", "a"]  # finished out of order...
    assert [r.item for r in results] == items  # ...returned in order
    assert [r.index for r in results] == [0, 1, 2, 3]
    assert [r.value for r in results] == ["A", "B", "C", "D"]


# --- failures are isolated ---------------------------------------------------
def test_raising_worker_is_captured_and_siblings_continue():
    seen: list[int] = []

    async def worker(item, index):
        seen.append(index)
        if index == 1:
            raise RuntimeError("boom")
        return item

    results = asyncio.run(run_bounded([10, 11, 12], worker, limit=1))

    assert sorted(seen) == [0, 1, 2]  # every sibling still ran
    assert results[1].ok is False
    assert results[1].error == "boom"
    assert results[1].value is None
    assert results[1].skipped is False
    assert [results[0].ok, results[2].ok] == [True, True]
    assert [results[0].value, results[2].value] == [10, 12]


def test_raising_worker_without_message_still_reports_an_error():
    async def worker(item, index):
        raise ValueError()  # str(exc) == ""

    results = asyncio.run(run_bounded([1], worker, limit=1))

    assert results[0].ok is False
    assert results[0].error == "ValueError"


# --- cancellation ------------------------------------------------------------
def test_cancel_skips_not_yet_started_items():
    flag = {"cancel": False}
    started: list[int] = []

    async def worker(item, index):
        started.append(index)
        await asyncio.sleep(0.01)
        flag["cancel"] = True
        return index

    results = asyncio.run(
        run_bounded(
            list(range(5)), worker, limit=1, should_cancel=lambda: flag["cancel"]
        )
    )

    assert started == [0]  # nothing after the cancel was ever started
    assert results[0].ok is True
    for result in results[1:]:
        assert result.skipped is True
        assert result.ok is False
        assert result.error == "cancelled"


def test_cancel_lets_in_flight_items_finish():
    flag = {"cancel": False}
    started: list[int] = []
    finished: list[int] = []

    async def worker(item, index):
        started.append(index)
        await asyncio.sleep(0.01)  # both slots are in flight by now
        if index == 0:
            flag["cancel"] = True
        await asyncio.sleep(0.02)
        finished.append(index)
        return index

    results = asyncio.run(
        run_bounded(
            list(range(5)), worker, limit=2, should_cancel=lambda: flag["cancel"]
        )
    )

    assert started == [0, 1]
    assert sorted(finished) == [0, 1]  # the in-flight sibling was not aborted
    assert results[0].ok is True and results[1].ok is True
    assert all(r.skipped for r in results[2:])


def test_should_cancel_raising_does_not_kill_the_batch():
    def exploding_cancel():
        raise RuntimeError("cancel flag unreadable")

    async def worker(item, index):
        return item

    results = asyncio.run(
        run_bounded([1, 2], worker, limit=2, should_cancel=exploding_cancel)
    )

    assert [r.ok for r in results] == [True, True]


# --- on_result ---------------------------------------------------------------
def test_on_result_accepts_a_sync_callback():
    seen: list[ItemResult] = []

    async def worker(item, index):
        return item

    results = asyncio.run(
        run_bounded([1, 2, 3], worker, limit=2, on_result=seen.append)
    )

    assert len(seen) == len(results)
    assert sorted(r.index for r in seen) == [0, 1, 2]


def test_on_result_accepts_an_async_callback():
    seen: list[int] = []

    async def worker(item, index):
        return item

    async def on_result(result):
        await asyncio.sleep(0)
        seen.append(result.index)

    asyncio.run(run_bounded([1, 2, 3], worker, limit=1, on_result=on_result))

    assert seen == [0, 1, 2]


def test_on_result_failure_does_not_abort_the_batch():
    def bad_callback(result):
        raise RuntimeError("callback exploded")

    async def worker(item, index):
        return item

    results = asyncio.run(
        run_bounded([1, 2], worker, limit=2, on_result=bad_callback)
    )

    assert [r.ok for r in results] == [True, True]


def test_empty_input_is_a_no_op():
    async def worker(item, index):  # pragma: no cover - must never be called
        raise AssertionError("worker should not run")

    assert asyncio.run(run_bounded([], worker, limit=4)) == []
