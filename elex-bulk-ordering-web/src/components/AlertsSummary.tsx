/**
 * The open-alert counts — fetched once, shared by everything that shows them.
 *
 * The nav badge, the dashboard panel and the alerts page all want the same
 * number, and this app's rule is "nothing running, nothing polled" (see the
 * dashboard). So the counts live in one tiny module-level store with no timer:
 * whoever mounts first fetches, everyone else reads the same answer, and
 * `refreshAlertSummary()` after a resolve / reopen / evaluate updates the badge,
 * the dashboard and the queue in the same tick.
 */
import { useEffect, useState, useSyncExternalStore } from "react";
import { Link } from "react-router-dom";
import { alertsSummary, errorMessage, listAlerts } from "../lib/api";
import { countLabel, formatCount, formatRelative } from "../lib/format";
import type { Alert as AlertRow, AlertSeverity, AlertSummary } from "../lib/types";
import { AlertSeverityBadge, kindLabel } from "./AlertSeverity";
import { SkeletonRows } from "./Skeleton";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Card } from "./Card";
import { EmptyState } from "./EmptyState";
import { StatTile } from "./StatTile";
import {
  SEVERITY_ORDER,
  openCount,
  openTotal,
  severityColorVar,
  severityHint,
  severityLabel,
  severityTone,
} from "./AlertSeverity";

export interface AlertSummaryState {
  /** null until a load has succeeded — "not loaded" is not "nothing open". */
  summary: AlertSummary | null;
  loading: boolean;
  /** Already human-readable: it came through errorMessage(). */
  error: string | null;
  /** When the last answer landed, as an ISO string. null before the first. */
  loadedAt: string | null;
}

/* Loading from the start, before anything is asked: the first reader to mount
   asks at once, and a cold open must show placeholders for its first frame,
   not "Could not load" — a summary that is null and not loading reads as a
   failure (spec FR-049). */
let state: AlertSummaryState = { summary: null, loading: true, error: null, loadedAt: null };
let fetchedAt = 0;
/** Only the newest request may land — a stale reply would undo a fresh count. */
let ticket = 0;
const listeners = new Set<() => void>();

function publish(next: AlertSummaryState): void {
  state = next;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function snapshot(): AlertSummaryState {
  return state;
}

/** Fetch the counts now and tell every subscriber. */
export function refreshAlertSummary(): void {
  ticket += 1;
  const mine = ticket;
  publish({ ...state, loading: true });
  alertsSummary().then(
    (summary) => {
      if (ticket !== mine) return;
      fetchedAt = Date.now();
      publish({ summary, loading: false, error: null, loadedAt: new Date().toISOString() });
    },
    (err: unknown) => {
      if (ticket !== mine) return;
      fetchedAt = Date.now();
      // Keep the last good counts: a stale number with a visible error beats
      // a badge that silently drops to nothing.
      publish({ ...state, loading: false, error: errorMessage(err) });
    },
  );
}

/** Counts older than this are refetched when something mounts. */
const STALE_MS = 60_000;

/**
 * Read the shared counts, fetching them when nobody has recently.
 *
 * Mounting several readers costs one request, not one each, and moving between
 * pages does not refetch until the numbers are a minute old.
 */
export function useAlertSummary(): AlertSummaryState & { reload: () => void } {
  const shared = useSyncExternalStore(subscribe, snapshot, snapshot);

  useEffect(() => {
    // A request is on its way only once one has been asked for: `loading`
    // starts true before the first (see `state`).
    if (state.loading && ticket > 0) return;
    if (state.summary !== null && Date.now() - fetchedAt < STALE_MS) return;
    refreshAlertSummary();
  }, []);

  return { ...shared, reload: refreshAlertSummary };
}

/* --- the nav badge -------------------------------------------------------- */

/**
 * The open count beside the "Alerts" nav item.
 *
 * Renders nothing at zero — an empty queue should not wear a badge. The number
 * carries the meaning; the hue only reinforces it, and the spelled-out
 * breakdown rides along for screen readers and as a tooltip.
 */
export function OpenAlertsBadge() {
  const { summary } = useAlertSummary();
  const open = openTotal(summary);
  if (open <= 0) return null;

  const critical = openCount(summary, "critical");
  const warning = openCount(summary, "warning");
  /* The hue follows the worst thing open, and only reinforces: the number and
     the spelled-out breakdown are what actually carry the meaning. */
  const hue =
    critical > 0
      ? severityColorVar("critical")
      : warning > 0
        ? severityColorVar("warning")
        : "var(--accent)";
  const breakdown = SEVERITY_ORDER.filter((severity) => openCount(summary, severity) > 0)
    .map((severity) => `${formatCount(openCount(summary, severity))} ${severityLabel(severity).toLowerCase()}`)
    .join(", ");

  return (
    <span
      className="ml-auto inline-flex min-w-[22px] items-center justify-center rounded-full border px-1.5 py-px text-[11px] font-semibold tabular-nums text-ink"
      style={{
        borderColor: `color-mix(in oklab, ${hue} 45%, transparent)`,
        backgroundColor: `color-mix(in oklab, ${hue} 18%, var(--surface-1))`,
      }}
      title={breakdown === "" ? `${countLabel(open, "alert")} open` : `${countLabel(open, "alert")} open — ${breakdown}`}
    >
      <span aria-hidden="true">{formatCount(open)}</span>
      <span className="sr-only">{`${countLabel(open, "alert")} open`}</span>
    </span>
  );
}

/* --- the tiles ------------------------------------------------------------ */

export interface AlertsSummaryTilesProps {
  summary: AlertSummary | null;
  className?: string;
}

/** One tile per severity, each with its glyph-free but labelled count. */
export function AlertsSummaryTiles({ summary, className = "" }: AlertsSummaryTilesProps) {
  return (
    <div className={`grid grid-cols-1 gap-3 sm:grid-cols-3 ${className}`}>
      {SEVERITY_ORDER.map((severity: AlertSeverity) => {
        const count = openCount(summary, severity);
        return (
          <StatTile
            key={severity}
            label={`${severityLabel(severity)} open`}
            value={formatCount(count)}
            /* A zero is toned down on purpose: nothing open is not a state
               worth painting red. */
            tone={count > 0 ? severityTone(severity) : "default"}
            hint={severityHint(severity)}
          />
        );
      })}
    </div>
  );
}

/* --- the dashboard panel -------------------------------------------------- */

const QUEUE_LINK =
  "inline-flex h-[38px] items-center rounded-control border border-line px-4 text-sm font-medium text-ink transition-colors hover:bg-page";

const LATEST = 3;

/**
 * "Needs attention" on the dashboard (spec FR-043): open counts by severity
 * and by kind, active against resolved, the latest few open rows with their
 * age, and the way into the queue. The counts come from the shared store the
 * nav badge reads; the latest rows are this panel's own small fetch.
 *
 * Every open kind is listed with its count, however many there are, so the
 * counts on screen add up to the active total (SC-004). With nothing open,
 * the totals line and "Open all alerts" stay: the resolved total and the way
 * into the queue mean as much on a quiet day.
 */
export function AlertsSummaryPanel({
  className = "",
  showRefresh = true,
}: {
  className?: string;
  /** False when the page has one Refresh of its own that covers this panel. */
  showRefresh?: boolean;
}) {
  const { summary, loading, error, loadedAt, reload } = useAlertSummary();
  const open = openTotal(summary);
  const [latest, setLatest] = useState<AlertRow[] | null>(null);
  const [latestError, setLatestError] = useState<string | null>(null);
  /** Which landing of the counts (its `loadedAt`) the latest rows, or their error, answer. */
  const [latestFor, setLatestFor] = useState<string | null>(null);

  // The latest open rows follow the counts: whenever they reload, so do these.
  // "Latest" means most recently raised, so the age shown is the first
  // sighting — a rules pass that merely confirms a problem must not make every
  // row read as new.
  useEffect(() => {
    // Not until the counts have landed once: the list follows them, and a
    // cold open would otherwise ask twice (at mount, then when they arrive).
    if (loadedAt === null) return;
    let alive = true;
    void listAlerts({ resolved: false, limit: LATEST, order: "created" })
      .then((rows) => {
        if (!alive) return;
        setLatest(rows);
        setLatestError(null);
        setLatestFor(loadedAt);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setLatestError(errorMessage(err));
        setLatestFor(loadedAt);
      });
    return () => {
      alive = false;
    };
  }, [loadedAt]);

  const kinds = Object.entries(summary?.by_kind ?? {})
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1]);

  const body = (() => {
    // Placeholders whenever the counts are on their way (spec FR-049): on a
    // cold open, on "Try again", and on the dashboard's Refresh, so the
    // counts from before it never pass for its answer.
    if (loading) {
      return <SkeletonRows rows={4} />;
    }
    if (summary === null) {
      return (
        <div className="flex flex-col items-start gap-3">
          <Alert tone="error" title="Could not load the alerts queue" className="w-full">
            {error ?? "Something went wrong."}
          </Alert>
          <Button size="sm" onClick={reload}>
            Try again
          </Button>
        </div>
      );
    }
    const totals = (
      <p className="text-[13px] text-ink-2">
        <span className="font-medium text-ink">{formatCount(open)}</span> active ·{" "}
        <span className="font-medium text-ink">{formatCount(summary.resolved)}</span> resolved all time
      </p>
    );
    if (open === 0) {
      return (
        <div className="flex flex-col gap-4">
          <EmptyState
            title="Nothing needs attention"
            description="No alert is open. Anything that needs a human — an order over target, a delivery that never arrived, an order Flipkart never confirmed — shows up in the queue."
          />
          <div className="flex flex-wrap items-center gap-3">
            <Link to="/alerts" className={QUEUE_LINK}>
              Open all alerts
            </Link>
            {totals}
          </div>
        </div>
      );
    }
    return (
      <div className="flex flex-col gap-4">
        <AlertsSummaryTiles summary={summary} />
        {totals}

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <section aria-label="Open alerts by type">
            <h3 className="mb-1.5 text-[12px] font-semibold uppercase tracking-wide text-muted">By type</h3>
            <ul className="flex flex-col gap-1 text-[13px]">
              {kinds.map(([kind, count]) => (
                <li key={kind} className="flex items-center justify-between gap-3">
                  <Link to={`/alerts?kind=${encodeURIComponent(kind)}`} className="truncate text-ink underline decoration-transparent underline-offset-2 hover:decoration-current">
                    {kindLabel(kind)}
                  </Link>
                  <span className="tabular-nums text-ink-2">{formatCount(count)}</span>
                </li>
              ))}
            </ul>
          </section>

          <section aria-label="Latest open alerts">
            <h3 className="mb-1.5 text-[12px] font-semibold uppercase tracking-wide text-muted">Latest</h3>
            {/* The rows that go with these counts are still on their way:
                placeholders, not the rows that went with the counts before. */}
            {latestFor !== loadedAt ? (
              <SkeletonRows rows={3} />
            ) : latest === null ? (
              <p role="status" className="text-[12px] text-ink-2">
                Could not load the latest alerts: {latestError}
              </p>
            ) : latest.length === 0 ? (
              <p className="text-[12px] text-muted">Nothing to show.</p>
            ) : (
              <>
                {latestError !== null ? (
                  <p role="status" className="mb-1 text-[12px] text-ink-2">
                    Showing the rows that last loaded — the refresh failed: {latestError}
                  </p>
                ) : null}
                <ul className="flex flex-col gap-1.5 text-[13px]">
                  {latest.map((row) => (
                    <li key={row.id} className="flex items-start gap-2">
                      <AlertSeverityBadge severity={row.severity} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-ink" title={row.message}>
                          {row.message}
                        </span>
                        <span className="block text-[12px] text-muted">
                          {kindLabel(row.kind)} · raised {formatRelative(row.created_at)}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <Link to="/alerts" className={QUEUE_LINK}>
            Open all alerts
          </Link>
          <p className="text-[12px] text-ink-2">
            Each one is a row someone has to act on or close. Resolving keeps it on file, so the
            same problem coming back reopens it instead of arriving as new.
          </p>
        </div>
      </div>
    );
  })();

  return (
    <Card
      title="Needs attention"
      description="Alerts raised over the synced order data — the queue of things a run cannot fix by itself."
      className={className}
      actions={
        showRefresh ? (
          <Button size="sm" onClick={reload} loading={loading && summary !== null}>
            {loading && summary !== null ? "Refreshing…" : "Refresh"}
          </Button>
        ) : undefined
      }
    >
      <div className="flex flex-col gap-3">
        {/* Not while the next try is on its way: the counts it speaks of are not on screen then. */}
        {error !== null && summary !== null && !loading ? (
          <p role="status" className="text-[12px] text-ink-2">
            Showing the counts that last loaded
            {loadedAt === null ? "" : ` (${formatRelative(loadedAt)})`} — the refresh failed:{" "}
            {error}
          </p>
        ) : null}
        {body}
      </div>
    </Card>
  );
}

export default AlertsSummaryPanel;
