"""Captchas answered by a person (app.automation.challenges, app.api.challenges).

The browser parts run a real Chromium (Playwright's own, else the installed
Chrome) on small local pages standing in for Flipkart's and HDFC's: a picture
captcha with its box, a click challenge in a frame, a block page, and the
invisible reCAPTCHA badge that is not a challenge. Skipped when no browser
can start.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.automation import challenges
from app.db.models import Alert, CaptchaChallenge, Job, JobStatus, JobType, naive_utcnow

# A 120 x 40 picture standing in for the captcha image.
PICTURE = (
    "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='120' height='40'>"
    "<rect width='120' height='40' fill='%23ddd'/><text x='10' y='28' font-size='22'>x7Kp2</text></svg>"
)
TEXT_PAGE = f"""
<form id="f" onsubmit="event.preventDefault(); document.body.innerHTML = '<p>Welcome back</p>';">
  <input name="username" value="corpuser">
  <img id="captchaImage" src="{PICTURE}" width="120" height="40">
  <input name="captchaText" placeholder="Enter captcha">
</form>"""
CLICK_PAGE = """
<p>Checkout</p>
<iframe id="ch" src="data:text/html,recaptcha anchor" style="width:300px;height:80px;border:1px solid"></iframe>
<script>
  window.clicks = [];
  document.addEventListener('click', e => {
    window.clicks.push([e.clientX, e.clientY]);
    const ch = document.getElementById('ch'); if (ch) ch.remove();
  });
</script>"""
BLOCK_PAGE = "<h1>Sorry</h1><p>We noticed unusual activity from your device.</p>"
PLAIN_PAGE = """<h1>Product</h1><p>Buy now</p>
<iframe src="data:text/html,recaptcha anchor size=invisible" style="width:256px;height:60px"></iframe>"""


def _settings(**overrides):
    values = dict(captcha_ask=True, captcha_wait_seconds=20, captcha_stop_count=5, captcha_stop_minutes=5)
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def settings(monkeypatch):
    current = _settings()
    monkeypatch.setattr(challenges, "get_settings", lambda: current)
    challenges.set_live(True)
    yield current
    challenges.set_live(False)
    challenges._OPEN.clear()


def _with_page(html: str, body):
    """Run ``body(page)`` on a page showing ``html``, in a fresh event loop."""
    playwright = pytest.importorskip("playwright.async_api")

    async def go():
        async with playwright.async_playwright() as pw:
            browser = None
            for kwargs in ({}, {"channel": "chrome"}):
                try:
                    browser = await pw.chromium.launch(**kwargs)
                    break
                except Exception:  # noqa: BLE001 — try the next, else skip
                    continue
            if browser is None:
                pytest.skip("no Chromium or Chrome can start here")
            page = await browser.new_page(viewport={"width": 800, "height": 600})
            await page.set_content(html)
            try:
                return await body(page)
            finally:
                await browser.close()

    return asyncio.run(go())


async def _first_open(timeout: float = 10.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        waiting = challenges.open_challenges()
        if waiting:
            return waiting[0]
        await asyncio.sleep(0.05)
    raise AssertionError("no captcha was opened")


def _rows(db) -> list[CaptchaChallenge]:
    db.expire_all()
    return db.query(CaptchaChallenge).order_by(CaptchaChallenge.id).all()


# --- noticing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "kind"),
    [(TEXT_PAGE, "text"), (CLICK_PAGE, "click"), (BLOCK_PAGE, "blocked"), (PLAIN_PAGE, None)],
    ids=["picture", "click-frame", "block-page", "no-captcha-invisible-badge"],
)
def test_each_kind_of_page_is_told_apart(html, kind):
    async def body(page):
        found = await challenges.detect(page)
        return found.kind if found else None

    assert _with_page(html, body) == kind


# --- a person answers ----------------------------------------------------------------


def test_a_picture_captcha_is_answered_by_a_person_and_the_flow_goes_on(db, settings):
    challenges.working_on(None, None)

    async def body(page):
        flow = asyncio.create_task(challenges.guard(page, site=challenges.FLIPKART, step="request OTP"))
        waiting = await _first_open()
        assert waiting["kind"] == "text" and waiting["site"] == "flipkart" and waiting["seconds_left"] > 0
        picture, media = await challenges.picture(waiting["id"])
        assert media == "image/png" and picture[:4] == b"\x89PNG"
        await challenges.answer_text(waiting["id"], "x7Kp2", who="ops@example.com")
        await flow
        return await page.inner_text("body")

    assert "Welcome back" in _with_page(TEXT_PAGE, body)
    (row,) = _rows(db)
    assert (row.site, row.kind, row.step, row.status) == ("flipkart", "text", "request OTP", "answered")
    assert row.answered_by == "ops@example.com" and row.resolved_at is not None
    assert challenges.open_challenges() == []


def test_a_click_challenge_is_worked_through_the_live_view(db, settings):
    async def body(page):
        flow = asyncio.create_task(challenges.guard(page, site=challenges.FLIPKART, step="checkout"))
        waiting = await _first_open()
        assert waiting["kind"] == "click"
        shot, media = await challenges.picture(waiting["id"])
        assert media == "image/jpeg" and len(shot) > 1000
        await challenges.click(waiting["id"], 0.25, 0.5)  # 200, 300 on an 800 x 600 window
        await challenges.type_keys(waiting["id"], "ok", key="Enter")
        challenges.mark_done(waiting["id"], who="ops@example.com")
        await flow
        return await page.evaluate("window.clicks")

    assert _with_page(CLICK_PAGE, body) == [[200, 300]]
    assert _rows(db)[0].status == "answered"


def test_a_captcha_in_a_form_the_flow_sends_itself_is_only_typed(db, settings):
    """The CorpAssist sign-in: the answer goes in the box, the flow presses Sign in."""

    async def body(page):
        flow = asyncio.create_task(challenges.guard(page, site=challenges.HDFC, step="sign-in", submit=False))
        waiting = await _first_open()
        await challenges.answer_text(waiting["id"], "x7Kp2", who="ops@example.com")
        await flow
        return await page.input_value("input[name=captchaText]"), await page.inner_text("body")

    typed, text = _with_page(TEXT_PAGE, body)
    assert typed == "x7Kp2" and "Welcome back" not in text
    assert _rows(db)[0].site == "hdfc" and _rows(db)[0].status == "answered"


# --- no answer --------------------------------------------------------------------------


def test_an_unanswered_captcha_fails_the_account_after_the_wait(db, settings):
    settings.captcha_wait_seconds = 1

    async def body(page):
        with pytest.raises(challenges.ChallengeFailed, match="not answered within 1 s"):
            await challenges.guard(page, site=challenges.FLIPKART, step="checkout")

    _with_page(TEXT_PAGE, body)
    assert _rows(db)[0].status == "expired"
    assert challenges.open_challenges() == []


def test_a_skipped_captcha_fails_the_account_and_says_who(db, settings):
    async def body(page):
        flow = asyncio.create_task(challenges.guard(page, site=challenges.FLIPKART, step="checkout"))
        waiting = await _first_open()
        challenges.skip(waiting["id"], who="ops@example.com")
        with pytest.raises(challenges.ChallengeFailed, match="skipped by ops@example.com"):
            await flow
        with pytest.raises(challenges.NotOpen):
            challenges.skip(waiting["id"], who="ops@example.com")

    _with_page(TEXT_PAGE, body)
    assert (_rows(db)[0].status, _rows(db)[0].answered_by) == ("skipped", "ops@example.com")


def test_a_block_page_sets_the_account_aside_without_asking(db, settings):
    async def body(page):
        with pytest.raises(challenges.ChallengeFailed, match="refused to go on"):
            await challenges.guard(page, site=challenges.FLIPKART, step="product page")
        return challenges.open_challenges()

    assert _with_page(BLOCK_PAGE, body) == []
    assert _rows(db)[0].status == "blocked"


@pytest.mark.parametrize("ask, live, why", [(False, True, "asking a person is off"), (True, False, "only the API process")])
def test_without_a_person_to_ask_the_account_fails_with_the_reason(db, settings, ask, live, why):
    settings.captcha_ask = ask
    challenges.set_live(live)

    async def body(page):
        with pytest.raises(challenges.ChallengeFailed, match=why):
            await challenges.guard(page, site=challenges.FLIPKART, step="checkout")

    _with_page(TEXT_PAGE, body)
    assert _rows(db)[0].status == "skipped"


def test_a_page_without_a_captcha_costs_nothing(db, settings):
    async def body(page):
        await challenges.guard(page, site=challenges.FLIPKART, step="product page")

    _with_page(PLAIN_PAGE, body)
    assert _rows(db) == []


# --- the automatic stop ---------------------------------------------------------------


def test_a_run_meeting_too_many_captchas_stops_starting_accounts(db, settings):
    job = Job(type=JobType.order, status=JobStatus.running)
    db.add(job)
    db.commit()
    now = naive_utcnow()
    for minutes_ago in (1, 2, 3, 4):
        db.add(CaptchaChallenge(site="flipkart", kind="text", step="checkout", job_id=job.id, status="answered",
                                created_at=now - timedelta(minutes=minutes_ago)))
    db.add(CaptchaChallenge(site="flipkart", kind="text", step="checkout", job_id=job.id, status="answered",
                            created_at=now - timedelta(minutes=30)))  # outside the window
    db.commit()
    assert challenges.stop_if_too_many(job.id, now=now) is False  # 4 in 5 minutes

    db.add(CaptchaChallenge(site="flipkart", kind="click", step="sign-in page", job_id=job.id, status="pending",
                            created_at=now))
    db.commit()
    assert challenges.stop_if_too_many(job.id, now=now) is True
    db.expire_all()
    assert db.get(Job, job.id).cancel_requested is True
    alert = db.query(Alert).filter(Alert.key == f"captcha_stop:job:{job.id}").one()
    assert alert.kind == "captcha" and "5 captchas in 5 minutes" in alert.message
    assert challenges.stop_if_too_many(job.id, now=now) is False  # stopped once, said once

    settings.captcha_stop_count = 0
    other = Job(type=JobType.order, status=JobStatus.running)
    db.add(other)
    db.commit()
    assert challenges.stop_if_too_many(other.id, now=now) is False


# --- the API --------------------------------------------------------------------------


def test_the_captchas_page_lists_counts_and_history(client, auth_headers, db, settings):
    now = naive_utcnow()
    for status in ("answered", "answered", "expired", "blocked"):
        db.add(CaptchaChallenge(site="flipkart", kind="text", step="checkout", status=status, created_at=now))
    db.add(CaptchaChallenge(site="hdfc", kind="text", step="sign-in", status="skipped",
                            created_at=now - timedelta(hours=2)))
    db.commit()
    page = client.get("/captchas", headers=auth_headers).json()
    assert page["open"] == []
    assert page["last_hour"] == {"pending": 0, "answered": 2, "skipped": 0, "expired": 1, "blocked": 1}
    assert (page["ask"], page["wait_seconds"]) == (True, 120) or page["wait_seconds"] > 0
    history = client.get("/captchas/history?limit=3", headers=auth_headers).json()
    assert [h["site"] for h in history] == ["hdfc", "flipkart", "flipkart"]


def test_answering_a_captcha_that_is_not_waiting_gets_404(client, auth_headers):
    for path, body in (("answer", {"text": "abc"}), ("click", {"x": 0.5, "y": 0.5}), ("keys", {"text": "a"})):
        assert client.post(f"/captchas/999/{path}", json=body, headers=auth_headers).status_code == 404
    assert client.post("/captchas/999/done", headers=auth_headers).status_code == 404
    assert client.post("/captchas/999/skip", headers=auth_headers).status_code == 404
    assert client.get("/captchas/999/image", headers=auth_headers).status_code == 404
    assert client.post("/captchas/999/click", json={"x": 2, "y": 0}, headers=auth_headers).status_code == 422
    assert client.post("/captchas/999/answer", json={"text": "  "}, headers=auth_headers).status_code == 422
    assert client.post("/captchas/999/keys", json={}, headers=auth_headers).status_code == 422


def test_the_captchas_page_needs_a_signed_in_user(client):
    assert client.get("/captchas").status_code == 401
