import { useCallback, useEffect, useMemo, useState } from "react";
import { errorMessage, startLoginJob } from "../lib/api";
import { countLabel, formatCount, formatDateTime, formatRelative } from "../lib/format";
import type { Account, AccountStatus, Job } from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { Checkbox } from "../components/Field";
import { ProgressStream } from "../components/ProgressStream";
import { RunAccountPicker, useRunAccounts } from "../components/RunAccounts";
import { isJobInFlight, RunHistory, useRecentJobs } from "../components/RunHistory";
import { RunPageHeader } from "../components/RunPageHeader";
import { RunError, RunLink, RunLoading } from "../components/RunStates";
import { StatusBadge } from "../components/StatusBadge";
import { Table, type Column } from "../components/Table";

/** Accounts that still need a session come first — that is the actionable set. */
const STATUS_RANK: Record<AccountStatus, number> = {
  session_invalid: 0,
  new: 1,
  session_valid: 2,
};

export default function Sessions() {
  const accountsState = useRunAccounts();
  const historyState = useRecentJobs("login");
  const reloadAccounts = accountsState.reload;
  const reloadHistory = historyState.reload;
  const { accounts } = accountsState;

  const [selected, setSelected] = useState<number[]>([]);
  const [force, setForce] = useState(false);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<number | null>(null);
  const [watchedInFlight, setWatchedInFlight] = useState(false);

  useEffect(() => {
    document.title = "Sessions — Ordering Suite";
  }, []);

  /* A run is in flight if the one we are watching is, or if the recent-runs
     list says one is — which also covers a page reload mid-run. */
  const runBusy = starting || watchedInFlight || historyState.busy;

  const accountIds = useMemo(() => new Set(accounts.map((a) => a.id)), [accounts]);
  const picked = useMemo(() => selected.filter((id) => accountIds.has(id)), [selected, accountIds]);
  const validCount = useMemo(
    () => accounts.filter((a) => a.status === "session_valid").length,
    [accounts],
  );
  const sorted = useMemo(
    () =>
      accounts
        .slice()
        .sort((a, b) => STATUS_RANK[a.status] - STATUS_RANK[b.status] || a.email.localeCompare(b.email)),
    [accounts],
  );

  const onStart = async () => {
    if (runBusy) return;
    setStarting(true);
    setStartError(null);
    try {
      const job = await startLoginJob({
        account_ids: picked.length > 0 ? picked : null,
        force,
      });
      setJobId(job.id);
      setWatchedInFlight(isJobInFlight(job));
      reloadHistory();
    } catch (err) {
      setStartError(errorMessage(err));
    } finally {
      setStarting(false);
    }
  };

  const onDone = useCallback(() => {
    setWatchedInFlight(false);
    reloadAccounts();
    reloadHistory();
  }, [reloadAccounts, reloadHistory]);

  const onSelectJob = useCallback((job: Job) => {
    setJobId(job.id);
    setWatchedInFlight(isJobInFlight(job));
    setStartError(null);
  }, []);

  const stopWatching = useCallback(() => {
    setJobId(null);
    setWatchedInFlight(false);
    reloadHistory();
  }, [reloadHistory]);

  const columns: Column<Account>[] = useMemo(
    () => [
      {
        key: "email",
        header: "Email",
        render: (account) => (
          <span className="block max-w-[200px] truncate sm:max-w-[360px]">{account.email}</span>
        ),
      },
      {
        key: "status",
        header: "Status",
        width: "170px",
        render: (account) => <StatusBadge status={account.status} />,
      },
      {
        key: "updated_at",
        header: "Updated",
        width: "130px",
        render: (account) => (
          <span className="whitespace-nowrap text-ink-2" title={formatDateTime(account.updated_at)}>
            {formatRelative(account.updated_at)}
          </span>
        ),
      },
    ],
    [],
  );

  const startHint =
    accounts.length === 0
      ? "Add accounts before starting a run."
      : picked.length === 0
        ? `Runs all ${countLabel(accounts.length, "account")}.`
        : `Runs ${countLabel(picked.length, "account")}.`;

  const accountsBody = (() => {
    if (accountsState.loading && accounts.length === 0) {
      return <RunLoading label="Loading accounts…" />;
    }
    if (accountsState.error && accounts.length === 0) {
      return (
        <RunError
          title="Could not load accounts"
          message={accountsState.error}
          onRetry={reloadAccounts}
        />
      );
    }
    if (accounts.length === 0) {
      return (
        <EmptyState
          title="No accounts yet"
          description="Import a sheet of accounts or add one by hand, then start a session run."
          action={<RunLink to="/accounts">Go to Accounts</RunLink>}
        />
      );
    }
    return (
      <div className="flex flex-col gap-3">
        <p className="text-[13px] text-ink-2" aria-live="polite">
          <span className="font-medium text-ink">
            {formatCount(validCount)} of {formatCount(accounts.length)}
          </span>{" "}
          have a valid session.
        </p>
        <Table
          columns={columns}
          rows={sorted}
          rowKey={(account) => account.id}
          caption="Accounts and their session status"
          minWidth="480px"
        />
      </div>
    );
  })();

  return (
    <div className="flex flex-col gap-5">
      <RunPageHeader
        step="Step 1 of 3"
        title="Sessions"
        lead="Creates a logged-in session for each account. The OTP is read automatically from your configured inbox."
      />

      {historyState.busy && !watchedInFlight ? (
        <Alert tone="info" title="A session run is already in flight">
          Open it under &ldquo;Recent runs&rdquo; below to watch it. Starting another is blocked
          until it finishes.
        </Alert>
      ) : null}

      <Card
        title="Start a session run"
        description="Pick the accounts to log in, or leave the selection empty to run every one of them."
      >
        <div className="flex flex-col gap-4">
          <RunAccountPicker
            state={accountsState}
            selected={selected}
            onChange={setSelected}
            disabled={runBusy}
            emptyAction={<RunLink to="/accounts">Go to Accounts</RunLink>}
          />

          <Checkbox
            label="Re-login accounts that already have a session"
            help="Left off, accounts with a valid session are skipped. Turn it on to force a fresh login for every account in this run."
            checked={force}
            disabled={runBusy}
            onChange={(event) => setForce(event.target.checked)}
          />

          <div aria-live="polite">
            {startError ? (
              <Alert tone="error" title="Could not start the run">
                {startError}
              </Alert>
            ) : null}
          </div>

          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <Button
              variant="primary"
              loading={starting}
              disabled={runBusy || accounts.length === 0}
              onClick={() => void onStart()}
            >
              {starting ? "Starting…" : "Start session run"}
            </Button>
            <p className="text-[12px] text-ink-2">{startHint}</p>
          </div>
        </div>
      </Card>

      {jobId !== null ? (
        <div className="flex flex-col items-end gap-2">
          <ProgressStream
            jobId={jobId}
            onDone={onDone}
            title="Session run"
            className="w-full"
          />
          <Button size="sm" variant="ghost" onClick={stopWatching}>
            Stop watching this run
          </Button>
        </div>
      ) : null}

      <Card
        title="Accounts & session status"
        description="Accounts that still need a session are listed first."
        actions={
          <Button
            size="sm"
            loading={accountsState.loading && accounts.length > 0}
            onClick={reloadAccounts}
          >
            Refresh
          </Button>
        }
      >
        <div className="flex flex-col gap-3">
          {accountsState.error && accounts.length > 0 ? (
            <p role="status" className="text-[12px] text-ink-2">
              Showing the last list that loaded — the refresh failed: {accountsState.error}
            </p>
          ) : null}
          {accountsBody}
        </div>
      </Card>

      <RunHistory
        state={historyState}
        activeJobId={jobId}
        onSelect={onSelectJob}
        title="Recent session runs"
        description="Pick a run to watch it again — a finished run still shows its per-account outcome."
        emptyTitle="No session runs yet"
        emptyDescription="Start one above and it will be listed here."
      />
    </div>
  );
}
