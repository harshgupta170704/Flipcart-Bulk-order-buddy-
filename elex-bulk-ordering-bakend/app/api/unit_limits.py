"""Unit limits API — the caps a run has to respect (ROADMAP §2 Phase 9).

The operator wants N units of a product spread across accounts, but the
platform caps what one account may buy of a category/brand/model in a rolling
window. This router is where those caps are written down, and
``POST /unit-limits/check`` is the pre-flight that answers the only question
worth asking before a run starts: *how many units can this selection still
take?*

Two deliberate rules, both mirrored by :mod:`app.core.unit_limits`:

* an empty ``value`` is a wildcard covering every value in its scope, so a
  blanket "30 of anything a month" needs no list of categories;
* a duplicate ``(scope, value)`` is a 409 **case-insensitively**, because
  matching is case-insensitive — a "vivo" rule and a "VIVO" rule are one rule,
  and keeping both would show the operator two half-counts of the same orders.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import ColumnElement, String, cast, delete, func, select
from sqlalchemy.orm import Session as DbSession

from app.core import account_filter, unit_limits
from app.core.security import require_admin, require_user
from app.db.base import get_db
from app.db.models import Account, LimitScope, UnitLimit
from app.schemas.unit_limit import (
    LimitCheckRequest,
    LimitUsageOut,
    UnitLimitOut,
    UnitLimitUpdate,
    UnitLimitWrite,
)

log = logging.getLogger("ordering.limits")

router = APIRouter(
    prefix="/unit-limits",
    tags=["unit-limits"],
    dependencies=[Depends(require_user)],
)

#: Fields a PATCH may set, in the order they are applied.
_PATCH_FIELDS = ("scope", "value", "max_units", "window_days", "enabled")


def _out(limit: UnitLimit) -> UnitLimitOut:
    return UnitLimitOut(
        id=limit.id,
        scope=limit.scope,
        value=limit.value or "",
        max_units=limit.max_units,
        window_days=limit.window_days,
        enabled=bool(limit.enabled),
        created_at=limit.created_at,
    )


def _describe(scope: LimitScope, value: str) -> str:
    """How a rule is named back to the operator in an error."""
    name = getattr(scope, "value", str(scope))
    return f"{name} '{value}'" if value else f"every {name}"


def _reject_duplicate(
    db: DbSession, scope: LimitScope, value: str, *, exclude_id: int | None = None
) -> None:
    """409 when the same rule already exists.

    Compared case-insensitively because that is how the engine matches: two
    rows that differ only in case would both apply, each counting the same
    orders, and the operator would see one cap twice.
    """
    stmt = select(UnitLimit.id).where(
        UnitLimit.scope == scope,
        func.lower(func.coalesce(UnitLimit.value, "")) == value.lower(),
    )
    if exclude_id is not None:
        stmt = stmt.where(UnitLimit.id != exclude_id)
    if db.scalar(stmt) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A limit for {_describe(scope, value)} already exists",
        )


def _account_where(payload: LimitCheckRequest) -> ColumnElement[bool] | None:
    """"Which accounts?", resolved the way the rest of the app resolves it.

    A filter wins, then an explicit id list, then (neither, or an empty list)
    every account — the same precedence as ``app.api.jobs._resolve_selection``,
    so a check and the run it precedes cover the same rows. The selection stays
    a *subquery*: at 37k accounts the client cannot post ids (ROADMAP §0).
    """
    if payload.filter is not None:
        flt = payload.filter.to_filter()
        if flt.is_empty():
            return None  # an all-default filter means every account
        return Account.id.in_(account_filter.apply(select(Account.id), flt))
    if payload.account_ids:
        return Account.id.in_(payload.account_ids)
    return None


@router.get("", response_model=list[UnitLimitOut])
def list_limits(db: DbSession = Depends(get_db)) -> list[UnitLimitOut]:
    """Every limit, ordered by scope then value (id breaks ties, so stable)."""
    # The scope as text: PostgreSQL would order the native enum by its
    # declaration, SQLite by spelling; the cast makes both alphabetical.
    limits = db.scalars(
        select(UnitLimit).order_by(
            cast(UnitLimit.scope, String).asc(), UnitLimit.value.asc(), UnitLimit.id.asc()
        )
    ).all()
    return [_out(limit) for limit in limits]


@router.post("", response_model=UnitLimitOut, status_code=201, dependencies=[Depends(require_admin)])
def create_limit(
    payload: UnitLimitWrite, db: DbSession = Depends(get_db)
) -> UnitLimitOut:
    _reject_duplicate(db, payload.scope, payload.value)

    limit = UnitLimit(
        scope=payload.scope,
        value=payload.value,
        max_units=payload.max_units,
        window_days=payload.window_days,
        enabled=payload.enabled,
    )
    db.add(limit)
    db.commit()
    db.refresh(limit)
    log.info(
        "created unit limit %s: %s",
        limit.id,
        unit_limits.describe(limit),
    )
    return _out(limit)


@router.post("/check", response_model=list[LimitUsageOut])
def check_limits(
    payload: LimitCheckRequest, db: DbSession = Depends(get_db)
) -> list[LimitUsageOut]:
    """How much room a selection of accounts still has for one product.

    Only the limits that actually govern the product come back, so an empty
    list means "nothing caps this" rather than "nothing is configured".
    """
    usages = unit_limits.selection_usage(
        db, payload.facts(), account_where=_account_where(payload)
    )
    return [
        LimitUsageOut(
            limit_id=usage.limit.id,
            scope=usage.limit.scope,
            value=usage.limit.value or "",
            max_units=usage.limit.max_units,
            window_days=usage.limit.window_days,
            used=usage.used,
            remaining=usage.remaining,
            blocked=usage.blocked,
        )
        for usage in usages
    ]


@router.patch("/{limit_id}", response_model=UnitLimitOut, dependencies=[Depends(require_admin)])
def update_limit(
    limit_id: int, payload: UnitLimitUpdate, db: DbSession = Depends(get_db)
) -> UnitLimitOut:
    limit = db.get(UnitLimit, limit_id)
    if limit is None:
        raise HTTPException(status_code=404, detail="Unit limit not found")

    sent = payload.model_fields_set
    # Re-check the pair whenever either half moves: a PATCH is the easy way to
    # end up with two rules that count the same orders.
    if "scope" in sent or "value" in sent:
        scope = payload.scope if payload.scope is not None else limit.scope
        value = payload.value if "value" in sent else (limit.value or "")
        _reject_duplicate(db, scope, value or "", exclude_id=limit_id)

    for field in _PATCH_FIELDS:
        if field in sent:
            sent_value = getattr(payload, field)
            if sent_value is not None:  # null cannot clear a required field
                setattr(limit, field, sent_value)

    db.commit()
    db.refresh(limit)
    log.info("updated unit limit %s: %s", limit.id, unit_limits.describe(limit))
    return _out(limit)


@router.delete("/{limit_id}", status_code=204, dependencies=[Depends(require_admin)])
def delete_limit(limit_id: int, db: DbSession = Depends(get_db)) -> None:
    """Remove a limit. Past orders keep their brand/category, so re-adding it
    counts the same history again rather than starting from zero."""
    if db.scalar(select(UnitLimit.id).where(UnitLimit.id == limit_id)) is None:
        raise HTTPException(status_code=404, detail="Unit limit not found")

    db.execute(delete(UnitLimit).where(UnitLimit.id == limit_id))
    db.commit()
    log.info("deleted unit limit %s", limit_id)
