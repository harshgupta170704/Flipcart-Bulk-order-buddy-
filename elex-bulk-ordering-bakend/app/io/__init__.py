"""The files the app reads in and writes out: card and account sheets, lists
of order ids, order exports, invoices.

One of openpyxl's warnings is silenced here, once, for all of them. When a
number sits in a date-styled cell and is too big to be a date, which every
card number is, openpyxl warns "Cell B2 is marked as a date but the serial
value 4111… is outside the limits for dates", quoting the value whole, and
Python prints a warning to stderr: the terminal, or the container's log. A
card sheet (``app.io.cards``) and an account sheet's legacy card columns
(``app.io.excel``) both pass through that reader, and importing either module
runs this first. Only that one message is ignored; every other warning still
prints. It is one filter set at import rather than a ``catch_warnings`` block
around each parse, because that block swaps the whole process's filter list,
which is not safe with the server's other threads.
"""

import warnings

warnings.filterwarnings(
    "ignore",
    message=r"Cell \S+ is marked as a date but the serial value",
    category=UserWarning,
    module=r"openpyxl\.worksheet\._reader",
)
