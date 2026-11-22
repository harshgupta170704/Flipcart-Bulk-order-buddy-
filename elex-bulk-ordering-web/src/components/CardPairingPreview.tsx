/**
 * Which card will pay for which account — shown before Start (26 Oct 2026).
 *
 * A card run pairs the sheet's rows with the selected accounts by email, and
 * until now the pairing was seen only afterwards: as skipped accounts, or a
 * wrong card. The server reads and pairs the attached sheet exactly as the run
 * would (POST /jobs/order/card-preview) and answers each card's last four
 * digits and whether its row carries the bank's password; this lists them,
 * warns about the accounts that have no card, and names the rows set aside.
 * Nothing is kept: Start sends the sheet again, as it always has.
 */
import { useEffect, useRef, useState } from "react";
import { errorMessage, previewOrderCards } from "../lib/api";
import { countLabel, formatCount } from "../lib/format";
import type { AccountSelection, CardPairing, CardPreview } from "../lib/types";
import { Alert } from "./Alert";
import { Table, type Column } from "./Table";

/** Rows listed; the rest are counted. A run can hold thousands of accounts. */
export const PAIRING_ROWS_SHOWN = 50;
/** The sheet or the selection is given a moment to settle before asking. */
const PREVIEW_DEBOUNCE_MS = 300;

const COLUMNS: Column<CardPairing>[] = [
  { key: "email", header: "Account", width: "50%" },
  {
    key: "card_last4",
    header: "Card",
    render: (row) =>
      row.card_last4 === null ? (
        <span className="font-medium text-ink">No card — will be skipped</span>
      ) : (
        <span className="font-mono tabular-nums">ending {row.card_last4}</span>
      ),
  },
  {
    key: "has_password",
    header: "Bank password",
    render: (row) => (row.has_password === null ? <span className="text-muted">—</span> : row.has_password ? "Yes" : "No"),
  },
];

export function CardPairingPreview({
  file,
  selection,
  cardType = "",
}: {
  file: File;
  selection: AccountSelection;
  /** Only the sheet's rows of this Card Type, as the run will use. */
  cardType?: string;
}) {
  const [preview, setPreview] = useState<CardPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  /* Only the newest request may land: a reply for an earlier sheet or
     selection must never be shown as this one's pairing. */
  const request = useRef(0);
  /* The sheet the newest request is about. */
  const askedFile = useRef<File | null>(null);
  const selectionKey = JSON.stringify(selection);

  useEffect(() => {
    const mine = ++request.current;
    if (askedFile.current !== file) {
      // Another sheet: the last one's card endings must not sit under this
      // one's name for a moment, so its answer goes at once. A new selection
      // alone keeps the table (marked out of date below), so the account
      // picker under it does not jump at every tick.
      askedFile.current = file;
      setPreview(null);
    }
    setLoading(true);
    setError(null);
    const timer = window.setTimeout(() => {
      previewOrderCards({
        cards_file: file,
        account_ids: selection.mode === "ids" ? selection.ids : null,
        account_filter: selection.mode === "filter" ? selection.filter : undefined,
        ...(cardType.trim() ? { card_type: cardType } : {}),
      }).then(
        (answer) => {
          if (request.current !== mine) return;
          setPreview(answer);
          setLoading(false);
        },
        (err: unknown) => {
          if (request.current !== mine) return;
          setPreview(null);
          setError(errorMessage(err));
          setLoading(false);
        },
      );
    }, PREVIEW_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
    // The selection is compared by its contents, not its identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file, selectionKey, cardType]);

  /* The answer on screen is for an earlier selection of this same sheet. */
  const outOfDate = loading && preview !== null;

  return (
    <section aria-label="Which card pays for which account" aria-busy={loading} className="flex flex-col gap-2">
      <h3 className="text-[13px] font-semibold text-ink">Which card pays for which account</h3>
      {loading && preview === null && error === null ? (
        <p className="text-[12px] text-muted">Reading the sheet…</p>
      ) : null}
      {outOfDate ? <p className="text-[12px] text-muted">Checking the new selection…</p> : null}
      {error !== null ? (
        <Alert tone="error" title="The run would refuse this sheet">
          {error}
        </Alert>
      ) : null}
      {preview !== null && error === null ? (
        <div className={`flex flex-col gap-2 transition-opacity ${outOfDate ? "opacity-60" : ""}`}>
          {/* The counts and the warning are this selection's or nothing: until
              its answer lands, the table below is the earlier one's. */}
          {outOfDate ? null : (
            <p className="text-[12px] text-ink-2">
              {formatCount(preview.paired)} of {countLabel(preview.accounts.length, "selected account")} have a card
              in this sheet{preview.paired_by === "email" ? ", matched by email" : ""}. Only each card&rsquo;s last
              four digits are shown.
            </p>
          )}
          {!outOfDate && preview.without_card > 0 ? (
            <Alert tone="warning" title={`${countLabel(preview.without_card, "account")} without a card`}>
              No row of this sheet carries {preview.without_card === 1 ? "its email" : "their emails"}, so the run
              will skip {preview.without_card === 1 ? "it" : "them"}.
            </Alert>
          ) : null}
          <Table
            columns={COLUMNS}
            rows={preview.accounts.slice(0, PAIRING_ROWS_SHOWN)}
            rowKey={(row) => String(row.account_id)}
            caption="Account, the card that pays for it, and whether its row has a bank password"
            minWidth="420px"
          />
          {preview.accounts.length > PAIRING_ROWS_SHOWN ? (
            <p className="text-[12px] text-muted">
              And {formatCount(preview.accounts.length - PAIRING_ROWS_SHOWN)} more accounts, paired the same way.
            </p>
          ) : null}
          {preview.rows_set_aside.length > 0 ? (
            <div className="text-[12px] text-ink-2">
              <p className="font-medium text-ink">
                {countLabel(preview.rows_set_aside.length, "row")} of the sheet set aside — the run will not use{" "}
                {preview.rows_set_aside.length === 1 ? "it" : "them"}:
              </p>
              <ul className="mt-0.5 list-disc pl-5">
                {preview.rows_set_aside.map((item) => (
                  <li key={item.row}>
                    Row {item.row}: {item.reason}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

export default CardPairingPreview;
