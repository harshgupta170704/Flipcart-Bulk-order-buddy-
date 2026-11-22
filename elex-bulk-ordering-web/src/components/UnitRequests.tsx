/**
 * One unit's cancel and reschedule requests (spec FR-020), as the row dialog
 * lists them: the kind and its status, the reason or the new date wanted, who
 * asked and when, and who closed it, when, with their note. Read once when the
 * dialog opens — the order's requests, of which this unit's are shown — never
 * with every row of the ledger. Nothing here acts on a request: that is the
 * Requests page's job.
 */
import { useEffect, useState } from "react";
import { RequestHistory } from "./RequestHistory";
import { errorMessage, listOrderRequests } from "../lib/api";
import { formatDate, formatDateTime } from "../lib/format";
import type { Order, UnitRequest, UnitRequestKind, UnitRequestStatus } from "../lib/types";
import { Alert } from "./Alert";
import { StatusBadge } from "./StatusBadge";

const KIND_LABEL: Record<UnitRequestKind, string> = { cancel: "Cancel", reschedule: "Reschedule" };

/** How a closed request is worded, by who closed it. */
const CLOSED_AS: Record<Exclude<UnitRequestStatus, "open">, string> = { done: "Marked done", dropped: "Dropped" };

/** The request's status as the Requests page badges it. */
function statusBadge(status: UnitRequestStatus) {
  switch (status) {
    case "done":
      return <StatusBadge status="done" label="Done" />;
    case "dropped":
      return <StatusBadge status="skipped" label="Dropped" />;
    default:
      return <StatusBadge status="pending" label="Open" />;
  }
}

function RequestItem({ row }: { row: UnitRequest }) {
  return (
    <li className="flex flex-col gap-0.5">
      <span className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-ink">{KIND_LABEL[row.kind] ?? row.kind}</span>
        {statusBadge(row.status)}
      </span>
      {row.kind === "reschedule" ? <span className="text-[12px] text-ink">New date: {formatDate(row.requested_for)}</span> : null}
      {row.reason ? <span className="text-[12px] leading-snug text-ink">{row.reason}</span> : null}
      <span className="text-[12px] text-ink-2">
        Requested by {row.requested_by} on {formatDateTime(row.requested_at)}
      </span>
      {row.status !== "open" ? (
        <span className="text-[12px] text-ink-2">
          {CLOSED_AS[row.status] ?? row.status}
          {row.closed_by ? ` by ${row.closed_by}` : ""}
          {row.closed_at ? ` on ${formatDateTime(row.closed_at)}` : ""}
        </span>
      ) : null}
      {row.note ? <span className="text-[12px] text-ink-2">Note: “{row.note}”</span> : null}
      <RequestHistory steps={row.history ?? []} />
    </li>
  );
}

export function UnitRequests({ order }: { order: Pick<Order, "id" | "order_row_id"> }) {
  const rowId = order.order_row_id ?? order.id;
  const unitId = order.id;
  const [rows, setRows] = useState<UnitRequest[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setRows(null);
    setError(null);
    listOrderRequests(rowId)
      .then((list) => {
        // The order's requests, newest first: this row is one unit of it.
        if (live) setRows((Array.isArray(list) ? list : []).filter((row) => row.unit_pk === unitId));
      })
      .catch((err) => {
        if (live) {
          setRows([]);
          setError(errorMessage(err));
        }
      });
    return () => {
      live = false;
    };
  }, [rowId, unitId]);

  if (error) return <Alert tone="error">{error}</Alert>;
  if (rows === null) return <span className="text-muted">Loading…</span>;
  if (rows.length === 0) return <span className="text-muted">No cancel or reschedule request</span>;
  return (
    <ul className="flex flex-col gap-2">
      {rows.map((row) => (
        <RequestItem key={row.id} row={row} />
      ))}
    </ul>
  );
}

export default UnitRequests;
