import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ACCOUNTS_MAX_SIZE, errorMessage, listAccounts } from "../lib/api";
import { formatCount } from "../lib/format";
import type { Account, AccountSelection, AccountStatus } from "../lib/types";
import { AccountPicker } from "./AccountPicker";
import { EmptyState } from "./EmptyState";
import { RunError, RunLoading } from "./RunStates";

/** One request's worth — the server's own MAX_SIZE. See useRunAccounts. */
const RUN_ACCOUNTS_LIMIT = ACCOUNTS_MAX_SIZE;

export interface RunAccountsState {
  /**
   * Up to RUN_ACCOUNTS_LIMIT accounts — NOT necessarily all of them. The run
   * pages render and count straight off this array, so it carries as much as
   * one request can and reports `truncated` rather than quietly paging behind
   * their backs.
   */
  accounts: Account[];
  loading: boolean;
  /** Already human-readable — it came through errorMessage(). */
  error: string | null;
  /** Re-fetch; call it after a run finishes so new statuses show up. */
  reload: () => void;
  /** Every account on the server, however few of them `accounts` holds. */
  total: number;
  /** True when `accounts` is only the first slice of `total`. */
  truncated: boolean;
}

/**
 * Loads the first page of GET /accounts for a run page and hands back a stable
 * `reload` so the page can refresh statuses the moment a run finishes.
 *
 * It asks for the largest page the server will serve, because the pages that
 * use it tabulate and total this array directly. `total` is the honest number
 * and `truncated` says when the two differ — which at 37,062 accounts they
 * always will.
 */
export function useRunAccounts(): RunAccountsState {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [total, setTotal] = useState(0);
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
    listAccounts({ page: 1, size: RUN_ACCOUNTS_LIMIT, sort: "id" }).then(
      (result) => {
        if (!alive.current) return;
        setAccounts(result.items);
        setTotal(result.total);
        setLoading(false);
      },
      (err: unknown) => {
        if (!alive.current) return;
        setError(errorMessage(err));
        setLoading(false);
      },
    );
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  return {
    accounts,
    loading,
    error,
    reload,
    total,
    truncated: total > accounts.length,
  };
}

export interface RunAccountPickerProps {
  state: RunAccountsState;
  /**
   * Ids-only value, for a host that sends `account_ids`. Empty still means
   * "every account the picker offers" — the picker now says the real number.
   */
  selected?: number[];
  onChange?: (ids: number[]) => void;
  /**
   * The richer value: a filter OR a list of ids. A host that passes this can
   * start a run on 37,062 accounts without naming one of them.
   */
  selection?: AccountSelection;
  onSelectionChange?: (next: AccountSelection) => void;
  filterStatus?: AccountStatus[];
  label?: string;
  disabled?: boolean;
  /** Usually a link to /accounts, shown when there is not one account yet. */
  emptyAction?: ReactNode;
  /** Tags, subscriptions and domains as checkbox lists. */
  checklists?: boolean;
  className?: string;
}

/**
 * AccountPicker with the loading / error / nothing-yet states around it, so
 * every run page gets all four states from one place. The picker itself handles
 * "no account matches this filter" and does its own paging.
 *
 * Two shapes of value are accepted because the run pages were written against
 * the old ids-only one. When only `onChange` is given the picker is told it is
 * `idsOnly`: a narrowed "all matching" is then resolved to real ids before it
 * is handed over, and the no-ticks default stays the host's own unfiltered set,
 * so the ids-only host can never be handed a selection it cannot express.
 */
export function RunAccountPicker({
  state,
  selected,
  onChange,
  selection,
  onSelectionChange,
  filterStatus,
  label,
  disabled,
  emptyAction,
  checklists,
  className = "",
}: RunAccountPickerProps) {
  const idsOnly = onSelectionChange === undefined;

  /*
   * An ids-only host owns a number[], which cannot hold a filter or a count.
   * The full selection therefore lives here and `onChange` keeps the host in
   * step. `selected` seeds it — after that this copy is authoritative, because
   * feeding the host's narrower view back in would erase the filter the picker
   * had just reported and the two would push each other back and forth.
   */
  const [internal, setInternal] = useState<AccountSelection>(() =>
    selected !== undefined && selected.length > 0
      ? { mode: "ids", ids: selected }
      : { mode: "filter", filter: {}, count: 0 },
  );

  const value = selection ?? internal;

  const handleChange = useCallback(
    (next: AccountSelection) => {
      if (onSelectionChange) onSelectionChange(next);
      else setInternal(next);
      // Filter mode reaching an ids-only host means "everything this host
      // offers", which is exactly what its empty list has always meant.
      if (onChange) onChange(next.mode === "ids" ? next.ids : []);
    },
    [onChange, onSelectionChange],
  );

  if (state.loading && state.accounts.length === 0) {
    return <RunLoading label="Loading accounts…" className={className} />;
  }

  if (state.error && state.accounts.length === 0) {
    return (
      <RunError
        title="Could not load accounts"
        message={state.error}
        onRetry={state.reload}
        className={className}
      />
    );
  }

  if (state.total === 0 && state.accounts.length === 0) {
    return (
      <EmptyState
        title="No accounts yet"
        description="Import a sheet of accounts or add one by hand, then come back to start a run."
        action={emptyAction}
        className={className}
      />
    );
  }

  return (
    <div className={`flex flex-col gap-2 ${className}`}>
      {state.error ? (
        <p role="status" className="text-[12px] text-ink-2">
          Showing the accounts that last loaded — the refresh failed: {state.error}
        </p>
      ) : null}
      {state.truncated ? (
        <p role="status" className="text-[12px] text-ink-2">
          This page's own tables and totals cover the first {formatCount(state.accounts.length)} of{" "}
          {formatCount(state.total)} accounts, while the picker counts and filters all{" "}
          {formatCount(state.total)} on the server. With nothing ticked, this page may start its run
          on only the accounts it loaded — tick rows, or use &ldquo;Select all&nbsp;…&nbsp;matching&rdquo;,
          to be exact about the set.
        </p>
      ) : null}
      <AccountPicker
        value={value}
        onChange={handleChange}
        filterStatus={filterStatus}
        label={label}
        idsOnly={idsOnly}
        checklists={checklists}
        disabled={disabled}
      />
    </div>
  );
}

export default RunAccountPicker;
