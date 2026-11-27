"""The Captchas page: captchas waiting for a person, and what happened to them.

The browser flows meet a captcha (app.automation.challenges), keep the window
where it is, and list it here. A signed-in user then:

* ``GET  /captchas`` — the open captchas, the last hour's counts, the settings.
* ``GET  /captchas/{id}/image`` — the captcha picture, or with ``?live=true``
  (always for a click challenge) the window as it is now. Never cached.
* ``POST /captchas/{id}/answer`` — the characters of a picture captcha.
* ``POST /captchas/{id}/click`` — a click on the live view, as fractions of its
  width and height; ``/keys`` types into it.
* ``POST /captchas/{id}/done`` — solved in the live view: look again.
* ``POST /captchas/{id}/skip`` — give up on this account; the run goes on.
* ``GET  /captchas/history`` — the latest captchas and their outcomes.

An answer for a captcha that is no longer waiting gets 404.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.automation import challenges
from app.config import get_settings
from app.core.security import require_user
from app.db.base import get_db
from app.db.models import Account, CaptchaChallenge, naive_utcnow

router = APIRouter(prefix="/captchas", tags=["captchas"], dependencies=[Depends(require_user)])


class OpenOut(BaseModel):
    id: int
    site: str
    kind: str
    step: str
    job_id: int | None
    account_id: int | None
    account_email: str | None
    created_at: datetime
    seconds_left: int


class CaptchasOut(BaseModel):
    open: list[OpenOut]
    #: The last hour's captchas by outcome: pending, answered, skipped, expired, blocked.
    last_hour: dict[str, int]
    ask: bool
    wait_seconds: int
    stop_count: int
    stop_minutes: int


class HistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site: str
    kind: str
    step: str
    job_id: int | None
    account_id: int | None
    status: str
    detail: str | None
    answered_by: str | None
    created_at: datetime
    resolved_at: datetime | None


class AnswerIn(BaseModel):
    text: str = Field(min_length=1, max_length=64)

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("type the characters shown")
        return value


class ClickIn(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class KeysIn(BaseModel):
    text: str = Field(default="", max_length=64)
    key: Literal["Enter", "Backspace", "Tab", "Escape"] | None = None


def _emails(db: DbSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    return dict(db.execute(select(Account.id, Account.email).where(Account.id.in_(ids))).all())


def _not_open() -> HTTPException:
    return HTTPException(status_code=404, detail="This captcha is no longer waiting (answered, skipped or timed out).")


@router.get("", response_model=CaptchasOut)
def list_captchas(db: DbSession = Depends(get_db)) -> CaptchasOut:
    settings = get_settings()
    waiting = challenges.open_challenges()
    emails = _emails(db, {c["account_id"] for c in waiting if c["account_id"]})
    since = naive_utcnow() - timedelta(hours=1)
    counts = dict(
        db.execute(
            select(CaptchaChallenge.status, func.count(CaptchaChallenge.id))
            .where(CaptchaChallenge.created_at >= since)
            .group_by(CaptchaChallenge.status)
        ).all()
    )
    return CaptchasOut(
        open=[OpenOut(**c, account_email=emails.get(c["account_id"])) for c in waiting],
        last_hour={s: int(counts.get(s, 0)) for s in ("pending", "answered", "skipped", "expired", "blocked")},
        ask=bool(settings.captcha_ask),
        wait_seconds=int(settings.captcha_wait_seconds),
        stop_count=int(settings.captcha_stop_count),
        stop_minutes=int(settings.captcha_stop_minutes),
    )


@router.get("/history", response_model=list[HistoryOut])
def captcha_history(limit: int = Query(50, ge=1, le=500), db: DbSession = Depends(get_db)) -> list[HistoryOut]:
    rows = db.scalars(select(CaptchaChallenge).order_by(CaptchaChallenge.id.desc()).limit(limit)).all()
    return [HistoryOut.model_validate(r) for r in rows]


@router.get("/{challenge_id}/image")
async def captcha_image(challenge_id: int, live: bool = False) -> Response:
    try:
        data, media_type = await challenges.picture(challenge_id, live=live)
    except challenges.NotOpen:
        raise _not_open() from None
    return Response(content=data, media_type=media_type, headers={"Cache-Control": "no-store"})


@router.post("/{challenge_id}/answer")
async def answer_captcha(challenge_id: int, payload: AnswerIn, who: str = Depends(require_user)) -> dict:
    try:
        await challenges.answer_text(challenge_id, payload.text, who=who)
    except challenges.NotOpen:
        raise _not_open() from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"ok": True}


@router.post("/{challenge_id}/click")
async def click_captcha(challenge_id: int, payload: ClickIn) -> dict:
    try:
        await challenges.click(challenge_id, payload.x, payload.y)
    except challenges.NotOpen:
        raise _not_open() from None
    return {"ok": True}


@router.post("/{challenge_id}/keys")
async def type_into_captcha(challenge_id: int, payload: KeysIn) -> dict:
    if not payload.text and not payload.key:
        raise HTTPException(status_code=422, detail="Send some text or a key.")
    try:
        await challenges.type_keys(challenge_id, payload.text, key=payload.key)
    except challenges.NotOpen:
        raise _not_open() from None
    return {"ok": True}


@router.post("/{challenge_id}/done")
def captcha_done(challenge_id: int, who: str = Depends(require_user)) -> dict:
    try:
        challenges.mark_done(challenge_id, who=who)
    except challenges.NotOpen:
        raise _not_open() from None
    return {"ok": True}


@router.post("/{challenge_id}/skip")
def skip_captcha(challenge_id: int, who: str = Depends(require_user)) -> dict:
    try:
        challenges.skip(challenge_id, who=who)
    except challenges.NotOpen:
        raise _not_open() from None
    return {"ok": True}
