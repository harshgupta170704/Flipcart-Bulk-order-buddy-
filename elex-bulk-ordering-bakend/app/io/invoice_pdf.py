"""Reading a Flipkart invoice PDF into the fields the invoice-data CSV needs.

Three kinds of document come out of Flipkart for one order: the product's
tax invoice (one line per product, plus shipping), a fee invoice per fee
("Secure Packaging Fee", "Offer Handling Fee" — one line each, no buyer
GSTIN), and a credit note when something is returned (no sample yet, so it
is parsed on its labels only). All three print the same header block —
Sold By, the seller's GSTIN, the invoice number, the order id and the two
dates — and a line table with Qty, Gross, Discount, Taxable, the tax
columns the seller's state calls for (CGST + SGST, or IGST) and Total.

:func:`parse_invoice` reads the PDF with pdfplumber and hands its text to
:func:`parse_invoice_text`, which is pure and testable on text. The text is
rebuilt line by line from the words' positions, with a tab wherever two words
are separated by a cell-sized gap: that is how the Product column ("Handsets",
"FSN:", the FSN, "HSN/SAC: …") is told apart from the Title column beside it,
and the Bill To block from the Ship To block, which pdfplumber's plain text
runs together. Plain text parses too — the labels are the same — but a
product's category then stays at the front of its title.

Nothing here ever raises over the file's content: garbage, a scan without a
text layer or an unfamiliar layout come back as kind "unknown" with ``error``
set, and the file stays on file regardless.

The reader runs in the process that places the orders, on whatever PDF it is
handed, so it is bounded: a file of more than :data:`MAX_PAGES` pages is not
read at all, and one still being read after :data:`MAX_READ_SECONDS` stops at
the next page. Either comes back as an unreadable file does, the reason in
``error`` (audit F-27, 26 Oct 2026).
"""

from __future__ import annotations

import io
import itertools
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from operator import itemgetter

log = logging.getLogger("ordering.invoice_pdf")

#: Flipkart's invoices run to one to three pages. A PDF longer than this is
#: not one of them, and reading every page of a long one would hold the
#: process that places the orders; it is kept unread, with the reason.
#: Raising it does not re-read the PDFs it already refused: those carry this
#: parser's version, so only a PARSER_VERSION bump reads them again.
MAX_PAGES = 20
#: How long one PDF may take to read, checked between pages. A real invoice
#: takes a tenth of a second; the check cannot cut a single page short, which
#: is why the page cap comes first. Running out of time says more about how
#: busy the machine was than about the file, so such a PDF is read once more
#: at the next start (core.invoices.apply_parse) before it is given up on.
MAX_READ_SECONDS = 20.0
#: How a reading stopped by the time limit begins its error.
TOOK_TOO_LONG = "reading took too long"
#: The clock the time limit reads (a test winds it forward).
_clock = time.monotonic

#: Bump when the parser reads something new or differently, so start-up
#: re-reads the rows parsed by an older version (core.invoices.backfill_unparsed).
#: 2 (26 Oct 2026): Flipkart India's transport (GTA) invoice — "Particulars"
#: header, rupee signs on the numbers, the SAC code after the words, "Billed
#: From / Billed To" — and a fee invoice with several lines labelled by all of
#: them.
#: 3 (11 Nov 2026): a second IMEI inside one bracket (a dual-SIM handset) is
#: kept beside the first.
#: 4 (27 Nov 2026): the owner's reference names — "Protect Promise" (not
#: "… Fee"), "GT Charges" for Flipkart India's delivery/transport invoice,
#: "<what it reverses> Credit Note" — and the seller of a "Billed From" block
#: printed on a line of its own (Flipkart's layout of Nov 2026).
PARSER_VERSION = 4

KIND_PRODUCT = "product"
KIND_FEE = "fee"
KIND_CREDIT_NOTE = "credit_note"
KIND_UNKNOWN = "unknown"

TYPE_PRODUCT = "Product"
TYPE_CREDIT_NOTE = "Credit Note"
#: A fee invoice whose line could not be read is still a fee invoice.
TYPE_FEE = "Fee"

#: Two words further apart than this (in points) are in different cells.
#: Words of one phrase sit 1–3 points apart on these invoices; the narrowest
#: gap between two cells seen is 7.
CELL_GAP = 5.0


@dataclass
class ParsedLine:
    title: str | None = None
    #: Flipkart's product code (FSN), the CSV's FSN column.
    fsn: str | None = None
    hsn_sac: str | None = None
    #: Several for a quantity above one, joined with ", ".
    imei: str | None = None
    qty: int | None = None
    gross: float | None = None
    discount: float | None = None
    taxable: float | None = None
    cgst: float | None = None
    sgst: float | None = None
    igst: float | None = None
    total: float | None = None


@dataclass
class ParsedInvoice:
    #: The label the Extract Invoices Data dialog lists: "Product", the fee's
    #: own name ("Secure Packaging Fee"), "Credit Note"; None when unknown.
    invoice_type: str | None = None
    kind: str = KIND_UNKNOWN
    invoice_number: str | None = None
    invoice_date: date | None = None
    order_id: str | None = None
    order_date: date | None = None
    seller_name: str | None = None
    seller_gstin: str | None = None
    buyer_name: str | None = None
    buyer_gstin: str | None = None
    grand_total: float | None = None
    #: Credit notes only: the invoice being reversed, and why.
    original_invoice: str | None = None
    reason: str | None = None
    lines: list[ParsedLine] = field(default_factory=list)
    #: Why the parse is incomplete; None when everything expected was read.
    error: str | None = None

    def lines_as_dicts(self) -> list[dict]:
        return [asdict(line) for line in self.lines]


# --- patterns ---------------------------------------------------------------------
_NUM = r"-?\d[\d,]*(?:\.\d+)?"
_DATE = r"(\d{1,2}[-/]\d{1,2}[-/]\d{4})"
#: 2-digit state code, the PAN, an entity digit, "Z", a check character.
_GSTIN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]\b")
_ORDER_ID = re.compile(r"\bOD\d{10,}\b")
_SOLD_BY = re.compile(r"^\s*Sold By\s*:\s*(.+?)\s*,?\s*$", re.IGNORECASE | re.MULTILINE)
#: Not the "Original Invoice Number" a credit note quotes — that is its own field.
_INVOICE_NUMBER = re.compile(
    r"(?<!Original )(?:Invoice|Credit Note)\s+(?:Number|No\.?)\s*[#:]?\s*([A-Z0-9][A-Z0-9/-]*)", re.IGNORECASE
)
_ORDER_DATE = re.compile(r"Order Date\s*:\s*" + _DATE, re.IGNORECASE)
_INVOICE_DATE = re.compile(r"(?<!Original )(?:Invoice|Credit Note)\s+Date\s*:\s*" + _DATE, re.IGNORECASE)
_GRAND_TOTAL = re.compile(r"Grand Total\s*₹?\s*(" + _NUM + ")", re.IGNORECASE)
_ORIGINAL_INVOICE = re.compile(
    r"Original Invoice(?:\s+(?:Number|No\.?))?\s*[#:]\s*([A-Z0-9][A-Z0-9/-]*)", re.IGNORECASE
)
_REASON = re.compile(r"^[^\S\n]*Reason(?:\s+(?:of|for)\s+[^:\n]*)?\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_CREDIT_NOTE = re.compile(r"\bCredit\s+Note\b", re.IGNORECASE)
_BILL_TO = re.compile(r"\bBill(?:ing)?\s+(?:To|Address)\b", re.IGNORECASE)
_SHIP_TO = re.compile(r"\bShip(?:ping)?\s+(?:To|Address)\b", re.IGNORECASE)
#: The left-column tokens of the buyer line, which share it with the buyer's name.
_BUYER_LINE_PREFIX = re.compile(r"^\s*(?:OD\d{10,}|Order Date\s*:\s*\d{1,2}[-/]\d{1,2}[-/]\d{4})\s*", re.IGNORECASE)

#: The line table's columns, by the word that names them in the header line.
_COLUMNS = (
    ("qty", re.compile(r"\bQty\b", re.IGNORECASE)),
    ("gross", re.compile(r"\bGross\b", re.IGNORECASE)),
    ("discount", re.compile(r"\bDiscounts?\b", re.IGNORECASE)),
    ("taxable", re.compile(r"\bTaxable\b", re.IGNORECASE)),
    ("cgst", re.compile(r"\bCGST\b", re.IGNORECASE)),
    ("sgst", re.compile(r"\bSGST\b", re.IGNORECASE)),
    ("igst", re.compile(r"\bIGST\b", re.IGNORECASE)),
    ("cess", re.compile(r"\bCess\b", re.IGNORECASE)),
    ("total", re.compile(r"\bTotal\b", re.IGNORECASE)),
)
_PRODUCT_HEADER = re.compile(r"\bProduct\b|\bTitle\b", re.IGNORECASE)
#: "Description" on a seller's or Flipkart Internet's fee invoice; "Particulars"
#: on Flipkart India's transport (GTA) invoice, whose one row is "GT Charges".
_FEE_HEADER = re.compile(r"\bDescription\b|\bParticulars\b", re.IGNORECASE)
#: A rupee sign glued to a number ("₹587.00") on the transport invoice's rows.
_RUPEE_BEFORE_NUMBER = re.compile(r"₹\s*(?=-?\d)")
#: A bare HSN/SAC code standing alone in a cell (the transport invoice prints
#: the particulars first and the SAC code in the next cell, the other layouts
#: the other way round with a label).
_BARE_CODE = re.compile(r"^\d{4,8}$")
#: "Billed From <tab> Billed To" — the transport invoice names the seller and
#: the buyer side by side under this header instead of "Sold By" / "Bill To".
_BILLED_FROM_TO = re.compile(r"^\s*Billed\s+From\s*\t\s*Billed\s+To\s*$", re.IGNORECASE)
#: The Nov 2026 layout prints "Billed From" on a line of its own, the seller
#: on the next, and "Billed To" further down beside the shipping blocks.
_BILLED_FROM_ALONE = re.compile(r"^\s*Billed\s+From\s*:?\s*$", re.IGNORECASE)
#: Line titles named the way the owner's reference file names them (27 Nov
#: 2026): Flipkart prints "Protect Promise Fee" and "Delivery Charges" (the
#: goods-transport invoice from Flipkart India); the reference says "Protect
#: Promise" and "GT Charges".
_REFERENCE_NAMES = (
    (re.compile(r"^\s*protect\s+promise(?:\s+fee)?\s*$", re.IGNORECASE), "Protect Promise"),
    (
        re.compile(r"delivery\s+charges?|shipping\s+charges?|transport|\bGTA\b|^\s*GT\s+charges?\s*$", re.IGNORECASE),
        "GT Charges",
    ),
)
#: A handset's HSN chapter: a credit-note line carrying it reverses a product.
_PRODUCT_HSN = re.compile(r"^8517")

_FSN_LABEL = re.compile(r"^\s*FSN\s*:\s*(?:([A-Z0-9]{13,20})\b)?\s*", re.IGNORECASE)
_FSN_BARE = re.compile(r"^\s*([A-Z0-9]{16})\b\s*")
_HSN_LABEL = re.compile(r"^\s*(?:HSN\s*/\s*SAC|HSN|SAC)\s*:\s*(\d{4,8})\b\s*", re.IGNORECASE)
_IMEI = re.compile(r"IMEI[^:\]]*:\s*([0-9A-Za-z]+)", re.IGNORECASE)
#: A further IMEI after the first inside the same bracket — a dual-SIM handset
#: prints two ("[IMEI/Serial No: 8644…330, 8644…348 ]"): eight characters or
#: more with a digit among them, so a word that follows is never taken for one.
_IMEI_MORE = re.compile(r"(?<![0-9A-Za-z])(?=[0-9A-Za-z]*\d)[0-9A-Za-z]{8,}(?![0-9A-Za-z])")
#: "IGST: 18.0 %" under a product or fee row; "IGST 18.0 %" (no colon) under
#: the transport invoice's row. Neither joins the title.
_TAX_RATE = re.compile(
    r"^\s*(?:CGST|SGST|IGST|UTGST|SGST\s*/\s*UTGST|Cess)\b\s*(?:[^:\n]*:)?\s*" + _NUM + r"\s*%", re.IGNORECASE
)
_WARRANTY = re.compile(r"^\s*Warranty\b", re.IGNORECASE)
_TOTAL_ROW = re.compile(r"^\s*Total\s*$", re.IGNORECASE)
_GRAND_TOTAL_LINE = re.compile(r"^\s*Grand Total\b", re.IGNORECASE)


def _number(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def _date(text: str | None) -> date | None:
    if not text:
        return None
    for pattern in ("%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _first(pattern: re.Pattern, text: str) -> str | None:
    """The first match's captured value (the whole match when it captures nothing)."""
    match = pattern.search(text)
    if match is None:
        return None
    return (match.group(1) if pattern.groups else match.group(0)).strip()


def _collapse(text: str) -> str:
    """Spaces squeezed, tabs kept: a tab is the one thing that carries meaning."""
    return re.sub(r"[^\S\t]+", " ", text).strip()


def _split_left(text: str) -> tuple[str, str]:
    """``(product-column text, title-column text)`` of one table line.

    A tab marks the column boundary when the text came from the PDF's word
    positions; plain text has only the labels to go by, so the FSN and HSN
    tokens are peeled off the front and anything else is left with the title.
    """
    if "\t" in text:
        left, rest = text.split("\t", 1)
        return left.strip(), _collapse(rest.replace("\t", " "))
    left_parts: list[str] = []
    rest = text.strip()
    while rest:
        match = _FSN_LABEL.match(rest) or _FSN_BARE.match(rest) or _HSN_LABEL.match(rest)
        if match is None:
            break
        left_parts.append(match.group(0).strip())
        rest = rest[match.end() :]
    return " ".join(left_parts), rest.strip()


def _read_left(left: str, line: ParsedLine, state: dict) -> None:
    """The product column: a category, "FSN:" with the code on it or the next
    line, and "HSN/SAC: <code>". The category is not kept."""
    rest = left
    while rest:
        fsn_label = _FSN_LABEL.match(rest)
        if fsn_label:
            if fsn_label.group(1):
                line.fsn = fsn_label.group(1).upper()
            else:
                state["fsn_pending"] = True
            rest = rest[fsn_label.end() :]
            continue
        hsn = _HSN_LABEL.match(rest)
        if hsn:
            line.hsn_sac = hsn.group(1)
            rest = rest[hsn.end() :]
            continue
        bare = _FSN_BARE.match(rest)
        if bare and (state.get("fsn_pending") or line.fsn is None):
            line.fsn = bare.group(1).upper()
            state["fsn_pending"] = False
            rest = rest[bare.end() :]
            continue
        break


def _imeis_in(text: str) -> list[str]:
    """Every IMEI or serial number a title-column line prints, in order, each
    once: the one after each "IMEI…:" label, and any more that follow it
    inside the same bracket."""
    found: list[str] = []
    for match in _IMEI.finditer(text):
        tail = text[match.end() :].split("]", 1)[0]
        again = _IMEI.search(tail)
        if again:
            tail = tail[: again.start()]
        for imei in [match.group(1), *_IMEI_MORE.findall(tail)]:
            if imei and imei not in found:
                found.append(imei)
    return found


def _read_rest(rest: str, line: ParsedLine, state: dict) -> None:
    """The title column's extra lines: the title's wrap, then warranty text,
    IMEIs and the tax rates — only the wrap joins the title."""
    if not rest:
        return
    imeis = _imeis_in(rest)
    if imeis:
        known = [part.strip() for part in (line.imei or "").split(",") if part.strip()]
        line.imei = ", ".join(known + [imei for imei in imeis if imei not in known])
        state["title_open"] = False
        return
    if _TAX_RATE.match(rest):
        state["title_open"] = False
        return
    if _WARRANTY.match(rest):
        state["title_open"] = False
        return
    if state.get("title_open"):
        line.title = f"{line.title} {rest}".strip() if line.title else rest


def _header_columns(line: str) -> list[str] | None:
    """The numeric columns named in a table header line, left to right, or
    None when the line is not one (it needs at least Qty and Total)."""
    found = [(match.start(), name) for name, pattern in _COLUMNS if (match := pattern.search(line))]
    names = [name for _pos, name in sorted(found)]
    if "qty" not in names or "total" not in names or len(names) < 4:
        return None
    if not (_PRODUCT_HEADER.search(line) or _FEE_HEADER.search(line)):
        return None
    return names


def _parse_lines(lines: list[str], header_at: int, columns: list[str]) -> tuple[list[ParsedLine], float | None]:
    """The rows under the header, up to the Total row. Returns them and the
    Total row's last number (the grand total's fallback)."""
    count = len(columns)
    row_pattern = re.compile(r"^(?P<left>.*?)\s+(?P<nums>" + r"\s+".join([_NUM] * count) + r")\s*$")
    parsed: list[ParsedLine] = []
    current: ParsedLine | None = None
    state: dict = {}
    total_row_last: float | None = None
    for raw in lines[header_at + 1 :]:
        text = _RUPEE_BEFORE_NUMBER.sub("", _collapse(raw))
        if not text:
            continue
        if _GRAND_TOTAL_LINE.match(text):
            break
        row = row_pattern.match(text)
        if row:
            numbers = [_number(token) for token in row.group("nums").split()]
            left = row.group("left").strip()
            if _TOTAL_ROW.match(left.replace("\t", " ")):
                total_row_last = numbers[-1]
                break
            line = ParsedLine()
            product_cell, title = _split_left(left)
            if title and _BARE_CODE.match(title) and product_cell and not _labelled_code(product_cell):
                # Particulars first, the SAC code in the next cell (the
                # transport invoice): the words are the title, the code the SAC.
                line.hsn_sac = title
                product_cell, title = "", product_cell
            state = {"title_open": True, "fsn_pending": False}
            _read_left(product_cell, line, state)
            line.title = title or None
            for name, value in zip(columns, numbers):
                if name == "qty":
                    line.qty = int(value) if value is not None and float(value).is_integer() else None
                elif name != "cess":
                    setattr(line, name, value)
            parsed.append(line)
            current = line
            continue
        if current is None:
            continue  # the header's own wrapped words ("Amount ₹ /Coupons Value ₹")
        product_cell, rest = _split_left(text)
        _read_left(product_cell, current, state)
        _read_rest(rest, current, state)
    return parsed, total_row_last


def _labelled_code(cell: str) -> bool:
    """Whether a product-column cell is the usual labelled one ("FSN: …",
    "HSN/SAC: …", a bare 16-character FSN) rather than free words."""
    return bool(_FSN_LABEL.match(cell) or _HSN_LABEL.match(cell) or _FSN_BARE.match(cell))


def _from_to_names(lines: list[str]) -> tuple[str | None, str | None]:
    """``(seller, buyer)`` from a "Billed From <tab> Billed To" header: the
    next line's two cells, in that order. (None, None) on the other layouts."""
    for index, raw in enumerate(lines):
        if not _BILLED_FROM_TO.match(_collapse(raw)):
            continue
        for candidate in lines[index + 1 : index + 3]:
            cells = [c.strip() for c in _collapse(candidate).split("\t") if c.strip()]
            if len(cells) >= 2:
                return cells[0], cells[1]
        return None, None
    return None, None


def _fee_label(parsed_lines: list[ParsedLine]) -> str:
    """The fee invoice's type: its one line's title, or every distinct title
    joined in a fixed order when it carries several ("Credit Card Fee + Offer
    Handling Fee"), so the same document never shows up under two names."""
    titles = sorted({reference_name(line.title) for line in parsed_lines if line.title})
    return " + ".join(titles) if titles else TYPE_FEE


def reference_name(title: str | None) -> str | None:
    """A fee line's title as the owner's reference file names it."""
    if not title:
        return title
    for pattern, name in _REFERENCE_NAMES:
        if pattern.search(title):
            return name
    return title.strip()


def _credit_note_label(parsed_lines: list[ParsedLine]) -> str:
    """"<what it reverses> Credit Note", as the reference file names them:
    "Product Credit Note" for a handset line (an FSN, or a handset HSN), else
    the fee's own name ("Protect Promise Credit Note"). A note with no
    readable line stays a plain "Credit Note"."""
    if any(line.fsn or _PRODUCT_HSN.match(line.hsn_sac or "") for line in parsed_lines):
        return f"{TYPE_PRODUCT} {TYPE_CREDIT_NOTE}"
    names = sorted({reference_name(line.title) for line in parsed_lines if line.title})
    return f"{' + '.join(names)} {TYPE_CREDIT_NOTE}" if names else TYPE_CREDIT_NOTE


def _billed_from_name(lines: list[str]) -> str | None:
    """The seller of a "Billed From" block printed on a line of its own: the
    first cell of the next line with words on it."""
    for index, raw in enumerate(lines):
        if not _BILLED_FROM_ALONE.match(_collapse(raw)):
            continue
        for candidate in lines[index + 1 : index + 3]:
            cells = [c.strip() for c in _collapse(candidate).split("\t") if c.strip()]
            if cells:
                return cells[0]
        return None
    return None


def _buyer_name(lines: list[str]) -> str | None:
    """The first line of the Bill To (or Billing Address) block.

    It shares its line with the order id or the order date on the left and,
    on a product invoice, with the Ship To block on the right; a tab (from the
    word positions) separates the blocks, and failing that a name printed
    twice — billed to and shipped to the same business — is read once.
    """
    for index, raw in enumerate(lines):
        if not _BILL_TO.search(raw):
            continue
        two_blocks = bool(_SHIP_TO.search(raw))
        for candidate in lines[index + 1 : index + 3]:
            text = candidate.strip()
            if not text:
                continue
            if "\t" in text:
                cells = [c.strip() for c in text.split("\t") if c.strip()]
                cells = [c for c in cells if not _BUYER_LINE_PREFIX.fullmatch(c)]
                return cells[0] if cells else None
            text = _BUYER_LINE_PREFIX.sub("", text, count=1).strip()
            if not text:
                continue
            if two_blocks:
                words = text.split()
                half = len(words) // 2
                if len(words) % 2 == 0 and words[:half] == words[half:]:
                    text = " ".join(words[:half])
            return text or None
        return None
    return None


def parse_invoice_text(text: str) -> ParsedInvoice:
    """Read one invoice from its text. Pure: no file, no PDF library."""
    out = ParsedInvoice()
    if not text or not text.strip():
        out.error = "the PDF has no text layer"
        return out
    lines = text.splitlines()
    flat = "\n".join(_collapse(line).replace("\t", " ") for line in lines)

    out.seller_name = _first(_SOLD_BY, flat)
    out.invoice_number = _first(_INVOICE_NUMBER, flat)
    out.order_id = _first(_ORDER_ID, flat)
    out.order_date = _date(_first(_ORDER_DATE, flat))
    out.invoice_date = _date(_first(_INVOICE_DATE, flat))
    out.grand_total = _number(_first(_GRAND_TOTAL, flat) or "")
    gstins = list(dict.fromkeys(_GSTIN.findall(flat)))
    if gstins:
        out.seller_gstin = gstins[0]
        others = [g for g in gstins[1:] if g != out.seller_gstin]
        out.buyer_gstin = others[0] if others else None
    out.buyer_name = _buyer_name(lines)
    if out.seller_name is None or out.buyer_name is None:
        billed_from, billed_to = _from_to_names(lines)
        out.seller_name = out.seller_name or billed_from
        out.buyer_name = out.buyer_name or billed_to
    if out.seller_name is None:
        out.seller_name = _billed_from_name(lines)

    head = "\n".join(flat.splitlines()[:6])
    is_credit_note = bool(_CREDIT_NOTE.search(head))
    if is_credit_note:
        out.kind = KIND_CREDIT_NOTE
        out.invoice_type = TYPE_CREDIT_NOTE
        out.original_invoice = _first(_ORIGINAL_INVOICE, flat)
        out.reason = _first(_REASON, flat)

    header_at = columns = None
    for index, raw in enumerate(lines):
        columns = _header_columns(_collapse(raw).replace("\t", " "))
        if columns is not None:
            header_at = index
            break
    if header_at is not None:
        header_text = _collapse(lines[header_at]).replace("\t", " ")
        out.lines, total_row_last = _parse_lines(lines, header_at, columns)
        if out.grand_total is None:
            out.grand_total = total_row_last
        if is_credit_note:
            out.invoice_type = _credit_note_label(out.lines)
        else:
            if _PRODUCT_HEADER.search(header_text):
                out.kind, out.invoice_type = KIND_PRODUCT, TYPE_PRODUCT
            else:
                out.kind = KIND_FEE
                out.invoice_type = _fee_label(out.lines)

    missing = []
    if out.kind == KIND_UNKNOWN:
        missing.append("no invoice, fee or credit note layout recognised")
    else:
        if not out.invoice_number:
            missing.append("no invoice number")
        if not out.order_id:
            missing.append("no order id")
        if not out.lines and out.kind != KIND_CREDIT_NOTE:
            missing.append("no invoice lines")
    out.error = "; ".join(missing) or None
    return out


# --- the PDF ------------------------------------------------------------------------
def _page_lines(page) -> list[str]:
    """The page's text, one string per visual line, words joined by a space
    and cells (a wide gap) by a tab."""
    from pdfplumber.utils import cluster_objects

    words = page.extract_words(x_tolerance=3, y_tolerance=3)
    if not words:
        return []
    out: list[str] = []
    for cluster in cluster_objects(words, itemgetter("top"), 3):
        row = sorted(cluster, key=itemgetter("x0"))
        parts = [row[0]["text"]]
        for previous, word in zip(row, row[1:]):
            parts.append(("\t" if word["x0"] - previous["x1"] > CELL_GAP else " ") + word["text"])
        out.append("".join(parts))
    return out


class ReadLimitExceeded(Exception):
    """The PDF is past one of the reader's limits (:data:`MAX_PAGES`,
    :data:`MAX_READ_SECONDS`); the message is the reason the row keeps."""


def _count_pages(document, stop_at: int) -> int:
    """How many pages the document has, counted no further than ``stop_at``.
    pdfminer walks the page tree lazily, so a file of a hundred thousand
    pages costs no more to count this far than one of ``stop_at``."""
    from pdfminer.pdfpage import PDFPage

    return sum(1 for _page in itertools.islice(PDFPage.create_pages(document), stop_at))


def _declared_pages(document) -> int | None:
    """The page count the file states in its page tree, for the message only:
    the walk decides, since a file can state anything."""
    from pdfminer.pdftypes import resolve1

    try:
        count = resolve1(resolve1(document.catalog.get("Pages")).get("Count"))
    except Exception:  # noqa: BLE001 — a count it cannot state is simply not quoted
        return None
    return count if isinstance(count, int) and not isinstance(count, bool) else None


def extract_text(data: bytes) -> str:
    """Every page's text, cells separated by tabs (see :func:`_page_lines`).
    Raises on anything that is not a readable PDF, and
    :class:`ReadLimitExceeded` on one past the reader's limits: more than
    :data:`MAX_PAGES` pages (refused before any page is read), or still being
    read after :data:`MAX_READ_SECONDS` (stopped before the next page)."""
    import pdfplumber

    started = _clock()
    pdf = pdfplumber.open(io.BytesIO(data))
    if _count_pages(pdf.doc, MAX_PAGES + 1) > MAX_PAGES:
        # Left without pdfplumber's close(): it lists every page of the file
        # first, the very walk the cap is there to spare. Nothing is open but
        # the bytes in memory, let go with ``pdf``.
        declared = _declared_pages(pdf.doc)
        count = declared if declared is not None and declared > MAX_PAGES else f"more than {MAX_PAGES}"
        raise ReadLimitExceeded(f"too many pages to read: {count}")
    lines: list[str] = []
    with pdf:
        for page in pdf.pages:
            if _clock() - started > MAX_READ_SECONDS:
                raise ReadLimitExceeded(f"{TOOK_TOO_LONG}: over {MAX_READ_SECONDS:g} seconds")
            lines.extend(_page_lines(page))
    return "\n".join(lines)


def parse_invoice(data: bytes) -> ParsedInvoice:
    """Read an invoice PDF. Never raises: an unreadable file — or one past the
    reader's limits — is kind "unknown" with the reason in ``error``."""
    try:
        text = extract_text(data)
    except ReadLimitExceeded as exc:
        log.warning("invoice PDF not read: %s", exc)
        return ParsedInvoice(error=str(exc))
    except Exception as exc:  # noqa: BLE001 — the reason is recorded, the file is kept
        reason = str(exc).strip() or exc.__class__.__name__
        return ParsedInvoice(error=f"could not read the PDF: {reason[:200]}")
    try:
        return parse_invoice_text(text)
    except Exception as exc:  # noqa: BLE001 — a layout the parser never met
        log.exception("invoice text could not be parsed")
        return ParsedInvoice(error=f"could not parse the invoice text: {exc}"[:300])
