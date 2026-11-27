"""Captchas: notice them, hand them to a person, stop a run that meets too many.

The Suite never answers a captcha itself. When a Flipkart or HDFC CorpAssist
flow meets one, :func:`guard` keeps that browser window where it is and asks a
person on the Captchas page (app.api.challenges), who either types the
characters of a picture captcha or works the page through a live view (clicks
and keys passed through to the real window). The other accounts of the run
carry on meanwhile.

* **Where the flows look.** Flipkart's sign-in (after the page opens, after
  the OTP is asked for, before the sign-in is checked), an order before
  anything is paid (product page, checkout, payment page), and the CorpAssist
  sign-in. A look costs a few element counts when there is no captcha.
* **Kinds.** ``text``: a picture of characters and a box to type them in.
  ``click``: a challenge solved by clicking (a tick box, a picture grid), shown
  live. ``blocked``: the site refused to go on ("unusual activity", "access
  denied"): nothing to answer, so the account is set aside at once.
* **No answer.** After ``captcha_wait_seconds`` (or Skip) the flow raises
  :class:`ChallengeFailed`; an order then fails before any payment step, so its
  card goes back to the pool.
* **Automatic stop.** A run that meets ``captcha_stop_count`` captchas within
  ``captcha_stop_minutes`` stops starting new accounts (the run's cancel flag)
  and raises an alert. Accounts already running finish.
* **Only the API process asks.** The browser and the person's answer must meet
  in one process; the API sets :func:`set_live` at start-up. Elsewhere (an arq
  worker, a script) a captcha fails the account with the reason.

What is kept: a ``captcha_challenges`` row per captcha (no picture). The
picture is held in memory only while the captcha is open. Each look at a page
that has one is logged with the site and step, never with what it shows.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import CaptchaChallenge, naive_utcnow

log = logging.getLogger("ordering.challenges")

FLIPKART, HDFC = "flipkart", "hdfc"
SITE_NAMES = {FLIPKART: "Flipkart", HDFC: "HDFC CorpAssist"}
TEXT, CLICK, BLOCKED = "text", "click", "blocked"
PENDING, ANSWERED, SKIPPED, EXPIRED = "pending", "answered", "skipped", "expired"
#: A captcha still showing after this many answers fails the account.
MAX_ROUNDS = 3
#: How long a page may take to settle after an answer before it is looked at.
SETTLE_SECONDS = 2.0

#: Challenges solved by clicking, in a frame of their own. The invisible
#: reCAPTCHA badge (``size=invisible``) is on many ordinary pages and is not a
#: challenge.
_CLICK_FRAMES = (
    "iframe[src*='recaptcha'][src*='bframe']",
    "iframe[src*='recaptcha'][src*='anchor']:not([src*='size=invisible'])",
    "iframe[src*='hcaptcha.com']",
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[title*='captcha' i]:not([src*='size=invisible'])",
)
#: A picture of characters to type.
_TEXT_IMAGES = (
    "img[src*='captcha' i]",
    "img[id*='captcha' i]",
    "img[alt*='captcha' i]",
    "img[class*='captcha' i]",
    "canvas[id*='captcha' i]",
    "canvas[class*='captcha' i]",
)
#: The box a picture captcha's characters go in.
_TEXT_INPUTS = (
    "input[name*='captcha' i]",
    "input[id*='captcha' i]",
    "input[placeholder*='captcha' i]",
    "input[aria-label*='captcha' i]",
    "input[placeholder*='characters' i]",
    "input[placeholder*='code shown' i]",
)
#: A page that refuses to go on, with nothing to answer.
_BLOCKED_TEXT = re.compile(
    r"unusual (?:activity|traffic)|are you (?:a )?human|verify (?:that )?you(?:'re| are) (?:a )?human"
    r"|suspicious activity|access denied|too many requests|temporarily blocked",
    re.IGNORECASE,
)
#: The smallest frame or picture that can be a captcha (CSS pixels).
_MIN_SIDE = 24


class ChallengeFailed(RuntimeError):
    """A captcha the flow could not get past; the message is the account's reason."""


@dataclass
class Found:
    kind: str
    detail: str
    #: The captcha picture (``text``), cropped for the page; None = the window.
    target: object | None = None


# --- which run and account a flow is working for ----------------------------

_WORK: ContextVar[tuple[int | None, int | None]] = ContextVar("captcha_work", default=(None, None))


def working_on(job_id: int | None, account_id: int | None) -> None:
    """Say which run and account the current task's browser works for.

    Called at the start of each account's worker in app.jobs.tasks: each
    account runs as its own asyncio task, so the value is that account's."""
    _WORK.set((job_id, account_id))


_LIVE = {"on": False}


def set_live(on: bool = True) -> None:
    """The API process can ask a person (app.main sets it at start-up)."""
    _LIVE["on"] = on


# --- noticing ------------------------------------------------------------------


async def _visible(locator, *, max_items: int = 4, min_side: int = _MIN_SIDE):
    """The first of ``locator``'s matches that is shown and big enough, or None."""
    try:
        count = min(await locator.count(), max_items)
    except Exception:  # noqa: BLE001 — a page mid-navigation has nothing to say
        return None
    for i in range(count):
        item = locator.nth(i)
        try:
            if not await item.is_visible():
                continue
            box = await item.bounding_box()
        except Exception:  # noqa: BLE001
            continue
        if box and box["width"] >= min_side and box["height"] >= min_side:
            return item
    return None


async def detect(page) -> Found | None:
    """The captcha or block page showing in ``page``, or None."""
    for css in _CLICK_FRAMES:
        if await _visible(page.locator(css)):
            return Found(CLICK, "a challenge to solve by clicking")
    for css in _TEXT_IMAGES:
        picture = await _visible(page.locator(css))
        if picture is not None:
            return Found(TEXT, "a picture of characters to type", picture)
    try:
        text = await page.locator("body").inner_text(timeout=2_000)
    except Exception:  # noqa: BLE001
        return None
    match = _BLOCKED_TEXT.search(text or "")
    if match:
        return Found(BLOCKED, f"the page says {match.group(0)!r}")
    return None


async def _answer_box(page):
    for css in _TEXT_INPUTS:
        box = await _visible(page.locator(css), min_side=8)
        if box is not None:
            return box
    return None


async def _picture(page, found: Found) -> bytes:
    """What the person is shown: the captcha picture itself when it can be cut
    out (no card form or address around it), else the window."""
    if found.target is not None:
        try:
            return await found.target.screenshot(type="png", timeout=5_000)
        except Exception:  # noqa: BLE001 — fall back to the window
            pass
    return await page.screenshot(type="jpeg", quality=70, timeout=10_000)


# --- the record ------------------------------------------------------------------


def _session():
    from app.db.base import SessionLocal

    return SessionLocal()


def _record(site: str, kind: str, step: str, status: str, detail: str | None) -> int:
    job_id, account_id = _WORK.get()
    with _session() as db:
        row = CaptchaChallenge(
            site=site,
            kind=kind,
            step=step[:64],
            account_id=account_id,
            job_id=job_id,
            status=status,
            detail=(detail or None) and detail[:300],
            resolved_at=None if status == PENDING else naive_utcnow(),
        )
        db.add(row)
        db.commit()
        log.info("captcha on %s at %s (%s): %s%s", SITE_NAMES[site], step, kind, status,
                 f", account {account_id}" if account_id else "")
        return row.id


def _resolve(challenge_id: int, status: str, *, who: str | None = None, detail: str | None = None) -> None:
    with _session() as db:
        row = db.get(CaptchaChallenge, challenge_id)
        if row is None:
            return
        row.status = status
        row.answered_by = who or row.answered_by
        if detail:
            row.detail = detail[:300]
        row.resolved_at = naive_utcnow()
        db.commit()


def stop_if_too_many(job_id: int | None, *, now: datetime | None = None) -> bool:
    """Stop the run from starting new accounts when its captchas in the last
    ``captcha_stop_minutes`` reach ``captcha_stop_count``; True when it did now."""
    settings = get_settings()
    count, minutes = int(settings.captcha_stop_count or 0), int(settings.captcha_stop_minutes or 0)
    if job_id is None or count <= 0 or minutes <= 0:
        return False
    from app.core.alerts import raise_alert
    from app.db.models import Job

    now = now or naive_utcnow()
    with _session() as db:
        seen = db.scalar(
            select(func.count(CaptchaChallenge.id))
            .where(CaptchaChallenge.job_id == job_id)
            .where(CaptchaChallenge.created_at >= now - timedelta(minutes=minutes))
        ) or 0
        if seen < count:
            return False
        job = db.get(Job, job_id)
        if job is None or job.cancel_requested:
            return False
        job.cancel_requested = True
        db.commit()
        raise_alert(
            db,
            key=f"captcha_stop:job:{job_id}",
            kind="captcha",
            message=(
                f"Run #{job_id} met {seen} captchas in {minutes} minutes and stopped starting new accounts. "
                "Accounts already running finish. Start the rest later, or at a lower speed."
            ),
            details={"job_id": job_id, "captchas": seen, "minutes": minutes},
        )
    log.warning("run %s stopped starting accounts: %s captchas in %s minutes", job_id, seen, minutes)
    return True


# --- asking a person -------------------------------------------------------------


@dataclass
class _Open:
    id: int
    site: str
    kind: str
    step: str
    job_id: int | None
    account_id: int | None
    page: object
    submit: bool
    image: bytes
    created_at: datetime
    deadline: float
    future: asyncio.Future = field(repr=False)


_OPEN: dict[int, _Open] = {}


async def guard(page, *, site: str, step: str, submit: bool = True) -> None:
    """Look for a captcha in ``page``; when there is one, get a person past it
    or raise :class:`ChallengeFailed`.

    ``submit``: an answer typed into a picture captcha's box is sent with
    Enter. False when the captcha sits in a form the flow submits itself (the
    CorpAssist sign-in): the answer is only typed, and the flow goes on."""
    found = await detect(page)
    if found is not None:
        await _handle(page, found, site=site, step=step, submit=submit)


async def _handle(page, found: Found, *, site: str, step: str, submit: bool) -> None:
    settings = get_settings()
    job_id, account_id = _WORK.get()
    name = SITE_NAMES[site]
    if found.kind == BLOCKED:
        _record(site, BLOCKED, step, BLOCKED, found.detail)
        stop_if_too_many(job_id)
        raise ChallengeFailed(f"{name} refused to go on at {step}: {found.detail}; this account was set aside")
    if not settings.captcha_ask or not _LIVE["on"]:
        why = (
            "asking a person is off (ORDERING_CAPTCHA_ASK=false)"
            if not settings.captcha_ask
            else "only the API process can ask a person"
        )
        _record(site, found.kind, step, SKIPPED, why)
        stop_if_too_many(job_id)
        raise ChallengeFailed(f"{name} showed a captcha at {step} and {why}")

    challenge_id = _record(site, found.kind, step, PENDING, found.detail)
    stop_if_too_many(job_id)
    wait = max(1, int(settings.captcha_wait_seconds or 120))
    loop = asyncio.get_running_loop()
    for round_no in range(1, MAX_ROUNDS + 1):
        entry = _Open(
            id=challenge_id,
            site=site,
            kind=found.kind,
            step=step,
            job_id=job_id,
            account_id=account_id,
            page=page,
            submit=submit,
            image=await _picture(page, found),
            created_at=naive_utcnow(),
            deadline=time.monotonic() + wait,
            future=loop.create_future(),
        )
        _OPEN[challenge_id] = entry
        try:
            action, who = await asyncio.wait_for(asyncio.shield(entry.future), timeout=wait)
        except asyncio.TimeoutError:
            _resolve(challenge_id, EXPIRED, detail=f"nobody answered within {wait} s")
            raise ChallengeFailed(f"{name} captcha at {step} was not answered within {wait} s") from None
        finally:
            _OPEN.pop(challenge_id, None)
        if action == "skip":
            _resolve(challenge_id, SKIPPED, who=who)
            raise ChallengeFailed(f"{name} captcha at {step} was skipped by {who}")
        if not submit and action == "answer":
            _resolve(challenge_id, ANSWERED, who=who)  # the flow submits the form itself
            return
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(SETTLE_SECONDS)
        again = await detect(page)
        if again is None:
            _resolve(challenge_id, ANSWERED, who=who)
            return
        if again.kind == BLOCKED:
            _resolve(challenge_id, BLOCKED, who=who, detail=again.detail)
            raise ChallengeFailed(f"{name} refused to go on after the captcha at {step}: {again.detail}")
        found = again
        log.info("captcha %s still showing after round %d", challenge_id, round_no)
    _resolve(challenge_id, EXPIRED, detail=f"still showing after {MAX_ROUNDS} answers")
    raise ChallengeFailed(f"{name} captcha at {step} was still showing after {MAX_ROUNDS} answers")


# --- the person's side (app.api.challenges) --------------------------------------


class NotOpen(LookupError):
    """No such captcha is waiting (answered, skipped or expired already)."""


def _entry(challenge_id: int) -> _Open:
    entry = _OPEN.get(challenge_id)
    if entry is None or entry.future.done():
        raise NotOpen(challenge_id)
    return entry


def open_challenges() -> list[dict]:
    now = time.monotonic()
    return [
        {
            "id": e.id,
            "site": e.site,
            "kind": e.kind,
            "step": e.step,
            "job_id": e.job_id,
            "account_id": e.account_id,
            "created_at": e.created_at,
            "seconds_left": max(0, int(e.deadline - now)),
        }
        for e in sorted(_OPEN.values(), key=lambda e: e.id)
        if not e.future.done()
    ]


async def picture(challenge_id: int, *, live: bool = False) -> tuple[bytes, str]:
    """The captcha's picture, or with ``live`` (and always for a click
    challenge) the window as it is now. Returns (bytes, media type)."""
    entry = _entry(challenge_id)
    if entry.kind == TEXT and not live:
        return entry.image, "image/png" if entry.image[:4] == b"\x89PNG" else "image/jpeg"
    shot = await entry.page.screenshot(type="jpeg", quality=70, timeout=10_000)
    return shot, "image/jpeg"


async def answer_text(challenge_id: int, text: str, *, who: str) -> None:
    """Type a picture captcha's characters into its box (and send them)."""
    entry = _entry(challenge_id)
    box = await _answer_box(entry.page)
    if box is None:
        raise ValueError("no box to type the answer in was found: use the live view instead")
    await box.fill(text)
    if entry.submit:
        await _send(entry.page, box)
    if not entry.future.done():
        entry.future.set_result(("answer", who))


_SEND_BUTTONS = (
    "button[type='submit'], input[type='submit'], button:has-text('Submit'), button:has-text('Verify'), "
    "button:has-text('Continue'), button:has-text('Proceed')"
)


async def _send(page, box) -> None:
    """Send a typed answer: Enter, and when the box is still there (a form with
    no submit button ignores Enter), its form's own button, else the form."""
    await box.press("Enter")
    await asyncio.sleep(1.0)
    try:
        if not await box.is_visible():
            return
        form = box.locator("xpath=ancestor::form[1]")
        if await form.count():
            button = form.locator(_SEND_BUTTONS)
            if await _visible(button, min_side=8):
                await button.first.click(timeout=5_000)
                return
            await box.evaluate("el => el.form.requestSubmit()")
    except Exception:  # noqa: BLE001 — the page moved on: the answer was sent
        return


async def click(challenge_id: int, x: float, y: float) -> None:
    """Click the window at (x, y), each a fraction (0-1) of its width and height."""
    entry = _entry(challenge_id)
    size = entry.page.viewport_size or {"width": 1280, "height": 720}
    await entry.page.mouse.click(
        min(max(x, 0.0), 1.0) * size["width"], min(max(y, 0.0), 1.0) * size["height"]
    )


async def type_keys(challenge_id: int, text: str = "", *, key: str | None = None) -> None:
    """Type into whatever the live view last clicked; ``key`` presses one key
    (Enter, Backspace, Tab)."""
    entry = _entry(challenge_id)
    if text:
        await entry.page.keyboard.type(text, delay=40)
    if key:
        await entry.page.keyboard.press(key)


def mark_done(challenge_id: int, *, who: str) -> None:
    """The person solved it in the live view: look at the page again."""
    entry = _entry(challenge_id)
    entry.future.set_result(("done", who))


def skip(challenge_id: int, *, who: str) -> None:
    entry = _entry(challenge_id)
    entry.future.set_result(("skip", who))
