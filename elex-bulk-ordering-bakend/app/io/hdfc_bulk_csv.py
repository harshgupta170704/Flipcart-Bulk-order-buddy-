"""HDFC CorpAssist's bulk virtual-card sheet, and the output file it answers with.

The portal's "Create Virtual Card (Bulk)" page (``/create-virtual-card-bulk-new``)
takes a ``.csv`` of at most 5 MB with exactly these headers, in this order,
all mandatory (the page's own note, seen 21 Nov 2026)::

    Aan, VirtualCardUserName, EmailId, MobileNumber, Amount, ValidForDays

* ``Aan``: the parent card's "Alternate A/c No." (Card Details > Card
  Relationship), the card every row's virtual card is issued under.
* ``VirtualCardUserName``, ``EmailId``, ``MobileNumber``: a Virtual Card User
  that already exists on the portal, as it is set up there. HDFC mails the
  card to that email and texts that mobile.
* ``Amount``: the card's limit in rupees; ``ValidForDays``: how long it lives.

One row is one card, so N cards for a user are N identical rows.

When the portal refuses a file it says "Upload Failed! Please download the
output file to rectify the errors and re-upload." :func:`read_output_file`
turns that file into one line per refused row for the batch's report.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass

HEADERS = ("Aan", "VirtualCardUserName", "EmailId", "MobileNumber", "Amount", "ValidForDays")
FILE_NAME = "bulkVirtualCardCreation.csv"
#: The portal's own cap on the file.
MAX_BYTES = 5 * 1024 * 1024

_CARD_RUN = re.compile(r"\d[\d -]{14,22}\d")


@dataclass(frozen=True)
class BulkRow:
    aan: str
    username: str
    email: str
    mobile: str
    amount: float
    valid_days: int


def _amount(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def _cell(value: str) -> str:
    """No cell may start a spreadsheet formula."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def build(rows: Iterable[BulkRow]) -> bytes:
    """The sheet as the portal wants it: UTF-8, CRLF, headers first."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(HEADERS)
    count = 0
    for row in rows:
        writer.writerow(
            [
                _cell(row.aan),
                _cell(row.username.upper()),
                _cell(row.email),
                _cell(row.mobile),
                _amount(row.amount),
                str(int(row.valid_days)),
            ]
        )
        count += 1
    if not count:
        raise ValueError("a bulk sheet needs at least one card")
    data = buffer.getvalue().encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("the sheet is over the portal's 5 MB limit: send fewer cards at once")
    return data


def _mask(text: str) -> str:
    """No card number may reach the report, whatever the file holds."""
    return _CARD_RUN.sub(lambda m: "XXXX" + re.sub(r"\D", "", m.group(0))[-4:], text)


_REASON_HEADERS = ("remark", "remarks", "error", "errors", "reason", "status", "message", "failurereason", "comments")


def read_output_file(raw: bytes, *, limit: int = 50) -> list[str]:
    """One line per refused row of the portal's output file: "row 3 (MANMOHAN):
    <the portal's words>". Rows the file marks as fine are left out."""
    text = raw.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    header = [re.sub(r"[^a-z]", "", h.lower()) for h in rows[0]]
    reason_cols = [i for i, h in enumerate(header) if h in _REASON_HEADERS]
    name_col = header.index("virtualcardusername") if "virtualcardusername" in header else None
    if not reason_cols:
        # No column says why: the extra columns past HDFC's six are the reason.
        reason_cols = list(range(len(HEADERS), len(header)))
    lines: list[str] = []
    for number, row in enumerate(rows[1:], start=2):
        reasons = [row[i].strip() for i in reason_cols if i < len(row) and row[i].strip()]
        reasons = [r for r in reasons if r.lower() not in ("success", "ok", "valid", "passed")]
        if not reasons:
            continue
        who = f" ({row[name_col].strip()})" if name_col is not None and name_col < len(row) else ""
        lines.append(_mask(f"row {number}{who}: {'; '.join(reasons)}")[:300])
        if len(lines) >= limit:
            lines.append("…")
            break
    return lines
