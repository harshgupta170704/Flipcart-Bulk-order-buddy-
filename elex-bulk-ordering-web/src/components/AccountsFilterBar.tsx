/**
 * The filter bar, shared by the Accounts table and the run pages' picker.
 *
 * Every control here narrows on the SERVER. The counts beside each option come
 * from GET /accounts/filter-options, so a user can see that "blocked" holds
 * 1,204 accounts before spending a request on it.
 */
import type { AccountQuery } from "./AccountsQuery";
import type { FilterOptions } from "../lib/types";
import { formatCount } from "../lib/format";
import { Button } from "./Button";
import { Field, Select, TextInput } from "./Field";
import { Spinner } from "./Spinner";
import { statusMeta } from "./StatusBadge";

/** The sentinel the tag <select> uses for "accounts with no tag". */
const UNTAGGED = "untagged";

export interface AccountsFilterBarProps {
  query: AccountQuery;
  onQChange: (value: string) => void;
  onToggleStatus: (value: string) => void;
  onTagChange: (next: number | "untagged" | null) => void;
  onSubscriptionChange: (next: string) => void;
  /** Optional: hosts that do not pass it show no domain filter. */
  onDomainChange?: (next: string) => void;
  /**
   * The host's own status restriction. When given, the status control is not
   * offered at all — a coupon run's scope is not the user's to widen.
   */
  lockedStatuses?: readonly string[];
  options: FilterOptions | null;
  optionsLoading: boolean;
  /** Already human-readable — it came through errorMessage(). */
  optionsError: string | null;
  onRetryOptions: () => void;
  /** True once anything is narrowed, which is when Clear earns its place. */
  narrowed: boolean;
  onClear: () => void;
  disabled?: boolean;
  /** Must be unique per bar on the page — two bars can share a screen. */
  idPrefix: string;
  /** Leave out Tag, Subscription and Email domain: the host shows checklists instead. */
  hideFacets?: boolean;
  className?: string;
}

export function AccountsFilterBar({
  query,
  onQChange,
  onToggleStatus,
  onTagChange,
  onSubscriptionChange,
  onDomainChange,
  lockedStatuses,
  options,
  optionsLoading,
  optionsError,
  onRetryOptions,
  narrowed,
  onClear,
  disabled = false,
  idPrefix,
  hideFacets = false,
  className = "",
}: AccountsFilterBarProps) {
  const statusOptions = options?.statuses ?? [];
  /* `narrowed` follows the DEBOUNCED search text, so Clear would appear 300ms
     after the first keystroke and shift the row. Follow the raw box instead. */
  const showClear = narrowed || query.q.trim() !== "";
  /* Only the very first load leaves nothing to choose from; a later refresh
     must not disable a control the user is in the middle of using. */
  const optionsMissing = options === null;
  const tagOptions = options?.tags ?? [];
  const subscriptionOptions = options?.subscriptions ?? [];
  const domainOptions = options?.domains ?? [];

  const tagValue =
    query.tag === null ? "" : query.tag === UNTAGGED ? UNTAGGED : String(query.tag);

  return (
    <div className={`flex flex-col gap-3 ${className}`}>
      <div className="flex flex-wrap items-end gap-3">
        <Field
          id={`${idPrefix}-q`}
          label="Search email or phone"
          className="min-w-[220px] flex-1"
          help="Matches anywhere in either field. The server does the searching."
        >
          <TextInput
            type="search"
            autoComplete="off"
            spellCheck={false}
            placeholder="Contains…"
            value={query.q}
            disabled={disabled}
            onChange={(event) => onQChange(event.target.value)}
          />
        </Field>

        {hideFacets ? null : (
        <>
        <Field
          id={`${idPrefix}-tag`}
          label="Tag"
          className="w-full sm:w-[200px]"
          help={
            options
              ? `${formatCount(options.untagged)} untagged`
              : "Loading the tag list…"
          }
        >
          <Select
            value={tagValue}
            disabled={disabled || optionsMissing}
            onChange={(event) => {
              const value = event.target.value;
              if (value === "") onTagChange(null);
              else if (value === UNTAGGED) onTagChange(UNTAGGED);
              else onTagChange(Number(value));
            }}
          >
            <option value="">Any tag</option>
            <option value={UNTAGGED}>
              Untagged{options ? ` (${formatCount(options.untagged)})` : ""}
            </option>
            {tagOptions.map((tag) => (
              <option key={tag.id} value={String(tag.id)}>
                {tag.name} ({formatCount(tag.count)})
              </option>
            ))}
          </Select>
        </Field>

        <Field
          id={`${idPrefix}-sub`}
          label="Subscription"
          className="w-full sm:w-[220px]"
          help="The platform's own tier text."
        >
          <Select
            value={query.subscription}
            disabled={disabled || optionsMissing}
            onChange={(event) => onSubscriptionChange(event.target.value)}
          >
            <option value="">Any subscription</option>
            {subscriptionOptions.map((subscription) => (
              <option key={subscription} value={subscription}>
                {subscription}
              </option>
            ))}
          </Select>
        </Field>
        </>
        )}

        {onDomainChange && !hideFacets ? (
          <Field
            id={`${idPrefix}-domain`}
            label="Email domain"
            className="w-full sm:w-[220px]"
            help="The part of the address after the @."
          >
            <Select
              value={query.domain ?? ""}
              disabled={disabled || optionsMissing}
              onChange={(event) => onDomainChange(event.target.value)}
            >
              <option value="">Any domain</option>
              {domainOptions.map((domain) => (
                <option key={domain.value} value={domain.value}>
                  {domain.value} ({formatCount(domain.count)})
                </option>
              ))}
            </Select>
          </Field>
        ) : null}

        {showClear ? (
          <Button onClick={onClear} disabled={disabled}>
            Clear filters
          </Button>
        ) : null}
      </div>

      {lockedStatuses === undefined ? (
        <fieldset
          className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-control border border-line px-3.5 py-2.5"
          disabled={disabled}
        >
          <legend className="px-1 text-[13px] font-medium leading-none text-ink">Status</legend>
          {optionsLoading && statusOptions.length === 0 ? (
            <span className="flex items-center gap-2 text-[12px] text-ink-2">
              <Spinner size={13} />
              Loading statuses…
            </span>
          ) : statusOptions.length === 0 ? (
            <span className="text-[12px] text-ink-2">
              No statuses to filter by yet.
            </span>
          ) : (
            statusOptions.map((status) => {
              const id = `${idPrefix}-st-${status.value}`;
              const checked = query.statuses.includes(status.value);
              return (
                <span key={status.value} className="flex items-center gap-2">
                  <input
                    id={id}
                    type="checkbox"
                    checked={checked}
                    disabled={disabled}
                    onChange={() => onToggleStatus(status.value)}
                    className="size-4 shrink-0 accent-accent"
                  />
                  <label htmlFor={id} className="cursor-pointer text-[13px] text-ink">
                    {statusMeta(status.value).label}{" "}
                    <span className="tabular-nums text-muted">
                      ({formatCount(status.count)})
                    </span>
                  </label>
                </span>
              );
            })
          )}
        </fieldset>
      ) : (
        <p className="text-[12px] leading-snug text-ink-2">
          Restricted to{" "}
          <span className="font-medium text-ink">
            {lockedStatuses.length === 0
              ? "every status"
              : lockedStatuses.map((status) => statusMeta(status).label).join(", ")}
          </span>{" "}
          — this run cannot touch accounts in any other state.
        </p>
      )}

      {optionsError ? (
        <p role="status" className="flex flex-wrap items-center gap-2 text-[12px] text-ink-2">
          <span>Tag and status choices could not load: {optionsError}</span>
          <Button size="sm" variant="ghost" onClick={onRetryOptions}>
            Try again
          </Button>
        </p>
      ) : null}
    </div>
  );
}

export default AccountsFilterBar;
