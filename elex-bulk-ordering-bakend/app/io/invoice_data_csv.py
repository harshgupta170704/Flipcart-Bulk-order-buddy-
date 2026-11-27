"""The invoice-data CSV: the spec's ``invoice_data_<date>_<HHMM>.csv`` (FR-027/028).

One row per invoice line of the ticked invoice types, for the invoices of the
orders whose units match the ledger filter. The values are what the parser
read off the PDF (app.io.invoice_pdf), plus the ledger's own product name
beside the invoice's ("BoB Product Name").

The layout is the owner's reference file, ``invoice_data_2026-10-28_1532.csv``
(27 Nov 2026), byte for byte: no BOM, CRLF line ends, ISO ``YYYY-MM-DD``
dates, two decimals on money and on Qty, HSN as plain digits, and an IMEI
written after a tab so a spreadsheet keeps every digit rather than rounding
it. A long all-digit invoice or order number is written the same way. Every
other cell still passes the formula guard (app.io.orders_csv.safe_cell).
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Iterator
from datetime import date, datetime

from app.db.models import Invoice, Order
from app.io.invoice_pdf import KIND_FEE, KIND_PRODUCT, reference_name
from app.io.orders_csv import IST, money, safe_cell, to_ist

#: The owner's reference file's header, in its order (FR-028).
HEADERS = [
    "Invoice Type",
    "Invoice Number",
    "Order ID",
    "Invoice Date",
    "Order Date",
    "Original Invoice Number",
    "Reason Of Issuance",
    "IMEI",
    "Invoice Product Name",
    "BoB Product Name",
    "Qty",
    "FSN",
    "HSN",
    "Taxable Amount",
    "IGST",
    "SGST",
    "CGST",
    "Total Amount",
    "Buyer Name",
    "Buyer GST",
    "Seller Name",
    "Seller GST",
]

#: A cell this module wrote as tab + digits: an identifier kept as text. It
#: passes the formula guard as it is (a tab followed by digits runs nothing).
_TAB_IDENTIFIER = re.compile(r"\t\d+")
_DIGITS = re.compile(r"\d+")
#: From this many digits a spreadsheet would show a number in E-notation.
_LONG = 12


def export_filename(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(IST)).astimezone(IST)
    return f"invoice_data_{stamp:%Y-%m-%d_%H%M}.csv"


def fmt_day(value: datetime | date | None) -> str:
    """A printed date (the invoice's own, not an instant): no clock to convert."""
    return "" if value is None else value.strftime("%Y-%m-%d")


def order_day(invoice: Invoice, order: Order) -> str:
    """The Order Date cell: the day printed on the invoice; failing that the
    ledger's own ``placed_at``, which is an instant in UTC and so is turned
    into India's day the way the order-units export does — the two files must
    name the same day for an order placed before 05:30."""
    if invoice.order_date is not None:
        return fmt_day(invoice.order_date)
    local = to_ist(order.placed_at)
    return local.strftime("%Y-%m-%d") if local else ""


def _num(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def imei_cell(value: object) -> str:
    """An IMEI as the reference file writes it: a tab, then the digits."""
    text = _text(value)
    return f"\t{text}" if _DIGITS.fullmatch(text) else text


def id_cell(value: object) -> str:
    """An invoice or order number: as printed, unless it is a long run of
    digits only, which goes after a tab like an IMEI so no digit is lost."""
    text = _text(value)
    return f"\t{text}" if len(text) >= _LONG and _DIGITS.fullmatch(text) else text


def guard(cell: str) -> str:
    """The formula guard, which leaves this module's tab identifiers alone."""
    return cell if _TAB_IDENTIFIER.fullmatch(cell) else safe_cell(cell)


def rows_for(invoice: Invoice, order: Order) -> list[list[str]]:
    """The CSV rows of one invoice: one per parsed line, in :data:`HEADERS`
    order. An invoice of a ticked type with no readable line still gets one
    row — its header fields and grand total — so it is seen rather than
    silently missing from the file."""
    lines = list(invoice.lines or []) or [{}]
    is_product = invoice.kind == KIND_PRODUCT
    is_fee = invoice.kind == KIND_FEE
    head = [
        id_cell(invoice.invoice_number),
        id_cell(invoice.order_id or order.order_id),
        fmt_day(invoice.invoice_date),
        order_day(invoice, order),
        id_cell(invoice.original_invoice),
        _text(invoice.reason),
    ]
    # The reference file's buyer is the business the order was billed to, in
    # capitals, on every row — a transport invoice prints only the person.
    # (app.core.invoices.data_pairs looks the business up for each order.)
    tail = [
        (_text(getattr(order, "invoice_buyer_name", None)) or _text(invoice.buyer_name)).upper(),
        _text(invoice.buyer_gstin) or _text(getattr(order, "invoice_buyer_gstin", None)) or _text(getattr(order, "gstin", None)),
        _text(invoice.seller_name),
        _text(invoice.seller_gstin),
    ]
    out: list[list[str]] = []
    for line in lines:
        get = line.get if isinstance(line, dict) else lambda _key, _default=None: None
        total = _num(get("total"))
        if not line:
            total = invoice.grand_total
        title = _text(get("title"))
        # A fee invoice of several lines ("Corporate Card Fee + Offer Handling
        # Fee") is a row per fee, each under its own name.
        kind = (reference_name(title) if is_fee and title and len(lines) > 1 else None) or _text(invoice.invoice_type)
        # The phone on every row, as in the reference file: a fee or a credit
        # note names the order's product as its product invoice prints it
        # (app.core.invoices.data_pairs looks that up), not itself.
        printed = _text(getattr(order, "invoice_product_title", None))
        product = (title or printed or _text(order.model)) if is_product else (printed or _text(order.model) or title)
        # The reference's tax columns: "IGST" is the line's whole GST — the
        # IGST of an inter-state supply, SGST + CGST of an intra-state one —
        # and SGST / CGST are the split, 0.00 when there is none. A row with
        # no readable line says nothing about tax.
        igst, sgst, cgst = _num(get("igst")), _num(get("sgst")), _num(get("cgst"))
        if line:
            gst = igst if igst is not None else round((sgst or 0.0) + (cgst or 0.0), 2)
            taxes = [money(gst), money(sgst or 0.0), money(cgst or 0.0)]
        else:
            taxes = ["", "", ""]
        out.append(
            [kind]
            + head
            + [
                imei_cell(get("imei")),
                product,
                _text(order.model),
                money(_num(get("qty"))),
                _text(get("fsn")),
                _text(get("hsn_sac")),
                money(_num(get("taxable"))),
                *taxes,
                money(total),
            ]
            + tail
        )
    return out


#: The text goes out in pieces of about this many characters rather than one
#: piece per invoice: each piece is a hop to a worker thread for the server,
#: and a file of fifty thousand invoices would be fifty thousand hops.
FLUSH_CHARS = 64 * 1024


def row_count(pairs: Iterable[tuple[Invoice, Order]]) -> int:
    return sum(max(1, len(invoice.lines or [])) for invoice, _order in pairs)


def stream(pairs: Iterable[tuple[Invoice, Order]]) -> Iterator[str]:
    """Yield the CSV text, header first, then every invoice's rows, as the
    pairs arrive — in pieces of about :data:`FLUSH_CHARS`, so only about that
    much text is held at a time."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(HEADERS)
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate()
    for invoice, order in pairs:
        writer.writerows([guard(cell) for cell in row] for row in rows_for(invoice, order))
        if buffer.tell() >= FLUSH_CHARS:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    if buffer.tell():
        yield buffer.getvalue()
