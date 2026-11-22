"""The invoice-data CSV: the spec's ``invoice_data_<date>_<HHMM>.csv`` (FR-027/028).

One row per invoice line of the ticked invoice types, for the invoices of the
orders whose units match the ledger filter. The values are what the parser
read off the PDF (app.io.invoice_pdf), plus the ledger's own product name
beside the invoice's ("Ledger Product"). The same Excel care as the ledger's
export (app.io.orders_csv): a BOM so Excel reads UTF-8, DD-MM-YYYY dates, two
decimals on money, and IMEIs written so a spreadsheet keeps every digit.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from datetime import date, datetime

from app.db.models import Invoice, Order
from app.io.orders_csv import IST, fmt_date, identifier_cell, money, safe_row, text_guard

#: ui-evidence X2 / the Invoice data model, in the recorded order (FR-028).
HEADERS = [
    "Invoice Type",
    "Invoice Number",
    "Order ID",
    "Invoice Date",
    "Order Date",
    "Original Invoice Number",
    "Reason",
    "IMEI",
    "Invoice Product",
    "Ledger Product",
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


def export_filename(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(IST)).astimezone(IST)
    return f"invoice_data_{stamp:%Y-%m-%d_%H%M}.csv"


def fmt_day(value: datetime | date | None) -> str:
    """A printed date (the invoice's own, not an instant): no clock to convert."""
    return "" if value is None else value.strftime("%d-%m-%Y")


def order_day(invoice: Invoice, order: Order) -> str:
    """The Order Date cell: the day printed on the invoice; failing that the
    ledger's own ``placed_at``, which is an instant in UTC and so is turned
    into India's day the way the order-units export does (orders_csv.fmt_date)
    — the two files must name the same day for an order placed before 05:30."""
    if invoice.order_date is not None:
        return fmt_day(invoice.order_date)
    return fmt_date(order.placed_at)


def _num(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _qty(value: object) -> str:
    number = _num(value)
    return "" if number is None else str(int(number)) if float(number).is_integer() else str(number)


def _text(value: object) -> str:
    return "" if value is None else str(value)


def rows_for(invoice: Invoice, order: Order) -> list[list[str]]:
    """The CSV rows of one invoice: one per parsed line, in :data:`HEADERS`
    order. An invoice of a ticked type with no readable line still gets one
    row — its header fields and grand total — so it is seen rather than
    silently missing from the file."""
    lines = list(invoice.lines or []) or [{}]
    head = [
        invoice.invoice_type or "",
        text_guard(invoice.invoice_number),
        text_guard(invoice.order_id or order.order_id),
        fmt_day(invoice.invoice_date),
        order_day(invoice, order),
        text_guard(invoice.original_invoice),
        invoice.reason or "",
    ]
    tail = [
        invoice.buyer_name or "",
        invoice.buyer_gstin or "",
        invoice.seller_name or "",
        invoice.seller_gstin or "",
    ]
    out: list[list[str]] = []
    for line in lines:
        get = line.get if isinstance(line, dict) else lambda _key, _default=None: None
        total = _num(get("total"))
        if not line:
            total = invoice.grand_total
        out.append(
            head
            + [
                identifier_cell(_text(get("imei")) or None),
                _text(get("title")),
                order.model or "",
                _qty(get("qty")),
                text_guard(_text(get("fsn")) or None),
                identifier_cell(_text(get("hsn_sac")) or None),
                money(_num(get("taxable"))),
                money(_num(get("igst"))),
                money(_num(get("sgst"))),
                money(_num(get("cgst"))),
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
    much text is held at a time. The BOM up front is what makes Excel read
    UTF-8."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(HEADERS)
    yield "﻿" + buffer.getvalue()
    buffer.seek(0)
    buffer.truncate()
    for invoice, order in pairs:
        writer.writerows(safe_row(row) for row in rows_for(invoice, order))
        if buffer.tell() >= FLUSH_CHARS:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    if buffer.tell():
        yield buffer.getvalue()
