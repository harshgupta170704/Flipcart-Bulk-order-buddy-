import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { errorMessage, fleetAction, getFleet } from "../lib/api";
import type { FleetStatus } from "../lib/types";

/** How often the bar reads the fleet again. */
export const AGENTS_BAR_REFRESH_MS = 30_000;

/**
 * "Agents Status: 8 online / 11 · 2 disabled · 3 offline" with Test all,
 * Enable all and Disable all — the line at the top of the Orders page.
 */
export function AgentsBar() {
  const [fleet, setFleet] = useState<FleetStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setFleet(await getFleet());
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), AGENTS_BAR_REFRESH_MS);
    return () => window.clearInterval(id);
  }, [load]);

  const run = async (action: "test-all" | "enable-all" | "disable-all") => {
    setBusy(action);
    try {
      setFleet(await fleetAction(action));
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  };

  const button =
    "inline-flex h-7 items-center gap-1 rounded-control border border-line px-2 text-[12px] text-ink-2 hover:bg-page hover:text-ink disabled:opacity-50";

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-control bg-page px-3 py-2 text-[13px]">
      <span className="font-medium text-ink">Agents Status:</span>
      {fleet ? (
        <Link to="/agents" className="text-ink-2 hover:underline">
          <span className="font-semibold text-ink">{fleet.online}</span> online / {fleet.total}
          {" · "}
          {fleet.disabled} disabled
          {" · "}
          <span style={{ color: fleet.offline ? "var(--status-critical)" : undefined }}>{fleet.offline} offline</span>
        </Link>
      ) : (
        <span className="text-ink-2">{error ?? "Loading…"}</span>
      )}
      <button type="button" className={button} disabled={busy !== null} onClick={() => void run("test-all")}>
        Test all
      </button>
      <button type="button" className={button} disabled={busy !== null} onClick={() => void run("enable-all")}>
        Enable all
      </button>
      <button type="button" className={button} disabled={busy !== null} onClick={() => void run("disable-all")}>
        Disable all
      </button>
      {error && fleet ? <span className="text-[12px] text-ink">{error}</span> : null}
    </div>
  );
}

export default AgentsBar;
