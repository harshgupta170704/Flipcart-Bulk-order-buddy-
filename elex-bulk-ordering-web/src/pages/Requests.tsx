/**
 * Requests — the cancel / reschedule log (spec FR-020, decision C-4).
 *
 * A row is a request someone recorded from Order Units: cancel this unit, or
 * move its delivery to this date, with the reason and who asked. Nothing here
 * talks to Flipkart. A person does it on the platform by hand and then marks
 * the row done — or drops it when it is no longer wanted — and a closed row
 * can be reopened. Open is what the page is opened for, so it is the default.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { errorMessage, listUnitRequests, updateUnitRequest } from "../lib/api";
import { countLabel, formatDate, formatDateTime, formatRelative } from "../lib/format";
import type { UnitRequest, UnitRequestKind, UnitRequestStatus } from "../lib/types";
import { AccountsPager } from "../components/AccountsPager";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Dialog } from "../components/Dialog";
import { EmptyState } from "../components/EmptyState";
import { Field, Select, Textarea } from "../components/Field";
import { RequestHistory } from "../components/RequestHistory";
export { RequestHistory };
import { RunError, RunLink, RunLoading } from "../components/RunStates";
import { StatusBadge } from "../components/StatusBadge";
import { Table, type Column } from "../components/Table";

/** Which rows are on screen. Open is what a queue is for. */
type View = UnitRequestStatus | "all";

const VIEWS: { value: View; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "done", label: "Done" },
  { value: "dropped", label: "Dropped" },
  { value: "all", label: "All" },
];

const KINDS: { value: UnitRequestKind | ""; label: string }[] = [
  { value: "", label: "Cancel and reschedule" },
  { value: "cancel", label: "Cancel" },
  { value: "reschedule", label: "Reschedule" },
];

const KIND_LABEL: Record<UnitRequestKind, string> = { cancel: "Cancel", reschedule: "Reschedule" };

const PAGE_SIZE = 50;

function toView(value: string): View {
  return VIEWS.some((view) => view.value === value) ? (value as View) : "open";
}

function toKind(value: string): UnitRequestKind | "" {
  return value === "cancel" || value === "reschedule" ? value : "";
}

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

function truncated(value: string | null, width: string, fallback = "—") {
  if (!value) return <span className="text-muted">{fallback}</span>;
  return (
    <span className="block truncate" style={{ maxWidth: width }} title={value}>
      {value}
    </span>
  );
}

/** The one dialog: marking a row done or dropping it, with an optional note. */
interface Closing {
  row: UnitRequest;
  status: "done" | "dropped";
}

export default function Requests() {
  const [view, setView] = useState<View>("open");
  const [kind, setKind] = useState<UnitRequestKind | "">("");
  const [page, setPage] = useState(1);
  /** null until a load has succeeded — "not loaded yet" is not "nothing open". */
  const [rows, setRows] = useState<UnitRequest[] | null>(null);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [busyRow, setBusyRow] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [closing, setClosing] = useState<Closing | null>(null);
  const [closingNote, setClosingNote] = useState("");
  const [closingError, setClosingError] = useState<string | null>(null);

  const alive = useRef(true);
  /* An action reloads the list, so two loads can be in the air; only the newest lands. */
  const ticket = useRef(0);

  useEffect(() => {
    document.title = "Requests — Ordering Suite";
  }, []);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(() => {
    ticket.current += 1;
    const mine = ticket.current;
    setLoading(true);
    listUnitRequests({ status: view, kind: kind === "" ? undefined : kind, page, size: PAGE_SIZE }).then(
      (result) => {
        if (!alive.current || ticket.current !== mine) return;
        setRows(Array.isArray(result?.items) ? result.items : []);
        setTotal(typeof result?.total === "number" ? result.total : 0);
        setError(null);
        setLoading(false);
      },
      (err: unknown) => {
        if (!alive.current || ticket.current !== mine) return;
        setError(errorMessage(err));
        setLoading(false);
      },
    );
  }, [view, kind, page]);

  useEffect(() => {
    load();
  }, [load]);

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  // The total can shrink under us (a row marked done): never stay past the last page.
  useEffect(() => {
    if (rows !== null && page > pages) setPage(pages);
  }, [rows, page, pages]);

  const changeView = (next: View) => {
    setView(next);
    setPage(1);
  };
  const changeKind = (next: UnitRequestKind | "") => {
    setKind(next);
    setPage(1);
  };
  const filtered = view !== "open" || kind !== "";
  const clearFilters = () => {
    changeView("open");
    setKind("");
  };

  const settle = useCallback(() => {
    setBusyRow(null);
    load();
  }, [load]);

  const onClose = async (target: Closing, text: string) => {
    setBusyRow(target.row.id);
    setClosingError(null);
    setActionError(null);
    setNote(null);
    try {
      await updateUnitRequest(target.row.id, target.status, text);
      if (!alive.current) return;
      // Closed only now: a failed request keeps the dialog, and the note, open.
      setClosing(null);
      setClosingNote("");
      setNote(
        target.status === "done"
          ? `Marked done: ${KIND_LABEL[target.row.kind].toLowerCase()} ${target.row.order_id ?? `#${target.row.unit_pk}`}. The row stays on file under Done.`
          : `Dropped: ${KIND_LABEL[target.row.kind].toLowerCase()} ${target.row.order_id ?? `#${target.row.unit_pk}`}. The unit stays as it is.`,
      );
      settle();
    } catch (err) {
      if (!alive.current) return;
      setClosingError(errorMessage(err));
      setBusyRow(null);
    }
  };

  // A callback, not a plain closure: the columns below are memoised, and a
  // Reopen must reload the filter that is on screen now, not the one at mount.
  const onReopen = useCallback(
    async (row: UnitRequest) => {
      setBusyRow(row.id);
      setActionError(null);
      setNote(null);
      try {
        await updateUnitRequest(row.id, "open");
        if (!alive.current) return;
        setNote(`${KIND_LABEL[row.kind]} ${row.order_id ?? `#${row.unit_pk}`} is open again.`);
        settle();
      } catch (err) {
        if (!alive.current) return;
        setActionError(errorMessage(err));
        setBusyRow(null);
      }
    },
    [settle],
  );

  const closeDialog = () => {
    if (busyRow !== null) return;
    setClosing(null);
    setClosingError(null);
  };

  const columns = useMemo<Column<UnitRequest>[]>(
    () => [
      {
        key: "requested_at",
        header: "When",
        width: "150px",
        render: (row) => (
          <span className="block">
            <span className="block whitespace-nowrap text-ink-2" title={formatDateTime(row.requested_at)}>
              {formatRelative(row.requested_at)}
            </span>
            {row.status !== "open" && row.closed_at ? (
              <span className="block whitespace-nowrap text-[12px] text-muted" title={formatDateTime(row.closed_at)}>
                {row.status} {formatRelative(row.closed_at)}
                {row.closed_by ? ` by ${row.closed_by}` : ""}
              </span>
            ) : null}
          </span>
        ),
      },
      {
        key: "kind",
        header: "Kind",
        width: "110px",
        render: (row) => <span className="font-medium text-ink">{KIND_LABEL[row.kind] ?? row.kind}</span>,
      },
      { key: "order_id", header: "Order ID", width: "190px", render: (row) => truncated(row.order_id, "190px") },
      {
        key: "unit_id",
        header: "Unit ID",
        width: "170px",
        render: (row) => truncated(row.unit_id, "170px", "Not yet seen"),
      },
      { key: "product", header: "Product", render: (row) => truncated(row.product, "240px") },
      { key: "account_email", header: "Account", width: "190px", render: (row) => truncated(row.account_email, "190px") },
      {
        key: "reason",
        header: "Reason / New date",
        render: (row) => (
          <span className="block max-w-[320px]">
            {row.kind === "reschedule" ? (
              <span className="block whitespace-nowrap text-ink">New date: {formatDate(row.requested_for)}</span>
            ) : null}
            {row.reason ? (
              <span className={`block leading-snug ${row.kind === "reschedule" ? "text-[12px] text-ink-2" : "text-ink"}`}>
                {row.reason}
              </span>
            ) : row.kind === "cancel" ? (
              <span className="text-muted">—</span>
            ) : null}
          </span>
        ),
      },
      { key: "requested_by", header: "Requested by", width: "190px", render: (row) => truncated(row.requested_by, "190px") },
      {
        key: "status",
        header: "Status",
        width: "150px",
        render: (row) => (
          <span className="block">
            {statusBadge(row.status)}
            {row.note ? (
              <span className="mt-1 block max-w-[220px] truncate text-[12px] text-ink-2" title={row.note}>
                “{row.note}”
              </span>
            ) : null}
            <RequestHistory steps={row.history ?? []} />
          </span>
        ),
      },
      {
        key: "actions",
        header: "",
        align: "right",
        width: "200px",
        sticky: "right",
        render: (row) =>
          row.status === "open" ? (
            <span className="inline-flex items-center gap-1">
              <Button
                size="sm"
                loading={busyRow === row.id}
                disabled={busyRow !== null}
                onClick={() => {
                  setClosingNote("");
                  setClosingError(null);
                  setClosing({ row, status: "done" });
                }}
              >
                Mark done
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={busyRow !== null}
                onClick={() => {
                  setClosingNote("");
                  setClosingError(null);
                  setClosing({ row, status: "dropped" });
                }}
              >
                Drop
              </Button>
            </span>
          ) : (
            <Button
              size="sm"
              variant="ghost"
              loading={busyRow === row.id}
              disabled={busyRow !== null}
              onClick={() => void onReopen(row)}
            >
              Reopen
            </Button>
          ),
      },
    ],
    // The buttons render by busyRow; the setters are stable, and onReopen
    // follows the filter.
    [busyRow, onReopen],
  );

  const refreshing = loading && rows !== null;

  const body = (() => {
    if (rows === null && error !== null) {
      return <RunError title="Could not load the requests" message={error} onRetry={load} />;
    }
    if (rows === null) return <RunLoading label="Loading requests…" />;
    return (
      <Table
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        caption="Requests, newest first"
        minWidth="1400px"
        empty={
          filtered ? (
            <EmptyState
              title="No requests match these filters"
              description="Nothing was recorded under this status and kind."
              action={
                <Button size="sm" onClick={clearFilters}>
                  Show open requests
                </Button>
              }
            />
          ) : (
            <EmptyState
              title="No open requests"
              description="Select units in Order Units and choose Cancel or Reschedule."
              action={<RunLink to="/order-units">Open Order Units</RunLink>}
            />
          )
        }
      />
    );
  })();

  const dialogTitle = closing
    ? `${closing.status === "done" ? "Mark done" : "Drop"}: ${KIND_LABEL[closing.row.kind].toLowerCase()} ${
        closing.row.order_id ?? `#${closing.row.unit_pk}`
      }`
    : "";

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold leading-tight tracking-tight text-ink">Requests</h1>
          <p className="mt-1 max-w-prose text-[13px] leading-relaxed text-ink-2">
            Cancel and reschedule requests recorded from Order Units. Nothing here is sent to Flipkart: a
            person does each one on the platform by hand, then marks it done here. Drop a request that is
            no longer wanted; a closed row can be reopened.
          </p>
        </div>
        <Button onClick={load} loading={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh"}
        </Button>
      </header>

      <div aria-live="polite" className="flex flex-col gap-2 empty:hidden">
        {note ? <Alert tone="success">{note}</Alert> : null}
        {actionError ? (
          <Alert tone="error" title="That did not work">
            {actionError}
          </Alert>
        ) : null}
        {error !== null && rows !== null ? (
          <p role="status" className="text-[12px] text-ink-2">
            Showing the requests that last loaded — the refresh failed: {error}
          </p>
        ) : null}
      </div>

      <Dialog
        open={closing !== null}
        onClose={closeDialog}
        title={dialogTitle}
        description={
          closing?.status === "done"
            ? "Confirm you have done this on Flipkart. The row stays on file with your name and the time."
            : "The request is no longer wanted; the unit stays as it is. The row stays on file under Dropped."
        }
        width="560px"
        footer={
          <>
            <Button size="sm" variant="ghost" disabled={busyRow !== null} onClick={closeDialog}>
              Cancel
            </Button>
            <Button
              size="sm"
              variant="primary"
              loading={closing !== null && busyRow === closing.row.id}
              disabled={busyRow !== null}
              onClick={() => {
                if (closing) void onClose(closing, closingNote);
              }}
            >
              {closing?.status === "done" ? "Mark it done" : "Drop it"}
            </Button>
          </>
        }
      >
        {closingError ? (
          <Alert tone="error" title="Could not update the request" className="mb-3">
            {closingError}
          </Alert>
        ) : null}
        {closing ? (
          <p className="mb-3 text-[13px] text-ink-2">
            {KIND_LABEL[closing.row.kind]} · {closing.row.product ?? "—"}
            {closing.row.kind === "reschedule" ? ` · new date ${formatDate(closing.row.requested_for)}` : ""}
            {closing.row.reason ? ` · “${closing.row.reason}”` : ""}
          </p>
        ) : null}
        <Field id="closing-note" label="Note (optional)" help="What you did, for whoever reads this row later.">
          <Textarea
            value={closingNote}
            onChange={(event) => setClosingNote(event.target.value)}
            maxLength={2000}
            placeholder={closing?.status === "done" ? "Cancelled on the app; refund due in 5 days." : "The customer wants it after all."}
          />
        </Field>
      </Dialog>

      <Card
        title="Log"
        description="Newest first. A row is one request on one unit; the same unit can carry a cancel and a reschedule at once, but only one open request of each."
      >
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-end gap-3">
            <Field label="Status" className="w-full sm:w-44">
              <Select value={view} disabled={busyRow !== null} onChange={(event) => changeView(toView(event.target.value))}>
                {VIEWS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Kind" className="w-full sm:w-56">
              <Select value={kind} disabled={busyRow !== null} onChange={(event) => changeKind(toKind(event.target.value))}>
                {KINDS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </Field>
            {filtered ? (
              <Button variant="ghost" onClick={clearFilters} disabled={busyRow !== null}>
                Show open requests
              </Button>
            ) : null}
          </div>

          <p className="text-[12px] text-ink-2" aria-live="polite">
            {rows === null ? "Loading…" : `${countLabel(total, "request")}${view === "open" ? " open" : ""}`}
          </p>

          {body}

          {rows !== null && (pages > 1 || page > 1) ? (
            <AccountsPager
              idPrefix="requests-pager"
              noun="requests"
              page={page}
              size={PAGE_SIZE}
              total={total}
              pages={pages}
              shown={rows.length}
              onPageChange={setPage}
              loading={loading}
              disabled={busyRow !== null}
            />
          ) : null}
        </div>
      </Card>
    </div>
  );
}
