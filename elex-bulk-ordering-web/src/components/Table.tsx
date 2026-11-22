import type { ReactNode } from "react";
import { DASH } from "../lib/format";

export type ColumnAlign = "left" | "center" | "right";

export interface Column<T> {
  /** Also the fallback accessor: with no `render`, row[key] is printed. */
  key: string;
  header: ReactNode;
  render?: (row: T, index: number) => ReactNode;
  align?: ColumnAlign;
  /** Right-aligns and sets tabular-nums. */
  numeric?: boolean;
  /** A CSS width, e.g. "160px" or "28%". */
  width?: string;
  /** Keep this column in view while the table scrolls sideways — for actions. */
  sticky?: "right";
}

export interface TableProps<T> {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string | number;
  /** Shown instead of the table body when there are no rows. */
  empty?: ReactNode;
  className?: string;
  /** Screen-reader name for the table, when no nearby heading names it. */
  caption?: string;
  /** Minimum table width before the container scrolls. Default "720px". */
  minWidth?: string;
  /** Tighter cells, for a table of many columns that should fit a laptop screen. */
  dense?: boolean;
}

const ALIGN: Record<ColumnAlign, string> = {
  left: "text-left",
  center: "text-center",
  right: "text-right",
};

/* A pinned cell needs its own background, or the scrolled columns show through it. */
const STICKY: Record<"right" | "none", string> = {
  right: "sticky right-0 z-10 border-l border-line bg-surface group-hover:bg-page",
  none: "",
};

function fallbackCell(row: unknown, key: string): ReactNode {
  if (!row || typeof row !== "object") return DASH;
  const value = (row as Record<string, unknown>)[key];
  if (value === null || value === undefined || value === "") return DASH;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string" || typeof value === "number") return String(value);
  return DASH;
}

/**
 * A plain data table. It scrolls inside its own container on narrow screens so
 * the page itself never scrolls sideways.
 */
export function Table<T>({
  columns,
  rows,
  rowKey,
  empty,
  className = "",
  caption,
  minWidth = "720px",
  dense = false,
}: TableProps<T>) {
  const pad = dense ? "px-2" : "px-4";
  return (
    <div className={`w-full overflow-x-auto rounded-card border border-line bg-surface ${className}`}>
      <table className={`w-full border-collapse ${dense ? "text-[13px]" : "text-sm"}`} style={{ minWidth }}>
        {caption ? <caption className="sr-only">{caption}</caption> : null}
        <thead>
          <tr className="border-b border-line">
            {columns.map((col) => {
              const align = col.align ?? (col.numeric ? "right" : "left");
              return (
                <th
                  key={col.key}
                  scope="col"
                  style={col.width ? { width: col.width } : undefined}
                  className={`${ALIGN[align]} ${STICKY[col.sticky ?? "none"]} whitespace-nowrap ${pad} py-2.5 text-[12px] font-semibold uppercase tracking-wide text-muted`}
                >
                  {col.header}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            // The header row stays: a filter that matches nothing still shows
            // which columns the reader was looking at (spec FR-018).
            <tr>
              <td colSpan={columns.length} className="px-4 py-6">
                {empty ?? <span className="block text-center text-[13px] text-ink-2">Nothing to show.</span>}
              </td>
            </tr>
          ) : (
            rows.map((row, index) => (
              <tr
                key={rowKey(row)}
                className="group border-b border-line last:border-b-0 hover:bg-page"
              >
                {columns.map((col) => {
                  const align = col.align ?? (col.numeric ? "right" : "left");
                  return (
                    <td
                      key={col.key}
                      className={`${ALIGN[align]} ${STICKY[col.sticky ?? "none"]} ${col.numeric ? "tabular-nums" : ""} ${pad} py-2.5 align-middle text-ink`}
                    >
                      {col.render ? col.render(row, index) : fallbackCell(row, col.key)}
                    </td>
                  );
                })}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

export default Table;
