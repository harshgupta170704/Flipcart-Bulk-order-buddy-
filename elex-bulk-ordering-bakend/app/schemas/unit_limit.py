"""Pydantic schemas for the unit-limits API (ROADMAP §2 Phase 9).

A unit limit is a cap the platform enforces on one account ("20 handsets per
31 days"), so the validation here is about never storing a rule that cannot
bite: ``max_units`` and ``window_days`` are both at least 1, because a cap of
zero units blocks every account and a window of zero days counts nothing.

``extra="forbid"`` on the request bodies is the same safety feature as on the
account filter: a mistyped field would otherwise be dropped silently, and the
quiet reading of a dropped ``max_units`` is "the cap I set is not being
applied" — which the operator only discovers when accounts start getting
flagged.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.unit_limits import ProductFacts
from app.db.models import LimitScope
from app.schemas.account import MAX_LISTED_IDS, AccountFilterIn, RowId

#: Matches ``UnitLimit.value``'s column width, so an over-long value is a clean
#: 422 here rather than a database error on a backend that enforces length.
MAX_VALUE_LENGTH = 128


def _clean_value(value: str | None) -> str:
    """Normalise a limit's value; blank (any whitespace) means the wildcard."""
    text = (value or "").strip()
    if len(text) > MAX_VALUE_LENGTH:
        raise ValueError(f"value must be at most {MAX_VALUE_LENGTH} characters")
    return text


class UnitLimitWrite(BaseModel):
    """POST body. ``value`` defaults to the wildcard — every value in the scope."""

    model_config = ConfigDict(extra="forbid")

    scope: LimitScope
    value: str = ""
    max_units: int = Field(ge=1)
    window_days: int = Field(default=30, ge=1)
    enabled: bool = True

    @field_validator("value")
    @classmethod
    def _value(cls, value: str) -> str:
        return _clean_value(value)


class UnitLimitUpdate(BaseModel):
    """PATCH body — every field optional.

    Callers read ``model_fields_set`` rather than the values, so "field absent"
    stays distinguishable from "field set to its default": sending nothing must
    not quietly reset a 31-day window to 30.
    """

    model_config = ConfigDict(extra="forbid")

    scope: LimitScope | None = None
    value: str | None = None
    max_units: int | None = Field(default=None, ge=1)
    window_days: int | None = Field(default=None, ge=1)
    enabled: bool | None = None

    @field_validator("value")
    @classmethod
    def _value(cls, value: str | None) -> str:
        return _clean_value(value)


class UnitLimitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    scope: LimitScope
    value: str = ""
    max_units: int
    window_days: int
    enabled: bool
    created_at: datetime


class LimitCheckRequest(BaseModel):
    """Body of ``POST /unit-limits/check`` — a pre-flight for one product.

    The account selection follows the same precedence as every job start
    (``app.api.jobs._resolve_selection``): a filter wins, then an explicit id
    list, then — neither given — every account. Nothing here writes, so the
    widest reading is safe; what matters is that a check and the run it
    precedes resolve "which accounts?" identically.
    """

    model_config = ConfigDict(extra="forbid")

    #: Bounded like every job start's list (audit F-40).
    account_ids: list[RowId] | None = Field(default=None, max_length=MAX_LISTED_IDS)
    filter: AccountFilterIn | None = None
    brand: str | None = None
    category: str | None = None
    model: str | None = None
    seller: str | None = None

    def facts(self) -> ProductFacts:
        """The product being checked, in the terms a limit matches on."""
        return ProductFacts(
            brand=(self.brand or "").strip() or None,
            category=(self.category or "").strip() or None,
            model=(self.model or "").strip() or None,
            seller=(self.seller or "").strip() or None,
        )


class LimitUsageOut(BaseModel):
    """One limit measured against the selection.

    ``max_units`` and ``window_days`` are the per-account rule; ``used`` and
    ``remaining`` are totals over the selected accounts, so ``remaining`` reads
    as "units this selection can still absorb". For a single account the two
    are the same number. ``blocked`` means no selected account has room left.
    """

    limit_id: int
    scope: LimitScope
    value: str = ""
    max_units: int
    window_days: int
    used: int
    remaining: int
    blocked: bool
