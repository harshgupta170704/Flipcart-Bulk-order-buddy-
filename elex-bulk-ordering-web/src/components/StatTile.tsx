import type { ReactNode } from "react";

export type StatTone = "default" | "good" | "warning" | "critical";

export interface StatTileProps {
  label: ReactNode;
  value: ReactNode;
  /** A short line under the number — context, not decoration. */
  hint?: ReactNode;
  tone?: StatTone;
  className?: string;
}

const TONE_COLOR: Record<StatTone, string> = {
  default: "var(--ink-primary)",
  good: "var(--status-good)",
  warning: "var(--status-warning)",
  critical: "var(--status-critical)",
};

/**
 * Headline counts are tiles, not charts. The big number keeps proportional
 * figures on purpose — tabular-nums is for columns and axis ticks.
 */
export function StatTile({ label, value, hint, tone = "default", className = "" }: StatTileProps) {
  return (
    <div className={`rounded-card border border-line bg-surface p-4 ${className}`}>
      <div className="text-[12px] font-medium uppercase tracking-wide text-muted">{label}</div>
      <div
        className="mt-2 text-[32px] font-semibold leading-none"
        style={{ color: TONE_COLOR[tone] }}
      >
        {value}
      </div>
      {hint ? <div className="mt-2 text-[12px] leading-snug text-ink-2">{hint}</div> : null}
    </div>
  );
}

export default StatTile;
