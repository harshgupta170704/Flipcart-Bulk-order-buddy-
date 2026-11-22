"""Compiled invoice archives: where the zips live, how they are named, how a
download is authorised, and how long they are kept (spec FR-031–FR-039).

A compilation (:mod:`app.jobs.compile_tasks`) builds one zip of the invoice
PDFs of the order units matching a ledger filter and files it here — a
directory, ``ORDERING_ARCHIVE_DIR`` (``./data/archives`` by default: the
``appdata`` volume in Docker, beside the invoices). Archives are kept for
``ORDERING_ARCHIVE_RETENTION_DAYS`` (90, spec C-11) and can be deleted by hand
before that.

A download is a plain browser navigation — an ``<a href>`` or
``window.location`` — because a gigabyte must stream to disk rather than be
read into a blob (NFR-006), and such a request cannot carry the bearer header.
So the download route takes a short-lived token instead: an HMAC over the
archive id and an expiry, signed with the deployment's JWT secret, issued by an
authenticated call seconds before. It opens exactly one archive, briefly, and
is not a bearer token for anything else.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.core.order_filter import card_digits
from app.db.base import SessionLocal
from app.db.models import CompiledArchive, utcnow

log = logging.getLogger("ordering.archives")

#: What an Invoice Name Prefix may keep: a file name's safe subset.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_PREFIX_CHARS = 64
#: Half-built zips live here, inside the store so the final rename is atomic.
TEMP_DIR = ".tmp"
#: How long a download token opens its archive for.
DOWNLOAD_TOKEN_SECONDS = 120


def archive_root() -> Path:
    """The configured directory — a function so a test can point it elsewhere."""
    return Path(get_settings().archive_dir)


def clean_prefix(raw: str) -> str:
    """The prefix as it will appear in the file name, or "" when nothing is left.

    Spaces and anything else unsafe become underscores, runs collapse, and
    leading or trailing dots and dashes go (a name starting with a dot would
    hide the file on the server).
    """
    text = _UNSAFE.sub("_", (raw or "").strip())
    text = re.sub(r"_+", "_", text).strip("._-")
    return text[:MAX_PREFIX_CHARS].rstrip("._-")


class ArchiveStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @classmethod
    def from_settings(cls) -> "ArchiveStore":
        return cls(archive_root())

    def path(self, name: str) -> Path:
        """The absolute path of an archive; refuses anything outside the root."""
        root = self.root.resolve()
        candidate = (self.root / name).resolve()
        if candidate.parent != root:
            raise ValueError(f"{name!r} is not an archive name")
        return candidate

    def exists(self, name: str) -> bool:
        try:
            return self.path(name).is_file()
        except ValueError:
            return False

    def temp_path(self, name: str) -> Path:
        """Where one compilation writes ``name`` before the rename: a name of
        this writer's own, as the invoice store names its ``.part`` files. Two
        compilations given the same archive name must not write one file
        between them — the second would truncate the first's zip under it,
        and the first's rename would then find nothing (audit F-39, 25 Oct
        2026)."""
        temp = self.root / TEMP_DIR
        temp.mkdir(parents=True, exist_ok=True)
        return temp / f"{name}.{os.getpid()}-{secrets.token_hex(4)}.part"

    def free_name(self, db: DbSession, prefix: str) -> str:
        """``<prefix>.zip``, or ``<prefix>-2.zip`` … when that name is taken.

        Taken on disk or in the table: an archive row whose file was deleted
        by hand still owns its name, and a file nobody recorded still exists.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        taken = set(
            db.scalars(
                select(CompiledArchive.name).where(CompiledArchive.name.like(f"{prefix}%"))
            ).all()
        )
        candidate, n = f"{prefix}.zip", 1
        while candidate in taken or (self.root / candidate).exists():
            n += 1
            candidate = f"{prefix}-{n}.zip"
        return candidate

    def delete(self, name: str) -> bool:
        """Remove the file; False when there was none (the row still goes)."""
        try:
            path = self.path(name)
        except ValueError:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True


# --- what an archive records of the filter that chose it ----------------------------
#: How much of a card filter a compilation records: its last four digits.
CARD_DIGITS_RECORDED = 4


def recorded_filters(raw: dict) -> dict:
    """The ledger filter as a compilation records it — on its job row, which
    every signed-in user can read (Compilation Status, the job list), and on
    its archive's row, which is kept as long as the archive.

    Everything is kept as it came except the card filter, of which only the
    last four digits are kept, after an ellipsis ("…1111"): enough to say
    which card's orders the archive holds, never the number typed (audit
    F-20, 25 Oct 2026). A card filter with no digits is kept as "…" — it
    still narrowed the run — and a blank one, which narrows nothing, is
    dropped. The run itself is handed the filter as typed, in memory only.
    """
    recorded = {key: value for key, value in raw.items() if key != "card"}
    text = str(raw.get("card") or "").strip()
    if text:
        recorded["card"] = "…" + card_digits(text)[-CARD_DIGITS_RECORDED:]
    return recorded


# --- download tokens ---------------------------------------------------------------
#: A token's signature is exactly this: 64 hex digits. Anything else is
#: refused before it reaches the comparison (``compare_digest`` would raise on
#: non-ASCII text, and an unauthenticated route must never answer 500).
_SIGNATURE = re.compile(r"[0-9a-f]{64}")


def _key() -> bytes:
    """The signing key: derived from the JWT secret with its own label, so a
    download token and a bearer token are never interchangeable."""
    secret = get_settings().jwt_secret.encode("utf-8")
    return hmac.new(secret, b"ordering:archive-download", hashlib.sha256).digest()


def _sign(archive_id: int, expires: int) -> str:
    message = f"archive:{int(archive_id)}:{int(expires)}".encode("utf-8")
    return hmac.new(_key(), message, hashlib.sha256).hexdigest()


def make_download_token(archive_id: int, *, now: float | None = None) -> str:
    """A token that opens this one archive for :data:`DOWNLOAD_TOKEN_SECONDS`."""
    expires = int((time.time() if now is None else now) + DOWNLOAD_TOKEN_SECONDS)
    return f"{expires}.{_sign(archive_id, expires)}"


def check_download_token(archive_id: int, token: str | None, *, now: float | None = None) -> bool:
    """True when ``token`` was issued for this archive and has not expired."""
    if not token:
        return False
    try:
        expires_text, signature = token.split(".", 1)
        expires = int(expires_text)
    except ValueError:
        return False
    moment = time.time() if now is None else now
    # Expired, or claiming a life no token is ever issued with.
    if expires < moment or expires > moment + DOWNLOAD_TOKEN_SECONDS + 60:
        return False
    if _SIGNATURE.fullmatch(signature) is None:
        return False
    return hmac.compare_digest(signature, _sign(archive_id, expires))


# --- retention -----------------------------------------------------------------------
def sweep_expired(
    db: DbSession,
    *,
    store: ArchiveStore | None = None,
    now: datetime | None = None,
    days: int | None = None,
) -> list[str]:
    """Delete archives older than the retention; returns their names.

    A retention of 0 (or less) keeps everything. The file goes first and the
    row after, so a sweep interrupted half-way leaves a row whose file is gone
    — which the download route answers with a 404 — rather than a file nobody
    can see.
    """
    days = get_settings().archive_retention_days if days is None else days
    if days <= 0:
        return []
    store = store or ArchiveStore.from_settings()
    moment = now or utcnow()
    if moment.tzinfo is not None:
        moment = moment.replace(tzinfo=None)
    cutoff = moment - timedelta(days=days)
    rows = db.scalars(
        select(CompiledArchive).where(CompiledArchive.created_at < cutoff).order_by(CompiledArchive.id)
    ).all()
    names: list[str] = []
    for row in rows:
        store.delete(row.name)
        db.delete(row)
        names.append(row.name)
    if names:
        db.commit()
    return names


def reclaim_unrecorded(db: DbSession, *, store: ArchiveStore | None = None) -> list[str]:
    """Remove what an interrupted compilation left behind; returns the names.

    A server stopped mid-build leaves ``.tmp/<name>.zip.<writer>.part``, and one
    stopped between the rename and the row leaves a finished ``<name>.zip`` that
    nothing lists, downloads, deletes or sweeps (the sweep works from rows).
    Run at start-up, when no compilation can be in progress, so a zip on disk
    without a row is never a race — only a leftover.
    """
    store = store or ArchiveStore.from_settings()
    removed: list[str] = []
    temp = store.root / TEMP_DIR
    if temp.is_dir():
        for part in temp.iterdir():
            if part.is_file():
                part.unlink()
                removed.append(f"{TEMP_DIR}/{part.name}")
    if store.root.is_dir():
        recorded = set(db.scalars(select(CompiledArchive.name)).all())
        for entry in store.root.iterdir():
            if entry.is_file() and entry.suffix == ".zip" and entry.name not in recorded:
                entry.unlink()
                removed.append(entry.name)
    return removed


def sweep_in_own_session() -> list[str]:
    """Run the retention sweep from outside a request: at start-up and daily.
    Never the reason the caller fails."""
    with SessionLocal() as db:
        try:
            names = sweep_expired(db)
        except Exception:  # noqa: BLE001 — logged, never fatal
            log.exception("could not sweep expired archives")
            return []
    if names:
        log.info("deleted %d expired archive(s): %s", len(names), ", ".join(names))
    return names


def reclaim_in_own_session() -> list[str]:
    """Start-up entry point for :func:`reclaim_unrecorded`; never fatal."""
    with SessionLocal() as db:
        try:
            names = reclaim_unrecorded(db)
        except Exception:  # noqa: BLE001 — logged, never fatal
            log.exception("could not reclaim leftover archive files")
            return []
    if names:
        log.warning("removed %d leftover archive file(s) from an interrupted compilation: %s", len(names), ", ".join(names))
    return names
