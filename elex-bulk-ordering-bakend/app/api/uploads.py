"""A sheet from an upload form, read in memory and never written to disk.

Two routes take a whole spreadsheet as a form upload: the account import
(``POST /accounts/import``) and the sheet of order ids to track
(``POST /orders/import-ids``). Either can be handed the owner's workbook in
the legacy layout, whose columns T to W hold every account's full card
number, expiry and CVV (PLAN.md §1). Neither route reads those columns, but
they are in the upload, and card details are never written to disk (PLAN.md
§5). Starlette's usual form reader, the one FastAPI runs for an
``UploadFile = File(...)`` parameter before the handler is called, writes a
file part over 1 MiB to a temp file, and the workbook of every account
(~37,000) is about 2 MB.

So these routes take the request itself, and the form is read here, by that
same reader told to keep a file part in memory up to the most the upload
guard (``app.api.jobs.CardUploadLimit``) lets a sheet upload declare. The
guard refuses, unread, a body over that or one that declares no size, and the
server never reads past the size a request declares, so no file part can
outgrow memory. nginx streams the two routes to the API for the same reason
(``frontend/nginx.conf``).
"""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.api.jobs import SHEET_UPLOAD_LIMIT

#: The form field the page sends a sheet in (``importAccounts`` and
#: ``importOrderIds`` in frontend/src/lib/api.ts).
SHEET_FIELD = "file"


class InMemoryForm(MultiPartParser):
    """Starlette's multipart reader, keeping a file part in memory up to the
    whole of what the guard lets a sheet upload declare: a part, being a
    piece of that body, never reaches the size at which it would go to disk."""

    spool_max_size = SHEET_UPLOAD_LIMIT


@dataclass(frozen=True)
class SheetUpload:
    """The sheet an upload carried: the name it was sent under, and its bytes."""

    filename: str
    data: bytes


async def read_sheet(request: Request, field: str = SHEET_FIELD) -> SheetUpload:
    """The file the upload form carries in ``field``, read in memory.

    A body that is not a form upload is answered 415, one that cannot be read
    as one 400, and a form with no file in ``field`` 422, the sheet unread.
    The form is closed before this returns, whatever happens.
    """
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "multipart/form-data":
        raise HTTPException(
            status_code=415,
            detail=f'Send the sheet as a form upload (multipart/form-data), in a field named "{field}".',
        )
    try:
        async with aclosing(request.stream()) as stream:
            form = await InMemoryForm(request.headers, stream).parse()
    except MultiPartException as exc:
        # Starlette's own words ("Invalid multipart data.", "Missing boundary
        # in multipart."): they name the problem, never a byte of the file.
        raise HTTPException(status_code=400, detail=exc.message) from None
    try:
        upload = form.get(field)
        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=422, detail=f'The upload has no file in its "{field}" field.')
        return SheetUpload(filename=upload.filename or "", data=await upload.read())
    finally:
        await form.close()
