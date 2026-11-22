import type { ReactNode } from "react";

export interface RunPageHeaderProps {
  /** Where this run sits in the pipeline, e.g. "Step 1 of 3". */
  step?: string;
  title: string;
  /** The one short line that says what the step actually does. */
  lead: ReactNode;
  actions?: ReactNode;
  className?: string;
}

/**
 * Page heading for a run screen: step marker, title, and the single explaining
 * line. Sessions and Coupons both open with this so they read as one pipeline.
 */
export function RunPageHeader({ step, title, lead, actions, className = "" }: RunPageHeaderProps) {
  return (
    <header className={`flex flex-wrap items-start justify-between gap-x-4 gap-y-2 ${className}`}>
      <div className="min-w-0">
        {step ? (
          <p className="text-[12px] font-medium uppercase tracking-wide text-muted">{step}</p>
        ) : null}
        <h1 className="mt-1 text-[21px] font-semibold leading-tight tracking-tight text-ink">
          {title}
        </h1>
        <p className="mt-1.5 max-w-prose text-[13px] leading-relaxed text-ink-2">{lead}</p>
      </div>
      {actions ? (
        // No shrink-0: with it the box could never wrap (its basis is every
        // button on one line), and ran off the page at phone widths. It now
        // shrinks to the header's width and wraps, right-aligned under the
        // title when it has to drop a line — the same fix as Card's actions.
        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">{actions}</div>
      ) : null}
    </header>
  );
}

export default RunPageHeader;
