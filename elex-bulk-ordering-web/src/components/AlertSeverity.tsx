/**
 * How an alert's severity looks, and how its kind reads.
 *
 * Every severity borrows an EXISTING status key, so its hue, glyph and label
 * all come out of `statusMeta()` — no new colour is invented here. That matters
 * most for `warning`: amber is only 1.79:1 on the light surface, so the hue is
 * never the thing carrying the meaning. The glyph and the text label are, and
 * both ride along in every badge and tile below.
 */
import type { AlertSeverity, AlertSummary } from "../lib/types";
import { StatusBadge, statusMeta } from "./StatusBadge";
import type { StatTone } from "./StatTile";

interface SeverityLook {
  /** An existing status key — the single source of hue and glyph. */
  status: string;
  label: string;
  /** What a StatTile of this severity is toned. */
  tone: StatTone;
  /** One line saying what this severity is for. */
  hint: string;
}

/* info is deliberately NOT a hue: it is worth knowing, not worth acting on,
   and giving it a colour would spend the operator's attention on the rows that
   need it least. */
const LOOK: Record<string, SeverityLook | undefined> = {
  info: {
    status: "new",
    label: "Info",
    tone: "default",
    hint: "Worth knowing. Nothing is blocked.",
  },
  warning: {
    status: "failed_over_target",
    label: "Warning",
    tone: "warning",
    hint: "Someone should look, but no order is lost.",
  },
  critical: {
    status: "failed",
    label: "Critical",
    tone: "critical",
    hint: "Needs a human now.",
  },
};

const UNKNOWN: SeverityLook = {
  status: "pending",
  label: "Unknown",
  tone: "default",
  hint: "A severity this app does not know yet.",
};

/** Most urgent first — how the filter, the tiles and the queue all order it. */
export const SEVERITY_ORDER: AlertSeverity[] = ["critical", "warning", "info"];

/**
 * The look for a severity, tolerating one this build has not heard of: the
 * server's enum can grow, and an unknown severity must still render as a
 * labelled row rather than disappear.
 */
export function severityLook(severity: string): SeverityLook {
  return LOOK[severity] ?? { ...UNKNOWN, label: severity || UNKNOWN.label };
}

export function severityLabel(severity: string): string {
  return severityLook(severity).label;
}

export function severityTone(severity: string): StatTone {
  return severityLook(severity).tone;
}

export function severityHint(severity: string): string {
  return severityLook(severity).hint;
}

/** A ready-to-use CSS value, e.g. "var(--status-critical)". */
export function severityColorVar(severity: string): string {
  return statusMeta(severityLook(severity).status).colorVar;
}

/** Critical before warning before info — for sorting a mixed queue. */
export function severityRank(severity: string): number {
  const index = SEVERITY_ORDER.indexOf(severity as AlertSeverity);
  return index === -1 ? SEVERITY_ORDER.length : index;
}

/**
 * "address_mismatch" -> "Address mismatch".
 *
 * Kinds are free text on the server so new rules need no client release; they
 * are rendered humanised, with the raw key kept in a tooltip by the caller.
 */
/** The spec's own names for the kinds it names (ui-evidence S3: "Needs attention"). */
const SPEC_KIND_LABELS: Record<string, string> = {
  address_mismatch: "Order unit address mismatch",
  address_change: "Order unit address change",
  gst_mismatch: "Order unit GST mismatch",
};

/** The invoice checks across orders (26 Oct 2026), named for what is wrong. */
const INVOICE_KIND_LABELS: Record<string, string> = {
  invoice_amount_mismatch: "Invoice amount differs",
  product_invoice_missing: "Product invoice missing",
};

/**
 * A rule whose kind says less than it reports: "order_failed" also carries the
 * orders Flipkart took and cancelled later, worded as such and kept under the
 * same kind so an alert already open was reworded, not raised again (26 Oct 2026).
 */
const RULE_KIND_LABELS: Record<string, string> = {
  order_failed: "Order failed or cancelled",
};

export function kindLabel(kind: string): string {
  const named = SPEC_KIND_LABELS[kind] ?? INVOICE_KIND_LABELS[kind] ?? RULE_KIND_LABELS[kind];
  if (named) return named;
  const words = kind.replace(/[_-]+/g, " ").trim();
  if (words === "") return "Unknown";
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * How many open alerts of one severity, reading a summary that may simply
 * leave out a severity with none. Returns 0 for "nothing open", never NaN.
 */
export function openCount(summary: AlertSummary | null, severity: AlertSeverity): number {
  const raw = summary?.by_severity?.[severity];
  if (typeof raw !== "number" || !Number.isFinite(raw)) return 0;
  return Math.max(0, Math.trunc(raw));
}

/** Total open, falling back to the per-severity counts if `open` is missing. */
export function openTotal(summary: AlertSummary | null): number {
  if (summary === null) return 0;
  if (typeof summary.open === "number" && Number.isFinite(summary.open)) {
    return Math.max(0, Math.trunc(summary.open));
  }
  return SEVERITY_ORDER.reduce((sum, severity) => sum + openCount(summary, severity), 0);
}

export interface AlertSeverityBadgeProps {
  severity: string;
  className?: string;
}

/** Hue + glyph + the word itself, always all three. */
export function AlertSeverityBadge({ severity, className = "" }: AlertSeverityBadgeProps) {
  const look = severityLook(severity);
  return <StatusBadge status={look.status} label={look.label} className={className} />;
}

export default AlertSeverityBadge;
