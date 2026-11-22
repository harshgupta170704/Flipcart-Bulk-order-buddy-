"""The gift card sheet of a "Gift Voucher" run: one row per voucher, with
the account's email, the voucher number and its PIN. Read in memory for the
run and never written anywhere, like the card sheet (PLAN.md §5).

CSV or .xlsx, with a heading row naming the three columns in any order
("Email", "Voucher Number", "Voucher PIN" — or "Gift Card Number" / "PIN").
An account may have several rows: Flipkart takes up to 15 cards per payment.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

_EMAIL = ("email", "emailid", "accountemail", "account")
_NUMBER = ("vouchernumber", "giftcardnumber", "cardnumber", "voucher", "giftcard", "number")
_PIN = ("voucherpin", "giftcardpin", "pin")


@dataclass
class GiftCardRow:
    email: str
    number: str
    pin: str

    def __repr__(self) -> str:  # never print a voucher
        return f"GiftCardRow(email={self.email!r}, number='****{self.number[-4:]}', pin='***')"


def _key(text: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _rows(raw: bytes, name: str) -> list[list[str]]:
    if name.lower().endswith(".xlsx"):
        from openpyxl import load_workbook

        sheet = load_workbook(io.BytesIO(raw), read_only=True, data_only=True).active
        return [["" if cell is None else str(cell) for cell in row] for row in sheet.iter_rows(values_only=True)]
    text = raw.decode("utf-8-sig", errors="replace")
    return [row for row in csv.reader(io.StringIO(text))]


def parse_gift_card_sheet(raw: bytes, name: str = "sheet.csv") -> tuple[list[GiftCardRow], list[dict]]:
    """The usable rows, and the rows set aside (by row number and reason only)."""
    rows = _rows(raw, name)
    if not rows:
        return [], [{"row": 1, "reason": "the sheet is empty"}]
    head = [_key(cell) for cell in rows[0]]

    def column(names: tuple[str, ...]) -> int | None:
        for wanted in names:
            if wanted in head:
                return head.index(wanted)
        return None

    email_at, number_at, pin_at = column(_EMAIL), column(_NUMBER), column(_PIN)
    if None in (email_at, number_at, pin_at):
        return [], [{"row": 1, "reason": "the heading row must name Email, Voucher Number and Voucher PIN"}]
    usable: list[GiftCardRow] = []
    rejected: list[dict] = []
    for number, row in enumerate(rows[1:], start=2):
        cells = row + [""] * (max(email_at, number_at, pin_at) + 1 - len(row))
        email = cells[email_at].strip().lower()
        code = re.sub(r"\s", "", cells[number_at])
        pin = re.sub(r"\s", "", cells[pin_at])
        if not (email or code or pin):
            continue
        if not email or "@" not in email:
            rejected.append({"row": number, "reason": "no email"})
        elif not code:
            rejected.append({"row": number, "reason": "no voucher number"})
        elif not pin:
            rejected.append({"row": number, "reason": "no voucher PIN"})
        else:
            usable.append(GiftCardRow(email=email, number=code, pin=pin))
    return usable, rejected
