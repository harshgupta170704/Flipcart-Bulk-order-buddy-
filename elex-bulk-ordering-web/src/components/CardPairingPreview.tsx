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
 *
 * With `leaveOut` (the order form's run check) the server also knows who the
 * run will pass over before it tries anyone — already bought this product,
 * ordered recently, set aside by Smart ID. A pool sheet (no email column)
 * gives them no card, so its cards go to the IDs the run tries (run #960,
 * 24 Nov 2026: 158 of 200 pool cards went to IDs the run then passed over).
 * They are listed last, each with why.
 */
import { useEffect, useRef, useState } from "react";
import { errorMessage, previewOrderCards } from "../lib/api";
import { countLabel, formatCount } from "../lib/format";
import type { AccountSelection, CardPairing, CardPreview, CardPreviewLeaveOut } from "../lib/types";
import { Alert } from "./Alert";
import { Table, type Column } from "./Table";

/** Rows listed; the rest are counted. A run can hold thousands of accounts. */
export const PAIRING_ROWS_SHOWN = 50;
/** Rows of accounts the run will not try, listed after the ones it tries. */
export const PASSED_OVER_ROWS_SHOWN = 20;
/** The sheet or the selection is given a moment to settle before asking. */
const PREVIEW_DEBOUNCE_MS = 300;

const COLUMNS: Column<CardPairing>[] = [
  { key: "email", header: "Account", width: "50%" },
  {
    key: "card_last4",
    header: "Card",
    render: (row) =>
      row.passed_over ? (
        <span className="text-ink-2">Not tried — {row.passed_over}</span>
      ) : row.card_last4 === null ? (
        <span className="font-medium text-ink">No card — will be skipped</span>
      ) : row.skip_reason ? (
        <span className="font-medium text-ink" title={row.skip_reason}>
          <span className="font-mono tabular-nums">ending {row.card_last4}</span> — will be skipped: card already used
          or kept out by a card rule
        </span>
      ) : (
        <span className="font-mono tabular-nums">ending {row.card_last4}</span>
      ),
  },
  {
    key: "has_password",
    header: "Bank password",
    render: (row) =>
      row.passed_over || row.has_password === null ? (
        <span className="text-muted">—</span>
      ) : row.has_password ? (
        "Yes"
      ) : (
        "No"
      ),
  },
];

export function CardPairingPreview({
  file,
  selection,
  cardType = "",
  minRemaining = null,
  excludeUsed = false,
  cooldownDays = null,
  failedDays = null,
  failedCodes = [],
  retryOf = null,
  leaveOut = null,
}: {
  file: File;
  selection: AccountSelection;
  /** Only the sheet's rows of this Card Type, as the run will use. */
  cardType?: string;
  /** The run's "minimum remaining" rule, so HDFC child cards pair as the run pairs them. */
  minRemaining?: number | null;
  /** The run's card rules: a pool sheet (no email column) gives no account a
   *  card these keep out, so the preview pairs as the run will. */
  excludeUsed?: boolean;
  cooldownDays?: number | null;
  failedDays?: number | null;
  failedCodes?: string[];
  /** A retry: the server widens the cooldown to cover every order since that run began. */
  retryOf?: number | null;
  /** Who the run will pass over before it tries anyone (the form's run check
   *  and its Smart ID / AI switches); null: nobody, as before. */
  leaveOut?: CardPreviewLeaveOut | null;
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
  /* Compared by its contents: the form builds a new object every render. */
  const leaveOutKey = leaveOut === null ? "" : JSON.stringify(leaveOut);

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
        ...(minRemaining ? { card_min_remaining: minRemaining } : {}),
        ...(excludeUsed ? { card_exclude_used: true } : {}),
        ...(cooldownDays ? { card_cooldown_days: cooldownDays } : {}),
        ...(failedDays ? { card_exclude_failed_days: failedDays } : {}),
        ...(failedCodes.length ? { card_exclude_failed_codes: failedCodes } : {}),
        ...(retryOf ? { retry_of: retryOf } : {}),
        ...(leaveOut !== null ? { leave_out: leaveOut } : {}),
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
  }, [
    file,
    selectionKey,
    cardType,
    minRemaining,
    excludeUsed,
    cooldownDays,
    failedDays,
    failedCodes.join(","),
    retryOf,
    leaveOutKey,
  ]);

  /* The answer on screen is for an earlier selection of this same sheet. */
  const outOfDate = loading && preview !== null;
  /* Selected accounts the run will not try at all (0 from an older server). */
  const passedOver = preview?.passed_over ?? 0;
  /* The server lists the accounts the run tries first and the rest after. */
  const triedRows = preview === null ? [] : preview.accounts.filter((row) => !row.passed_over);
  const passedRows = preview === null ? [] : preview.accounts.filter((row) => row.passed_over);
  const noneTried = preview !== null && passedOver > 0 && triedRows.length === 0;

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
          {outOfDate ? null : noneTried ? (
            <p className="text-[12px] font-medium text-ink">
              The run will try none of the {countLabel(passedOver, "selected account")}: each already bought this
              product, ordered recently or is set aside by Smart ID. No card of this sheet is used.
            </p>
          ) : (
            <p className="text-[12px] text-ink-2">
              {passedOver > 0 ? (
                <>
                  {formatCount(preview.paired)} of the{" "}
                  {countLabel(preview.accounts.length - passedOver, "account")} the run will try{" "}
                  {preview.paired === 1 ? "has" : "have"} a card
                </>
              ) : (
                <>
                  {formatCount(preview.paired)} of {countLabel(preview.accounts.length, "selected account")} have a
                  card
                </>
              )}{" "}
              in this sheet
              {preview.paired_by === "email"
                ? ", matched by email"
                : preview.paired_by === "balance"
                  ? ", matched by what each HDFC child card has left in CorpAssist (most first)"
                  : ""}
              . Only each card&rsquo;s last
              four digits are shown.
            </p>
          )}
          {!outOfDate && passedOver > 0 && !noneTried ? (
            <p className="text-[12px] text-ink-2">
              {countLabel(passedOver, "selected account")} will not be tried: already bought this product, ordered
              recently or set aside by Smart ID (listed after the accounts the run tries, with why).{" "}
              {preview.paired_by === "email"
                ? `Their cards in this sheet are not used.`
                : `${passedOver === 1 ? "It gets" : "They get"} no card, so the cards go to the accounts the run tries.`}
            </p>
          ) : null}
          {!outOfDate && (preview.cards_kept_out ?? 0) > 0 ? (
            <p className="text-[12px] text-ink-2">
              {countLabel(preview.cards_kept_out ?? 0, "card")} of this sheet{" "}
              {(preview.cards_kept_out ?? 0) === 1 ? "is" : "are"} left out:{" "}
              {excludeUsed
                ? "already paid for an order in this tool"
                : `paid for an order${cooldownDays || retryOf ? " inside the card cooldown" : ""}`}{" "}
              or failed at payment recently. No account gets {(preview.cards_kept_out ?? 0) === 1 ? "it" : "them"}.
            </p>
          ) : null}
          {!outOfDate && (preview.will_skip ?? 0) > 0 ? (
            <Alert
              tone="warning"
              title={`${countLabel(preview.will_skip ?? 0, "account")} will be passed over: ${(preview.will_skip ?? 0) === 1 ? "its card is" : "their cards are"} kept out`}
            >
              The sheet gives {(preview.will_skip ?? 0) === 1 ? "this account a card" : "these accounts cards"} that the
              card rules keep out (for example, already paid for an order in this tool). With an email column no other
              card can take {(preview.will_skip ?? 0) === 1 ? "its" : "their"} place, so the run places no order for{" "}
              {(preview.will_skip ?? 0) === 1 ? "it" : "them"}.
            </Alert>
          ) : null}
          {!outOfDate && preview.without_card > 0 ? (
            <Alert tone="warning" title={`${countLabel(preview.without_card, "account")} without a card`}>
              {preview.paired_by === "email" ? (
                <>
                  No row of this sheet carries {preview.without_card === 1 ? "its email" : "their emails"}, so the
                  run will skip {preview.without_card === 1 ? "it" : "them"}.
                </>
              ) : (
                <>
                  This sheet has too few usable cards for every account, so the run will skip{" "}
                  {preview.without_card === 1 ? "it" : "them"}.
                </>
              )}
            </Alert>
          ) : null}
          <Table
            columns={COLUMNS}
            rows={[...triedRows.slice(0, PAIRING_ROWS_SHOWN), ...passedRows.slice(0, PASSED_OVER_ROWS_SHOWN)]}
            rowKey={(row) => String(row.account_id)}
            caption="Account, the card that pays for it, and whether its row has a bank password"
            minWidth="420px"
          />
          {triedRows.length > PAIRING_ROWS_SHOWN || passedRows.length > PASSED_OVER_ROWS_SHOWN ? (
            <p className="text-[12px] text-muted">
              {passedRows.length === 0
                ? `And ${formatCount(triedRows.length - PAIRING_ROWS_SHOWN)} more accounts, paired the same way.`
                : [
                    triedRows.length > PAIRING_ROWS_SHOWN
                      ? `${formatCount(triedRows.length - PAIRING_ROWS_SHOWN)} more accounts the run will try are paired the same way`
                      : "",
                    passedRows.length > PASSED_OVER_ROWS_SHOWN
                      ? `${formatCount(passedRows.length - PASSED_OVER_ROWS_SHOWN)} more will not be tried`
                      : "",
                  ]
                    .filter(Boolean)
                    .join("; ")
                    .replace(/^./, (first) => `And ${first.toLowerCase()}`) + "."}
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
