import { useCallback, useEffect, useState } from "react";
import { errorMessage, getAgents } from "../lib/api";
import { formatCount } from "../lib/format";
import type { AgentsStatus } from "../lib/types";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { SkeletonRows } from "../components/Skeleton";
import { FleetTable } from "../components/FleetTable";

/** How often the page reads the agents again while it is open. */
export const AGENTS_REFRESH_MS = 30_000;

function State({ up, label }: { up: boolean | null; label: string }) {
  const tone = up === null ? "bg-line" : up ? "bg-good" : "bg-critical";
  return (
    <span className="inline-flex items-center gap-2 text-[13px] font-medium text-ink">
      <span aria-hidden="true" className={`size-2.5 rounded-full ${tone}`} />
      {label}
    </span>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-t border-line py-1.5 text-[13px]">
      <span className="text-ink-2">{label}</span>
      <span className="text-right text-ink">{children}</span>
    </div>
  );
}

/**
 * Agents (step H): the processes that do the browser work. Order runs happen
 * in the API process; sign-in and coupon runs go to queue workers when a
 * queue is set up. Read again every 30 seconds while the page is open.
 */
export default function Agents() {
  const [data, setData] = useState<AgentsStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await getAgents());
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), AGENTS_REFRESH_MS);
    return () => window.clearInterval(id);
  }, [load]);

  const queue = data?.queue;
  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-[20px] font-semibold leading-tight tracking-tight text-ink">Agents</h1>
          <p className="mt-1 max-w-prose text-[13px] leading-relaxed text-ink-2">
            The processes that run the browsers, and whether they are up. Refreshed every 30 seconds.
          </p>
        </div>
        <Button onClick={() => void load()}>Test now</Button>
      </header>

      {error ? (
        <Card title="Could not reach the API">
          <p className="text-[13px] text-ink-2">{error}</p>
        </Card>
      ) : null}

      {data === null && !error ? (
        <SkeletonRows rows={4} />
      ) : data ? (
        <div className="grid gap-5 lg:grid-cols-2">
          <Card title="Ordering agent (the API)" description="Every order run happens here, so card details never leave this process.">
            <State up={data.api.online} label={data.api.online ? "Online" : "Offline"} />
            <div className="mt-3">
              <Row label="Accounts at once">{formatCount(data.api.max_concurrency)}</Row>
              <Row label="Accounts per browser">{formatCount(data.api.contexts_per_browser)}</Row>
              <Row label="Runs going now">{formatCount(data.api.jobs_running)}</Row>
              <Row label="Runs waiting">{formatCount(data.api.jobs_queued)}</Row>
            </div>
          </Card>
          <Card title="Queue workers" description="Sign-in and coupon runs, when a queue (Redis) is set up.">
            {!queue?.configured ? (
              <>
                <State up={null} label="Not set up" />
                <p className="mt-3 text-[13px] text-ink-2">
                  No queue is configured, so sign-in and coupon runs also happen in the API process. Set
                  ORDERING_REDIS_URL and start workers to spread them out.
                </p>
              </>
            ) : queue.reachable ? (
              <>
                <State up={queue.health !== null} label={queue.health ? "Workers reporting" : "Queue up, no worker reporting"} />
                <div className="mt-3">
                  <Row label="Last worker report">{queue.health ?? "none yet"}</Row>
                </div>
              </>
            ) : (
              <>
                <State up={false} label="Queue unreachable" />
                <p className="mt-3 text-[13px] text-ink-2">The queue did not answer ({queue.error}).</p>
              </>
            )}
          </Card>
        </div>
      ) : null}

      <FleetTable />
    </div>
  );
}
