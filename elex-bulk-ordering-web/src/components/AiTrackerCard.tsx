import { formatCurrency, formatTime } from "../lib/format";
import type { AiTrackerState } from "../lib/types";

export interface AiTrackerCardProps {
  tracker: AiTrackerState;
  terminal?: boolean;
  /** Show up to this many recent probes. Default 8. */
  maxProbes?: number;
}

/**
 * Shared live AI Price-Tracker panel rendered on both Smart Bulk Order
 * (ProgressStream) and the Orders run-details modal.
 */
export function AiTrackerCard({ tracker, terminal = false, maxProbes = 8 }: AiTrackerCardProps) {
  const badgeTone =
    tracker.status === "scaling_up" || tracker.status === "completed"
      ? "bg-good/15 text-ink"
      : tracker.status === "watching" || tracker.status === "cooling_down"
        ? "bg-accent/15 text-ink"
        : "bg-warning/20 text-ink";
  const recentProbes = (tracker.history ?? []).slice(-maxProbes).reverse();

  return (
    <div className="flex flex-col gap-3 rounded-control border border-line bg-page p-3.5" aria-label="AI Price Tracker">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-[12px] font-semibold uppercase tracking-wide text-ink">AI Price Tracker</span>
          <span className={`rounded px-2 py-0.5 text-[11px] font-medium ${badgeTone}`}>
            {tracker.status.replace(/_/g, " ")}
          </span>
          {tracker.in_stock === false ? (
            <span className="rounded bg-critical/15 px-2 py-0.5 text-[11px] font-medium text-critical">
              Out of stock
            </span>
          ) : null}
        </div>
        <span className="text-[11px] text-ink-2 tabular-nums">
          {tracker.next_probe_at && !terminal
            ? `Next probe: ${formatTime(tracker.next_probe_at)}`
            : tracker.last_probe_at
              ? `Last probe: ${formatTime(tracker.last_probe_at)}`
              : ""}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div className="rounded border border-line bg-surface px-2.5 py-2">
          <p className="text-[11px] text-muted">Last Seen Price</p>
          <p className="text-[13px] font-semibold tabular-nums text-ink">
            {tracker.last_seen_price != null ? formatCurrency(tracker.last_seen_price) : "—"}
          </p>
        </div>
        <div className="rounded border border-line bg-surface px-2.5 py-2">
          <p className="text-[11px] text-muted">Lowest Seen</p>
          <p className="text-[13px] font-semibold tabular-nums text-ink">
            {tracker.lowest_seen_price != null ? formatCurrency(tracker.lowest_seen_price) : "—"}
          </p>
        </div>
        <div className="rounded border border-line bg-surface px-2.5 py-2">
          <p className="text-[11px] text-muted">Target Cap</p>
          <p className="text-[13px] font-semibold tabular-nums text-ink">
            {tracker.target_price != null ? formatCurrency(tracker.target_price) : "No cap"}
          </p>
        </div>
        <div className="rounded border border-line bg-surface px-2.5 py-2">
          <p className="text-[11px] text-muted">Probes / Placed</p>
          <p className="text-[13px] font-semibold tabular-nums text-ink">
            {tracker.total_attempts} / {tracker.placed_orders}
            {tracker.recycled_attempts > 0 ? ` (${tracker.recycled_attempts} recycled)` : ""}
          </p>
        </div>
      </div>

      {tracker.last_reason ? (
        <p className="text-[12px] text-ink-2">Latest check: {tracker.last_reason}</p>
      ) : null}

      {recentProbes.length > 0 ? (
        <div className="flex flex-wrap gap-1.5 text-[11px]">
          {recentProbes.map((pt, idx) => (
            <span
              key={`${pt.at}-${idx}`}
              className="inline-flex items-center gap-1 rounded border border-line bg-surface px-2 py-0.5 tabular-nums text-ink-2"
              title={pt.reason ?? undefined}
            >
              <span className="text-muted">{formatTime(pt.at)}</span>
              <span className="font-medium text-ink">
                {pt.price != null ? formatCurrency(pt.price) : pt.in_stock === false ? "OOS" : "—"}
              </span>
              <span>
                ({pt.placed}/{pt.batch_size})
              </span>
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
