import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { errorMessage, listJobs } from "../lib/api";
import { clampProgress, formatCount, formatRelative } from "../lib/format";
import type { Job, JobType } from "../lib/types";
import { Button } from "./Button";
import { Card } from "./Card";
import { EmptyState } from "./EmptyState";
import { RunError, RunLoading } from "./RunStates";
import { StatusBadge } from "./StatusBadge";

/** GET /jobs returns every type, so we over-fetch and filter down to one. */
const FETCH_LIMIT = 50;

export interface RecentJobsState {
  jobs: Job[];
  loading: boolean;
  error: string | null;
  reload: () => void;
  /** True while any of these runs is queued or in flight. */
  busy: boolean;
}

/** The most recent runs of one job type, newest first. */
export function useRecentJobs(type: JobType, limit = 5): RecentJobsState {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const reload = useCallback(() => {
    setLoading(true);
    setError(null);
    listJobs(FETCH_LIMIT).then(
      (rows) => {
        if (!alive.current) return;
        setJobs(rows.filter((job) => job.type === type).slice(0, limit));
        setLoading(false);
      },
      (err: unknown) => {
        if (!alive.current) return;
        setError(errorMessage(err));
        setLoading(false);
      },
    );
  }, [type, limit]);

  useEffect(() => {
    reload();
  }, [reload]);

  const busy = jobs.some((job) => job.status === "queued" || job.status === "running");

  return { jobs, loading, error, reload, busy };
}

export function isJobInFlight(job: Job): boolean {
  return job.status === "queued" || job.status === "running";
}

export interface RunHistoryProps {
  state: RecentJobsState;
  /** The run currently attached to the live view, so the row can say so. */
  activeJobId: number | null;
  onSelect: (job: Job) => void;
  title?: string;
  description?: ReactNode;
  emptyTitle?: ReactNode;
  emptyDescription?: ReactNode;
  className?: string;
}

/**
 * Recent runs of one type. Each row is a button: picking one re-attaches the
 * live view to it, which works for a finished run too — the opening snapshot
 * still carries its per-account outcome.
 */
export function RunHistory({
  state,
  activeJobId,
  onSelect,
  title = "Recent runs",
  description,
  emptyTitle = "No runs yet",
  emptyDescription = "Start one above and it will be listed here.",
  className = "",
}: RunHistoryProps) {
  const { jobs, loading, error, reload } = state;

  const body = (() => {
    if (loading && jobs.length === 0) return <RunLoading label="Loading recent runs…" />;
    if (error && jobs.length === 0) {
      return <RunError title="Could not load recent runs" message={error} onRetry={reload} />;
    }
    if (jobs.length === 0) {
      return <EmptyState title={emptyTitle} description={emptyDescription} />;
    }

    return (
      <ul className="divide-y divide-line rounded-control border border-line">
        {jobs.map((job) => {
          const active = job.id === activeJobId;
          const inFlight = isJobInFlight(job);
          const percent = clampProgress(job.progress);
          return (
            <li key={job.id}>
              <button
                type="button"
                onClick={() => onSelect(job)}
                aria-current={active ? "true" : undefined}
                className={`flex w-full flex-wrap items-center gap-x-3 gap-y-2 border-l-2 px-3 py-2.5 text-left transition-colors ${
                  active ? "border-l-accent bg-page" : "border-l-transparent hover:bg-page"
                }`}
              >
                <span className="shrink-0 text-[13px] font-medium text-ink">Run #{job.id}</span>
                <StatusBadge status={job.status} />
                {active ? (
                  <span className="shrink-0 rounded-control border border-accent px-1.5 py-px text-[11px] font-medium text-accent">
                    Showing
                  </span>
                ) : null}
                <span className="shrink-0 text-[12px] tabular-nums text-ink-2">
                  {formatCount(job.done_items)}/{formatCount(job.total_items)} done
                  {job.failed_items > 0 ? `, ${formatCount(job.failed_items)} failed` : ""}
                  {(job.unconfirmed_items ?? 0) > 0
                    ? `, ${formatCount(job.unconfirmed_items)} unconfirmed`
                    : ""}
                </span>
                <span className="ml-auto shrink-0 text-[12px] text-muted">
                  {formatRelative(job.started_at)}
                </span>
                <span
                  aria-hidden="true"
                  className="block h-1.5 w-full basis-full overflow-hidden rounded-full"
                  style={{ backgroundColor: "var(--grid)" }}
                >
                  <span
                    className="block h-full rounded-full"
                    style={{
                      width: `${percent}%`,
                      backgroundColor:
                        !inFlight && (job.unconfirmed_items ?? 0) > 0
                          ? "var(--status-critical)"
                          : !inFlight && job.failed_items > 0
                            ? "var(--status-warning)"
                            : "var(--accent)",
                    }}
                  />
                </span>
                <span className="sr-only">
                  {active ? "Shown in the live view" : "Show this run in the live view"}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    );
  })();

  return (
    <Card
      title={title}
      description={description}
      className={className}
      actions={
        <Button size="sm" variant="secondary" loading={loading && jobs.length > 0} onClick={reload}>
          Refresh
        </Button>
      }
    >
      <div className="flex flex-col gap-3">
        {error && jobs.length > 0 ? (
          <p role="status" className="text-[12px] text-ink-2">
            Showing the last list that loaded — the refresh failed: {error}
          </p>
        ) : null}
        {body}
      </div>
    </Card>
  );
}

export default RunHistory;
