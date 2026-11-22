"""Pydantic schemas for the tags API.

Tags are the operator's segmentation tool (ROADMAP §2 Phase 7): a tag *is* a
filter, so the only interesting payload is a name. ``account_count`` travels
with every tag because the list is read as "how many accounts does this cover",
which is the number that decides whether a run is worth starting.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

#: Matches ``Tag.name``'s column width, so an over-long name is a clean 422
#: here rather than a database error on a backend that enforces length.
MAX_NAME_LENGTH = 64


class TagWrite(BaseModel):
    """Create/rename body. Blank names are rejected — an unnamed tag is unusable."""

    model_config = ConfigDict(extra="forbid")

    name: str

    @field_validator("name")
    @classmethod
    def _clean(cls, value: str) -> str:
        name = (value or "").strip()
        if not name:
            raise ValueError("name must not be blank")
        if len(name) > MAX_NAME_LENGTH:
            raise ValueError(f"name must be at most {MAX_NAME_LENGTH} characters")
        return name


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    account_count: int = 0
