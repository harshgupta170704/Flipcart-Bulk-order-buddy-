/**
 * Every step of a cancel or reschedule request, oldest first (spec C-4's
 * status log): opened, marked done or dropped, opened again, a note added
 * later. Folded away until asked for, and shown only once there is more than
 * the opening, since a request's own row already says who asked and when.
 * Used on the Requests page and in the row dialog.
 */
import { formatDateTime, formatRelative } from "../lib/format";
import type { UnitRequestStep } from "../lib/types";

/** What one step of a request did, in words. */
function stepLabel(step: UnitRequestStep, index: number): string {
  if (step.status === "open") return index === 0 ? "Opened" : "Opened again";
  return step.status === "done" ? "Marked done" : "Dropped";
}

export function RequestHistory({ steps }: { steps: readonly UnitRequestStep[] }) {
  if (steps.length < 2) return null;
  return (
    <details className="mt-1 text-[12px] text-ink-2">
      <summary className="cursor-pointer select-none">History ({steps.length} steps)</summary>
      <ol className="mt-1 flex flex-col gap-1">
        {steps.map((step, index) => (
          <li key={`${step.at}-${index}`}>
            {stepLabel(step, index)} by {step.actor} · <span title={formatDateTime(step.at)}>{formatRelative(step.at)}</span>
            {step.note ? <span className="block text-ink">“{step.note}”</span> : null}
          </li>
        ))}
      </ol>
    </details>
  );
}

export default RequestHistory;
