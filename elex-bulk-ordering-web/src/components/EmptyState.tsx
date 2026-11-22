import type { ReactNode } from "react";

export interface EmptyStateProps {
  title: ReactNode;
  description?: ReactNode;
  /** Usually a Button that gets the user out of the empty state. */
  action?: ReactNode;
  className?: string;
}

/** What a data screen shows when there is genuinely nothing yet. */
export function EmptyState({ title, description, action, className = "" }: EmptyStateProps) {
  return (
    <div
      className={`flex flex-col items-center justify-center gap-2 rounded-card border border-dashed border-line px-6 py-10 text-center ${className}`}
    >
      <p className="text-sm font-semibold text-ink">{title}</p>
      {description ? (
        <p className="max-w-sm text-[13px] leading-relaxed text-ink-2">{description}</p>
      ) : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

export default EmptyState;
