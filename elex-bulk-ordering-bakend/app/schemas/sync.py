"""Request shape for the post-order sync run (ROADMAP §10).

The response is ``app.schemas.job.JobOut`` — a sync is an ordinary ``Job``, so
it is polled through ``GET /jobs/{id}`` and followed over ``/ws/jobs/{id}``
like every other run. Nothing new is needed on the way out.

``AccountFilterIn`` is imported, never re-declared: "which accounts?" has one
meaning across this API (``app.core.account_filter``), and a second copy of the
shape here would be free to drift from the one the other three start endpoints
resolve.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.account import MAX_LISTED_IDS, RowId

# The jobs flavour of the filter, not the accounts one: it carries
# ``as_provenance()``, which is what records on the Job *how* the accounts were
# chosen, and it is the shape ``app.api.jobs._resolve_selection`` consumes.
from app.schemas.job import AccountFilterIn

#: How many of an account's most recent orders one run reads. Twenty covers the
#: first page of the orders list, which is where anything still moving is.
DEFAULT_LIMIT_PER_ACCOUNT = 20

#: Hard ceiling per account. Each order costs a navigation to its own detail
#: page (that is where the ids and the OTP live), so an unbounded limit turns
#: one run over 500 accounts into tens of thousands of page loads.
MAX_LIMIT_PER_ACCOUNT = 200


class SyncJobRequest(BaseModel):
    """Body of ``POST /jobs/sync``.

    ``extra="forbid"`` for the same reason as every other selection body here:
    a mistyped ``account_filter`` would be dropped silently and the run would
    widen from a handful of accounts to all of them, and a mistyped
    ``import_unknown`` would quietly not import. Both are failures that look
    like success, so they are a 422 instead.
    """

    model_config = ConfigDict(extra="forbid")

    #: null/empty means "every account" — the same convention as the other runs.
    #: Bounded like every other run's list (audit F-40).
    account_ids: list[RowId] | None = Field(default=None, max_length=MAX_LISTED_IDS)
    account_filter: AccountFilterIn | None = None
    limit_per_account: int = Field(
        default=DEFAULT_LIMIT_PER_ACCOUNT, ge=1, le=MAX_LIMIT_PER_ACCOUNT
    )
    #: Create rows for orders this tool never placed. Off by default: see
    #: ``app.jobs.sync_tasks._imported_order`` for why that default matters.
    import_unknown: bool = False
    #: Read exactly these orders, by id, instead of each account's recent list
    #: — for ids handed in to track (``POST /orders/import-ids``) or older than
    #: the list's window. With no account selection the accounts are the ones
    #: holding these orders in the ledger.
    #: Capped where the id sheet is capped (app.io.order_ids.MAX_ROWS), so a
    #: sheet that imported can always be read back in one run.
    order_ids: list[str] | None = Field(default=None, max_length=5_000)
