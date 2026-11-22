/**
 * The one place that turns "what the user picked in a filter bar" into a
 * server request.
 *
 * Everything here exists because the real portal holds 37,062 accounts: the
 * narrowing lives on the server, the client only ever holds one page, and the
 * same filter object that drove the table is what gets sent to bulk tagging or
 * to the start of a run. Nothing in this module ever materialises a full list.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { errorMessage, getFilterOptions, listAccounts } from "../lib/api";
import type {
  Account,
  AccountFilterInput,
  AccountSortField,
  FilterOptions,
  PagedAccountsParams,
} from "../lib/types";

/** How long the search box waits before it costs a request. */
export const SEARCH_DEBOUNCE_MS = 300;

/** The filter bar's draft state, before it becomes a request. */
export interface AccountQuery {
  /** Substring over email or phone. */
  q: string;
  statuses: string[];
  /** null = any tag · a number = that tag · "untagged" = no tag at all. */
  tag: number | "untagged" | null;
  subscription: string;
  /** An email domain ("soniclip.in"); "" for any. */
  domain: string;
  /** Checklist picks, added to the single choices above (any of them matches). */
  tags?: (number | "untagged")[];
  subscriptions?: string[];
  domains?: string[];
}

export const EMPTY_QUERY: AccountQuery = { q: "", statuses: [], tag: null, subscription: "", domain: "" };

/**
 * Build the server's filter body.
 *
 * `lockedStatuses` is the host's own restriction (a coupon run only ever
 * touches accounts with a valid session). It replaces the user's status choice
 * rather than merging with it, so a run can never widen past what its page
 * promised.
 */
export function toFilterInput(
  query: AccountQuery,
  lockedStatuses?: readonly string[],
): AccountFilterInput {
  const filter: AccountFilterInput = {};

  const q = query.q.trim();
  if (q !== "") filter.q = q;

  const statuses = lockedStatuses !== undefined ? lockedStatuses : query.statuses;
  if (statuses.length > 0) filter.statuses = [...statuses];

  const tags = [...(query.tag === null ? [] : [query.tag]), ...(query.tags ?? [])];
  if (tags.includes("untagged")) filter.include_untagged = true;
  const tagIds = [...new Set(tags.filter((tag): tag is number => typeof tag === "number"))];
  if (tagIds.length > 0) filter.tag_ids = tagIds;

  const subscriptions = [
    ...new Set([query.subscription, ...(query.subscriptions ?? [])].map((s) => s.trim()).filter(Boolean)),
  ];
  if (subscriptions.length > 0) filter.subscriptions = subscriptions;

  const domains = [
    ...new Set([query.domain ?? "", ...(query.domains ?? [])].map((d) => d.trim().toLowerCase()).filter(Boolean)),
  ];
  if (domains.length > 0) filter.domains = domains;

  return filter;
}

/** True when the filter selects everything — the server's `is_empty()`. */
export function isFilterEmpty(filter: AccountFilterInput): boolean {
  return (
    (filter.q ?? "") === "" &&
    (filter.statuses ?? []).length === 0 &&
    (filter.tag_ids ?? []).length === 0 &&
    filter.include_untagged !== true &&
    (filter.subscriptions ?? []).length === 0 &&
    (filter.domains ?? []).length === 0 &&
    (filter.ids ?? []).length === 0
  );
}

/** True when the user has narrowed beyond the host's own restriction. */
export function isQueryNarrowed(query: AccountQuery, lockedStatuses?: readonly string[]): boolean {
  return (
    query.q.trim() !== "" ||
    query.tag !== null ||
    query.subscription.trim() !== "" ||
    (query.domain ?? "").trim() !== "" ||
    (query.tags ?? []).length > 0 ||
    (query.subscriptions ?? []).length > 0 ||
    (query.domains ?? []).length > 0 ||
    (lockedStatuses === undefined && query.statuses.length > 0)
  );
}

/** A stable string for a filter, so effects can tell a real change apart. */
export function filterKey(filter: AccountFilterInput): string {
  return JSON.stringify({
    q: filter.q ?? "",
    statuses: [...(filter.statuses ?? [])].sort(),
    tag_ids: [...(filter.tag_ids ?? [])].sort((a, b) => a - b),
    untagged: filter.include_untagged === true,
    subscriptions: [...(filter.subscriptions ?? [])].sort(),
    domains: [...(filter.domains ?? [])].sort(),
    ids: [...(filter.ids ?? [])].sort((a, b) => a - b),
  });
}

/* --- filter options ------------------------------------------------------- */

export interface FilterOptionsState {
  options: FilterOptions | null;
  loading: boolean;
  /** Already human-readable — it came through errorMessage(). */
  error: string | null;
  reload: () => void;
}

/** Statuses, tags and subscriptions with their counts, for the filter bar. */
export function useFilterOptions(): FilterOptionsState {
  const [options, setOptions] = useState<FilterOptions | null>(null);
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
    getFilterOptions().then(
      (next) => {
        if (!alive.current) return;
        setOptions(next);
        setError(null);
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

  return { options, loading, error, reload };
}

/* --- the paged query ------------------------------------------------------ */

export interface UseAccountsQueryOptions {
  /** The host's status restriction. Locks the status control when given. */
  lockedStatuses?: readonly string[];
  initialSize?: number;
  initialSort?: AccountSortField;
  initialDescending?: boolean;
}

export interface AccountsQueryState {
  /** The draft the filter bar is bound to — `q` updates on every keystroke. */
  query: AccountQuery;
  setQ: (value: string) => void;
  setStatuses: (next: string[]) => void;
  toggleStatus: (value: string) => void;
  setTag: (next: number | "untagged" | null) => void;
  setSubscription: (next: string) => void;
  setDomain: (next: string) => void;
  /** The checklist picks (the order form's Email ID Filters). */
  setTags: (next: (number | "untagged")[]) => void;
  setSubscriptions: (next: string[]) => void;
  setDomains: (next: string[]) => void;
  clearFilters: () => void;
  /** True once the user has narrowed past the host's restriction. */
  narrowed: boolean;
  /** The request body for this filter — the exact value to send onward. */
  filter: AccountFilterInput;
  /** Serialised `filter`, for effect dependencies. */
  key: string;

  page: number;
  size: number;
  pages: number;
  total: number;
  setPage: (next: number) => void;
  setSize: (next: number) => void;

  sort: AccountSortField;
  descending: boolean;
  /** Same column flips direction; a new column starts ascending. */
  toggleSort: (field: AccountSortField) => void;

  /** This page only — never more than `size` rows. */
  items: Account[];
  loading: boolean;
  error: string | null;
  /** True before the first page has ever arrived. */
  initial: boolean;
  reload: () => void;
  /** Swap one row in place, for an optimistic edit and its rollback. */
  replaceItem: (account: Account) => void;
}

/**
 * One page of accounts for a given filter, with debounced search.
 *
 * Changing anything about the filter resets to page 1 — paging through results
 * that no longer exist is the classic way a paged table lies to someone.
 */
export function useAccountsQuery(options: UseAccountsQueryOptions = {}): AccountsQueryState {
  const { initialSize = 50, initialSort = "id", initialDescending = false } = options;

  // Callers pass `["session_valid"]` inline, so a fresh array arrives on every
  // render. Key it by value, or every render would refetch.
  const lockedKey = options.lockedStatuses === undefined ? null : [...options.lockedStatuses].sort().join(",");
  const locked = useMemo<readonly string[] | undefined>(
    () => (lockedKey === null ? undefined : lockedKey === "" ? [] : lockedKey.split(",")),
    [lockedKey],
  );

  const [qInput, setQInput] = useState("");
  const [qApplied, setQApplied] = useState("");
  const [statuses, setStatusesState] = useState<string[]>([]);
  const [tag, setTagState] = useState<number | "untagged" | null>(null);
  const [subscription, setSubscriptionState] = useState("");
  const [domain, setDomainState] = useState("");
  const [tags, setTagsState] = useState<(number | "untagged")[]>([]);
  const [subscriptions, setSubscriptionsState] = useState<string[]>([]);
  const [domains, setDomainsState] = useState<string[]>([]);

  const [page, setPageState] = useState(1);
  const [size, setSizeState] = useState(initialSize);
  const [sort, setSort] = useState<AccountSortField>(initialSort);
  const [descending, setDescending] = useState(initialDescending);

  const [items, setItems] = useState<Account[]>([]);
  const [total, setTotal] = useState(0);
  const [pages, setPages] = useState(0);
  const [loading, setLoading] = useState(true);
  const [initial, setInitial] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  /* Debounce the search box: typing "aarthbodha" should cost one request. */
  useEffect(() => {
    if (qInput === qApplied) return;
    const timer = window.setTimeout(() => {
      setQApplied(qInput);
      setPageState(1);
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [qInput, qApplied]);

  const setQ = useCallback((value: string) => setQInput(value), []);

  const setStatuses = useCallback((next: string[]) => {
    setStatusesState(next);
    setPageState(1);
  }, []);

  const toggleStatus = useCallback((value: string) => {
    setStatusesState((current) =>
      current.includes(value) ? current.filter((s) => s !== value) : [...current, value],
    );
    setPageState(1);
  }, []);

  const setTag = useCallback((next: number | "untagged" | null) => {
    setTagState(next);
    setPageState(1);
  }, []);

  const setSubscription = useCallback((next: string) => {
    setSubscriptionState(next);
    setPageState(1);
  }, []);

  const setDomain = useCallback((next: string) => {
    setDomainState(next);
    setPageState(1);
  }, []);

  const setTags = useCallback((next: (number | "untagged")[]) => {
    setTagsState(next);
    setPageState(1);
  }, []);

  const setSubscriptions = useCallback((next: string[]) => {
    setSubscriptionsState(next);
    setPageState(1);
  }, []);

  const setDomains = useCallback((next: string[]) => {
    setDomainsState(next);
    setPageState(1);
  }, []);

  const clearFilters = useCallback(() => {
    setQInput("");
    setQApplied("");
    setStatusesState([]);
    setTagState(null);
    setSubscriptionState("");
    setDomainState("");
    setTagsState([]);
    setSubscriptionsState([]);
    setDomainsState([]);
    setPageState(1);
  }, []);

  const setPage = useCallback((next: number) => {
    setPageState(Math.max(1, Math.trunc(next)));
  }, []);

  const setSize = useCallback((next: number) => {
    setSizeState(Math.max(1, Math.trunc(next)));
    setPageState(1);
  }, []);

  /* Written as two plain updaters rather than one nested in the other: a
     reducer that flips another piece of state would double-flip whenever React
     re-invokes it. */
  const toggleSort = useCallback(
    (field: AccountSortField) => {
      if (field === sort) {
        setDescending((wasDescending) => !wasDescending);
      } else {
        setSort(field);
        setDescending(false);
      }
      setPageState(1);
    },
    [sort],
  );

  const query = useMemo<AccountQuery>(
    () => ({ q: qInput, statuses, tag, subscription, domain, tags, subscriptions, domains }),
    [qInput, statuses, tag, subscription, domain, tags, subscriptions, domains],
  );

  /* The applied filter, built from the DEBOUNCED search text so it matches
     what the page on screen actually shows. */
  const filter = useMemo(
    () => toFilterInput({ q: qApplied, statuses, tag, subscription, domain, tags, subscriptions, domains }, locked),
    [qApplied, statuses, tag, subscription, domain, tags, subscriptions, domains, locked],
  );
  const key = useMemo(() => filterKey(filter), [filter]);

  const params = useMemo<PagedAccountsParams>(
    () => ({
      page,
      size,
      sort,
      desc: descending,
      q: filter.q,
      statuses: filter.statuses,
      tag_ids: filter.tag_ids,
      untagged: filter.include_untagged,
      subscriptions: filter.subscriptions,
      domains: filter.domains,
    }),
    [page, size, sort, descending, filter],
  );

  /* Only the newest request is allowed to write state: a slow response for an
     abandoned filter must never overwrite the one on screen. */
  const latest = useRef(0);
  const [nonce, setNonce] = useState(0);
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    const ticket = latest.current + 1;
    latest.current = ticket;
    setLoading(true);

    listAccounts(params).then(
      (result) => {
        if (!alive.current || latest.current !== ticket) return;
        setItems(result.items);
        setTotal(result.total);
        setPages(result.pages);
        setError(null);
        setLoading(false);
        setInitial(false);
      },
      (err: unknown) => {
        if (!alive.current || latest.current !== ticket) return;
        setError(errorMessage(err));
        setLoading(false);
        setInitial(false);
      },
    );
  }, [params, nonce]);

  /* A filter that shrinks under you can leave the page number past the end. */
  useEffect(() => {
    if (!loading && pages > 0 && page > pages) setPageState(pages);
  }, [loading, page, pages]);

  const replaceItem = useCallback((account: Account) => {
    setItems((current) => current.map((row) => (row.id === account.id ? account : row)));
  }, []);

  return {
    query,
    setQ,
    setStatuses,
    toggleStatus,
    setTag,
    setSubscription,
    setDomain,
    setTags,
    setSubscriptions,
    setDomains,
    clearFilters,
    narrowed: isQueryNarrowed({ q: qApplied, statuses, tag, subscription, domain }, locked),
    filter,
    key,
    page,
    size,
    pages,
    total,
    setPage,
    setSize,
    sort,
    descending,
    toggleSort,
    items,
    loading,
    error,
    initial,
    reload,
    replaceItem,
  };
}
