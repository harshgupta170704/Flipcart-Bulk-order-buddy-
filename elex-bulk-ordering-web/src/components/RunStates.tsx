import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Spinner } from "./Spinner";

/**
 * The loading and error halves of the four-state contract, shared by the
 * Sessions and Coupons run pages so every panel on both reads the same way.
 */

export interface RunLoadingProps {
  /** Visible text — it doubles as the accessible name, so say what is loading. */
  label?: string;
  className?: string;
}

export function RunLoading({ label = "Loading…", className = "" }: RunLoadingProps) {
  return (
    <p
      aria-live="polite"
      className={`flex items-center justify-center gap-2 rounded-control border border-dashed border-line px-4 py-8 text-[13px] text-ink-2 ${className}`}
    >
      <Spinner size={14} />
      {label}
    </p>
  );
}

export interface RunErrorProps {
  title?: ReactNode;
  /** Always pass errorMessage(err) — never a raw unknown. */
  message: ReactNode;
  onRetry: () => void;
  retryLabel?: string;
  className?: string;
}

export function RunError({
  title = "Could not load this",
  message,
  onRetry,
  retryLabel = "Try again",
  className = "",
}: RunErrorProps) {
  return (
    <div className={`flex flex-col items-start gap-3 ${className}`}>
      <Alert tone="error" title={title} className="w-full">
        {message}
      </Alert>
      <Button size="sm" onClick={onRetry}>
        {retryLabel}
      </Button>
    </div>
  );
}

export interface RunLinkProps {
  to: string;
  children: ReactNode;
  className?: string;
}

/**
 * A router link wearing the secondary Button's clothes — for the action in an
 * empty state, where the way out of it is another page rather than a request.
 */
export function RunLink({ to, children, className = "" }: RunLinkProps) {
  return (
    <Link
      to={to}
      className={`inline-flex h-[38px] items-center justify-center rounded-control border border-line bg-surface px-4 text-sm font-medium text-ink transition-colors hover:bg-page ${className}`}
    >
      {children}
    </Link>
  );
}

export default RunLoading;
