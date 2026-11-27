import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { cancelJob, errorMessage, getJob, listBulkOrderCreators, listBulkOrders } from "../lib/api";
import type { AiTrackerState, BulkOrderRun, BulkOrderRunFilters, JobDetail } from "../lib/types";
import { jobNotes, PAYMENT_MODE_LABELS } from "../lib/types";
import { overrideLines } from "../lib/limitChanges";
import { DASH, formatCount, formatCurrency, formatDateTime, formatLink, truncate } from "../lib/format";
import { AiTrackerCard } from "../components/AiTrackerCard";
import { Alert } from "../components/Alert";
import { AgentsBar } from "../components/AgentsBar";
import OrderUnits from "./OrderUnits";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Dialog } from "../components/Dialog";
import { EmptyState } from "../components/EmptyState";
import { Select, TextInput } from "../components/Field";
import { SkeletonRows } from "../components/Skeleton";
import { StatusBadge } from "../components/StatusBadge";
import { Table, type Column } from "../components/Table";
import { timeTaken } from "./Dashboard";

/** The page reads the runs again this often while it is open. */
export const ORDERS_REFRESH_MS = 30_000;

const PAYMENT_LABELS: Record<string, string> = PAYMENT_MODE_LABELS;

export function paymentLabel(mode: string | null | undefined): string {
  if (!mode) return DASH;
  return PAYMENT_LABELS[mode] ?? mode;
}

/** "100 total · 4 per order", or what the run was given. */
export function quantityLabel(run: Pick<BulkOrderRun, "total_quantity" | "qty_per_order">): string {
  const parts: string[] = [];
  if (run.total_quantity) parts.push(`${formatCount(run.total_quantity)} total`);
  if (run.qty_per_order) parts.push(`${formatCount(run.qty_per_order)} per order`);
  return parts.length ? parts.join(" · ") : DASH;
}

/** Units placed against what was asked: "18/20 · 90%", or a bare count when no total was set. */
export function unitsLabel(run: Pick<BulkOrderRun, "units_placed" | "total_quantity">): string {
  if (!run.total_quantity) return formatCount(run.units_placed);
  const pct = Math.round((run.units_placed / run.total_quantity) * 100);
  return `${formatCount(run.units_placed)}/${formatCount(run.total_quantity)} · ${pct}%`;
}

/** The share of the asked total placed so far, 0–100 (orders of attempts when no total was set). */
export function unitsShare(
  run: Pick<BulkOrderRun, "units_placed" | "total_quantity" | "orders_placed" | "total_items">,
): number {
  const whole = run.total_quantity || run.total_items;
  if (!whole) return 0;
  const part = run.total_quantity ? run.units_placed : run.orders_placed;
  return Math.max(0, Math.min(100, Math.round((part / whole) * 100)));
}

/** Date presets, as the Created filter offers them. */
const PRESETS: [string, string, number | null][] = [
  ["24h", "Last 24 hours", 1],
  ["7d", "Last 7 days", 7],
  ["30d", "Last 30 days", 30],
  ["all", "All time", null],
];

export function presetRange(preset: string, now: number = Date.now()): { since?: string } {
  const days = PRESETS.find(([value]) => value === preset)?.[2];
  if (!days) return {};
  return { since: new Date(now - days * 86_400_000).toISOString() };
}

const STATUS_OPTIONS: [string, string][] = [
  ["", "All statuses"],
  ["running", "Running"],
  ["queued", "Queued"],
  ["completed", "Completed"],
  ["failed", "Failed / cancelled"],
];

const MODE_OPTIONS: [string, string][] = [
  ["", "All orders"],
  ["real", "Real orders"],
  ["ai", "AI orders"],
  ["mock", "Mock orders"],
];

function ModeBadge({ mode }: { mode: string }) {
  if (mode === "real") return null;
  const label = mode === "ai" ? "AI" : mode === "mock" ? "Mock" : mode;
  const tone = mode === "ai" ? "bg-accent/15 text-accent" : "border border-line bg-page text-ink-2";
  return <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-semibold ${tone}`}>{label}</span>;
}

function UnitsCell({ run }: { run: BulkOrderRun }) {
  const share = unitsShare(run);
  const remaining = Math.max(0, run.total_items - run.done_items);
  const whole = run.total_quantity ?? run.total_items;
  const part = run.total_quantity ? run.units_placed : run.orders_placed;
  return (
    <div className="flex min-w-[150px] flex-col gap-1 text-[12px]">
      <div className="flex items-center justify-between gap-2 tabular-nums">
        <span className="text-ink">
          {formatCount(part)}/{formatCount(whole)}
        </span>
        <span className="text-ink-2">{share}%</span>
      </div>
      <span aria-hidden="true" className="block h-1.5 overflow-hidden rounded-full bg-[var(--grid)]">
        <span className="block h-full rounded-full" style={{ width: `${share}%`, backgroundColor: "var(--status-good)" }} />
      </span>
      <span className="flex flex-wrap gap-x-2 tabular-nums">
        <span style={{ color: "var(--status-good)" }}>✓ {formatCount(run.orders_placed)}</span>
        <span style={{ color: "var(--status-critical)" }}>✗ {formatCount(run.orders_failed)}</span>
        {remaining > 0 ? <span className="text-ink-2">⏱ {formatCount(remaining)}</span> : null}
      </span>
      <span className="text-accent">Total: {formatCount(run.total_items)}</span>
    </div>
  );
}

/** "Retry failed" shows on a finished run with accounts that failed or were
 *  never really tried; the form then reads exactly which ones (GET /jobs/{id}/retry). */
export function canRetryFailed(run: BulkOrderRun): boolean {
  if (run.status === "running" || run.status === "queued") return false;
  return run.failed_items > 0 || run.skipped_items > 0 || run.done_items < run.total_items;
}

function IconButton({ label, onClick, children }: { label: string; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      className="inline-flex size-7 items-center justify-center rounded-control border border-line text-ink-2 hover:bg-page hover:text-ink"
    >
      {children}
    </button>
  );
}

/** Runs as a CSV file, the way the table shows them. */
export function runsCsv(runs: BulkOrderRun[]): string {
  const head = [
    "Run", "Created", "Platform", "Mode", "Status", "Product", "Product link", "Total quantity", "Per order",
    "Units placed", "Orders placed", "Orders failed", "Attempts", "Amount placed", "User", "Ref code",
    "Card type", "Parent card", "Address label", "GST label", "Payment mode", "Time taken",
  ];
  const cell = (value: unknown) => {
    const text = value === null || value === undefined ? "" : String(value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const rows = runs.map((run) => [
    run.id, run.started_at, run.platform, run.mode, run.status, run.product_name, run.product_link, run.total_quantity,
    run.qty_per_order, run.units_placed, run.orders_placed, run.orders_failed, run.total_items, run.amount_placed,
    run.started_by, run.ref_code, run.card_type, run.parent_card, run.address_label, run.gst_label,
    paymentLabel(run.payment_mode), timeTaken(run),
  ]);
  return [head, ...rows].map((row) => row.map(cell).join(",")).join("\n") + "\n";
}

/** Saved settings shown in words by the Run settings dialog, not as raw keys
 *  (the AI tracker has a card of its own). */
const WORDED_SETTINGS = new Set([
  "smart_id",
  "smart_selection",
  "notes",
  "limit_overrides",
  "ai_tracker",
  "retry_of",
  "card_exclude_used",
  "cards_kept_out",
]);

function SettingRow({ label, raw = false, children }: { label: string; raw?: boolean; children: ReactNode }) {
  return (
    <div className="contents">
      <dt className="text-ink-2">{label}</dt>
      <dd className={`${raw ? "break-all" : "break-words"} text-ink`}>{children}</dd>
    </div>
  );
}

/**
 * A run's saved settings, as the Run settings dialog lists them: Smart ID
 * Selection, "Spread orders across accounts" and the run's notes in words
 * first, then every other saved setting as it was saved. A run from before
 * Smart ID Selection saved no value for it, and says so.
 */
export function RunSettingsList({ payload }: { payload: Record<string, unknown> | null }) {
  const saved = payload ?? {};
  const smartId =
    saved.smart_id === true ? "On" : saved.smart_id === false ? "Off" : "not available (older run)";
  const notes = jobNotes(saved);
  const limitChanges = overrideLines(saved.limit_overrides);
  const rest = Object.entries(saved).filter(([key]) => !WORDED_SETTINGS.has(key));
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-[13px]">
      {typeof saved.retry_of === "number" ? (
        <SettingRow label="Retry of">Run #{saved.retry_of}: its accounts that got no order</SettingRow>
      ) : null}
      <SettingRow label="Smart ID Selection">{smartId}</SettingRow>
      {typeof saved.smart_selection === "boolean" ? (
        <SettingRow label="Spread orders across accounts">{saved.smart_selection ? "On" : "Off"}</SettingRow>
      ) : null}
      {typeof saved.card_exclude_used === "boolean" ? (
        <SettingRow label="Skip cards already used">{saved.card_exclude_used ? "On" : "Off"}</SettingRow>
      ) : null}
      {typeof saved.cards_kept_out === "number" ? (
        <SettingRow label="Cards left out before pairing">
          {saved.cards_kept_out} (already used in an order, or kept out by another card rule)
        </SettingRow>
      ) : null}
      {limitChanges.length > 0 ? (
        <SettingRow label="Limits for this order">
          <ul className="flex flex-col gap-0.5">
            {limitChanges.map((line, index) => (
              <li key={index}>{line}</li>
            ))}
          </ul>
        </SettingRow>
      ) : null}
      {notes.length > 0 ? (
        <SettingRow label="Notes">
          <ul className="flex flex-col gap-0.5">
            {notes.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        </SettingRow>
      ) : null}
      {rest.map(([key, value]) => (
        <SettingRow key={key} label={key.replace(/_/g, " ")} raw>
          {typeof value === "object" ? JSON.stringify(value) : String(value)}
        </SettingRow>
      ))}
    </dl>
  );
}

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

type Tab = "bulk" | "all" | "csv";

/**
 * Orders: one row per bulk order run, with what it asked for and what it
 * placed. "All Orders" is the Order Units ledger, one row per unit; "CSV
 * Downloads" the exports.
 */
export default function Orders() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>("bulk");
  const [runs, setRuns] = useState<BulkOrderRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stopping, setStopping] = useState<number | null>(null);
  const [creators, setCreators] = useState<string[]>([]);
  const [preset, setPreset] = useState("all");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [status, setStatus] = useState("");
  const [mode, setMode] = useState("");
  const [creator, setCreator] = useState("");
  const [platform, setPlatform] = useState("");
  const [picked, setPicked] = useState<number[]>([]);
  const [details, setDetails] = useState<{ kind: "settings" | "errors"; job: JobDetail } | null>(null);

  const filters = useMemo<BulkOrderRunFilters>(() => {
    const range = startDate || endDate ? {} : presetRange(preset);
    return {
      ...range,
      ...(startDate ? { since: new Date(`${startDate}T00:00:00`).toISOString() } : {}),
      ...(endDate ? { until: new Date(new Date(`${endDate}T00:00:00`).getTime() + 86_400_000).toISOString() } : {}),
      ...(status ? { status: [status] } : {}),
      ...(mode ? { mode } : {}),
      ...(creator ? { created_by: creator } : {}),
      ...(platform ? { platform } : {}),
    };
  }, [preset, startDate, endDate, status, mode, creator, platform]);

  const load = useCallback(async () => {
    try {
      setRuns(await listBulkOrders(200, filters));
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [filters]);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), ORDERS_REFRESH_MS);
    return () => window.clearInterval(id);
  }, [load]);

  useEffect(() => {
    listBulkOrderCreators().then(setCreators, () => undefined);
  }, []);

  const stop = useCallback(
    async (run: BulkOrderRun) => {
      setStopping(run.id);
      try {
        await cancelJob(run.id);
        await load();
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        setStopping(null);
      }
    },
    [load],
  );

  const open = useCallback(async (run: BulkOrderRun, kind: "settings" | "errors") => {
    try {
      setDetails({ kind, job: await getJob(run.id) });
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  const shownRuns = runs ?? [];
  const allPicked = shownRuns.length > 0 && picked.length === shownRuns.length;

  const columns = useMemo<Column<BulkOrderRun>[]>(
    () => [
      {
        key: "pick",
        header: (
          <input
            type="checkbox"
            aria-label="Select every run"
            className="size-4 accent-accent"
            checked={allPicked}
            onChange={() => setPicked(allPicked ? [] : shownRuns.map((run) => run.id))}
          />
        ),
        width: "36px",
        render: (run) => (
          <input
            type="checkbox"
            aria-label={`Select run ${run.id}`}
            className="size-4 accent-accent"
            checked={picked.includes(run.id)}
            onChange={() =>
              setPicked((current) =>
                current.includes(run.id) ? current.filter((id) => id !== run.id) : [...current, run.id],
              )
            }
          />
        ),
      },
      {
        key: "created",
        header: "Created",
        width: "104px",
        render: (run) => (
          <span className="whitespace-nowrap text-[12px] text-ink-2" title={`Run #${run.id}`}>
            {formatDateTime(run.started_at)}
          </span>
        ),
      },
      {
        key: "platform",
        header: "Platform",
        render: (run) => (
          <span className="flex items-center gap-1.5 text-[12px] font-medium uppercase text-ink">
            {run.platform}
            <ModeBadge mode={run.mode} />
          </span>
        ),
      },
      {
        key: "status",
        header: "Status",
        render: (run) =>
          run.status === "failed" && run.cancel_requested ? (
            <StatusBadge status="failed" label="Cancelled" />
          ) : (
            <StatusBadge status={run.status} />
          ),
      },
      {
        key: "product",
        header: "Product",
        width: "220px",
        render: (run) => {
          const tracker = (run.payload as { ai_tracker?: AiTrackerState } | null | undefined)?.ai_tracker;
          return run.product_link ? (
            <div className="flex flex-col gap-0.5">
              <a
                href={run.product_link}
                target="_blank"
                rel="noreferrer noopener"
                className="block text-[12px] leading-snug text-ink hover:underline"
                title={run.product_name ?? run.product_link}
              >
                {run.product_name ? truncate(run.product_name, 60) : formatLink(run.product_link, 40)}
              </a>
              {tracker ? (
                <button
                  type="button"
                  onClick={() => void open(run, "settings")}
                  className="w-fit text-left text-[11px] tabular-nums text-accent hover:underline"
                >
                  {tracker.last_seen_price != null
                    ? `Live ${formatCurrency(tracker.last_seen_price)}`
                    : tracker.in_stock === false
                      ? "Out of stock"
                      : "AI tracker"}
                  {tracker.lowest_seen_price != null ? ` · low ${formatCurrency(tracker.lowest_seen_price)}` : ""}
                  {` · ${tracker.status.replace(/_/g, " ")}`}
                </button>
              ) : null}
            </div>
          ) : (
            DASH
          );
        },
      },
      {
        key: "quantity",
        header: "Quantity",
        render: (run) => (
          <span className="flex flex-col text-[12px] leading-tight">
            <span className="text-ink">{run.total_quantity ? `${formatCount(run.total_quantity)} total` : "1 per account"}</span>
            <span className="text-ink-2">{run.qty_per_order ?? 1}/order</span>
          </span>
        ),
      },
      { key: "units", header: "Units", render: (run) => <UnitsCell run={run} /> },
      {
        key: "amount",
        header: "Amount",
        numeric: true,
        render: (run) => (run.amount_placed ? formatCurrency(run.amount_placed) : DASH),
      },
      { key: "user", header: "User", render: (run) => <span className="text-[12px]">{run.started_by ?? DASH}</span> },
      {
        key: "ref_code",
        header: "Ref Code",
        render: (run) =>
          run.ref_code ? (
            <span className="rounded border border-line px-1.5 py-0.5 font-mono text-[11px]">{run.ref_code}</span>
          ) : (
            DASH
          ),
      },
      { key: "card_type", header: "Card Type", render: (run) => <span className="text-[12px]">{run.card_type ?? DASH}</span> },
      // "Parent Cards" is left out until cards carry a parent card (audit of
      // 19 Nov 2026: the column was a label only).
      {
        key: "address_label",
        header: "Address Label",
        render: (run) => (
          <span className="flex flex-wrap items-center gap-1.5 text-[12px]">
            {run.address_label ?? DASH}
            {run.flipkart_minutes ? (
              <span className="rounded px-1.5 py-0.5 text-[11px] font-semibold text-white" style={{ backgroundColor: "#d6336c" }}>
                Minutes
              </span>
            ) : null}
          </span>
        ),
      },
      { key: "gst_label", header: "GST Label", render: (run) => <span className="text-[12px]">{run.gst_label ?? DASH}</span> },
      {
        key: "payment",
        header: "Payment Mode",
        render: (run) => (
          <span className="rounded border border-line px-1.5 py-0.5 text-[11px]">{paymentLabel(run.payment_mode)}</span>
        ),
      },
      {
        key: "time",
        header: "Time Taken",
        render: (run) => <span className="tabular-nums text-[12px] text-ink-2">{timeTaken(run)}</span>,
      },
      {
        key: "actions",
        header: "Actions",
        sticky: "right",
        render: (run) => (
          <div className="flex items-center gap-1.5">
            <Link
              to={`/order-units?job_id=${run.id}`}
              className="inline-flex h-7 items-center whitespace-nowrap rounded-control border border-line px-2 text-[12px] font-medium text-ink hover:bg-page"
            >
              View Orders
            </Link>
            {canRetryFailed(run) ? (
              <button
                type="button"
                title="Order again on this run's accounts that got no order, with cards not used since it began"
                onClick={() => navigate(`/ordering?retry=${run.id}`)}
                className="inline-flex h-7 items-center whitespace-nowrap rounded-control border border-line px-2 text-[12px] font-medium text-ink hover:bg-page"
              >
                Retry failed
              </button>
            ) : null}
            <IconButton label="Run again with these settings" onClick={() => navigate(`/ordering?from=${run.id}`)}>
              <svg viewBox="0 0 16 16" className="size-3.5" aria-hidden="true">
                <path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v2.8h-2.8" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </IconButton>
            <IconButton label="Run settings" onClick={() => void open(run, "settings")}>
              <svg viewBox="0 0 16 16" className="size-3.5" aria-hidden="true">
                <circle cx="8" cy="8" r="2.1" fill="none" stroke="currentColor" strokeWidth="1.5" />
                <path
                  d="M8 1.8v1.6M8 12.6v1.6M1.8 8h1.6M12.6 8h1.6M3.6 3.6l1.1 1.1M11.3 11.3l1.1 1.1M12.4 3.6l-1.1 1.1M4.7 11.3l-1.1 1.1"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                />
              </svg>
            </IconButton>
            <IconButton label="Errors" onClick={() => void open(run, "errors")}>
              <svg viewBox="0 0 16 16" className="size-3.5" aria-hidden="true" style={{ color: "var(--status-critical)" }}>
                <path
                  d="M8 2 14.5 13.5h-13z M8 6.5v3.2M8 11.6v.2"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinejoin="round"
                  strokeLinecap="round"
                />
              </svg>
            </IconButton>
            {(run.status === "running" || run.status === "queued") && !run.cancel_requested ? (
              <Button size="sm" variant="secondary" loading={stopping === run.id} onClick={() => void stop(run)}>
                Stop
              </Button>
            ) : null}
          </div>
        ),
      },
    ],
    [stop, stopping, open, navigate, picked, allPicked, shownRuns],
  );

  const toDownload = picked.length ? shownRuns.filter((run) => picked.includes(run.id)) : shownRuns;
  const failures = details?.kind === "errors" ? details.job.items.filter((item) => item.status !== "done") : [];

  return (
    <div className="flex flex-col gap-4">
      <h1 className="sr-only">Orders</h1>
      <div role="tablist" aria-label="Orders views" className="grid grid-cols-3 rounded-control border border-line bg-page p-1">
        {(
          [
            ["bulk", "Bulk Orders"],
            ["all", "All Orders"],
            ["csv", "CSV Downloads"],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            role="tab"
            type="button"
            aria-selected={tab === value}
            onClick={() => setTab(value)}
            className={`rounded-[6px] px-3 py-2 text-[13px] font-medium ${
              tab === value ? "bg-surface text-ink shadow-sm" : "text-ink-2 hover:text-ink"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {error ? <Alert tone="error">{error}</Alert> : null}

      {tab === "bulk" ? (
        <Card>
          <div className="flex flex-col gap-4">
            <AgentsBar />
            <p className="text-[13px] text-ink-2">
              {runs === null ? "Loading…" : `${formatCount(runs.length)} bulk orders`} • Auto-refreshing every 30s
            </p>
            <section className="flex flex-col gap-3 rounded-card border border-line p-4">
              <div className="flex items-center justify-between gap-2">
                <h2 className="text-[13px] font-semibold text-ink">Filters</h2>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={toDownload.length === 0}
                  onClick={() => download(`bulk_orders_${new Date().toISOString().slice(0, 10)}.csv`, runsCsv(toDownload))}
                >
                  Download CSV
                </Button>
              </div>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 2xl:grid-cols-8">
                <Select aria-label="Created" value={preset} onChange={(e) => setPreset(e.target.value)}>
                  {PRESETS.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </Select>
                <TextInput type="date" aria-label="Start Date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
                <TextInput type="date" aria-label="End Date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
                <Select aria-label="Platform" value={platform} onChange={(e) => setPlatform(e.target.value)}>
                  <option value="">All Platforms</option>
                  <option value="flipkart">FLIPKART</option>
                </Select>
                <Select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}>
                  {STATUS_OPTIONS.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </Select>
                <Select aria-label="Mode" value={mode} onChange={(e) => setMode(e.target.value)}>
                  {MODE_OPTIONS.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </Select>
                <Select aria-label="Created by" value={creator} onChange={(e) => setCreator(e.target.value)}>
                  <option value="">Created by</option>
                  {creators.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </Select>
                <Button size="md" variant="secondary" onClick={() => void load()}>
                  Refresh
                </Button>
              </div>
            </section>
            {runs === null ? (
              <SkeletonRows rows={6} />
            ) : (
              <Table
                caption="Bulk order runs"
                columns={columns}
                rows={runs}
                rowKey={(run) => run.id}
                minWidth="1500px"
                dense
                empty={<EmptyState title="No bulk orders" description="Runs started from Smart Bulk Order appear here, newest first." />}
              />
            )}
          </div>
        </Card>
      ) : tab === "all" ? (
        <OrderUnits embedded />
      ) : (
        <Card title="CSV Downloads" description="Exports of the runs and of every order unit.">
          <div className="flex flex-wrap gap-2">
            <Button
              variant="secondary"
              disabled={shownRuns.length === 0}
              onClick={() => download(`bulk_orders_${new Date().toISOString().slice(0, 10)}.csv`, runsCsv(shownRuns))}
            >
              Bulk orders (CSV)
            </Button>
            <Link
              to="/order-units"
              className="inline-flex h-[38px] items-center rounded-control border border-line px-4 text-sm font-medium text-ink hover:bg-page"
            >
              Order units CSV (from Order Units History)
            </Link>
          </div>
        </Card>
      )}

      <Dialog
        open={details !== null}
        title={details ? `Run #${details.job.id} ${details.kind === "settings" ? "settings" : "errors"}` : ""}
        onClose={() => setDetails(null)}
        width="720px"
      >
        {details?.kind === "settings" ? (
          <div className="flex flex-col gap-4">
            {(() => {
              const tracker = (details.job.payload as { ai_tracker?: AiTrackerState } | null | undefined)?.ai_tracker;
              const done = details.job.status === "completed" || details.job.status === "failed";
              return tracker ? <AiTrackerCard tracker={tracker} terminal={done} maxProbes={20} /> : null;
            })()}
            <RunSettingsList payload={details.job.payload} />
          </div>
        ) : details ? (
          failures.length === 0 ? (
            <p className="text-[13px] text-ink-2">Every account in this run finished without an error.</p>
          ) : (
            <ul className="flex max-h-96 flex-col gap-1 overflow-auto text-[13px]">
              {failures.map((item) => (
                <li key={item.account_id} className="rounded-control border border-line px-2.5 py-1.5">
                  <span className="font-medium text-ink">{item.email ?? `Account ${item.account_id}`}</span>{" "}
                  <span className="text-ink-2">
                    — {item.status}: {item.message ?? "no message"}
                  </span>
                </li>
              ))}
            </ul>
          )
        ) : null}
      </Dialog>
    </div>
  );
}
