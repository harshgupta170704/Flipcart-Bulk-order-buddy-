/**
 * What "Validate card file" found in the corporate-card sheet (the owner,
 * 24 Nov 2026).
 *
 * HDFC CorpAssist's export (and any sheet Excel opened) writes a CVV such as
 * 007 as 7 and 098 as 98. The server reads the sheet exactly as a run would
 * (POST /jobs/order/card-check) and gives such CVVs their zeros back; this
 * lists what was fixed, the rows the run cannot use, and the rows to check in
 * the sheet — each by row number and the card's last four digits, never a
 * value. The run applies the same fixes: Start sends the sheet again.
 *
 * It also answers "how many cards are still pending": how many of the sheet's
 * cards have not paid for any order in this tool yet, and which already have
 * (how many orders, the rupees, the latest date). The sheet is never stored,
 * so this is worked out from the orders each time the button is pressed.
 */
import type { ReactNode } from "react";
import { countLabel, formatCount, formatCurrency, formatDate } from "../lib/format";
import type { CardCheck, CardCheckRow, CardCheckUsed } from "../lib/types";
import { Alert } from "./Alert";

/** Rows listed per group; the rest are counted. A sheet can hold hundreds. */
export const CHECK_ROWS_SHOWN = 30;

function where(item: CardCheckRow): string {
  return item.card_last4 ? `Row ${item.row} · card ending ${item.card_last4}` : `Row ${item.row}`;
}

function RowList<T extends CardCheckRow>({ rows, text }: { rows: T[]; text?: (item: T) => string | undefined }) {
  return (
    <>
      <ul className="mt-1 list-disc pl-5">
        {rows.slice(0, CHECK_ROWS_SHOWN).map((item, index) => (
          <li key={`${item.row}-${index}`}>
            <span className="font-medium tabular-nums">{where(item)}</span>
            {text ? `: ${text(item) ?? ""}` : null}
          </li>
        ))}
      </ul>
      {rows.length > CHECK_ROWS_SHOWN ? (
        <p className="mt-1">And {formatCount(rows.length - CHECK_ROWS_SHOWN)} more.</p>
      ) : null}
    </>
  );
}

/** "2 orders, ₹32,423.00, the last on Nov 24, 2026". */
export function usedNote(item: CardCheckUsed): string {
  const parts = [countLabel(item.orders, "order"), formatCurrency(item.amount)];
  if (item.last_used) parts.push(`the last on ${formatDate(item.last_used)}`);
  return parts.join(", ");
}

function ShowRows({ children }: { children: ReactNode }) {
  return (
    <details className="mt-1">
      <summary className="cursor-pointer select-none font-medium">Show the rows</summary>
      {children}
    </details>
  );
}

export function CardCheckReport({
  check,
  fileName,
  skipUsed,
}: {
  check: CardCheck;
  fileName: string;
  /** Whether "Skip cards already used in an order" is ticked on the form; unknown leaves it unsaid. */
  skipUsed?: boolean;
}) {
  const clean = check.fixed.length === 0 && check.set_aside.length === 0 && check.warnings.length === 0;
  const used = check.used ?? [];
  const unused = check.unused ?? [];
  const knowsUsage = check.used !== undefined && check.unused !== undefined;
  return (
    <section aria-label="Card file check" className="flex flex-col gap-2 text-[12px]">
      <p className="text-ink-2">
        <span className="font-medium text-ink">{fileName}</span>: {countLabel(check.cards, "card")} the run can use
        {check.cards > 0 ? `, ${formatCount(check.with_password)} with a bank password` : ""}.
        {clean ? " No problems found." : ""}
      </p>
      {knowsUsage && check.cards > 0 ? (
        <p className="text-ink-2">
          Not used in any order yet: <span className="font-semibold text-ink tabular-nums">{formatCount(unused.length)}</span>
          {" · "}
          Already paid for an order: <span className="font-semibold text-ink tabular-nums">{formatCount(used.length)}</span>
        </p>
      ) : null}
      {check.fixed.length > 0 ? (
        <Alert
          tone="success"
          title={`Fixed ${countLabel(check.fixed.length, "CVV")} that had lost ${check.fixed.length === 1 ? "its" : "their"} leading zeros`}
        >
          The run will use the fixed {check.fixed.length === 1 ? "card" : "cards"} (for example 7 becomes 007, 98
          becomes 098). The file on your computer is not changed.
          <RowList rows={check.fixed} text={(item) => item.note} />
        </Alert>
      ) : null}
      {check.set_aside.length > 0 ? (
        <Alert
          tone="warning"
          title={`${countLabel(check.set_aside.length, "row")} set aside — the run will not use ${check.set_aside.length === 1 ? "it" : "them"}`}
        >
          <RowList rows={check.set_aside} text={(item) => item.reason} />
        </Alert>
      ) : null}
      {check.warnings.length > 0 ? (
        <Alert tone="warning" title={`${countLabel(check.warnings.length, "thing")} to check in the sheet`}>
          These rows are used as they are, but Flipkart or the bank is likely to refuse them. Correct the sheet and
          choose it again, or leave them.
          <RowList rows={check.warnings} text={(item) => item.note} />
        </Alert>
      ) : null}
      {unused.length > 0 ? (
        <Alert tone="info" title={`${countLabel(unused.length, "card")} not used in any order yet`}>
          <ShowRows>
            <RowList rows={unused} />
          </ShowRows>
        </Alert>
      ) : null}
      {used.length > 0 ? (
        <Alert tone="info" title={`${countLabel(used.length, "card")} already paid for an order`}>
          Counted from this tool's placed and not-yet-confirmed orders; spending outside this tool is not seen.{" "}
          {skipUsed === true
            ? "“Skip cards already used in an order” is ticked, so the run will not pay with these: from a sheet without an email column the next unused card pays instead; with an email column, that account is passed over."
            : skipUsed === false
              ? "“Skip cards already used in an order” is not ticked, so the run may use these again."
              : "A run skips a used card only when a card rule says so, such as Skip cards already used in an order."}
          <ShowRows>
            <RowList rows={used} text={usedNote} />
          </ShowRows>
        </Alert>
      ) : null}
    </section>
  );
}

export default CardCheckReport;
