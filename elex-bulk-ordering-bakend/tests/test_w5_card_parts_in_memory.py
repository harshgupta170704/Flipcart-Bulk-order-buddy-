"""A card upload's file part never reaches disk, whichever client sends it
(review of 27 Oct 2026). The form reader FastAPI runs for the card routes
wrote a part over 1 MiB to a temp file, and the upload guard lets a card
upload declare 1 MiB plus 64 KiB, so a client posting straight to the API
could still have a part just over 1 MiB written to disk. The reader now keeps
parts that big in memory."""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient
from starlette import formparsers

from app.api import jobs as jobs_api
from app.core.security import create_access_token
from app.main import app


@pytest.fixture
def spools(monkeypatch) -> dict[str, int]:
    seen = {"made": 0, "disk": 0}

    class Watched(tempfile.SpooledTemporaryFile):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen["made"] += 1

        def rollover(self):
            if not getattr(self, "_rolled", False):
                seen["disk"] += 1
            super().rollover()

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", Watched)
    return seen


@pytest.mark.parametrize("route", sorted(jobs_api.CARD_UPLOAD_PATHS))
def test_a_card_part_just_over_a_megabyte_stays_in_memory(route, spools):
    api = TestClient(app, headers={"Authorization": f"Bearer {create_access_token('tester', role='admin')}"})
    part = b"9" * (jobs_api.MAX_CARD_FILE_BYTES + 32 * 1024)  # made-up bytes, over 1 MiB, within the guard
    response = api.post(route, data={"product_link": "https://www.flipkart.com/x/p/itm1"}, files={"cards_file": ("cards.csv", part, "text/csv")})
    assert response.status_code != 413  # the guard let it through
    assert spools["made"] >= 1 and spools["disk"] == 0
