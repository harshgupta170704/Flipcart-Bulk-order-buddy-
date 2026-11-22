"""Reading an HDFC CorpAssist child-card email (app.core.corpassist).

**Provisional.** No real child-card email has been seen yet (19 Nov 2026), so
this reads only what can be recognised safely and refuses the rest, with a
reason the CorpAssist page shows:

* the card number: a 16-digit run (spaces or dashes allowed) that passes the
  Luhn check. A masked number ("XXXX XXXX XXXX 1234") is refused, since a
  card that cannot be matched to a run's sheet is no use, and so is an email
  carrying two different numbers, since which is the child card is a guess;
* the opening amount: a rupee amount beside "limit", "amount", "value" or
  "balance";
* the parent card's last four digits, beside "parent" or "corporate card".

An expiry and a CVV are never read, even when the email carries them (PLAN.md
§5). Nothing here logs; a reason never repeats a card number.

Once a real email is in hand, adjust the patterns here and add it, with its
digits replaced, to tests/fixtures as a regression case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape

#: 16 digits in groups of four, separated by nothing, a space or a dash.
_CARD = re.compile(r"(?<![\dXx*])(\d{4})[ -]?(\d{4})[ -]?(\d{4})[ -]?(\d{4})(?![\dXx*])")
#: A masked card number, to say why nothing was read.
_MASKED = re.compile(r"[Xx*•]{4}[ -]?[Xx*•]{4}[ -]?[Xx*•]{4}[ -]?\d{4}")
_AMOUNT = re.compile(
    r"(?:limit|amount|value|balance)[^0-9\n]{0,40}?(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_PARENT = re.compile(
    r"(?:parent|corporate)\s+card[^0-9\n]{0,40}?(?:[Xx*•]{4}[ -]?){0,3}(\d{4})\b",
    re.IGNORECASE,
)
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"[ \t\r\f\v]+")


@dataclass(frozen=True)
class ChildCardMail:
    """What one email said: a card, or why none was read."""

    number: str | None = None
    opening_amount: float | None = None
    parent_last4: str | None = None
    reason: str | None = None

    def __repr__(self) -> str:  # never a full number in a log or traceback
        tail = self.number[-4:] if self.number else None
        return f"ChildCardMail(last4={tail!r}, opening_amount={self.opening_amount!r}, reason={self.reason!r})"

    __str__ = __repr__


def luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def plain_text(body: str) -> str:
    """An HTML body as text: tags become line breaks, entities are decoded."""
    text = unescape(_TAGS.sub("\n", body or ""))
    return "\n".join(_SPACE.sub(" ", line).strip() for line in text.splitlines())


def read_child_card(subject: str | None, body: str | None) -> ChildCardMail:
    text = plain_text(f"{subject or ''}\n{body or ''}")
    numbers = {"".join(m.groups()) for m in _CARD.finditer(text)}
    numbers = {n for n in numbers if luhn_ok(n)}
    if not numbers:
        if _MASKED.search(text):
            return ChildCardMail(reason="the email shows only a masked card number")
        return ChildCardMail(reason="no card number was found in the email")
    if len(numbers) > 1:
        return ChildCardMail(reason="the email holds more than one card number")
    number = numbers.pop()

    amount = None
    found = _AMOUNT.search(text)
    if found:
        try:
            amount = float(found.group(1).replace(",", ""))
        except ValueError:
            amount = None
    parent = _PARENT.search(text)
    parent_last4 = parent.group(1) if parent and parent.group(1) != number[-4:] else None
    return ChildCardMail(number=number, opening_amount=amount, parent_last4=parent_last4)
