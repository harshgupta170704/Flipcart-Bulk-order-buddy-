import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { errorMessage, getJob, startCouponJob } from "../lib/api";
import {
  countLabel,
  DASH,
  formatCount,
  formatDateTime,
  formatRelative,
  formatSpent,
} from "../lib/format";
import type { Job, JobDetail, JobItem } from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { Field, TextInput } from "../components/Field";
import { ProgressStream } from "../components/ProgressStream";
import { RunAccountPicker, useRunAccounts } from "../components/RunAccounts";
import { isJobInFlight, RunHistory, useRecentJobs } from "../components/RunHistory";
import { RunPageHeader } from "../components/RunPageHeader";
import { RunError, RunLink, RunLoading } from "../components/RunStates";
import { StatTile } from "../components/StatTile";
import { StatusBadge } from "../components/StatusBadge";
import { Table, type Column } from "../components/Table";

/* --- outcomes -------------------------------------------------------------
   The backend words each item's message "claimed" / "already_used" / "error".
   "already used" is an expected result, not a failure, so it gets a neutral
   state of its own instead of being counted as an error. If the backend ever
   rewords it, ALREADY_USED below is the single place to change.
-------------------------------------------------------------------------- */

type CouponOutcome = "claimed" | "already_used" | "skipped" | "error" | "running" | "pending";

const ALREADY_USED = /already[\s_-]*(used|claimed|applied)/i;

function couponOutcome(item: JobItem): CouponOutcome {
  if (item.status === "running") return "running";
  if (item.status === "pending") return "pending";
  if (ALREADY_USED.test(item.message ?? "")) return "already_used";
  if (item.status === "skipped") return "skipped";
  if (item.status === "done") return "claimed";
  return "error";
}

/**
 * Every outcome borrows an existing status key, so its color, glyph and label
 * all still come from statusMeta() — no new hue is invented here.
 */
const OUTCOME_BADGE: Record<CouponOutcome, { status: string; label: string }> = {
  claimed: { status: "done", label: "Claimed" },
  already_used: { status: "skipped", label: "Already used" },
  skipped: { status: "skipped", label: "Skipped" },
  error: { status: "failed", label: "Error" },
  running: { status: "running", label: "Running" },
  pending: { status: "pending", label: "Pending" },
};

/** Whatever still needs a human first, settled results last. */
const OUTCOME_RANK: Record<CouponOutcome, number> = {
  running: 0,
  error: 1,
  pending: 2,
  skipped: 3,
  already_used: 4,
  claimed: 5,
};

type CouponTally = Record<CouponOutcome, number>;

function tallyOutcomes(items: JobItem[]): CouponTally {
  const tally: CouponTally = {
    claimed: 0,
    already_used: 0,
    skipped: 0,
    error: 0,
    running: 0,
    pending: 0,
  };
  for (const item of items) tally[couponOutcome(item)] += 1;
  return tally;
}

function summarise(items: JobItem[], tally: CouponTally): string {
  const parts = [
    `${formatCount(tally.claimed)} claimed`,
    `${formatCount(tally.already_used)} already used`,
  ];
  if (tally.skipped > 0) parts.push(`${formatCount(tally.skipped)} skipped`);
  if (tally.error > 0) {
    parts.push(`${formatCount(tally.error)} ${tally.error === 1 ? "error" : "errors"}`);
  }
  const outstanding = tally.pending + tally.running;
  if (outstanding > 0) parts.push(`${formatCount(outstanding)} still to go`);
  return `${countLabel(items.length, "account")}: ${parts.join(", ")}.`;
}

export default function Coupons() {
  const accountsState = useRunAccounts();
  const historyState = useRecentJobs("coupon");
  const reloadAccounts = accountsState.reload;
  const reloadHistory = historyState.reload;
  const { accounts } = accountsState;

  const [code, setCode] = useState("");
  const [selected, setSelected] = useState<number[]>([]);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<number | null>(null);
  const [watchedInFlight, setWatchedInFlight] = useState(false);

  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const wantedDetail = useRef<number | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    document.title = "Coupons — Ordering Suite";
  }, []);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const runBusy = starting || watchedInFlight || historyState.busy;

  const trimmed = code.trim();
  const eligible = useMemo(
    () => accounts.filter((account) => account.status === "session_valid"),
    [accounts],
  );
  const eligibleIds = useMemo(() => new Set(eligible.map((account) => account.id)), [eligible]);
  const picked = useMemo(
    () => selected.filter((id) => eligibleIds.has(id)),
    [selected, eligibleIds],
  );
  /* Nothing picked means "every account the picker offered", which with the
     session_valid filter is the eligible set — so send those ids rather than
     null, and the run's item list matches what the picker promised. */
  const toRun = picked.length > 0 ? picked : eligible.map((account) => account.id);

  /** Results come from GET /jobs/{id}; the live view owns the WebSocket. */
  const loadDetail = useCallback((id: number) => {
    wantedDetail.current = id;
    setDetailLoading(true);
    setDetailError(null);
    getJob(id).then(
      (job) => {
        if (!alive.current || wantedDetail.current !== id) return;
        setDetail(job);
        setDetailLoading(false);
      },
      (err: unknown) => {
        if (!alive.current || wantedDetail.current !== id) return;
        setDetailError(errorMessage(err));
        setDetailLoading(false);
      },
    );
  }, []);

  useEffect(() => {
    if (jobId === null) {
      wantedDetail.current = null;
      setDetail(null);
      setDetailError(null);
      setDetailLoading(false);
      return;
    }
    setDetail(null);
    loadDetail(jobId);
  }, [jobId, loadDetail]);

  const onStart = async () => {
    if (runBusy || trimmed === "" || toRun.length === 0) return;
    setStarting(true);
    setStartError(null);
    try {
      const job = await startCouponJob({
        account_ids: toRun.length > 0 ? toRun : null,
        coupon_code: trimmed,
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
    if (jobId !== null) loadDetail(jobId);
    reloadAccounts();
    reloadHistory();
  }, [jobId, loadDetail, reloadAccounts, reloadHistory]);

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

  const resultColumns: Column<JobItem>[] = useMemo(
    () => [
      {
        key: "email",
        header: "Account",
        render: (item) => (
          <span className="block max-w-[180px] truncate sm:max-w-[320px]">
            {item.email || `Account ${item.account_id}`}
          </span>
        ),
      },
      {
        key: "outcome",
        header: "Outcome",
        width: "160px",
        render: (item) => {
          const badge = OUTCOME_BADGE[couponOutcome(item)];
          return <StatusBadge status={badge.status} label={badge.label} />;
        },
      },
      {
        key: "message",
        header: "Message",
        render: (item) =>
          item.message ? (
            <span
              className="block max-w-[200px] truncate text-ink-2 sm:max-w-[340px]"
              title={item.message}
            >
              {item.message}
            </span>
          ) : (
            DASH
          ),
      },
      {
        key: "spent",
        header: "Took",
        width: "90px",
        render: (item) => {
          const spent = formatSpent(item);
          return spent ? (
            <span className="whitespace-nowrap tabular-nums text-ink-2">{spent}</span>
          ) : (
            DASH
          );
        },
      },
      {
        key: "updated_at",
        header: "Updated",
        width: "130px",
        render: (item) => (
          <span className="whitespace-nowrap text-ink-2" title={formatDateTime(item.updated_at)}>
            {formatRelative(item.updated_at)}
          </span>
        ),
      },
    ],
    [],
  );

  const items = useMemo(() => detail?.items ?? [], [detail]);
  const tally = useMemo(() => tallyOutcomes(items), [items]);
  const sortedItems = useMemo(
    () =>
      items
        .slice()
        .sort(
          (a, b) =>
            OUTCOME_RANK[couponOutcome(a)] - OUTCOME_RANK[couponOutcome(b)] ||
            a.email.localeCompare(b.email),
        ),
    [items],
  );

  const noEligible = accounts.length > 0 && eligible.length === 0 && !accountsState.loading;
  const startHint = (() => {
    if (accounts.length === 0) return "Add accounts before starting a run.";
    if (eligible.length === 0) return "No account has a valid session yet.";
    if (trimmed === "") return "Enter a coupon code to start.";
    if (picked.length === 0) return `Runs all ${countLabel(eligible.length, "account")} with a session.`;
    return `Runs ${countLabel(picked.length, "account")}.`;
  })();

  const resultsBody = (() => {
    if (detailLoading && detail === null) return <RunLoading label="Loading results…" />;
    if (detailError && detail === null) {
      return (
        <RunError
          title="Could not load the results"
          message={detailError}
          onRetry={() => {
            if (jobId !== null) loadDetail(jobId);
          }}
        />
      );
    }
    if (detail === null) return <RunLoading label="Loading results…" />;
    if (items.length === 0) {
      return (
        <EmptyState
          title="No accounts in this run"
          description="This run finished without any account to work on."
        />
      );
    }
    return (
      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile
            label="Claimed"
            value={formatCount(tally.claimed)}
            hint="Coupon applied."
            tone="good"
          />
          <StatTile
            label="Already used"
            value={formatCount(tally.already_used)}
            hint="Expected — the account already had it. Not a failure."
          />
          <StatTile
            label="Skipped"
            value={formatCount(tally.skipped)}
            hint="No valid session to work with."
          />
          <StatTile
            label="Errors"
            value={formatCount(tally.error)}
            hint="Worth running again."
            tone="critical"
          />
        </div>
        <p className="text-[13px] text-ink-2" aria-live="polite">
          {summarise(items, tally)}
        </p>
        <Table
          columns={resultColumns}
          rows={sortedItems}
          rowKey={(item) => item.account_id}
          caption="Per-account coupon outcome"
          minWidth="640px"
        />
      </div>
    );
  })();

  return (
    <div className="flex flex-col gap-5">
      <RunPageHeader
        step="Step 2 of 3"
        title="Coupons"
        lead="Applies one coupon code to every selected account that has a session. Accounts without one are skipped."
      />

      {historyState.busy && !watchedInFlight ? (
        <Alert tone="info" title="A coupon run is already in flight">
          Open it under &ldquo;Recent runs&rdquo; below to watch it. Starting another is blocked
          until it finishes.
        </Alert>
      ) : null}

      <Card
        title="Apply a coupon"
        description="Only accounts with a valid session are listed — a coupon cannot go on an account that is not logged in."
      >
        <div className="flex flex-col gap-4">
          <Field
            label="Coupon code"
            required
            help="Applied as-is to every account in this run. An account without a valid session is skipped, and one that already has the coupon is reported as “already used”, not as an error."
            className="max-w-xl"
          >
            <TextInput
              value={code}
              onChange={(event) => setCode(event.target.value)}
              placeholder="e.g. WELCOME500"
              autoComplete="off"
              spellCheck={false}
              maxLength={64}
              disabled={runBusy}
              className="max-w-sm"
            />
          </Field>

          {noEligible ? (
            <div className="flex flex-col items-start gap-3">
              <Alert tone="warning" title="No account has a valid session yet" className="w-full">
                A coupon can only be applied to an account that is logged in. Run step 1 first,
                then come back.
              </Alert>
              <RunLink to="/sessions">Go to Sessions</RunLink>
            </div>
          ) : null}

          <RunAccountPicker
            state={accountsState}
            selected={selected}
            onChange={setSelected}
            filterStatus={["session_valid"]}
            label="Accounts with a valid session"
            disabled={runBusy}
            emptyAction={<RunLink to="/accounts">Go to Accounts</RunLink>}
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
              disabled={runBusy || trimmed === "" || toRun.length === 0}
              onClick={() => void onStart()}
            >
              {starting ? "Starting…" : "Apply coupon"}
            </Button>
            <p className="text-[12px] text-ink-2">{startHint}</p>
          </div>
        </div>
      </Card>

      {jobId !== null ? (
        <div className="flex flex-col items-end gap-2">
          <ProgressStream jobId={jobId} onDone={onDone} title="Coupon run" className="w-full" />
          <Button size="sm" variant="ghost" onClick={stopWatching}>
            Stop watching this run
          </Button>
        </div>
      ) : null}

      {jobId !== null ? (
        <Card
          title="Results"
          description={
            detail && isJobInFlight(detail)
              ? "This run is still going — the live view above updates as it happens. Refresh for the final tally."
              : "How each account came out of this run."
          }
          actions={
            <Button
              size="sm"
              loading={detailLoading && detail !== null}
              onClick={() => loadDetail(jobId)}
            >
              Refresh
            </Button>
          }
        >
          <div className="flex flex-col gap-3">
            {detailError && detail !== null ? (
              <p role="status" className="text-[12px] text-ink-2">
                Showing the last results that loaded — the refresh failed: {detailError}
              </p>
            ) : null}
            {resultsBody}
          </div>
        </Card>
      ) : null}

      <RunHistory
        state={historyState}
        activeJobId={jobId}
        onSelect={onSelectJob}
        title="Recent coupon runs"
        description="Pick a run to watch it again — a finished run still shows its per-account outcome."
        emptyTitle="No coupon runs yet"
        emptyDescription="Enter a code above and apply it to see runs here."
      />
    </div>
  );
}
