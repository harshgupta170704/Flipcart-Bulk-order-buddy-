import { useCallback, useEffect, useState } from "react";
import { errorMessage, getFleet, updateAgent } from "../lib/api";
import { formatRelative } from "../lib/format";
import type { FleetAgent, FleetStatus } from "../lib/types";
import { AgentsBar } from "./AgentsBar";
import { Card } from "./Card";
import { Table, type Column } from "./Table";

const STATE_COLOR: Record<string, string> = {
  online: "var(--status-good)",
  offline: "var(--status-critical)",
  disabled: "var(--ink-muted)",
};

/** Every agent, with a switch to enable it and one to keep it for test runs. */
export function FleetTable() {
  const [fleet, setFleet] = useState<FleetStatus | null>(null);
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
    const id = window.setInterval(() => void load(), 30_000);
    return () => window.clearInterval(id);
  }, [load]);

  const patch = async (agent: FleetAgent, change: { enabled?: boolean; isolated?: boolean }) => {
    try {
      setFleet(await updateAgent(agent.id, change));
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const columns: Column<FleetAgent>[] = [
    { key: "name", header: "Agent", render: (a) => <span className="font-mono text-[12px]">{a.name}</span> },
    { key: "kind", header: "Runs", render: (a) => (a.kind === "api" ? "Orders" : "Sign-in & coupons") },
    {
      key: "state",
      header: "State",
      render: (a) => (
        <span className="inline-flex items-center gap-1.5 text-[13px] capitalize">
          <span aria-hidden="true" className="size-2 rounded-full" style={{ backgroundColor: STATE_COLOR[a.state] }} />
          {a.state}
        </span>
      ),
    },
    { key: "last_seen_at", header: "Last heard", render: (a) => (a.last_seen_at ? formatRelative(a.last_seen_at) : "never") },
    {
      key: "last_test",
      header: "Last test",
      render: (a) =>
        a.last_test_at ? `${a.last_test_ok ? "Passed" : "Failed"} · ${formatRelative(a.last_test_at)}` : "—",
    },
    {
      key: "enabled",
      header: "Enabled",
      render: (a) => (
        <input
          type="checkbox"
          aria-label={`Enable ${a.name}`}
          className="size-4 accent-accent"
          checked={a.enabled}
          onChange={(e) => void patch(a, { enabled: e.target.checked })}
        />
      ),
    },
    {
      key: "isolated",
      header: "Isolated",
      render: (a) => (
        <input
          type="checkbox"
          aria-label={`Keep ${a.name} for isolated test runs`}
          className="size-4 accent-accent"
          checked={a.isolated}
          onChange={(e) => void patch(a, { isolated: e.target.checked })}
        />
      ),
    },
  ];

  return (
    <Card
      title="Agent fleet"
      description="Each API process and queue worker reports here every 30 seconds. A disabled agent takes no new work; an isolated one takes only runs started with “Test on isolated agents”."
    >
      <div className="flex flex-col gap-3">
        <AgentsBar />
        {error ? <p className="text-[13px] text-ink">{error}</p> : null}
        <Table caption="Agents" columns={columns} rows={fleet?.agents ?? []} rowKey={(a) => a.id} dense minWidth="720px" />
      </div>
    </Card>
  );
}

export default FleetTable;
