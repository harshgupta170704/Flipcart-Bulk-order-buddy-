"""Pydantic schemas for the addresses and GSTs APIs (ROADMAP §2 Phase 8).

Both tables share this module because they are one decision: an order ships to
an address *and* bills under the GST that address is linked to, and the operator
picks them as a pair at order time.

The validation is strict on purpose. Getting a pincode or a GSTIN wrong does not
fail an order — the order succeeds and the parcel goes to the wrong place under
the wrong registration. That is the quiet failure this phase exists to remove,
so a malformed field is a 422 here rather than a surprise on an invoice.

The PATCH bodies keep "absent" and "null" apart, which is why they are separate
classes from the create bodies: absent leaves a column alone, null clears a
*nullable* one, and null on a required one is a 422 rather than a NOT NULL error
from the database. Routes read ``model_fields_set``, never the values, to tell
the two apart.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    StringConstraints,
    field_validator,
    model_validator,
)

#: Lengths match the ORM column widths, so an over-long value is a clean 422
#: here rather than a database error on a backend that enforces length.
MAX_LABEL_LENGTH = 64
MAX_NAME_LENGTH = 255
MAX_PHONE_LENGTH = 32
MAX_FLOOR_LENGTH = 64
MAX_CITY_LENGTH = 128
MAX_STATE_LENGTH = 128
MAX_BUSINESS_NAME_LENGTH = 255
#: ``Address.line1`` is a Text column, so this cap is ours: an address line
#: longer than this is a paste accident, not a delivery address.
MAX_LINE1_LENGTH = 1000
#: A GSTIN is fixed-width by law — 2 state digits, 10 PAN, 1 entity, 1 "Z",
#: 1 check digit. Anything else is a typo, and a typo bills the wrong entity.
GSTIN_LENGTH = 15
#: Indian pincodes are exactly six digits. "12345" and "abcdef" are both 422.
#: ASCII digits: the pattern runs on pydantic-core's regex, where \d is any
#: Unicode digit, so "５６０００１" would pass and be typed into Flipkart.
PINCODE_PATTERN = r"^[0-9]{6}$"


# --- reusable field types -----------------------------------------------------
# Every string is stripped first, so "  " fails the min_length check and a
# padded pincode still matches the pattern.
Label = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_LABEL_LENGTH),
]
RequiredName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NAME_LENGTH),
]
Line1 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_LINE1_LENGTH),
]
Pincode = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=PINCODE_PATTERN)
]
#: A GSTIN's shape: state code, PAN (five letters, four digits, a letter), the
#: entity digit, "Z", a check character. A malformed one would be typed into
#: Flipkart as it stands and refused there, or worse, accepted as another
#: entity's (audit F-24). ASCII and upper-case only, spelled [0-9] and [A-Z] on
#: purpose: pydantic-core's regex reads ``\d`` as any Unicode digit and
#: ``(?i)[A-Z]`` as any letter that folds to A–Z, so the pattern this replaced
#: let full-width or Devanagari digits and the Kelvin sign through, and saved
#: them. Lower case is still accepted — :func:`normalise_gstin` upper-cases
#: before this is checked.
GSTIN_PATTERN = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
_GSTIN_RE = re.compile(GSTIN_PATTERN)


def normalise_gstin(value: object) -> object:
    """Strip and upper-case a GSTIN, ready for its shape to be checked.

    A validator of its own, not ``StringConstraints(to_upper=True)``, because
    pydantic-core tests the pattern before it upper-cases, so a lower-case
    GSTIN would never reach the upper-case pattern. Only a plain-ASCII value is
    upper-cased: ``str.upper()`` turns some non-ASCII letters into ASCII ones
    ("ſ" into "S"), and a GSTIN nobody typed must not appear that way — left
    as it came, the ASCII pattern refuses it. Anything not a string passes
    through for the string check to refuse.
    """
    if not isinstance(value, str):
        return value
    value = value.strip()
    return value.upper() if value.isascii() else value


def is_gstin(value: str) -> bool:
    """Whether ``value``, once normalised, has a GSTIN's shape — the API's own
    check, for the places that take a GSTIN without a request body (the
    start-up carry-over of the retired business settings)."""
    normalised = normalise_gstin(value)
    return isinstance(normalised, str) and _GSTIN_RE.fullmatch(normalised) is not None


# The order matters: a BeforeValidator listed after the constraints runs ahead
# of them, so the value is stripped and upper-cased first and only then held
# to the length and the pattern.
Gstin = Annotated[
    str,
    StringConstraints(min_length=GSTIN_LENGTH, max_length=GSTIN_LENGTH, pattern=GSTIN_PATTERN),
    BeforeValidator(normalise_gstin),
]
BusinessName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=MAX_BUSINESS_NAME_LENGTH
    ),
]
Phone = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_PHONE_LENGTH)]
Floor = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_FLOOR_LENGTH)]
City = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_CITY_LENGTH)]
State = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_STATE_LENGTH)]


def _reject_null_on_required(model: BaseModel, fields: tuple[str, ...]) -> None:
    """422 when a PATCH sends ``null`` for a column that cannot be null.

    The alternative readings are both wrong: silently ignoring it loses an edit
    the operator thinks they made, and writing it through raises a NOT NULL
    error as a 500.
    """
    sent = model.model_fields_set
    for field in fields:
        if field in sent and getattr(model, field) is None:
            raise ValueError(f"{field} must not be null")


# --- GST ----------------------------------------------------------------------
class GstWrite(BaseModel):
    """``POST /gsts`` body."""

    model_config = ConfigDict(extra="forbid")

    label: Label
    gstin: Gstin
    business_name: BusinessName
    is_default: bool = False


class GstPatch(BaseModel):
    """``PATCH /gsts/{id}`` body — any subset of the create fields."""

    model_config = ConfigDict(extra="forbid")

    label: Label | None = None
    gstin: Gstin | None = None
    business_name: BusinessName | None = None
    is_default: bool | None = None

    @model_validator(mode="after")
    def _no_nulls(self) -> GstPatch:
        _reject_null_on_required(self, ("label", "gstin", "business_name", "is_default"))
        return self


class GstRef(BaseModel):
    """A GST inlined into an address row, so the table needs no second call."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    gstin: str
    business_name: str


class GstDeleted(BaseModel):
    """``DELETE /gsts/{id}``'s answer: how many addresses and accounts the
    delete unlinked, as its own UPDATEs counted them. The Address Book words
    its note from these, not from the list it loaded earlier, which another
    admin may have changed since (review of 27 Oct 2026)."""

    unlinked_addresses: int
    unlinked_accounts: int


class GstOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    gstin: str
    business_name: str
    is_default: bool = False
    #: How many addresses bill under this GST. Read as "is this entity actually
    #: in use", which is what decides whether deleting it is safe.
    address_count: int = 0
    #: How many accounts have it as their own GST — the accounts a delete
    #: would put back on their runs' GST.
    account_count: int = 0
    created_at: datetime


# --- Address ------------------------------------------------------------------
class AddressWrite(BaseModel):
    """``POST /addresses`` body."""

    model_config = ConfigDict(extra="forbid")

    label: Label
    name: RequiredName
    phone: Phone | None = None
    line1: Line1
    floor: Floor | None = None
    city: City | None = None
    state: State | None = None
    pincode: Pincode
    gst_id: int | None = None
    is_default: bool = False

    @field_validator("phone", "floor", "city", "state", mode="after")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        """A blank optional field means "not given", not an empty string."""
        return value or None


class AddressPatch(BaseModel):
    """``PATCH /addresses/{id}`` body — any subset of the create fields.

    ``gst_id: null`` explicitly unlinks the address from its GST.
    """

    model_config = ConfigDict(extra="forbid")

    label: Label | None = None
    name: RequiredName | None = None
    phone: Phone | None = None
    line1: Line1 | None = None
    floor: Floor | None = None
    city: City | None = None
    state: State | None = None
    pincode: Pincode | None = None
    gst_id: int | None = None
    is_default: bool | None = None

    @field_validator("phone", "floor", "city", "state", mode="after")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        """Sending "" clears the field, same as sending null."""
        return value or None

    @model_validator(mode="after")
    def _no_nulls(self) -> AddressPatch:
        _reject_null_on_required(
            self, ("label", "name", "line1", "pincode", "is_default")
        )
        return self


class AddressOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    name: str
    phone: str | None = None
    line1: str
    floor: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str
    is_default: bool = False
    #: The GST this address bills under, inlined — the pair is what a run needs.
    gst: GstRef | None = None
    created_at: datetime
