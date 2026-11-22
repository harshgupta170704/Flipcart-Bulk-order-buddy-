import { Spinner } from "./Spinner";

export type StatusTone = "good" | "warning" | "critical" | "muted" | "accent";

export interface StatusMeta {
  /** Human label. Always rendered — color never carries meaning alone. */
  label: string;
  /** Glyph paired with the color. Empty string when `spinner` stands in. */
  glyph: string;
  tone: StatusTone;
  /** A ready-to-use CSS value, e.g. "var(--status-good)". */
  colorVar: string;
  /** True for "running": show a spinner instead of a static glyph. */
  spinner: boolean;
}

const GOOD = "var(--status-good)";
const WARNING = "var(--status-warning)";
const CRITICAL = "var(--status-critical)";
const MUTED = "var(--ink-muted)";
const ACCENT = "var(--accent)";

/**
 * One source of truth for how a status looks — badges, table cells and the
 * dashboard chart all read it.
 *
 * Amber is only 1.79:1 on the light surface, so the color is always paired with
 * BOTH a text label and a glyph, and the label itself is drawn in primary ink.
 */
export function statusMeta(status: string): StatusMeta {
  switch (status) {
    /* --- good ------------------------------------------------------------ */
    case "placed":
      return { label: "Placed", glyph: "✓", tone: "good", colorVar: GOOD, spinner: false };
    case "done":
      return { label: "Done", glyph: "✓", tone: "good", colorVar: GOOD, spinner: false };
    case "session_valid":
      return {
        label: "Session valid",
        glyph: "✓",
        tone: "good",
        colorVar: GOOD,
        spinner: false,
      };
    case "completed":
      return { label: "Completed", glyph: "✓", tone: "good", colorVar: GOOD, spinner: false };

    /* --- warning: the price-target breach, never a generic failure ------- */
    case "failed_over_target":
      return {
        label: "Over target",
        glyph: "!",
        tone: "warning",
        colorVar: WARNING,
        spinner: false,
      };

    /* Flipkart took the order and cancelled it later: not the run's failure,
       so never the red "Failed". It shares the amber of "Over target" — both
       are the order not going ahead for a reason outside the run — and the
       "⊘" glyph and the label are what tell them apart, never the colour. */
    case "cancelled_by_flipkart":
      return {
        label: "Cancelled by Flipkart",
        glyph: "⊘",
        tone: "warning",
        colorVar: WARNING,
        spinner: false,
      };

    /* Flipkart gave the attempt an order id and then said "Order Not Placed":
       the payment did not go through. Not a cancellation and not the run's
       failure; the same amber, told apart by its label. */
    case "not_placed":
      return {
        label: "Not placed — payment failed",
        glyph: "⊘",
        tone: "warning",
        colorVar: WARNING,
        spinner: false,
      };

    /* --- critical -------------------------------------------------------- */
    case "failed":
      return { label: "Failed", glyph: "✕", tone: "critical", colorVar: CRITICAL, spinner: false };
    /* Submitted but never confirmed: the order may exist. Shares the critical
       hue with "Failed" because both need a human now; the "?" glyph and the
       label are what tell them apart, never the colour. */
    case "unconfirmed":
      return {
        label: "Unconfirmed",
        glyph: "?",
        tone: "critical",
        colorVar: CRITICAL,
        spinner: false,
      };
    case "session_invalid":
      return {
        label: "Session invalid",
        glyph: "✕",
        tone: "critical",
        colorVar: CRITICAL,
        spinner: false,
      };

    /* --- deliberately not a status hue ----------------------------------- */
    case "skipped":
      return { label: "Skipped", glyph: "–", tone: "muted", colorVar: MUTED, spinner: false };
    /* Cancelled on Flipkart at the account's own request: someone meant it. */
    case "cancelled_by_user":
      return {
        label: "Cancelled by user",
        glyph: "⊘",
        tone: "muted",
        colorVar: MUTED,
        spinner: false,
      };

    /* --- waiting --------------------------------------------------------- */
    case "pending":
      return { label: "Pending", glyph: "•", tone: "muted", colorVar: MUTED, spinner: false };
    case "queued":
      return { label: "Queued", glyph: "•", tone: "muted", colorVar: MUTED, spinner: false };
    case "new":
      return { label: "New", glyph: "•", tone: "muted", colorVar: MUTED, spinner: false };

    /* --- in flight ------------------------------------------------------- */
    case "running":
      return { label: "Running", glyph: "", tone: "accent", colorVar: ACCENT, spinner: true };

    default:
      return {
        label: humanize(status),
        glyph: "•",
        tone: "muted",
        colorVar: MUTED,
        spinner: false,
      };
  }
}

function humanize(status: string): string {
  if (!status) return "Unknown";
  const words = status.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export interface StatusBadgeProps {
  status: string;
  /** Override the label text; the glyph and color still come from the status. */
  label?: string;
  className?: string;
}

/**
 * Color + glyph + text label, always all three.
 *
 * The glyph and the label are drawn in primary ink rather than the status hue:
 * amber on the light surface is only 1.79:1, so a hue-colored glyph would be
 * the one thing a user could not see. The hue arrives as the border and the
 * background tint, where it reinforces the label instead of replacing it.
 */
export function StatusBadge({ status, label, className = "" }: StatusBadgeProps) {
  const meta = statusMeta(status);
  const text = label ?? meta.label;
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-control border px-2 py-[3px] text-[12px] font-medium leading-5 text-ink ${className}`}
      style={{
        borderColor: `color-mix(in oklab, ${meta.colorVar} 45%, transparent)`,
        backgroundColor: `color-mix(in oklab, ${meta.colorVar} 16%, var(--surface-1))`,
      }}
    >
      {meta.spinner ? (
        <span style={{ color: meta.colorVar }} className="inline-flex">
          <Spinner size={12} />
        </span>
      ) : (
        <span aria-hidden="true" className="font-semibold">
          {meta.glyph}
        </span>
      )}
      <span>{text}</span>
    </span>
  );
}

export default StatusBadge;
