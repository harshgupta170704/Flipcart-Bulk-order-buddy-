/**
 * Alerts — the queue of things needing a human.
 *
 * This is the screen the operator's existing portal shows as "Needs attention
 * — 443 open": rules run over the synced order data and raise a row per
 * problem (an order sent but never confirmed, a delivery that never arrived). Two things
 * here are deliberate.
 *
 * An alert is RESOLVED, never deleted. The row stays on file under its stable
 * key, so the same problem coming back reopens it instead of arriving as new —
 * which is why "Reopen" exists next to "Resolve" rather than a delete. A close
 * by hand is an acceptance: the rules leave it closed while the problem
 * persists and reopen it only once the problem has gone away and returned.
 *
 * And severity is never colour alone. Amber is only 1.79:1 on the light
 * surface, so every row pairs the hue with the glyph and the word ("Warning"),
 * exactly as `statusMeta` does everywhere else.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  errorMessage,
  evaluateAlerts,
  listAlerts,
  reopenAlert,
  resolveAlert,
  resolveAlerts,
} from "../lib/api";
import { countLabel, formatCount, formatCurrency, formatDateTime, formatRelative } from "../lib/format";
import { ALERT_SEVERITIES } from "../lib/types";
import type { Alert as AlertRow, AlertSeverity } from "../lib/types";
import { Alert } from "../components/Alert";
import { Dialog } from "../components/Dialog";
import {
  AlertSeverityBadge,
  SEVERITY_ORDER,
  kindLabel,
  openTotal,
  severityLabel,
  severityRank,
} from "../components/AlertSeverity";
import { AlertsSummaryTiles, refreshAlertSummary, useAlertSummary } from "../components/AlertsSummary";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { Field, Select, Textarea } from "../components/Field";
import { RunError, RunLoading } from "../components/RunStates";
import { StatusBadge } from "../components/StatusBadge";
import { Table, type Column } from "../components/Table";

/** Which half of the queue is on screen. Open is what a queue is for. */
type View = "open" | "resolved" | "all";

const VIEWS: { value: View; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "resolved", label: "Resolved" },
  { value: "all", label: "Open and resolved" },
];

/** Enough rows that paging is not the next thing anyone asks for. */
const PAGE_LIMIT = 200;

function toView(value: string): View {
  const match = VIEWS.find((view) => view.value === value);
  return match ? match.value : "open";
}

function toSeverity(value: string): AlertSeverity | "" {
  return (ALERT_SEVERITIES as readonly string[]).includes(value) ? (value as AlertSeverity) : "";
}

/**
 * Most recently moved first — mirroring the server's own order.
 *
 * By `updated_at`, not `created_at`, and deliberately: an alert that has been
 * reopened is an old problem that came back, and burying it under rows that
 * have not moved in weeks is how a recurring problem gets missed. One evaluate
 * pass stamps every row it touches with the same instant, so a tie falls back
 * to severity and then to the id, which keeps the order stable across reloads.
 */
function byNewest(a: AlertRow, b: AlertRow): number {
  const ta = Date.parse(a.updated_at);
  const tb = Date.parse(b.updated_at);
  if (!Number.isNaN(ta) && !Number.isNaN(tb) && ta !== tb) return tb - ta;
  const rank = severityRank(a.severity) - severityRank(b.severity);
  if (rank !== 0) return rank;
  return b.id - a.id;
}

/** True when this row has moved since it was first raised. */
function hasMoved(row: AlertRow): boolean {
  const created = Date.parse(row.created_at);
  const updated = Date.parse(row.updated_at);
  if (Number.isNaN(created) || Number.isNaN(updated)) return false;
  // A second of slack: one evaluate pass writes both stamps together.
  return updated - created > 1000;
}

/**
 * The facts the rules record in rupees (app.core.alerts: over target,
 * unconfirmed, invoice amount differs). Shown as money, as the message above
 * them already is (NFR-010) — "Amount: ₹1,29,900.00", never "Amount: 129900".
 */
const MONEY_FACTS = new Set(["amount", "target", "product_invoice_total", "with_fee_invoices"]);

/**
 * The first few scalar facts a rule recorded, e.g. "expected: 560001 · found:
 * 110024". The shape is per-kind, so anything that is not a plain value is
 * skipped rather than stringified into noise.
 */
/** Facts a rule records for the server's own use, or that its message already
 *  says in words: the order's product page (a web address hundreds of
 *  characters long, which ran out of the table until 11 Nov 2026), why it
 *  failed or was cancelled, and who cancelled it. */
const UNSHOWN_FACTS = new Set(["product_link", "reason", "cancel_reason", "cancelled_by", "never_placed"]);

function detailLine(details: Record<string, unknown> | null): string {
  if (!details) return "";
  const parts: string[] = [];
  for (const [key, value] of Object.entries(details)) {
    if (parts.length >= 3) break;
    if (UNSHOWN_FACTS.has(key)) continue;
    // A web address is a link, not a fact to read; none belongs in this line.
    if (typeof value === "string" && /^https?:\/\//i.test(value.trim())) continue;
    if (typeof value === "string" && value.trim() !== "") parts.push(`${kindLabel(key)}: ${value}`);
    else if (typeof value === "number" && Number.isFinite(value))
      parts.push(`${kindLabel(key)}: ${MONEY_FACTS.has(key) ? formatCurrency(value) : value}`);
    else if (typeof value === "boolean") parts.push(`${kindLabel(key)}: ${value ? "yes" : "no"}`);
  }
  return parts.join(" · ");
}

/** Who or what the alert is about. Never a bare id with no word for it. */
function linkedCell(row: AlertRow) {
  const bits: string[] = [];
  // Checked as numbers, not against null: an id the server left out entirely
  // must not render as "Account #undefined".
  if (typeof row.account_id === "number") {
    const email = row.account_email?.trim();
    bits.push(email ? email : `Account #${row.account_id}`);
  }
  // The order itself is a click away: the ledger's own row number means
  // nothing to a person (it read "Order #26" until 11 Nov 2026), so the cell
  // links to the row instead of naming it.
  const order = typeof row.order_id === "number" ? row.order_id : null;
  if (bits.length === 0 && order === null) {
    return <span className="text-muted">Whole account set</span>;
  }
  return (
    <span className="block" title={bits.join(" · ") || undefined}>
      {bits.map((bit) => (
        <span key={bit} className="block truncate" style={{ maxWidth: "200px" }}>
          {bit}
        </span>
      ))}
      {order !== null ? (
        <Link to={`/order-units?row=${order}`} className="block text-accent underline-offset-2 hover:underline">
          View the order
        </Link>
      ) : null}
    </span>
  );
}

/** The three filters as they travel in the query string, for comparing. */
function filterKey(kind: string, severity: string, view: string): string {
  return `${kind}|${severity}|${view === "open" ? "" : view}`;
}

export default function Alerts() {
  const summaryState = useAlertSummary();
  const [searchParams, setSearchParams] = useSearchParams();

  /* The filters live in the URL as well as in state, so a link from the
     dashboard ("the delivery-overdue ones") lands on that slice of the queue
     and a narrowed queue can be bookmarked. */
  const [severity, setSeverity] = useState<AlertSeverity | "">(() =>
    toSeverity(searchParams.get("severity") ?? ""),
  );
  const [kind, setKind] = useState(() => (searchParams.get("kind") ?? "").trim());
  const [view, setView] = useState<View>(() => toView(searchParams.get("view") ?? "open"));

  /** null until a load has succeeded — "not loaded yet" is not "nothing open". */
  const [rows, setRows] = useState<AlertRow[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [busyRow, setBusyRow] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [evaluating, setEvaluating] = useState(false);
  /** The row being closed by hand, while its note dialog is open. */
  const [resolving, setResolving] = useState<AlertRow | null>(null);
  const [resolveNote, setResolveNote] = useState("");
  /** Shown inside the dialog, so a failed resolve keeps the note on screen. */
  const [resolveError, setResolveError] = useState<string | null>(null);
  /** Closing every open row on screen in one go: the dialog, its note, its state. */
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkNote, setBulkNote] = useState("");
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkError, setBulkError] = useState<string | null>(null);

  const alive = useRef(true);
  /* Resolving a row reloads the queue, so two loads can be in the air at once.
     Only the newest may land. */
  const ticket = useRef(0);

  useEffect(() => {
    document.title = "Alerts — Ordering Suite";
  }, []);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  /* URL ⇄ state. The URL is the source when it changes under us — a dashboard
     link clicked while already here — and the state is the source otherwise,
     with the URL following it. The key comparison is what stops the two
     effects feeding each other. */
  const urlKey = filterKey(
    (searchParams.get("kind") ?? "").trim(),
    toSeverity(searchParams.get("severity") ?? ""),
    toView(searchParams.get("view") ?? "open"),
  );
  const lastKey = useRef(urlKey);
  useEffect(() => {
    if (lastKey.current === urlKey) return;
    lastKey.current = urlKey;
    setKind((searchParams.get("kind") ?? "").trim());
    setSeverity(toSeverity(searchParams.get("severity") ?? ""));
    setView(toView(searchParams.get("view") ?? "open"));
  }, [urlKey, searchParams]);

  useEffect(() => {
    const nextKey = filterKey(kind, severity, view);
    if (nextKey === lastKey.current) return;
    lastKey.current = nextKey;
    const next = new URLSearchParams();
    if (kind !== "") next.set("kind", kind);
    if (severity !== "") next.set("severity", severity);
    if (view !== "open") next.set("view", view);
    setSearchParams(next, { replace: true });
  }, [kind, severity, view, setSearchParams]);

  const load = useCallback(() => {
    ticket.current += 1;
    const mine = ticket.current;
    setLoading(true);
    listAlerts({
      severity: severity === "" ? undefined : severity,
      kind: kind === "" ? undefined : kind,
      // "any" rather than nothing: an absent `resolved` is the OPEN queue on
      // the server, so "both halves" has to be asked for out loud.
      resolved: view === "all" ? "any" : view === "resolved",
      limit: PAGE_LIMIT,
    }).then(
      (next) => {
        if (!alive.current || ticket.current !== mine) return;
        setRows(Array.isArray(next) ? next : []);
        setError(null);
        setLoading(false);
      },
      (err: unknown) => {
        if (!alive.current || ticket.current !== mine) return;
        setError(errorMessage(err));
        setLoading(false);
      },
    );
  }, [kind, severity, view]);

  useEffect(() => {
    load();
  }, [load]);

  const sorted = useMemo(() => (rows ?? []).slice().sort(byNewest), [rows]);
  /** The open rows on screen: what "Resolve all shown" closes. */
  const openShown = useMemo(() => sorted.filter((row) => !row.resolved), [sorted]);

  /* Kinds come from the summary when the server breaks them down, because the
     rows on screen are already narrowed and would otherwise hide the very
     option needed to widen the filter again. */
  const kinds = useMemo(() => {
    const seen = new Set<string>();
    for (const name of Object.keys(summaryState.summary?.by_kind ?? {})) seen.add(name);
    for (const row of rows ?? []) seen.add(row.kind);
    if (kind !== "") seen.add(kind);
    return [...seen].filter((name) => name !== "").sort((a, b) => kindLabel(a).localeCompare(kindLabel(b)));
  }, [kind, rows, summaryState.summary]);

  const filtered = severity !== "" || kind !== "" || view !== "open";

  const clearFilters = useCallback(() => {
    setSeverity("");
    setKind("");
    setView("open");
  }, []);

  const begin = (id: number) => {
    setBusyRow(id);
    setActionError(null);
    setNote(null);
  };

  const settle = useCallback(() => {
    setBusyRow(null);
    load();
    // The nav badge and the dashboard read the same counts.
    refreshAlertSummary();
  }, [load]);

  const onResolve = async (row: AlertRow, reason: string) => {
    begin(row.id);
    setResolveError(null);
    try {
      await resolveAlert(row.id, reason);
      if (!alive.current) return;
      // Closed only now: a failed request keeps the dialog, and the note, open.
      setResolving(null);
      setResolveNote("");
      setNote(
        `Resolved “${kindLabel(row.kind)}” #${row.id}${reason.trim() ? " with your note" : ""}. It stays closed while the problem persists; if the problem goes away and comes back, this row reopens rather than a new one being added.`,
      );
      settle();
    } catch (err) {
      if (!alive.current) return;
      setResolveError(errorMessage(err));
      setBusyRow(null);
    }
  };

  /** Close every open alert on screen — the filtered list — under one note. */
  const onResolveShown = async (ids: number[]) => {
    setBulkBusy(true);
    setBulkError(null);
    try {
      const result = await resolveAlerts(ids, bulkNote);
      if (!alive.current) return;
      setBulkOpen(false);
      setBulkNote("");
      setNote(
        `Resolved ${countLabel(result.resolved, "alert")}${bulkNote.trim() ? " with your note" : ""}. Each stays closed while its problem persists.`,
      );
      settle();
    } catch (err) {
      if (!alive.current) return;
      setBulkError(errorMessage(err));
    } finally {
      if (alive.current) setBulkBusy(false);
    }
  };

  const closeResolveDialog = () => {
    if (busyRow !== null) return; // not while the request is in flight
    setResolving(null);
    setResolveError(null);
  };

  const onReopen = async (row: AlertRow) => {
    begin(row.id);
    try {
      await reopenAlert(row.id);
      if (!alive.current) return;
      setNote(`“${kindLabel(row.kind)}” #${row.id} is back in the open queue.`);
      settle();
    } catch (err) {
      if (!alive.current) return;
      setActionError(errorMessage(err));
      setBusyRow(null);
    }
  };

  const onEvaluate = async () => {
    setEvaluating(true);
    setActionError(null);
    setNote(null);
    try {
      const result = await evaluateAlerts();
      if (!alive.current) return;
      /* `counts` is per rule kind and covers every registered rule, zeros
         included; `open` is the whole queue, which is larger when a sync has
         raised an ad-hoc alert that belongs to no rule. Only the kinds with
         something open are worth a sentence. */
      const raised = Object.entries(result?.counts ?? {})
        .filter(([, count]) => typeof count === "number" && count > 0)
        .sort((a, b) => b[1] - a[1])
        .map(([name, count]) => `${formatCount(count)} ${kindLabel(name).toLowerCase()}`);
      const total = typeof result?.open === "number" ? formatCount(result.open) : null;
      setNote(
        raised.length > 0
          ? `Rules re-run — open now: ${raised.join(", ")}${total === null ? "" : ` (${total} in the queue)`}.`
          : total === "0" || total === null
            ? "Rules re-run. No rule is raising anything."
            : `Rules re-run. No rule is raising anything; ${total} open in the queue.`,
      );
      load();
      refreshAlertSummary();
    } catch (err) {
      if (!alive.current) return;
      setActionError(errorMessage(err));
    } finally {
      if (alive.current) setEvaluating(false);
    }
  };

  const columns = useMemo<Column<AlertRow>[]>(
    () => [
      {
        key: "severity",
        header: "Severity",
        width: "120px",
        render: (row) => <AlertSeverityBadge severity={row.severity} />,
      },
      {
        key: "kind",
        header: "Kind",
        width: "150px",
        render: (row) => (
          <span className="block font-medium text-ink" title={row.kind}>
            {kindLabel(row.kind)}
          </span>
        ),
      },
      {
        key: "message",
        header: "What happened",
        render: (row) => {
          const detail = detailLine(row.details);
          return (
            <span className="block max-w-[420px] break-words">
              <span className="block leading-snug text-ink">{row.message}</span>
              {detail !== "" ? (
                <span className="mt-1 block break-words text-[12px] leading-snug text-ink-2">{detail}</span>
              ) : null}
            </span>
          );
        },
      },
      {
        key: "linked",
        header: "About",
        width: "190px",
        render: linkedCell,
      },
      {
        key: "created_at",
        header: "Raised",
        width: "140px",
        render: (row) => (
          <span className="block">
            <span
              className="block whitespace-nowrap text-ink-2"
              title={formatDateTime(row.created_at)}
            >
              {formatRelative(row.created_at)}
            </span>
            {/* First sighting above, last movement below: an old row that
                changed recently is a problem that came back. */}
            {hasMoved(row) ? (
              <span
                className="block whitespace-nowrap text-[12px] text-muted"
                title={formatDateTime(row.updated_at)}
              >
                changed {formatRelative(row.updated_at)}
              </span>
            ) : null}
          </span>
        ),
      },
      {
        key: "actions",
        header: "",
        align: "right",
        width: "190px",
        render: (row) =>
          row.resolved ? (
            <span className="inline-flex items-center gap-2">
              <span title={row.resolved_at ? `Closed ${formatDateTime(row.resolved_at)}` : "Closed"}>
                <StatusBadge status="done" label="Resolved" />
              </span>
              {row.resolution_note ? (
                <span className="max-w-[220px] truncate text-[12px] text-ink-2" title={row.resolution_note}>
                  “{row.resolution_note}”
                </span>
              ) : null}
              <Button
                size="sm"
                variant="ghost"
                loading={busyRow === row.id}
                disabled={busyRow !== null}
                onClick={() => void onReopen(row)}
              >
                Reopen
              </Button>
            </span>
          ) : (
            <Button
              size="sm"
              loading={busyRow === row.id}
              disabled={busyRow !== null}
              onClick={() => {
                setResolveNote("");
                setResolveError(null);
                setResolving(row);
              }}
            >
              Resolve
            </Button>
          ),
      },
    ],
    // The handlers read only their own `row` and state setters, so what the
    // buttons render by — busyRow — is the real dependency.
    [busyRow],
  );

  const open = openTotal(summaryState.summary);
  const refreshing = loading && rows !== null;

  const body = (() => {
    if (rows === null && error !== null) {
      return <RunError title="Could not load the alerts" message={error} onRetry={load} />;
    }
    if (rows === null) {
      return <RunLoading label="Loading alerts…" />;
    }
    return (
      <Table
        columns={columns}
        rows={sorted}
        rowKey={(row) => row.id}
        caption="Alerts, newest first"
        minWidth="1000px"
        empty={
          filtered ? (
            <EmptyState
              title="No alert matches these filters"
              description={
                view === "resolved"
                  ? "Nothing has been resolved under this narrowing yet."
                  : "Nothing in the queue matches the current severity, kind and state."
              }
              action={
                <Button size="sm" onClick={clearFilters}>
                  Show the open queue
                </Button>
              }
            />
          ) : (
            <EmptyState
              title="Nothing needs attention"
              description="The queue is empty: no rule is raising anything right now. Read orders back from Flipkart on the Order Units page and anything that needs a human — an order sent but never confirmed, a delivery that never arrived — shows up here."
              action={
                <Button size="sm" loading={evaluating} onClick={() => void onEvaluate()}>
                  {evaluating ? "Checking…" : "Evaluate now"}
                </Button>
              }
            />
          )
        }
      />
    );
  })();

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold leading-tight tracking-tight text-ink">
            Alerts
          </h1>
          <p className="mt-1 max-w-prose text-[13px] leading-relaxed text-ink-2">
            Things that need a person: an order Flipkart never confirmed, a delivery that is late,
            an account that is signed out. Old run results leave by themselves after a few days.
            Resolving a row keeps it on file, so the same problem coming back reopens it instead of
            arriving as something new.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button loading={evaluating} onClick={() => void onEvaluate()}>
            {evaluating ? "Checking…" : "Evaluate now"}
          </Button>
          <Button onClick={load} loading={refreshing}>
            {refreshing ? "Refreshing…" : "Refresh"}
          </Button>
        </div>
      </header>

      <div aria-live="polite" className="flex flex-col gap-2 empty:hidden">
        {note ? <Alert tone="success">{note}</Alert> : null}

        <Dialog
          open={resolving !== null}
          onClose={closeResolveDialog}
          title={resolving ? `Resolve “${kindLabel(resolving.kind)}” #${resolving.id}` : ""}
          description={resolving?.message}
          width="560px"
          footer={
            <>
              <Button size="sm" variant="ghost" disabled={busyRow !== null} onClick={closeResolveDialog}>
                Cancel
              </Button>
              <Button
                size="sm"
                variant="primary"
                loading={resolving !== null && busyRow === resolving.id}
                disabled={busyRow !== null}
                onClick={() => {
                  if (resolving) void onResolve(resolving, resolveNote);
                }}
              >
                Resolve
              </Button>
            </>
          }
        >
          {resolveError ? (
            <Alert tone="error" title="Could not resolve it" className="mb-3">
              {resolveError}
            </Alert>
          ) : null}
          <Field
            id="resolve-note"
            label="Note (optional)"
            help="Why it is closed, for whoever reads this row later. Closing by hand counts as accepting the problem: the alert stays closed while it persists, reopens only if it goes away and comes back, and the note is cleared then."
          >
            <Textarea
              value={resolveNote}
              onChange={(event) => setResolveNote(event.target.value)}
              maxLength={1000}
              placeholder="Checked the account — the order was delivered on the 20th."
            />
          </Field>
        </Dialog>
        <Dialog
          open={bulkOpen}
          onClose={() => {
            if (!bulkBusy) setBulkOpen(false);
          }}
          title={`Resolve the ${countLabel(openShown.length, "alert")} shown`}
          description="Every open alert in the list below, as it is filtered now. Narrow the list first to close only one kind."
          width="560px"
          footer={
            <>
              <Button size="sm" variant="ghost" disabled={bulkBusy} onClick={() => setBulkOpen(false)}>
                Cancel
              </Button>
              <Button
                size="sm"
                variant="primary"
                loading={bulkBusy}
                disabled={bulkBusy || openShown.length === 0}
                onClick={() => void onResolveShown(openShown.map((row) => row.id))}
              >
                Resolve {formatCount(openShown.length)}
              </Button>
            </>
          }
        >
          {bulkError ? (
            <Alert tone="error" title="Could not resolve them" className="mb-3">
              {bulkError}
            </Alert>
          ) : null}
          <Field
            id="resolve-all-note"
            label="Note (optional)"
            help="One reason for them all, kept on each row. Closing by hand counts as accepting the problem: an alert stays closed while its problem persists and reopens only if it goes away and comes back."
          >
            <Textarea
              value={bulkNote}
              onChange={(event) => setBulkNote(event.target.value)}
              maxLength={1000}
              placeholder="Test runs from last week — nothing to do."
            />
          </Field>
        </Dialog>
        {actionError ? (
          <Alert tone="error" title="That did not work">
            {actionError}
          </Alert>
        ) : null}
        {error !== null && rows !== null ? (
          <p role="status" className="text-[12px] text-ink-2">
            Showing the alerts that last loaded — the refresh failed: {error}
          </p>
        ) : null}
      </div>

      <section aria-label="Open alerts by severity" className="flex flex-col gap-2">
        <AlertsSummaryTiles summary={summaryState.summary} />
        <p className="text-[12px] text-ink-2">
          {summaryState.summary === null
            ? summaryState.error === null
              ? "Counting the open alerts…"
              : `Could not count the open alerts: ${summaryState.error}`
            : open === 0
              ? "Nothing is open. Every alert raised so far has been dealt with."
              : `${countLabel(open, "alert")} open across every severity.`}
        </p>
      </section>

      <Card
        title="Queue"
        description="Newest first. Severity carries a glyph and a word as well as a colour, so it reads the same whatever the screen."
      >
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-end gap-3">
            <Field label="State" className="w-full sm:w-52">
              <Select
                value={view}
                disabled={busyRow !== null}
                onChange={(event) => setView(toView(event.target.value))}
              >
                {VIEWS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </Field>

            <Field label="Severity" className="w-full sm:w-48">
              <Select
                value={severity}
                disabled={busyRow !== null}
                onChange={(event) => setSeverity(toSeverity(event.target.value))}
              >
                <option value="">All severities</option>
                {SEVERITY_ORDER.map((value) => (
                  <option key={value} value={value}>
                    {severityLabel(value)}
                  </option>
                ))}
              </Select>
            </Field>

            <Field label="Kind" className="w-full sm:w-60">
              <Select
                value={kind}
                disabled={busyRow !== null}
                onChange={(event) => setKind(event.target.value)}
              >
                <option value="">All kinds</option>
                {kinds.map((name) => (
                  <option key={name} value={name}>
                    {kindLabel(name)}
                  </option>
                ))}
              </Select>
            </Field>

            {filtered ? (
              <Button variant="ghost" onClick={clearFilters} disabled={busyRow !== null}>
                Clear filters
              </Button>
            ) : null}
          </div>

          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-[12px] text-ink-2" aria-live="polite">
              {rows === null
                ? "Loading…"
                : `${countLabel(sorted.length, "alert")} shown${
                    sorted.length >= PAGE_LIMIT ? ` — the newest ${PAGE_LIMIT}` : ""
                  }${view === "open" ? "" : ` · ${VIEWS.find((v) => v.value === view)?.label ?? ""}`}`}
            </p>
            {/* A queue of thirty old alerts is thirty dialogs one by one; this
                closes what the list shows — narrow it by kind first to pick. */}
            {openShown.length > 1 ? (
              <Button
                size="sm"
                disabled={busyRow !== null || bulkBusy}
                onClick={() => {
                  setBulkError(null);
                  setBulkOpen(true);
                }}
              >
                Resolve all {formatCount(openShown.length)} shown
              </Button>
            ) : null}
            {sorted.length >= PAGE_LIMIT ? (
              <p className="text-[12px] text-ink-2">
                Narrow by severity or kind to see further back.
              </p>
            ) : null}
          </div>

          {body}
        </div>
      </Card>
    </div>
  );
}
