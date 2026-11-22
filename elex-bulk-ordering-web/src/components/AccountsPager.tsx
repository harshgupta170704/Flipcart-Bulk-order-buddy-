/**
 * "Showing 1–50 of 37,062", a page-size choice and prev/next.
 *
 * The count comes from the server's `total`, never from the rows on screen —
 * at this scale the two are almost never the same number, and quoting the
 * visible rows is how a paged table ends up lying about how much is there.
 */
import { formatCount } from "../lib/format";
import { ACCOUNT_PAGE_SIZES } from "../lib/types";
import { Button } from "./Button";
import { Select } from "./Field";

export interface AccountsPagerProps {
  page: number;
  size: number;
  /** The server's total for the current filter. */
  total: number;
  pages: number;
  /** How many rows this page actually rendered. */
  shown: number;
  onPageChange: (next: number) => void;
  /** Omit to hide the size control — a fixed window needs no chooser. */
  onSizeChange?: (next: number) => void;
  sizeOptions?: readonly number[];
  loading?: boolean;
  disabled?: boolean;
  /** Must be unique per pager on the page. */
  idPrefix: string;
  /** What the rows are, for the loading line ("Loading order units…"). */
  noun?: string;
  className?: string;
}

export function AccountsPager({
  page,
  size,
  total,
  pages,
  shown,
  onPageChange,
  onSizeChange,
  sizeOptions = ACCOUNT_PAGE_SIZES,
  loading = false,
  disabled = false,
  idPrefix,
  noun = "accounts",
  className = "",
}: AccountsPagerProps) {
  const first = total === 0 ? 0 : (page - 1) * size + 1;
  const last = total === 0 ? 0 : first + Math.max(0, shown) - 1;
  const lastPage = Math.max(1, pages);

  const range =
    total === 0
      ? "Nothing matches"
      : `Showing ${formatCount(first)}–${formatCount(last)} of ${formatCount(total)}`;

  return (
    <div className={`flex flex-wrap items-center justify-between gap-3 ${className}`}>
      <p role="status" aria-live="polite" className="text-[12px] text-ink-2">
        {loading ? `Loading ${noun}…` : range}
      </p>

      <div className="flex flex-wrap items-center gap-3">
        {onSizeChange ? (
          <div className="flex items-center gap-2">
            <label htmlFor={`${idPrefix}-size`} className="text-[12px] text-ink-2">
              Per page
            </label>
            <Select
              id={`${idPrefix}-size`}
              value={String(size)}
              disabled={disabled}
              className="h-8 w-[76px] text-[13px]"
              onChange={(event) => onSizeChange(Number(event.target.value))}
            >
              {sizeOptions.map((option) => (
                <option key={option} value={String(option)}>
                  {option}
                </option>
              ))}
            </Select>
          </div>
        ) : null}

        <div className="flex items-center gap-2">
          <Button
            size="sm"
            disabled={disabled || loading || page <= 1}
            onClick={() => onPageChange(page - 1)}
          >
            Previous
          </Button>
          <span className="whitespace-nowrap text-[12px] tabular-nums text-ink-2">
            Page {formatCount(page)} of {formatCount(lastPage)}
          </span>
          <Button
            size="sm"
            disabled={disabled || loading || page >= lastPage}
            onClick={() => onPageChange(page + 1)}
          >
            Next
          </Button>
        </div>
      </div>
    </div>
  );
}

export default AccountsPager;
