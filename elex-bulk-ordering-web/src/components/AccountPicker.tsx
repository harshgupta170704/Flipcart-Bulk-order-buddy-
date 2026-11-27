/**
 * Choosing the accounts a run works through, at 37,062 accounts.
 *
 * The old picker rendered a checkbox per account. This one asks the server how
 * many match, lists only the current page for eyeballing, and hands its host a
 * SELECTION rather than a list:
 *
 *   { mode: "ids",    ids }               — the rows the user actually ticked
 *   { mode: "filter", filter, count }     — everything the filter matches
 *
 * The second one is the important one: "all 37,062" travels as the filter that
 * produced it, so the run is started with one small request and the server
 * resolves the set itself.
 *
 * The picker never reports an empty ids selection. Unticking the last row goes
 * back to "everything this filter matches", which is what the old "an empty
 * selection means all accounts" convention always meant — and the helper text
 * now says the actual number out loud instead of implying it.
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { FilterChecklists } from "./FilterChecklists";
import { errorMessage, listAccountIds } from "../lib/api";
import { countLabel, formatCount } from "../lib/format";
import type { AccountSelection, AccountStatus } from "../lib/types";
import { RUN_ACCOUNT_CEILING } from "../lib/types";
import { AccountsFilterBar } from "./AccountsFilterBar";
import { AccountsPager } from "./AccountsPager";
import { filterKey, useAccountsQuery, useFilterOptions } from "./AccountsQuery";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Spinner } from "./Spinner";
import { StatusBadge } from "./StatusBadge";

/** How many rows the picker shows at once. Deliberately small: it is for
 *  checking the filter caught the right accounts, not for reading the table. */
const PICKER_PAGE_SIZE = 25;

export interface AccountPickerProps {
  /** The current selection. The picker is fully controlled. */
  value: AccountSelection;
  /** Must be referentially stable (useState's setter or a useCallback). */
  onChange: (next: AccountSelection) => void;
  /** Statuses this run is restricted to. Locks the status filter. */
  filterStatus?: AccountStatus[];
  /** Group label. Default "Accounts". */
  label?: string;
  /**
   * True when the host can only carry a list of ids.
   *
   * "All N matching" then has to be resolved to real ids before it can be
   * sent, so a narrowed filter is offered as an explicit button (bounded by
   * the run ceiling) and the no-ticks default stays the host's own unfiltered
   * set — never "everything" dressed up as a narrowed selection.
   */
  idsOnly?: boolean;
  /** Tags, subscriptions and domains as checkbox lists (the order form's Email ID Filters). */
  checklists?: boolean;
  /**
   * The ticks say exactly what will run (Smart Bulk Order). "All matching"
   * shows every row ticked on every page under a "Select all" box; unticking
   * a row from there drops only that row; unticking everything leaves NONE
   * selected (an empty ids selection the host must refuse to start) instead
   * of quietly meaning "all". Ignored for an `idsOnly` host.
   */
  explicitTicks?: boolean;
  disabled?: boolean;
  className?: string;
}

export function AccountPicker({
  value,
  onChange,
  filterStatus,
  label = "Accounts",
  idsOnly = false,
  checklists = false,
  explicitTicks = false,
  disabled = false,
  className = "",
}: AccountPickerProps) {
  /** Ticks mean "will run", and "nothing ticked" means nothing. */
  const explicit = explicitTicks && !idsOnly;
  /* Hosts pass `["session_valid"]` inline, so a fresh array arrives on every
     render. Key it by value: everything below memoises off it, and an array
     that changed identity but not content would otherwise rebuild the filter
     and re-run the effect that keeps the selection honest on every render. */
  const lockKey = filterStatus === undefined ? null : [...filterStatus].sort().join(",");
  const locked = useMemo<readonly string[] | undefined>(
    () => (lockKey === null ? undefined : lockKey === "" ? [] : lockKey.split(",")),
    [lockKey],
  );

  const q = useAccountsQuery({ lockedStatuses: locked, initialSize: PICKER_PAGE_SIZE });
  const optionsState = useFilterOptions();
  const { options } = optionsState;
  /** Unique per picker, so two of them on one screen keep distinct labels. */
  const prefix = `pick${useId()}`;

  /* How many accounts the host's own restriction covers, with no narrowing.
     Read off the status counts rather than spending another request. */
  const baseTotal = useMemo<number | null>(() => {
    if (options === null) return null;
    if (locked === undefined || locked.length === 0) return options.total;
    const wanted = new Set<string>(locked);
    return options.statuses
      .filter((status) => wanted.has(status.value))
      .reduce((sum, status) => sum + status.count, 0);
  }, [options, locked]);

  /** The filter with the user's narrowing stripped back out. */
  const baseFilter = useMemo(
    () => (locked !== undefined && locked.length > 0 ? { statuses: [...locked] } : {}),
    [locked],
  );

  /* onChange is called from effects; keeping it in a ref means a host that
     re-creates the callback cannot turn this into a render loop. */
  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  /* --- keeping the selection honest -------------------------------------
     A tick was made under one filter; once the filter changes those rows are
     no longer the ones on screen, so the selection falls back to "everything
     this filter matches". The guards below make this converge in one push.
  ---------------------------------------------------------------------- */

  const lastFilter = useRef<string | null>(null);

  /** What "nothing ticked" means for this host — always a filter. */
  const defaultSelection = useMemo<Extract<AccountSelection, { mode: "filter" }>>(() => {
    if (idsOnly) {
      // The host can only say "these ids" or "all of them". A narrowed filter
      // it cannot carry, so the honest default is its own unfiltered set.
      return { mode: "filter", filter: baseFilter, count: baseTotal ?? 0 };
    }
    return { mode: "filter", filter: q.filter, count: q.total };
  }, [idsOnly, baseFilter, baseTotal, q.filter, q.total]);

  useEffect(() => {
    if (q.loading || q.error !== null) return;

    const key = filterKey(q.filter);
    const filterChanged = lastFilter.current !== key;
    lastFilter.current = key;

    const stale =
      filterChanged ||
      (value.mode === "filter" &&
        (value.count !== defaultSelection.count ||
          filterKey(value.filter) !== filterKey(defaultSelection.filter)));

    if (stale) onChangeRef.current(defaultSelection);
  }, [q.loading, q.error, q.filter, value, defaultSelection]);

  /* --- ticking rows ------------------------------------------------------ */

  const tickedIds = value.mode === "ids" ? value.ids : [];
  const tickedSet = useMemo(() => new Set(tickedIds), [tickedIds]);
  const pageIds = useMemo(() => q.items.map((account) => account.id), [q.items]);

  /** Explicit ticks: "all matching" is every row ticked, on every page. */
  const allMatchingTicked = explicit && value.mode === "filter";
  const isTicked = (id: number) => allMatchingTicked || tickedSet.has(id);

  const setIds = useCallback(
    (ids: number[]) => {
      // Never emit an empty ids selection: "nothing ticked" has a meaning, and
      // that meaning is the default selection, stated with its real count.
      // With explicit ticks it means none, and the host refuses to start.
      if (ids.length === 0) {
        onChangeRef.current(explicit ? { mode: "ids", ids: [] } : defaultSelection);
        return;
      }
      // Every match ticked by hand is the same set as "all matching", and
      // saying it as the filter keeps the Select all box honest.
      if (explicit && q.total > 0 && ids.length >= q.total) {
        onChangeRef.current(defaultSelection);
        return;
      }
      onChangeRef.current({ mode: "ids", ids });
    },
    [defaultSelection, explicit, q.total],
  );

  const [resolving, setResolving] = useState(false);
  const [resolveError, setResolveError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  /* The filter on screen right now, so an answer that comes back after the
     user changed the filter is dropped instead of landing on the new one. */
  const currentFilterKey = useRef("");
  useEffect(() => {
    currentFilterKey.current = filterKey(q.filter);
  }, [q.filter]);

  /** Every match's id (up to one run's worth), or null after saying why not. */
  const resolveMatching = async (): Promise<number[] | null> => {
    const asked = currentFilterKey.current;
    setResolving(true);
    try {
      const resolved = await listAccountIds(q.filter, RUN_ACCOUNT_CEILING);
      if (!alive.current || currentFilterKey.current !== asked) return null;
      if (resolved.truncated) {
        setResolveError(
          `${formatCount(resolved.total)} accounts match — more than the ${formatCount(RUN_ACCOUNT_CEILING)} a single run can take. Narrow the filter further.`,
        );
        return null;
      }
      return resolved.ids;
    } catch (err) {
      if (alive.current) setResolveError(errorMessage(err));
      return null;
    } finally {
      if (alive.current) setResolving(false);
    }
  };

  const toggleRow = async (id: number) => {
    if (disabled || resolving) return;
    if (allMatchingTicked) {
      // Unticking one row out of "all": every other match stays ticked, on
      // every page, so the set has to be named id by id from here on.
      setResolveError(null);
      const ids = await resolveMatching();
      if (ids !== null) setIds(ids.filter((x) => x !== id));
      return;
    }
    setIds(tickedSet.has(id) ? tickedIds.filter((x) => x !== id) : [...tickedIds, id]);
  };

  const allPageTicked = pageIds.length > 0 && pageIds.every((id) => isTicked(id));

  /* The Select all box shows a dash while only some accounts are ticked. */
  const selectAllBox = useRef<HTMLInputElement>(null);
  const someTicked = explicit && value.mode === "ids" && value.ids.length > 0;
  useEffect(() => {
    if (selectAllBox.current) selectAllBox.current.indeterminate = someTicked;
  }, [someTicked]);

  /* --- "all N matching" -------------------------------------------------- */

  /** Only a host that can carry a filter gets the filter straight through. */
  const overCeiling = idsOnly && q.narrowed && q.total > RUN_ACCOUNT_CEILING;

  /* "All N matching" IS the no-ticks default, so the button only has work to
     do once rows are ticked — or when ids have to be resolved first. */
  const allMatchingIsNoop = value.mode === "filter" && !(idsOnly && q.narrowed);

  const selectAllMatching = async () => {
    if (disabled || q.total === 0) return;
    setResolveError(null);

    if (!idsOnly || !q.narrowed) {
      // Either the host carries filters, or the filter is the host's own base
      // set — both are expressible without naming a single id.
      onChangeRef.current({
        mode: "filter",
        filter: idsOnly ? baseFilter : q.filter,
        count: idsOnly ? (baseTotal ?? q.total) : q.total,
      });
      return;
    }

    if (overCeiling) return;

    setResolving(true);
    try {
      const resolved = await listAccountIds(q.filter, RUN_ACCOUNT_CEILING);
      if (!alive.current) return;
      if (resolved.truncated || resolved.ids.length === 0) {
        setResolveError(
          resolved.ids.length === 0
            ? "Nothing matches that filter any more. Adjust it and try again."
            : `${formatCount(resolved.total)} accounts match — more than the ${formatCount(RUN_ACCOUNT_CEILING)} a single run can take. Narrow the filter further.`,
        );
        return;
      }
      onChangeRef.current({ mode: "ids", ids: resolved.ids });
    } catch (err) {
      if (alive.current) setResolveError(errorMessage(err));
    } finally {
      if (alive.current) setResolving(false);
    }
  };

  /* --- the sentence that must always be true ----------------------------- */

  const runCount = value.mode === "ids" ? value.ids.length : value.count;

  const summary = (() => {
    if (explicit) {
      if (value.mode === "filter") {
        return `All ${countLabel(runCount, "account")} selected, on every page — all of them will run.`;
      }
      if (value.ids.length === 0) {
        return "No account ticked — nothing will run. Tick some, or Select all.";
      }
      return `${formatCount(value.ids.length)} of ${countLabel(q.total, "account")} ticked — exactly those will run.`;
    }
    if (value.mode === "ids") {
      return `${countLabel(value.ids.length, "account")} ticked — exactly those will run.`;
    }
    if (idsOnly && q.narrowed) {
      return `Nothing ticked: all ${countLabel(runCount, "account")} will run, not just the ${formatCount(q.total)} shown. Use “Select all ${formatCount(q.total)} matching” to run only those.`;
    }
    if (q.narrowed) {
      return `All ${countLabel(runCount, "account")} matching this filter will run.`;
    }
    return `Nothing ticked: all ${countLabel(runCount, "account")} will run.`;
  })();

  const listBody = (() => {
    if (q.initial && q.loading) {
      return (
        <p role="status" className="flex items-center justify-center gap-2 px-4 py-8 text-[13px] text-ink-2">
          <Spinner size={14} />
          Loading accounts…
        </p>
      );
    }
    if (q.error !== null && q.items.length === 0) {
      return (
        <div className="flex flex-col items-start gap-3 px-4 py-6">
          <Alert tone="error" title="Could not load accounts" className="w-full">
            {q.error}
          </Alert>
          <Button size="sm" onClick={q.reload} loading={q.loading}>
            Try again
          </Button>
        </div>
      );
    }
    if (q.total === 0) {
      return (
        <p className="px-4 py-8 text-center text-[13px] text-ink-2">
          {q.narrowed
            ? "No account matches those filters."
            : "No eligible account yet."}
        </p>
      );
    }
    return (
      <ul className="divide-y divide-line">
        {q.items.map((account) => {
          const rowId = `${prefix}-row-${account.id}`;
          return (
            <li key={account.id}>
              <div className="flex items-center gap-3 px-3 py-2">
                <input
                  id={rowId}
                  type="checkbox"
                  checked={isTicked(account.id)}
                  disabled={disabled || resolving}
                  onChange={() => void toggleRow(account.id)}
                  className="size-4 shrink-0 accent-accent"
                />
                <label htmlFor={rowId} className="min-w-0 flex-1 cursor-pointer">
                  <span className="block truncate text-[13px] text-ink">{account.email}</span>
                  <span className="block truncate text-[12px] text-muted">
                    {account.phone ? <span className="tabular-nums">{account.phone}</span> : null}
                    {account.phone && account.tag ? " · " : null}
                    {account.tag ? account.tag.name : null}
                    {(account.phone || account.tag) && account.subscription_type ? " · " : null}
                    {account.subscription_type}
                  </span>
                </label>
                <StatusBadge status={account.status} />
              </div>
            </li>
          );
        })}
      </ul>
    );
  })();

  return (
    <div className={`flex flex-col gap-3 ${className}`}>
      <div className="flex flex-col gap-1">
        <h3 className="text-[13px] font-medium leading-none text-ink">{label}</h3>
        <p role="status" aria-live="polite" className="text-[12px] text-ink-2">
          {q.loading && q.initial
            ? "Counting accounts…"
            : `${countLabel(q.total, "account")} match`}
          {q.narrowed ? " the filters below" : ""}.
        </p>
      </div>

      <AccountsFilterBar
        idPrefix={prefix}
        query={q.query}
        onQChange={q.setQ}
        onToggleStatus={q.toggleStatus}
        onTagChange={q.setTag}
        onSubscriptionChange={q.setSubscription}
        onDomainChange={q.setDomain}
        lockedStatuses={locked}
        options={options}
        optionsLoading={optionsState.loading}
        optionsError={optionsState.error}
        onRetryOptions={optionsState.reload}
        narrowed={q.narrowed}
        onClear={q.clearFilters}
        disabled={disabled}
        hideFacets={checklists}
      />
      {checklists ? (
        <FilterChecklists
          options={options}
          tags={q.query.tags ?? []}
          subscriptions={q.query.subscriptions ?? []}
          domains={q.query.domains ?? []}
          onTags={q.setTags}
          onSubscriptions={q.setSubscriptions}
          onDomains={q.setDomains}
          disabled={disabled}
        />
      ) : null}

      {explicit ? (
      <div className="flex flex-wrap items-center gap-2">
        <label className="mr-2 flex cursor-pointer items-center gap-2 text-[13px] font-medium text-ink">
          <input
            ref={selectAllBox}
            type="checkbox"
            name="select_all_matching"
            checked={allMatchingTicked}
            disabled={disabled || resolving || q.total === 0}
            onChange={() => {
              setResolveError(null);
              if (allMatchingTicked) setIds([]);
              else void selectAllMatching();
            }}
            className="size-4 shrink-0 accent-accent"
          />
          {resolving ? "Selecting…" : `Select all ${formatCount(q.total)} (every page)`}
        </label>
        <Button
          size="sm"
          variant="secondary"
          disabled={disabled || resolving || q.items.length === 0 || allPageTicked}
          onClick={() => setIds(Array.from(new Set([...tickedIds, ...pageIds])))}
        >
          Tick this page
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={disabled || resolving || (value.mode === "ids" && value.ids.length === 0)}
          onClick={() => setIds([])}
        >
          Untick all
        </Button>
      </div>
      ) : (
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          loading={resolving}
          disabled={disabled || q.total === 0 || overCeiling || allMatchingIsNoop}
          onClick={() => void selectAllMatching()}
        >
          {resolving
            ? "Selecting…"
            : allMatchingIsNoop
              ? `All ${formatCount(q.total)} matching selected`
              : `Select all ${formatCount(q.total)} matching`}
        </Button>
        <Button
          size="sm"
          variant="secondary"
          disabled={disabled || q.items.length === 0 || allPageTicked}
          onClick={() => setIds(Array.from(new Set([...tickedIds, ...pageIds])))}
        >
          Tick this page
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={disabled || value.mode !== "ids"}
          onClick={() => setIds([])}
        >
          Clear ticks
        </Button>
      </div>
      )}

      <p className="text-[12px] leading-snug text-ink-2" aria-live="polite">
        {summary}
      </p>

      {/* Only when the ticks themselves are under the ceiling — otherwise the
          alert below already says the same thing about the real run. */}
      {overCeiling && runCount <= RUN_ACCOUNT_CEILING ? (
        <Alert tone="warning" title="Too many matches for one run">
          {formatCount(q.total)} accounts match, and a single run takes at most{" "}
          {formatCount(RUN_ACCOUNT_CEILING)}. Narrow the filter, or tick accounts individually.
        </Alert>
      ) : null}

      {runCount > RUN_ACCOUNT_CEILING ? (
        <Alert tone="warning" title="More than one run's worth">
          {formatCount(runCount)} accounts are selected, past the{" "}
          {formatCount(RUN_ACCOUNT_CEILING)} ceiling for a single run. The server will refuse this
          one — narrow the filter first.
        </Alert>
      ) : null}

      <div aria-live="polite">
        {resolveError ? (
          <Alert tone="error" title="Could not select those accounts">
            {resolveError}
          </Alert>
        ) : null}
      </div>

      {value.mode === "filter" && q.total > 0 ? (
        <p className="text-[12px] leading-snug text-muted">
          {explicit
            ? "Every account on every page is ticked. Untick any you want to leave out."
            : "Every account the filter matches is in the run, including the ones on other pages. Tick rows below to narrow it to just those."}
        </p>
      ) : null}

      <div role="group" aria-label={`${label} — page ${q.page}`} className="rounded-card border border-line bg-surface">
        {listBody}
      </div>

      {q.error !== null && q.items.length > 0 ? (
        <p role="status" className="text-[12px] text-ink-2">
          Showing the page that last loaded — the refresh failed: {q.error}
        </p>
      ) : null}

      <AccountsPager
        idPrefix={prefix}
        page={q.page}
        size={q.size}
        total={q.total}
        pages={q.pages}
        shown={q.items.length}
        onPageChange={q.setPage}
        loading={q.loading}
        disabled={disabled}
      />
    </div>
  );
}

export default AccountPicker;
