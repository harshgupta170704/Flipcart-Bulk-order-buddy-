/**
 * The Accounts table, rebuilt for 37,062 rows.
 *
 * Nothing here holds more than one page. Searching, filtering and sorting all
 * happen on the server; the counts quoted on screen are the server's `total`,
 * not the rows in the DOM. And a bulk action over "everything matching" sends
 * the FILTER — never a list of ids, which at this scale would be a
 * multi-megabyte request that still only describes what the filter already
 * said.
 *
 * Each account can also have a GST and a billing phone of its own: its orders
 * bill under that GST whatever GST a run picks, and are placed with that
 * phone. Both are set per row, and the GST for a whole selection at once —
 * by an admin: they are setup (spec C-7), so an operator sees them on every
 * row but gets no control for them, and the API refuses an operator's write.
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import {
  ApiError,
  assignAccountsGst,
  createAccount,
  downloadAccounts,
  errorMessage,
  importAccounts,
  listGsts,
  patchAccount,
  tagAccounts,
  startBalancesJob,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import { BILLING_PHONE_MAX_LENGTH } from "../lib/types";
import type { Account, AccountPatch, AccountSortField, Gst, GstRef, ImportResult } from "../lib/types";
import { countLabel, formatBytes, formatCount, formatDateTime, DASH } from "../lib/format";
import { Alert } from "../components/Alert";
import { AccountsFilterBar } from "../components/AccountsFilterBar";
import { AccountsPager } from "../components/AccountsPager";
import { AccountsTagManager } from "../components/AccountsTagManager";
import { useAccountsQuery, useFilterOptions } from "../components/AccountsQuery";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { Field, FileInput, Select, TextInput } from "../components/Field";
import { Spinner } from "../components/Spinner";
import { StatusBadge } from "../components/StatusBadge";
import { Table, type Column } from "../components/Table";

/** A stable id so the empty state can send focus to the file picker. */
const IMPORT_FILE_ID = "accounts-import-file";

/** Deliberately loose: the server is the real authority on an address. */
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

/** The sentinel the bulk-tag <select> uses before a target is chosen. */
const NO_CHOICE = "";
/** The sentinel for "take the tag off". */
const CLEAR_TAG = "clear";
/** The sentinel for "take the account's own GST off" — it bills under the run's again. */
const CLEAR_GST = "clear";

/** What an account row carries of a GST, from the Address Book's full row. */
function gstRef(gst: Gst): GstRef {
  return { id: gst.id, label: gst.label, gstin: gst.gstin, business_name: gst.business_name };
}

function isXlsx(file: File): boolean {
  return file.name.toLowerCase().endsWith(".xlsx");
}

function focusImportControl(): void {
  const el = document.getElementById(IMPORT_FILE_ID);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.focus();
}

/* --- sortable column header ----------------------------------------------- */

interface SortHeaderProps {
  label: string;
  field: AccountSortField;
  sort: AccountSortField;
  descending: boolean;
  onSort: (field: AccountSortField) => void;
}

/**
 * The shared Table renders plain <th>s, so the sort control lives inside the
 * header cell as a real button. Its accessible name carries the current
 * direction, which is what `aria-sort` on the <th> would otherwise say.
 */
/** Refused rows listed at first; "Show all" lists every one the server returned. */
const SHOWN_REFUSALS = 20;

/** The workbook rows the import refused, each with its reason (audit F-26). */
function ImportRefusals({ rows }: { rows: readonly { row: number; reason: string }[] }) {
  const [all, setAll] = useState(false);
  const listId = useId();
  if (rows.length === 0) return null;
  return (
    <div className="mt-2 flex flex-col gap-1">
      <ul
        id={listId}
        aria-label="Refused rows"
        // Scrolls once every row is listed: focusable then, so the keyboard can scroll it.
        tabIndex={all ? 0 : undefined}
        className={`list-disc pl-5 text-[12px] ${all ? "max-h-[320px] overflow-y-auto" : ""}`}
      >
        {(all ? rows : rows.slice(0, SHOWN_REFUSALS)).map((item) => (
          <li key={item.row}>
            Row {item.row}: {item.reason}
          </li>
        ))}
        {!all && rows.length > SHOWN_REFUSALS ? <li>… and {rows.length - SHOWN_REFUSALS} more</li> : null}
      </ul>
      {rows.length > SHOWN_REFUSALS ? (
        <div>
          <Button size="sm" variant="ghost" aria-expanded={all} aria-controls={listId} onClick={() => setAll((open) => !open)}>
            {all ? `Show the first ${SHOWN_REFUSALS}` : `Show all ${rows.length}`}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function SortHeader({ label, field, sort, descending, onSort }: SortHeaderProps) {
  const active = sort === field;
  const direction = active ? (descending ? "descending" : "ascending") : "not sorted";
  return (
    <button
      type="button"
      onClick={() => onSort(field)}
      aria-label={`${label}, ${direction}. Activate to sort${active ? " the other way" : " by this column"}.`}
      // The focus ring comes from the one global :focus-visible rule.
      className="inline-flex items-center gap-1 rounded-control text-[12px] font-semibold uppercase tracking-wide text-muted hover:text-ink"
    >
      <span>{label}</span>
      <span aria-hidden="true" className={active ? "text-ink" : "opacity-50"}>
        {active ? (descending ? "▼" : "▲") : "↕"}
      </span>
    </button>
  );
}

export default function Accounts() {
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    document.title = "Accounts — Ordering Suite";
  }, []);

  const q = useAccountsQuery({ initialSize: 50 });
  const optionsState = useFilterOptions();
  const { options } = optionsState;
  /* An account's own GST and billing phone are setup (FR-058, C-7): only an
     admin changes them. The API refuses an operator's write too; this is the
     half that keeps the controls honest, as on the Address Book. */
  const { isAdmin } = useAuth();

  const tagById = useMemo(() => {
    const map = new Map<number, { id: number; name: string }>();
    for (const tag of options?.tags ?? []) map.set(tag.id, { id: tag.id, name: tag.name });
    return map;
  }, [options]);

  /* --- the Address Book's GSTs, for an account's own GST ------------------
     A short, hand-kept list (one row per business entity), so it is fetched
     whole, like the Address Book does. Until it arrives the GST controls stay
     disabled, but a row still shows the GST it has. */
  const [gsts, setGsts] = useState<Gst[] | null>(null);
  const [gstsError, setGstsError] = useState<string | null>(null);
  const reloadGsts = useCallback(() => {
    listGsts().then(
      (rows) => {
        if (!alive.current) return;
        setGsts(rows);
        setGstsError(null);
      },
      (err: unknown) => {
        if (alive.current) setGstsError(errorMessage(err));
      },
    );
  }, []);
  useEffect(() => {
    reloadGsts();
  }, [reloadGsts]);
  const gstById = useMemo(() => new Map((gsts ?? []).map((gst) => [gst.id, gst])), [gsts]);

  /** Everything the table and its bulk actions depend on, refreshed together. */
  const reloadPage = q.reload;
  const reloadOptions = optionsState.reload;
  const refreshAll = useCallback(() => {
    reloadPage();
    reloadOptions();
    reloadGsts();
  }, [reloadPage, reloadOptions, reloadGsts]);

  /* --- import ------------------------------------------------------------ */

  const [file, setFile] = useState<File | null>(null);
  /** Bumping this remounts the file input, which is how it gets cleared. */
  const [fileKey, setFileKey] = useState(0);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);

  function pickFile(event: ChangeEvent<HTMLInputElement>) {
    const picked = event.target.files?.[0] ?? null;
    setImportResult(null);
    setFile(picked);
    setImportError(
      picked && !isXlsx(picked)
        ? "That is not an .xlsx workbook. Save the sheet as .xlsx and pick it again."
        : null,
    );
  }

  async function runImport() {
    if (!file) return;
    if (!isXlsx(file)) {
      setImportError("That is not an .xlsx workbook. Save the sheet as .xlsx and pick it again.");
      return;
    }
    setImporting(true);
    setImportError(null);
    setImportResult(null);
    try {
      const result = await importAccounts(file);
      if (!alive.current) return;
      setImportResult(result);
      setFile(null);
      setFileKey((key) => key + 1);
      refreshAll();
    } catch (err) {
      if (!alive.current) return;
      setImportError(errorMessage(err));
    } finally {
      if (alive.current) setImporting(false);
    }
  }

  /* --- add one by hand --------------------------------------------------- */

  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [emailError, setEmailError] = useState<string | null>(null);
  const [addError, setAddError] = useState<string | null>(null);
  const [added, setAdded] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  async function submitAdd(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = email.trim();
    if (!EMAIL_RE.test(value)) {
      setEmailError("Enter an email address like name@example.com.");
      setAdded(null);
      return;
    }
    setEmailError(null);
    setAddError(null);
    setAdded(null);
    setAdding(true);
    try {
      const trimmedPhone = phone.trim();
      const created = await createAccount(
        trimmedPhone ? { email: value, phone: trimmedPhone } : { email: value },
      );
      if (!alive.current) return;
      setAdded(created.email);
      setEmail("");
      setPhone("");
      refreshAll();
    } catch (err) {
      if (!alive.current) return;
      if (err instanceof ApiError && err.status === 409) {
        setEmailError(`${value} is already in the list — nothing was changed.`);
      } else {
        setAddError(errorMessage(err));
      }
    } finally {
      if (alive.current) setAdding(false);
    }
  }

  /* --- export ------------------------------------------------------------ */

  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const [readingBalances, setReadingBalances] = useState(false);
  const [balancesNote, setBalancesNote] = useState<string | null>(null);
  const readBalances = async () => {
    setReadingBalances(true);
    try {
      const job = await startBalancesJob({ account_ids: null, account_filter: q.filter, force: false });
      setBalancesNote(`Reading balances for ${job.total_items} account(s) — run #${job.id}. Refresh in a minute to see them.`);
    } catch (err) {
      setBalancesNote(errorMessage(err));
    } finally {
      setReadingBalances(false);
    }
  };

  async function runExport() {
    setExporting(true);
    setExportError(null);
    try {
      await downloadAccounts();
    } catch (err) {
      if (alive.current) setExportError(errorMessage(err));
    } finally {
      if (alive.current) setExporting(false);
    }
  }

  /* --- selection ---------------------------------------------------------
     Two modes, and the bar always says which one is live:
       · ticked rows — bounded by the page size, sent as account_ids
       · "all N matching" — sent as the FILTER, so 37,062 accounts cost one
         small request instead of 37,062 ids.
  -------------------------------------------------------------------------- */

  const [ticked, setTicked] = useState<number[]>([]);
  const [allMatching, setAllMatching] = useState(false);

  const pageIds = useMemo(() => q.items.map((account) => account.id), [q.items]);
  const tickedSet = useMemo(() => new Set(ticked), [ticked]);

  /* Ticks belong to the rows on screen: a new page, size or sort order means
     the ticked rows are no longer the ones in front of the user. */
  useEffect(() => {
    setTicked([]);
  }, [q.key, q.page, q.size, q.sort, q.descending]);

  /* "All matching" belongs to the filter, so it survives paging but not a
     change of filter — that would silently re-aim the bulk action. */
  useEffect(() => {
    setAllMatching(false);
  }, [q.key]);

  const allPageTicked = pageIds.length > 0 && pageIds.every((id) => tickedSet.has(id));
  const somePageTicked = pageIds.some((id) => tickedSet.has(id));
  const selectionActive = allMatching || ticked.length > 0;
  const affected = allMatching ? q.total : ticked.length;
  const canOfferAllMatching = !allMatching && q.total > q.items.length;

  const toggleHeader = () => {
    if (allMatching) {
      setAllMatching(false);
      setTicked([]);
      return;
    }
    setTicked(allPageTicked ? [] : pageIds);
  };

  const toggleRow = (id: number) => {
    if (allMatching) {
      // Leaving "all matching" by unticking one row keeps the rest of the page
      // ticked — the same thing every mail client does.
      setAllMatching(false);
      setTicked(pageIds.filter((pageId) => pageId !== id));
      return;
    }
    setTicked((current) =>
      current.includes(id) ? current.filter((x) => x !== id) : [...current, id],
    );
  };

  /* --- bulk tagging ------------------------------------------------------ */

  const [bulkChoice, setBulkChoice] = useState<string>(NO_CHOICE);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkError, setBulkError] = useState<string | null>(null);
  const [bulkNote, setBulkNote] = useState<string | null>(null);
  const [confirmBulk, setConfirmBulk] = useState(false);

  const bulkTagId: number | null = bulkChoice === CLEAR_TAG ? null : Number(bulkChoice);
  const bulkTagName = bulkTagId === null ? null : (tagById.get(bulkTagId)?.name ?? null);
  /** Applying past the rows on screen deserves a second look. */
  const bulkNeedsConfirm = allMatching && q.total > q.items.length;

  const applyBulk = async () => {
    if (bulkChoice === NO_CHOICE || !selectionActive) return;
    setBulkBusy(true);
    setBulkError(null);
    setBulkNote(null);
    setConfirmBulk(false);
    try {
      // "All matching" sends the FILTER. This is the whole point of the phase:
      // 37,062 accounts cost one small request, not 37,062 ids.
      const result = await tagAccounts(
        allMatching
          ? { tag_id: bulkTagId, filter: q.filter }
          : { tag_id: bulkTagId, account_ids: ticked },
      );
      if (!alive.current) return;
      setBulkNote(
        bulkChoice === CLEAR_TAG
          ? `Tag removed from ${countLabel(result.updated, "account")}.`
          : `${countLabel(result.updated, "account")} tagged “${bulkTagName ?? "—"}”.`,
      );
      setTicked([]);
      setAllMatching(false);
      setBulkChoice(NO_CHOICE);
      refreshAll();
    } catch (err) {
      if (!alive.current) return;
      setBulkError(errorMessage(err));
    } finally {
      if (alive.current) setBulkBusy(false);
    }
  };

  /* --- bulk GST -----------------------------------------------------------
     The same selection, the same two modes and the same second look past the
     rows on screen as tagging: an account's own GST decides which entity its
     orders bill to, whatever GST a run picks. */

  const [gstChoice, setGstChoice] = useState<string>(NO_CHOICE);
  const [gstBusy, setGstBusy] = useState(false);
  const [gstError, setGstError] = useState<string | null>(null);
  const [gstNote, setGstNote] = useState<string | null>(null);
  const [confirmGst, setConfirmGst] = useState(false);
  /** Either bulk action running: the selection must hold still for both. */
  const anyBulkBusy = bulkBusy || gstBusy;

  const bulkGstId: number | null = gstChoice === CLEAR_GST ? null : Number(gstChoice);
  const bulkGstLabel = bulkGstId === null ? null : (gstById.get(bulkGstId)?.label ?? null);

  const applyGst = async () => {
    if (gstChoice === NO_CHOICE || !selectionActive) return;
    setGstBusy(true);
    setGstError(null);
    setGstNote(null);
    setConfirmGst(false);
    try {
      // "All matching" sends the FILTER, exactly as tagging does.
      const result = await assignAccountsGst(
        allMatching
          ? { gst_id: bulkGstId, filter: q.filter }
          : { gst_id: bulkGstId, account_ids: ticked },
      );
      if (!alive.current) return;
      setGstNote(
        gstChoice === CLEAR_GST
          ? `${countLabel(result.updated, "account")} back on the run's GST.`
          : `${countLabel(result.updated, "account")} now ${result.updated === 1 ? "bills" : "bill"} under “${
              bulkGstLabel ?? "—"
            }”, whatever GST a run picks.`,
      );
      setTicked([]);
      setAllMatching(false);
      setGstChoice(NO_CHOICE);
      refreshAll();
    } catch (err) {
      if (!alive.current) return;
      setGstError(errorMessage(err));
    } finally {
      if (alive.current) setGstBusy(false);
    }
  };

  /* --- per-row tag (optimistic) ------------------------------------------ */

  const [rowBusy, setRowBusy] = useState<number[]>([]);
  const [rowError, setRowError] = useState<string | null>(null);
  const [rowStale, setRowStale] = useState(false);

  const changeRowTag = async (account: Account, tagId: number | null) => {
    const optimistic: Account = {
      ...account,
      tag: tagId === null ? null : (tagById.get(tagId) ?? account.tag),
    };
    q.replaceItem(optimistic);
    setRowBusy((current) => [...current, account.id]);
    setRowError(null);
    try {
      const updated = await patchAccount(account.id, { tag_id: tagId });
      if (!alive.current) return;
      q.replaceItem(updated);
      optionsState.reload();
      // A row whose tag just changed may no longer match a tag filter. Say so
      // rather than yanking it out from under the pointer.
      if ((q.filter.tag_ids ?? []).length > 0 || q.filter.include_untagged === true) {
        setRowStale(true);
      }
    } catch (err) {
      if (!alive.current) return;
      q.replaceItem(account); // roll back to exactly what the server last gave us
      setRowError(
        `Could not change the tag on ${account.email} — it has been put back. ${errorMessage(err)}`,
      );
    } finally {
      if (alive.current) setRowBusy((current) => current.filter((id) => id !== account.id));
    }
  };

  /* --- per-row GST and billing phone (optimistic, like the tag) ---------- */

  /**
   * Send one edit, showing `shown` on the row at once and putting the row back
   * as the server last gave it if the server refuses.
   */
  const saveRow = async (account: Account, shown: Partial<Account>, patch: AccountPatch, what: string) => {
    q.replaceItem({ ...account, ...shown });
    setRowBusy((current) => [...current, account.id]);
    setRowError(null);
    try {
      const updated = await patchAccount(account.id, patch);
      if (!alive.current) return;
      q.replaceItem(updated);
    } catch (err) {
      if (!alive.current) return;
      q.replaceItem(account);
      setRowError(`Could not change the ${what} on ${account.email} — it has been put back. ${errorMessage(err)}`);
    } finally {
      if (alive.current) setRowBusy((current) => current.filter((id) => id !== account.id));
    }
  };

  const changeRowGst = (account: Account, gstId: number | null) => {
    const picked = gstId === null ? undefined : gstById.get(gstId);
    void saveRow(account, { gst: picked ? gstRef(picked) : null }, { gst_id: gstId }, "GST");
  };

  /** The row whose billing phone is open for editing, and what is typed so far. */
  const [phoneDraft, setPhoneDraft] = useState<{ id: number; value: string } | null>(null);

  const saveBillingPhone = (account: Account) => {
    if (phoneDraft?.id !== account.id) return;
    // Trimmed, and blank means "use the account's own phone" — the Address
    // Book's phone rule, which the server applies too.
    const value = phoneDraft.value.trim();
    setPhoneDraft(null);
    if (value === (account.billing_phone ?? "")) return; // nothing changed
    const next = value === "" ? null : value;
    void saveRow(account, { billing_phone: next }, { billing_phone: next }, "billing phone");
  };

  /* --- columns ----------------------------------------------------------- */

  const columns = useMemo<Column<Account>[]>(() => {
    const sortable = (label: string, field: AccountSortField) => (
      <SortHeader
        label={label}
        field={field}
        sort={q.sort}
        descending={q.descending}
        onSort={q.toggleSort}
      />
    );

    return [
      {
        key: "select",
        width: "44px",
        header: (
          <>
            <label htmlFor="accounts-select-page" className="sr-only">
              Select the accounts on this page
            </label>
            <input
              id="accounts-select-page"
              type="checkbox"
              checked={allMatching || allPageTicked}
              ref={(el) => {
                if (el) el.indeterminate = !allMatching && somePageTicked && !allPageTicked;
              }}
              disabled={q.items.length === 0 || anyBulkBusy}
              onChange={toggleHeader}
              className="size-4 accent-accent"
            />
          </>
        ),
        render: (account) => (
          <>
            <label htmlFor={`accounts-row-${account.id}`} className="sr-only">
              Select {account.email}
            </label>
            <input
              id={`accounts-row-${account.id}`}
              type="checkbox"
              checked={allMatching || tickedSet.has(account.id)}
              disabled={anyBulkBusy}
              onChange={() => toggleRow(account.id)}
              className="size-4 accent-accent"
            />
          </>
        ),
      },
      {
        key: "id",
        width: "72px",
        header: sortable("ID", "id"),
        render: (account) => <span className="tabular-nums text-muted">{account.id}</span>,
      },
      {
        key: "email",
        width: "26%",
        header: sortable("Email", "email"),
        render: (account) => (
          <span className="block max-w-[260px] truncate font-medium" title={account.email}>
            {account.email}
          </span>
        ),
      },
      {
        key: "phone",
        width: "130px",
        header: sortable("Phone", "phone"),
        render: (account) =>
          account.phone ? (
            <span className="tabular-nums">{account.phone}</span>
          ) : (
            <span className="text-muted">{DASH}</span>
          ),
      },
      {
        key: "status",
        width: "170px",
        header: sortable("Status", "status"),
        render: (account) => <StatusBadge status={account.status} />,
      },
      {
        key: "subscription_type",
        width: "180px",
        header: sortable("Subscription", "subscription_type"),
        render: (account) =>
          account.subscription_type ? (
            <span className="block max-w-[170px] truncate" title={account.subscription_type}>
              {account.subscription_type}
            </span>
          ) : (
            <span className="text-muted">{DASH}</span>
          ),
      },
      {
        key: "tag",
        width: "190px",
        header: "Tag",
        render: (account) => {
          const busy = rowBusy.includes(account.id);
          const selectId = `accounts-tag-${account.id}`;
          return (
            <span className="flex items-center gap-2">
              <label htmlFor={selectId} className="sr-only">
                Tag for {account.email}
              </label>
              <Select
                id={selectId}
                value={account.tag === null ? "" : String(account.tag.id)}
                disabled={busy || options === null}
                className="h-8 text-[13px]"
                onChange={(event) => {
                  const value = event.target.value;
                  void changeRowTag(account, value === "" ? null : Number(value));
                }}
              >
                <option value="">Untagged</option>
                {(options?.tags ?? []).map((tag) => (
                  <option key={tag.id} value={String(tag.id)}>
                    {tag.name}
                  </option>
                ))}
                {/* A tag that the options list has not caught up with yet must
                    still be selectable, or the control would reset the row. */}
                {account.tag && !tagById.has(account.tag.id) ? (
                  <option value={String(account.tag.id)}>{account.tag.name}</option>
                ) : null}
              </Select>
              {busy ? <Spinner size={13} /> : null}
            </span>
          );
        },
      },
      {
        // The account's own GST wins over the run's: "Run's GST" means none of its own.
        key: "gst",
        width: "190px",
        header: "GST",
        render: (account) => {
          const busy = rowBusy.includes(account.id);
          const selectId = `accounts-gst-${account.id}`;
          const own = account.gst ?? null;
          if (!isAdmin) {
            return own ? (
              <span className="block max-w-[170px] truncate" title={`${own.gstin} · ${own.business_name}`}>
                {own.label}
              </span>
            ) : (
              <span className="text-muted" title="Bills under the GST the run picks">
                Run's GST
              </span>
            );
          }
          return (
            <span className="flex items-center gap-2">
              <label htmlFor={selectId} className="sr-only">
                GST for {account.email}
              </label>
              <Select
                id={selectId}
                value={own === null ? "" : String(own.id)}
                disabled={busy || gsts === null}
                className="h-8 text-[13px]"
                title={own ? `${own.gstin} · ${own.business_name}` : "Bills under the GST the run picks"}
                onChange={(event) => {
                  const value = event.target.value;
                  changeRowGst(account, value === "" ? null : Number(value));
                }}
              >
                <option value="">Run's GST</option>
                {(gsts ?? []).map((gst) => (
                  <option key={gst.id} value={String(gst.id)}>
                    {gst.label}
                  </option>
                ))}
                {/* Shown before the list arrives, so the row still says what it has. */}
                {own && !gstById.has(own.id) ? <option value={String(own.id)}>{own.label}</option> : null}
              </Select>
              {busy ? <Spinner size={13} /> : null}
            </span>
          );
        },
      },
      {
        // The phone its orders are placed with; a dash means its own phone.
        key: "billing_phone",
        width: "230px",
        header: "Billing phone",
        render: (account) => {
          const busy = rowBusy.includes(account.id);
          const inputId = `accounts-billing-phone-${account.id}`;
          if (phoneDraft?.id === account.id) {
            return (
              <form
                className="flex items-center gap-1.5"
                onSubmit={(event) => {
                  event.preventDefault();
                  saveBillingPhone(account);
                }}
              >
                <label htmlFor={inputId} className="sr-only">
                  Billing phone for {account.email}
                </label>
                <TextInput
                  id={inputId}
                  type="tel"
                  inputMode="tel"
                  autoComplete="off"
                  autoFocus
                  maxLength={BILLING_PHONE_MAX_LENGTH}
                  placeholder="Own phone"
                  value={phoneDraft.value}
                  className="h-8 w-[118px] text-[13px]"
                  onChange={(event) => setPhoneDraft({ id: account.id, value: event.target.value })}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") setPhoneDraft(null);
                  }}
                />
                <Button type="submit" size="sm" variant="primary">
                  Save
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setPhoneDraft(null)}>
                  Cancel
                </Button>
              </form>
            );
          }
          return (
            <span className="flex items-center gap-2">
              {account.billing_phone ? (
                <span className="tabular-nums">{account.billing_phone}</span>
              ) : (
                <span className="text-muted" title="Orders use the account's own phone">
                  {DASH}
                </span>
              )}
              {isAdmin ? (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  aria-label={`Edit the billing phone for ${account.email}`}
                  onClick={() => setPhoneDraft({ id: account.id, value: account.billing_phone ?? "" })}
                >
                  Edit
                </Button>
              ) : null}
              {busy ? <Spinner size={13} /> : null}
            </span>
          );
        },
      },
      {
        key: "is_parent",
        header: "Is parent",
        render: (account) => (account.is_parent ? "Yes" : <span className="text-muted">—</span>),
      },
      {
        key: "parent_email",
        header: "Parent emails",
        render: (account) => <span className="text-[12px]">{account.parent_email ?? "—"}</span>,
      },
      {
        key: "supercoins_balance",
        header: "Supercoins Balance",
        align: "right",
        render: (account) => (
          <span
            className="tabular-nums"
            title={account.balances_read_at ? `Read ${formatDateTime(account.balances_read_at)}` : "Not read yet"}
          >
            {account.supercoins_balance ?? "—"}
          </span>
        ),
      },
      {
        key: "gift_voucher_balance",
        header: "Gift Voucher Balance",
        align: "right",
        render: (account) => (
          <span className="tabular-nums">
            {account.gift_voucher_balance == null ? "—" : `₹${account.gift_voucher_balance.toLocaleString("en-IN")}`}
          </span>
        ),
      },
      {
        key: "created_at",
        width: "150px",
        header: sortable("Created", "created_at"),
        render: (account) => (
          <span className="whitespace-nowrap text-ink-2">{formatDateTime(account.created_at)}</span>
        ),
      },
      {
        key: "updated_at",
        width: "150px",
        header: sortable("Updated", "updated_at"),
        render: (account) => (
          <span className="whitespace-nowrap text-ink-2">{formatDateTime(account.updated_at)}</span>
        ),
      },
    ];
    // Re-derived whenever anything a header or cell reads changes. The handlers
    // close over exactly this state, so they are rebuilt alongside it.
  }, [
    q.sort,
    q.descending,
    q.toggleSort,
    q.items,
    allMatching,
    allPageTicked,
    somePageTicked,
    tickedSet,
    rowBusy,
    anyBulkBusy,
    options,
    tagById,
    gsts,
    gstById,
    phoneDraft,
    isAdmin,
  ]);

  /* --- render ------------------------------------------------------------ */

  const tableBody = (() => {
    if (q.initial && q.loading) {
      return (
        <div role="status" className="flex items-center gap-2.5 py-8 text-[13px] text-ink-2">
          <Spinner size={16} />
          <span>Loading accounts…</span>
        </div>
      );
    }
    if (q.error !== null && q.items.length === 0) {
      return (
        <div className="flex flex-col items-start gap-3">
          <Alert tone="error" title="Could not load the accounts" className="w-full">
            {q.error}
          </Alert>
          <Button variant="primary" onClick={q.reload} loading={q.loading}>
            {q.loading ? "Retrying…" : "Retry"}
          </Button>
        </div>
      );
    }
    if (q.total === 0 && !q.narrowed) {
      return (
        <EmptyState
          title="No accounts yet"
          description="Import an .xlsx of emails with the card above, or add a single account by hand."
          action={
            <Button variant="primary" onClick={focusImportControl}>
              Import a workbook
            </Button>
          }
        />
      );
    }
    if (q.total === 0) {
      return (
        <EmptyState
          title="Nothing matches those filters"
          description="No account matches the search text, status, tag and subscription you picked."
          action={<Button onClick={q.clearFilters}>Clear filters</Button>}
        />
      );
    }
    return (
      <Table
        columns={columns}
        rows={q.items}
        rowKey={(account) => account.id}
        caption="Accounts"
        minWidth="1560px"
      />
    );
  })();

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold leading-tight tracking-tight text-ink">
            Accounts
          </h1>
          <p className="mt-1 max-w-prose text-[13px] leading-relaxed text-ink-2">
            The accounts every run works through. Searching, filtering and sorting all happen on the
            server, so the table stays fast however many there are.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            onClick={() => void readBalances()}
            loading={readingBalances}
            disabled={q.total === 0}
            title="Read the SuperCoins and gift card balance of every account the filters show (read-only on Flipkart)"
          >
            Read balances
          </Button>
          <Button onClick={() => void runExport()} loading={exporting} disabled={q.total === 0}>
            {exporting ? "Preparing…" : "Export .xlsx"}
          </Button>
          <Button onClick={refreshAll} loading={q.loading && !q.initial}>
            {q.loading && !q.initial ? "Refreshing…" : "Refresh"}
          </Button>
        </div>
        {balancesNote ? <p className="w-full text-[13px] text-ink-2">{balancesNote}</p> : null}
      </header>

      <div aria-live="polite" className="flex flex-col gap-2 empty:hidden">
        {exportError ? (
          <Alert tone="error" title="Export failed">
            {exportError}
          </Alert>
        ) : null}
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card
          title="Import from Excel"
          description="One row per buyer account. The column headed “Email” is read, and a “Phone” or “Mobile” column when there is one; the columns can be in any order. An email already in the list is updated, never duplicated."
        >
          <div className="flex flex-col gap-4">
            <Field
              id={IMPORT_FILE_ID}
              label="Workbook (.xlsx)"
              help={
                <>
                  Card columns in the sheet are ignored and never stored.
                  {file ? (
                    <>
                      {" Picked: "}
                      <span className="font-medium text-ink">{file.name}</span>
                      {` · ${formatBytes(file.size)}`}
                    </>
                  ) : null}
                </>
              }
              error={importError ?? undefined}
            >
              <FileInput
                key={fileKey}
                accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                onChange={pickFile}
                disabled={importing}
              />
            </Field>

            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="primary"
                onClick={() => void runImport()}
                loading={importing}
                disabled={!file}
              >
                {importing ? "Importing…" : "Import accounts"}
              </Button>
              <p className="text-[12px] text-muted">Card numbers are never read into the system.</p>
            </div>

            <div aria-live="polite">
              {importResult ? (
                <Alert tone={importResult.rejected?.length ? "warning" : "success"} title="Import finished">
                  {formatCount(importResult.created)} created ·{" "}
                  {formatCount(importResult.updated)} updated ·{" "}
                  {formatCount(importResult.skipped)} already in the list
                  {importResult.rejected?.length ? <> · {formatCount(importResult.rejected.length)} refused</> : null}.
                  <ImportRefusals rows={importResult.rejected ?? []} />
                </Alert>
              ) : null}
            </div>
          </div>
        </Card>

        <Card title="Add one manually" description="For a single account that is not in a sheet yet.">
          <form onSubmit={(event) => void submitAdd(event)} className="flex flex-col gap-4" noValidate>
            <Field
              label="Email"
              required
              error={emailError ?? undefined}
              help="The Flipkart account's login email."
            >
              <TextInput
                type="email"
                inputMode="email"
                autoComplete="off"
                spellCheck={false}
                placeholder="name@example.com"
                value={email}
                onChange={(event) => {
                  setEmail(event.target.value);
                  if (emailError) setEmailError(null);
                }}
                disabled={adding}
              />
            </Field>

            <Field label="Phone" help="Optional. Only used where Flipkart asks for it.">
              <TextInput
                type="tel"
                inputMode="tel"
                autoComplete="off"
                placeholder="9876543210"
                value={phone}
                onChange={(event) => setPhone(event.target.value)}
                disabled={adding}
              />
            </Field>

            <div className="flex flex-wrap items-center gap-3">
              <Button type="submit" variant="primary" loading={adding}>
                {adding ? "Adding…" : "Add account"}
              </Button>
            </div>

            <div aria-live="polite" className="flex flex-col gap-2 empty:hidden">
              {added ? (
                <Alert tone="success">
                  <span className="font-medium">{added}</span> was added.
                </Alert>
              ) : null}
              {addError ? (
                <Alert tone="error" title="Could not add that account">
                  {addError}
                </Alert>
              ) : null}
            </div>
          </form>
        </Card>
      </div>

      <AccountsTagManager onChanged={refreshAll} />

      <Card
        title="Accounts"
        description={`Filter down to the group you care about, then tag it${isAdmin ? " or give it a GST" : ""} in one go — “all matching” sends the filter, not thousands of ids. An account's own GST wins over the GST a run picks, and its billing phone is the phone its orders are placed with.`}
        actions={
          isAdmin ? undefined : (
            <span className="text-[12px] text-ink-2">Only an admin can change an account's GST and billing phone.</span>
          )
        }
      >
        <div className="flex flex-col gap-4">
          <AccountsFilterBar
            idPrefix="accounts"
            query={q.query}
            onQChange={q.setQ}
            onToggleStatus={q.toggleStatus}
            onTagChange={q.setTag}
            onSubscriptionChange={q.setSubscription}
            onDomainChange={q.setDomain}
            options={options}
            optionsLoading={optionsState.loading}
            optionsError={optionsState.error}
            onRetryOptions={optionsState.reload}
            narrowed={q.narrowed}
            onClear={q.clearFilters}
            disabled={anyBulkBusy}
          />

          {/* --- bulk bar --- */}
          <div className="flex flex-col gap-3 rounded-control border border-line bg-page px-3.5 py-3">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <p role="status" aria-live="polite" className="text-[13px] text-ink">
                {selectionActive ? (
                  <>
                    <span className="font-medium">
                      {allMatching
                        ? `All ${formatCount(q.total)} matching accounts selected`
                        : `${countLabel(ticked.length, "account")} on this page selected`}
                    </span>
                    {allMatching ? (
                      <span className="text-ink-2"> — sent as a filter, not as a list of ids.</span>
                    ) : null}
                  </>
                ) : (
                  <span className="text-ink-2">
                    Nothing selected. Tick rows, or select everything the filter matches.
                  </span>
                )}
              </p>

              {canOfferAllMatching ? (
                <Button
                  size="sm"
                  disabled={anyBulkBusy || q.total === 0}
                  onClick={() => {
                    setAllMatching(true);
                    setTicked([]);
                    setBulkNote(null);
                    setBulkError(null);
                    setGstNote(null);
                    setGstError(null);
                  }}
                >
                  Select all {formatCount(q.total)} matching
                </Button>
              ) : null}

              {selectionActive ? (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={anyBulkBusy}
                  onClick={() => {
                    setAllMatching(false);
                    setTicked([]);
                    setConfirmBulk(false);
                    setConfirmGst(false);
                  }}
                >
                  Clear selection
                </Button>
              ) : null}
            </div>

            {selectionActive ? (
              <div className="flex flex-wrap items-end gap-3">
                <div className="flex flex-col gap-1.5">
                  <label htmlFor="accounts-bulk-tag" className="text-[12px] font-medium text-ink">
                    Apply tag to {formatCount(affected)}{" "}
                    {affected === 1 ? "account" : "accounts"}
                  </label>
                  <Select
                    id="accounts-bulk-tag"
                    value={bulkChoice}
                    disabled={anyBulkBusy || options === null}
                    className="h-8 w-[220px] text-[13px]"
                    onChange={(event) => {
                      setBulkChoice(event.target.value);
                      setConfirmBulk(false);
                      setBulkError(null);
                    }}
                  >
                    <option value={NO_CHOICE}>Choose a tag…</option>
                    <option value={CLEAR_TAG}>Remove the tag (make untagged)</option>
                    {(options?.tags ?? []).map((tag) => (
                      <option key={tag.id} value={String(tag.id)}>
                        {tag.name}
                      </option>
                    ))}
                  </Select>
                </div>
                <Button
                  variant="primary"
                  size="sm"
                  loading={bulkBusy}
                  disabled={bulkChoice === NO_CHOICE || gstBusy}
                  onClick={() => {
                    if (bulkNeedsConfirm) setConfirmBulk(true);
                    else void applyBulk();
                  }}
                >
                  {bulkBusy ? "Applying…" : "Apply"}
                </Button>
              </div>
            ) : null}

            {selectionActive && isAdmin ? (
              <div className="flex flex-wrap items-end gap-3">
                <div className="flex flex-col gap-1.5">
                  <label htmlFor="accounts-bulk-gst" className="text-[12px] font-medium text-ink">
                    Give {formatCount(affected)} {affected === 1 ? "account" : "accounts"} a GST of{" "}
                    {affected === 1 ? "its" : "their"} own
                  </label>
                  <Select
                    id="accounts-bulk-gst"
                    value={gstChoice}
                    disabled={anyBulkBusy || gsts === null}
                    className="h-8 w-[220px] text-[13px]"
                    onChange={(event) => {
                      setGstChoice(event.target.value);
                      setConfirmGst(false);
                      setGstError(null);
                    }}
                  >
                    <option value={NO_CHOICE}>Choose a GST…</option>
                    <option value={CLEAR_GST}>None of their own (use the run's GST)</option>
                    {(gsts ?? []).map((gst) => (
                      <option key={gst.id} value={String(gst.id)}>
                        {gst.label} · {gst.gstin}
                      </option>
                    ))}
                  </Select>
                </div>
                <Button
                  variant="primary"
                  size="sm"
                  loading={gstBusy}
                  disabled={gstChoice === NO_CHOICE || bulkBusy}
                  onClick={() => {
                    if (bulkNeedsConfirm) setConfirmGst(true);
                    else void applyGst();
                  }}
                >
                  {gstBusy ? "Applying…" : "Apply GST"}
                </Button>
              </div>
            ) : null}

            {confirmBulk ? (
              <Alert
                tone="warning"
                title={`Apply to all ${formatCount(q.total)} matching accounts?`}
              >
                <span className="block">
                  That is well past the {countLabel(q.items.length, "row")} on screen.{" "}
                  {bulkChoice === CLEAR_TAG
                    ? "Every one of them becomes untagged."
                    : `Every one of them is tagged “${bulkTagName ?? "—"}”.`}
                </span>
                <span className="mt-2 flex flex-wrap items-center gap-2">
                  <Button size="sm" variant="danger" loading={bulkBusy} onClick={() => void applyBulk()}>
                    Yes, apply to {formatCount(q.total)}
                  </Button>
                  <Button size="sm" variant="ghost" disabled={bulkBusy} onClick={() => setConfirmBulk(false)}>
                    Cancel
                  </Button>
                </span>
              </Alert>
            ) : null}

            {confirmGst ? (
              <Alert
                tone="warning"
                title={`Give all ${formatCount(q.total)} matching accounts this GST?`}
              >
                <span className="block">
                  That is well past the {countLabel(q.items.length, "row")} on screen.{" "}
                  {gstChoice === CLEAR_GST
                    ? "Every one of them goes back to the GST each run picks."
                    : `Every one of them bills under “${bulkGstLabel ?? "—"}”, whatever GST a run picks.`}
                </span>
                <span className="mt-2 flex flex-wrap items-center gap-2">
                  <Button size="sm" variant="danger" loading={gstBusy} onClick={() => void applyGst()}>
                    Yes, apply to {formatCount(q.total)}
                  </Button>
                  <Button size="sm" variant="ghost" disabled={gstBusy} onClick={() => setConfirmGst(false)}>
                    Cancel
                  </Button>
                </span>
              </Alert>
            ) : null}

            <div aria-live="polite" className="flex flex-col gap-2 empty:hidden">
              {bulkNote ? <Alert tone="success">{bulkNote}</Alert> : null}
              {bulkError ? (
                <Alert tone="error" title="Could not apply the tag">
                  {bulkError}
                </Alert>
              ) : null}
              {gstNote ? <Alert tone="success">{gstNote}</Alert> : null}
              {gstError ? (
                <Alert tone="error" title="Could not apply the GST">
                  {gstError}
                </Alert>
              ) : null}
            </div>
          </div>

          <div aria-live="polite" className="flex flex-col gap-2 empty:hidden">
            {rowError ? (
              <Alert tone="error" title="Change not saved">
                {rowError}
              </Alert>
            ) : null}
            {gstsError && isAdmin ? (
              <p role="status" className="flex flex-wrap items-center gap-2 text-[12px] text-ink-2">
                <span>Could not load the Address Book's GSTs, so an account's GST cannot be changed: {gstsError}</span>
                <Button size="sm" variant="ghost" onClick={reloadGsts}>
                  Try again
                </Button>
              </p>
            ) : null}
            {rowStale ? (
              <p role="status" className="flex flex-wrap items-center gap-2 text-[12px] text-ink-2">
                <span>
                  A row's tag changed while a tag filter is on, so this page may now show an account
                  that no longer matches.
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setRowStale(false);
                    refreshAll();
                  }}
                >
                  Refresh the list
                </Button>
              </p>
            ) : null}
            {q.error !== null && q.items.length > 0 ? (
              <p role="status" className="text-[12px] text-ink-2">
                Showing the page that last loaded — the refresh failed: {q.error}
              </p>
            ) : null}
          </div>

          {tableBody}

          <AccountsPager
            idPrefix="accounts"
            page={q.page}
            size={q.size}
            total={q.total}
            pages={q.pages}
            shown={q.items.length}
            onPageChange={q.setPage}
            onSizeChange={q.setSize}
            loading={q.loading}
            disabled={anyBulkBusy}
          />
        </div>
      </Card>
    </div>
  );
}
