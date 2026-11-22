import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { errorMessage, getStats, listJobs } from "../lib/api";
import type { Job, JobType, Stats } from "../lib/types";
import {
  clampProgress,
  countLabel,
  formatCount,
  formatDateTime,
  formatPercent,
  formatRelative,
  formatTime,
} from "../lib/format";
import { Alert } from "../components/Alert";
import { AlertsSummaryPanel, refreshAlertSummary } from "../components/AlertsSummary";
import { ChangesPanel, refreshChanges } from "../components/ChangesFeed";
import { OutForDelivery } from "../components/OutForDelivery";
import { AccountsOverview } from "../components/AccountsOverview";
import { ToBeDelivered } from "../components/ToBeDelivered";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { SkeletonRows } from "../components/Skeleton";
import { StatTile } from "../components/StatTile";
import { StatusBadge, statusMeta } from "../components/StatusBadge";
import { Table, type Column } from "../components/Table";
import { DashboardStatusChart, type StatusBarDatum } from "../components/DashboardStatusChart";

/** How many recent jobs the table shows. */
const RECENT_LIMIT = 8;
/** Poll cadence — only ever used while a job is actually running. */
const POLL_MS = 5000;

/** "58s", "4m 12s", "1h 3m": from start to finish, or so far for a run still going. */
export function timeTaken(job: Pick<Job, "started_at" | "finished_at" | "status">, now: number = Date.now()): string {
  if (!job.started_at) return "—";
  const start = Date.parse(job.started_at);
  const end = job.finished_at ? Date.parse(job.finished_at) : job.status === "running" ? now : NaN;
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "—";
  const seconds = Math.round((end - start) / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

interface Snapshot {
  stats: Stats;
  jobs: Job[];
}

function jobLabel(type: JobType): string {
  switch (type) {
    case "login":
      return "Login";
    case "coupon":
      return "Coupon";
    case "order":
      return "Order";
    case "sync":
      return "Sync";
    case "compile":
      return "Compile invoices";
    default:
      return "Job";
  }
}

/** Where a job's results are, so a row can link to them. */
function jobRoute(job: Job): string {
  switch (job.type) {
    case "login":
      return "/sessions";
    case "coupon":
      return "/coupons";
    // An ordering run's rows live in the ledger, filtered to that run; the
    // Place orders page only shows a run started in the same session.
    case "order":
      return `/order-units?job_id=${job.id}`;
    // A sync is started from the ledger page, where what it read back is shown;
    // a compilation too, and its archive is under "Download compiled invoices".
    case "sync":
    case "compile":
      return "/order-units";
    default:
      return "/";
  }
}

/** Why a load was started: the first paint, the page's Refresh, or the poll. */
type LoadReason = "initial" | "refresh" | "poll";

/** What a card says when the first load failed and nothing is on its way. */
function NotLoaded() {
  return <p className="text-[13px] text-ink-2">Not loaded — use Retry above.</p>;
}

/** Grey tiles in the totals' place while the numbers are on their way. */
function SkeletonTiles({ count }: { count: number }) {
  return (
    <>
      {Array.from({ length: count }, (_, index) => (
        <div
          key={index}
          aria-hidden="true"
          className="h-[92px] animate-pulse rounded-card border border-line bg-page"
        />
      ))}
    </>
  );
}

export default function Dashboard() {
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  /** Bumped by the page's Refresh, so the cards with their own data reload with it. */
  const [version, setVersion] = useState(0);
  /* A poll tick and a Refresh can be in the air together; only the newest
     reply may land, and "Refreshing…" ends only when the newest has. */
  const ticket = useRef(0);
  const refreshTicket = useRef(0);

  const load = useCallback(async (reason: LoadReason) => {
    const mine = ++ticket.current;
    if (reason === "refresh") {
      refreshTicket.current = mine;
      setBusy(true);
      // One Refresh for the whole page, and all of it at once: the delivery
      // card and the alert counts reload in parallel with the tiles, whether
      // or not the tiles' own requests succeed, and each panel shows its
      // placeholders until its own reply lands. The first paint needs neither
      // (each card fetches as it mounts, and the nav badge has already asked
      // for the counts), and the poll leaves them alone — it exists for job
      // progress, and would otherwise fan out to five requests a tick.
      setVersion((current) => current + 1);
      refreshAlertSummary();
      // The changes list and its nav badge share one store, like the alert counts.
      void refreshChanges();
    }
    try {
      const [stats, jobs] = await Promise.all([getStats(), listJobs(RECENT_LIMIT)]);
      if (!alive.current || mine !== ticket.current) return;
      setSnapshot({ stats, jobs });
      setUpdatedAt(new Date().toISOString());
      setError(null);
    } catch (err) {
      if (!alive.current || mine !== ticket.current) return;
      setError(errorMessage(err));
    } finally {
      if (alive.current && mine === ticket.current) {
        setLoading(false);
        if (refreshTicket.current !== 0 && mine >= refreshTicket.current) {
          refreshTicket.current = 0;
          setBusy(false);
        }
      }
    }
  }, []);

  useEffect(() => {
    void load("initial");
  }, [load]);

  // Poll ONLY while something is running. Nothing running, nothing polled.
  const polling = (snapshot?.stats.jobs_running ?? 0) > 0;
  useEffect(() => {
    if (!polling) return;
    const id = window.setInterval(() => {
      void load("poll");
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [polling, load]);

  const stats = snapshot?.stats ?? null;
  const jobs = snapshot?.jobs ?? null;
  /** Placeholders in place of the totals, the chart and the jobs (spec
   *  FR-049): before the first numbers land, and from the moment the page's
   *  Refresh or Retry is pressed until its reply lands, so the numbers from
   *  before it never pass for its answer. The five-second poll is not the
   *  user's Refresh and leaves `busy` alone: job progress keeps its figures
   *  on screen rather than blinking every tick. */
  const placeholders = busy || (snapshot === null && loading);

  const chartData = useMemo<StatusBarDatum[]>(
    () =>
      stats
        ? [
            { status: "placed", value: stats.orders_placed },
            { status: "failed_over_target", value: stats.orders_over_target },
            // The run's own failures, and apart from them the orders Flipkart
            // took and cancelled later: until 26 Oct 2026 one red bar held both.
            { status: "failed", value: stats.orders_failed },
            { status: "cancelled_by_flipkart", value: stats.orders_cancelled ?? 0 },
            { status: "cancelled_by_user", value: stats.orders_cancelled_by_user ?? 0 },
            { status: "unconfirmed", value: stats.orders_unconfirmed },
          ]
        : [],
    [stats],
  );

  const ordersTotal = chartData.reduce((sum, row) => sum + row.value, 0);

  const columns = useMemo<Column<Job>[]>(
    () => [
      {
        key: "type",
        header: "Job",
        width: "26%",
        render: (job) => (
          <Link
            to={jobRoute(job)}
            className="inline-flex items-baseline gap-2 font-medium text-ink underline decoration-transparent underline-offset-2 transition-colors hover:decoration-current"
            title={
              job.type === "order"
                ? "Open this run's orders in the ledger"
                : `Open the page this ${jobLabel(job.type).toLowerCase()} run was started from`
            }
          >
            <span>{jobLabel(job.type)}</span>
            <span className="text-[12px] font-normal text-muted">#{job.id}</span>
          </Link>
        ),
      },
      {
        key: "status",
        header: "Status",
        width: "150px",
        // A compilation the operator cancelled ends "completed by request",
        // like a cancelled sync; say so rather than show a green Completed.
        render: (job) =>
          job.payload?.outcome === "cancelled" ? (
            <StatusBadge status="skipped" label="Cancelled" />
          ) : (
            <StatusBadge status={job.status} />
          ),
      },
      {
        key: "progress",
        header: "Progress",
        width: "28%",
        render: (job) => {
          const pct = clampProgress(job.progress);
          return (
            <div className="flex flex-col gap-1">
              <div className="flex items-center gap-2">
                <span
                  aria-hidden="true"
                  className="inline-block h-2 w-20 shrink-0 overflow-hidden rounded-full border border-line bg-page"
                >
                  <span
                    className="block h-full rounded-full"
                    style={{
                      width: `${pct}%`,
                      backgroundColor: statusMeta(job.status).colorVar,
                    }}
                  />
                </span>
                <span className="tabular-nums text-[13px] text-ink-2">
                  {formatCount(job.done_items)}/{formatCount(job.total_items)}
                </span>
              </div>
              {job.failed_items > 0 ? (
                <span className="text-[12px] text-ink-2">
                  {formatCount(job.failed_items)} did not complete
                </span>
              ) : null}
              {(job.unconfirmed_items ?? 0) > 0 ? (
                <span className="text-[12px] font-medium text-ink">
                  {formatCount(job.unconfirmed_items)} unconfirmed — check before re-running
                </span>
              ) : null}
            </div>
          );
        },
      },
      {
        key: "time_taken",
        header: "Time taken",
        render: (job) => <span className="tabular-nums text-ink-2">{timeTaken(job)}</span>,
      },
      {
        key: "started_at",
        header: "Started",
        render: (job) => (
          <span className="text-ink-2" title={formatDateTime(job.started_at)}>
            {formatRelative(job.started_at)}
          </span>
        ),
      },
    ],
    [],
  );

  const sessionShare =
    stats && stats.accounts > 0 ? (stats.sessions_valid / stats.accounts) * 100 : null;

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold leading-tight tracking-tight text-ink">
            Dashboard
          </h1>
          <p className="mt-1 max-w-prose text-[13px] leading-relaxed text-ink-2">
            Where every account, session and order stands right now.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <p role="status" aria-live="polite" className="text-[12px] text-muted">
            {busy
              ? "Refreshing…"
              : polling
                ? `Live — updating every 5s · last update ${formatTime(updatedAt)}`
                : updatedAt
                  ? `Updated ${formatTime(updatedAt)}`
                  : ""}
          </p>
          {/* The one Refresh on the page: tiles, the changes list, the delivery
              card, the alert counts and the jobs table all reload from it. */}
          <Button onClick={() => void load("refresh")} loading={busy}>
            {busy ? "Refreshing…" : "Refresh"}
          </Button>
        </div>
      </header>

      {/* Not while a Refresh is on its way: the values it speaks of are not on
          screen then, and its reply decides whether they are stale. */}
      {error !== null && snapshot !== null && !placeholders ? (
        <Alert tone="warning" title="These numbers may be stale">
          {error} The last values that loaded are shown below.
        </Alert>
      ) : null}
      {error !== null && snapshot === null ? (
        <Alert tone="error" title="Could not load the dashboard totals">
          <span className="block">{error}</span>
          <Button variant="primary" className="mt-3" onClick={() => void load("refresh")} loading={busy}>
            {busy ? "Retrying…" : "Retry"}
          </Button>
        </Alert>
      ) : null}

      {/* Placeholder tiles while the numbers are on their way — the first
          load, a Refresh, a Retry (spec FR-049) — so the page keeps its shape
          and the cards below load in parallel. */}
      {stats !== null || placeholders ? (
        <section
          aria-label="Totals"
          aria-busy={placeholders}
          className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4"
        >
          {stats === null || placeholders ? (
            <SkeletonTiles count={9} />
          ) : (
            <>
              <StatTile label="Accounts" value={formatCount(stats.accounts)} hint="Loaded into the suite" />
              <StatTile
                label="Sessions valid"
                value={formatCount(stats.sessions_valid)}
                hint={
                  sessionShare === null
                    ? "No accounts yet"
                    : `${formatPercent(sessionShare)} of ${countLabel(stats.accounts, "account")}`
                }
              />
              <StatTile
                label="Coupons applied"
                value={formatCount(stats.coupons_applied)}
                hint="Accounts with a coupon claimed"
              />
              <StatTile
                label="Orders placed"
                value={formatCount(stats.orders_placed)}
                tone="good"
                hint="Placed by a run at or below target, or handed in by id to track"
              />
              <StatTile
                label="Over target"
                value={formatCount(stats.orders_over_target)}
                tone="warning"
                hint="Final amount was above the cap, so the order was not placed"
              />
              <StatTile
                label="Failed"
                value={formatCount(stats.orders_failed)}
                tone="critical"
                hint="Stopped before anything was sent to Flipkart"
              />
              <StatTile
                label="Cancelled by Flipkart"
                value={formatCount(stats.orders_cancelled ?? 0)}
                tone="warning"
                hint={
                  "Placed on Flipkart, then cancelled there, or never placed because the payment failed — the reason is on each order" +
                  (stats.orders_cancelled_by_user
                    ? ` · ${formatCount(stats.orders_cancelled_by_user)} more cancelled at the account's request`
                    : "")
                }
              />
              <StatTile
                label="Unconfirmed"
                value={formatCount(stats.orders_unconfirmed)}
                tone="critical"
                hint="Submitted but never confirmed — may exist, check before retrying"
              />
              <StatTile
                label="Jobs running"
                value={formatCount(stats.jobs_running)}
                hint={polling ? "Refreshing every 5 seconds" : "Nothing running right now"}
              />
            </>
          )}
        </section>
      ) : null}

      {/* What the read-backs found since you last looked: first, because it is
          the news. Its own store, shared with the nav badge; the page's
          Refresh reloads it and it reloads itself while on screen. */}
      <AccountsOverview refreshKey={version} />

      <ChangesPanel />

      <OutForDelivery refreshKey={version} />

      <ToBeDelivered refreshKey={version} />

      {/* Its own counts and loading state. The page's Refresh reloads it; the
          five-second polling, which exists for job progress, leaves it and the
          delivery card alone. */}
      <AlertsSummaryPanel showRefresh={false} />

      <Card
        title="Orders by status"
        description={
          // The total is a figure too: not shown while placeholders stand in for the rest.
          ordersTotal > 0 && !placeholders
            ? `${countLabel(ordersTotal, "order")} placed by runs or tracked by upload so far.`
            : "Counts across every run and upload so far."
        }
      >
        {placeholders ? (
          <SkeletonRows rows={4} />
        ) : stats === null ? (
          <NotLoaded />
        ) : ordersTotal > 0 ? (
          <>
            <DashboardStatusChart data={chartData} title="Orders by status" unit="orders" />
            <p className="mt-4 max-w-prose text-[12px] leading-relaxed text-ink-2">
              <span className="font-medium text-ink">Over target</span> means the final payable
              amount came out above the cap for that one order, so it was not placed — over by even
              ₹0.01 is enough. Every other account in the run carried on normally.
            </p>
            <p className="mt-2 max-w-prose text-[12px] leading-relaxed text-ink-2">
              <span className="font-medium text-ink">Failed</span> means the run stopped before anything
              was sent to Flipkart. <span className="font-medium text-ink">Cancelled by Flipkart</span>{" "}
              means Flipkart took the order and cancelled it afterwards; the reason it gave is on each
              order in Order Units.
            </p>
          </>
        ) : (
          <EmptyState
            title="No orders yet"
            description="Start a run from Place orders and the breakdown by status shows up here."
            action={
              <Link
                to="/ordering"
                className="inline-flex h-[38px] items-center rounded-control border border-line px-4 text-sm font-medium text-ink transition-colors hover:bg-page"
              >
                Go to Place orders
              </Link>
            }
          />
        )}
      </Card>

      <Card title="Recent jobs" description={`The last ${RECENT_LIMIT} runs, newest first.`}>
        {placeholders ? (
          <SkeletonRows rows={5} />
        ) : jobs === null ? (
          <NotLoaded />
        ) : jobs.length === 0 ? (
          <EmptyState
            title="No runs yet"
            description="Generate sessions first, then apply a coupon, then place orders. Every run you start shows up here."
            action={
              <Link
                to="/sessions"
                className="inline-flex h-[38px] items-center rounded-control border border-line px-4 text-sm font-medium text-ink transition-colors hover:bg-page"
              >
                Go to Sessions
              </Link>
            }
          />
        ) : (
          <Table
            columns={columns}
            rows={jobs}
            rowKey={(job) => job.id}
            caption="Recent jobs, newest first"
            minWidth="640px"
          />
        )}
      </Card>
    </div>
  );
}
