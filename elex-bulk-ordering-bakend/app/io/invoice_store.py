"""Where invoice PDFs live on disk.

A directory on the server for now (``ORDERING_INVOICE_DIR``, a mounted volume
in Docker), S3 later behind the same few operations. Files are laid out by
the day they were put on file and named by order id plus a hash prefix, the
way the owner's own specification shows invoice paths
(``/invoices/2025/04/17/OD334935538466132100.pdf``):

    <root>/2025/04/17/OD334935538466132100-3f9a1c2e.pdf

The hash makes a second, different PDF for the same order (a fee invoice
beside the product invoice) a second file rather than an overwrite, and the
same bytes put twice land on the same path.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_root() -> Path:
    """The configured directory — a function so a test can point it elsewhere."""
    return Path(get_settings().invoice_dir)


class InvoiceStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @classmethod
    def from_settings(cls) -> "InvoiceStore":
        return cls(store_root())

    def put(
        self, order_id: str, data: bytes, digest: str | None = None, *, when: datetime | None = None
    ) -> str:
        """Write the PDF and return its path relative to the store.

        Written to a temporary name and renamed, so a reader never sees a
        half-written file; the same bytes put again are a no-op.
        """
        return self.add(order_id, data, digest, when=when)[0]

    def add(
        self, order_id: str, data: bytes, digest: str | None = None, *, when: datetime | None = None
    ) -> tuple[str, bool]:
        """:meth:`put`, also saying whether this call wrote the file: False
        when the same bytes were on that path already. A file the caller
        added is the caller's to take back (:meth:`remove`) when its row
        cannot be saved; one that was already there may be another row's."""
        digest = digest or sha256(data)
        stamp = when or datetime.now(timezone.utc)
        safe_id = "".join(ch for ch in order_id.upper() if ch.isalnum()) or "ORDER"
        relative = Path(f"{stamp:%Y}/{stamp:%m}/{stamp:%d}/{safe_id}-{digest[:8]}.pdf")
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return relative.as_posix(), False
        # A temporary name of this writer's own: two writers of the same
        # bytes at once (an upload and a read-back) must not share a
        # half-written file (audit F-39, 25 Oct 2026).
        temporary = target.with_name(f"{target.name}.{os.getpid()}-{secrets.token_hex(4)}.part")
        temporary.write_bytes(data)
        temporary.replace(target)
        return relative.as_posix(), True

    def remove(self, relative: str) -> bool:
        """Delete a stored file; False when it was not there, or not in the
        store at all. Only for taking back a file whose row could not be
        saved (app.core.invoices) — a row must never point at a missing file."""
        try:
            self.path(relative).unlink()
        except (FileNotFoundError, ValueError):
            return False
        return True

    def path(self, relative: str) -> Path:
        """The absolute path of a stored file; refuses anything outside the root."""
        root = self.root.resolve()
        candidate = (self.root / relative).resolve()
        if root != candidate and root not in candidate.parents:
            raise ValueError(f"{relative!r} is not inside the invoice store")
        return candidate

    def exists(self, relative: str) -> bool:
        try:
            return self.path(relative).is_file()
        except ValueError:
            return False
