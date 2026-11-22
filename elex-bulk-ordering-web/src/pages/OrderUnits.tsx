/**
 * Order Units History: the ledger of every order this tool attempted, with the
 * filters, column choice, saved view and exports the spec describes. Reading
 * orders back from Flipkart lives here too, folded away until wanted, because
 * "what happened to the orders?" belongs with the orders, not with the form
 * that spends money.
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router-dom";
import {
  clearOrderView,
  downloadOrders,
  downloadOrdersCsv,
  errorMessage,
  getOrderView,
  listOrders,
  markReceived,
  orderFilterOptions,
  saveOrderView,
} from "../lib/api";
import type { OrderSheet } from "../lib/api";
import type { DeliveryState, Order, OrderFilterOptions, OrderFilters, OrderOutcomeFilter } from "../lib/types";
import { formatCount } from "../lib/format";
import { DELIVERY_STATES, OUTCOMES, restoredFilters, viewFilters } from "../lib/orderView";
import { AccountsPager } from "../components/AccountsPager";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { CompileInvoicesControls } from "../components/CompileInvoices";
import { ExtractInvoicesDataControls } from "../components/ExtractInvoicesData";
import { Checkbox } from "../components/Field";
import { OrderDetail } from "../components/OrderDetail";
import { DEFAULT_ORDER_COLUMNS, ORDER_COLUMN_OPTIONS, OrderTable } from "../components/OrderTable";
import { OrderUnitsFilters, isNarrowed } from "../components/OrderUnitsFilters";
import { CsvDownloads } from "../components/CsvDownloads";
import { RunPageHeader } from "../components/RunPageHeader";
import { useRunAccounts } from "../components/RunAccounts";
import { useRecentJobs } from "../components/RunHistory";
import { SyncOrders } from "../components/SyncOrders";
import { Toaster, useToasts } from "../components/Toast";
import { UnitRequestDialog, type UnitRequestDraft } from "../components/UnitRequestDialog";
import { UploadInvoices } from "../components/UploadInvoices";
import { UploadOrderIds } from "../components/UploadOrderIds";
import { UploadOrderReport } from "../components/UploadOrderReport";
import { useDismiss } from "../components/useDismiss";

const PAGE_SIZE = 100;
const EMPTY: OrderFilters = { date_field: "placed_at" };
/** The deep link's run id: a positive integer the server will accept. */
const JOB_ID = /^[1-9]\d{0,8}$/;
const KNOWN_COLUMNS = new Set(ORDER_COLUMN_OPTIONS.map((column) => column.id));

function jobFromParams(params: URLSearchParams): number | null {
  const raw = params.get("job_id");
  return raw !== null && JOB_ID.test(raw) ? Number(raw) : null;
}

/** `?row=`: one order by the ledger's own row id, as an alert links to it. */
export function rowFromParams(params: URLSearchParams): number | null {
  const raw = params.get("row");
  return raw !== null && JOB_ID.test(raw) ? Number(raw) : null;
}

/** The order deep link's id — what the dashboard's changes list links each
 *  change with: letters, digits and dashes, no longer than the column. */
const ORDER_ID = /^[A-Za-z0-9-]{1,64}$/;

/** `?order_id=` as the order-id filter holds it: trimmed and upper-cased, as stored. */
export function orderFromParams(params: URLSearchParams): string | null {
  const raw = params.get("order_id")?.trim() ?? "";
  return ORDER_ID.test(raw) ? raw.toUpperCase() : null;
}

/** `?status=` as the Outcome select holds it: one of the server's outcomes.
 *  The dashboard's "To be delivered" link opens the page on `placed`. */
export function outcomeFromParams(params: URLSearchParams): OrderOutcomeFilter | null {
  const raw = params.get("status")?.trim() ?? "";
  return OUTCOMES.includes(raw) ? (raw as OrderOutcomeFilter) : null;
}

/** `?delivery_state=`, repeated: each state the server knows, once, in its
 *  order — the same link's `in_transit` and `unknown`. null for none. */
export function deliveryFromParams(params: URLSearchParams): DeliveryState[] | null {
  const asked = new Set(params.getAll("delivery_state").map((value) => value.trim()));
  const states = DELIVERY_STATES.filter((state) => asked.has(state)) as DeliveryState[];
  return states.length > 0 ? states : null;
}

/** Whether two lists of delivery states hold the same states, in any order. */
function sameStates(a: readonly string[] | null | undefined, b: readonly string[]): boolean {
  const left = new Set(a ?? []);
  const right = new Set(b);
  return left.size === right.size && [...left].every((state) => right.has(state));
}

/** The filter a deep link opens the page on: one run's rows, or one order's —
 *  or the dashboard's "To be delivered" units, by outcome and delivery state. */
function linkedFilters(
  jobId: number | null,
  orderId: string | null,
  outcome: OrderOutcomeFilter | null,
  delivery: DeliveryState[] | null,
  row: number | null = null,
): OrderFilters {
  const filters: OrderFilters = orderId ? { ...EMPTY, job_id: jobId, order_ids: [orderId] } : { ...EMPTY, job_id: jobId };
  if (row !== null) filters.order_row = row;
  if (outcome) filters.status = outcome;
  if (delivery) filters.delivery_states = delivery;
  return filters;
}

/** The read-back panel; mounted only when opened, so its account fetch is too. */
function SyncPanel({ onSynced, blocked }: { onSynced: () => void; blocked: boolean }) {
  const accounts = useRunAccounts();
  return <SyncOrders accounts={accounts} onSynced={onSynced} blocked={blocked} />;
}

/**
 * A read-back competes with an ordering run for the same browser concurrency,
 * so every control on this page that starts one waits while a run is queued
 * or running — the same rule for both panels.
 */
function useOrderingRunActive(enabled: boolean): boolean {
  const ordering = useRecentJobs("order", 3);
  return enabled && ordering.jobs.some((job) => job.status === "queued" || job.status === "running");
}

/** What the CSV button can download: the spec's full ledger, or one of the owner's own sheets. */
type ExportKind = "csv" | "xlsx" | OrderSheet;
const EXPORT_LABEL: Record<Exclude<ExportKind, "xlsx">, string> = {
  csv: "Full ledger CSV",
  out_for_delivery: "Out for delivery sheet",
  order_data: "Order data sheet",
};
const CSV_CHOICES: { kind: Exclude<ExportKind, "xlsx">; help: string }[] = [
  { kind: "csv", help: "Every column the spec lists, plus this tool's own." },
  { kind: "out_for_delivery", help: "Order date, status, product, order ID, delivery OTP, tracking ID, phone's last 6." },
  { kind: "order_data", help: "The same, with the estimated delivery date — every order after placing." },
];

/**
 * "Download CSV" with a choice of layout; the rows are always the current
 * filter's. It is a real menu button, so it keeps the keyboard contract that
 * role="menu" promises: opening puts focus on the first choice (the last for
 * ArrowUp), the arrows cycle, Home and End jump, Escape or a pick closes and
 * hands focus back to the button, Tab closes and carries on from the button,
 * and a press outside closes. Without the hand-back, closing left keyboard
 * focus on <body> and the next Tab started from the top of the page.
 */
export function DownloadMenu({
  busy,
  disabled,
  onPick,
}: {
  busy: Exclude<ExportKind, "xlsx"> | null;
  disabled: boolean;
  onPick: (kind: Exclude<ExportKind, "xlsx">) => void;
}) {
  const [open, setOpen] = useState(false);
  const menu = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);
  /** Which choice takes focus when the menu opens; ArrowUp on the button asks for the last. */
  const openAt = useRef<"first" | "last">("first");
  const triggerId = useId();
  const menuId = useId();
  useDismiss(menu, open, () => setOpen(false));

  const choices = () => Array.from(list.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
  // Button does not take a ref; the id is there for the menu's name anyway.
  const trigger = () => document.getElementById(triggerId);

  useEffect(() => {
    if (!open) return;
    const items = Array.from(list.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
    (openAt.current === "last" ? items[items.length - 1] : items[0])?.focus();
    openAt.current = "first";
  }, [open]);

  // A pick hands focus back to the button and in the same breath disables it
  // for the download, and a disabled control cannot hold focus: browsers drop
  // it to <body>. When the download ends and the button is a button again,
  // put focus back where the keyboard user left it — unless they moved on.
  const wasBusy = useRef(busy);
  useEffect(() => {
    const ended = wasBusy.current !== null && busy === null;
    wasBusy.current = busy;
    if (!ended) return;
    const active = document.activeElement;
    if (active === null || active === document.body) trigger()?.focus();
  }, [busy]); // eslint-disable-line react-hooks/exhaustive-deps -- trigger reads a stable id

  const show = (at: "first" | "last") => {
    openAt.current = at;
    setOpen(true);
  };
  /** Close and put focus back on the button, where it left the page's tab order. */
  const close = () => {
    setOpen(false);
    trigger()?.focus();
  };
  /** Focus the choice at `index`, counting round at either end. */
  const focusChoice = (index: number) => {
    const items = choices();
    if (items.length > 0) items[((index % items.length) + items.length) % items.length].focus();
  };
  const onMenuKey = (event: KeyboardEvent<HTMLDivElement>) => {
    const at = choices().indexOf(document.activeElement as HTMLElement);
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        focusChoice(at + 1);
        break;
      case "ArrowUp":
        event.preventDefault();
        focusChoice(at < 0 ? -1 : at - 1);
        break;
      case "Home":
        event.preventDefault();
        focusChoice(0);
        break;
      case "End":
        event.preventDefault();
        focusChoice(-1);
        break;
      case "Escape":
        event.preventDefault();
        close();
        break;
      case "Tab":
        // Not prevented: focus is on the button again by the time the browser
        // moves it, so Tab and Shift+Tab carry on from there, not from <body>.
        close();
        break;
      default:
        break;
    }
  };
  const onTriggerKey = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      show(event.key === "ArrowUp" ? "last" : "first");
    }
  };

  return (
    <div ref={menu} className="relative">
      <Button
        id={triggerId}
        size="sm"
        variant="primary"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        loading={busy !== null}
        disabled={disabled}
        onClick={() => (open ? close() : show("first"))}
        onKeyDown={onTriggerKey}
      >
        {busy ? `Downloading ${EXPORT_LABEL[busy].toLowerCase()}…` : "Download CSV"}
      </Button>
      {open ? (
        <div
          ref={list}
          id={menuId}
          role="menu"
          aria-labelledby={triggerId}
          onKeyDown={onMenuKey}
          className="absolute right-0 top-[38px] z-20 w-[300px] rounded-card border border-line bg-surface p-1 shadow-lg"
        >
          {CSV_CHOICES.map((choice) => (
            <button
              key={choice.kind}
              type="button"
              role="menuitem"
              tabIndex={-1}
              className="block w-full rounded-control px-3 py-2 text-left hover:bg-page focus:bg-page"
              onClick={() => {
                close();
                onPick(choice.kind);
              }}
            >
              <span className="block text-[13px] font-medium text-ink">{EXPORT_LABEL[choice.kind]}</span>
              <span className="block text-[12px] text-ink-2">{choice.help}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ColumnsMenu({
  visible,
  onChange,
}: {
  visible: string[];
  onChange: (next: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const menu = useRef<HTMLDivElement>(null);
  useDismiss(menu, open, () => setOpen(false));
  const chosen = new Set(visible);
  return (
    <div ref={menu} className="relative">
      <Button size="sm" aria-haspopup="true" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        Columns
      </Button>
      {open ? (
        <div className="absolute right-0 top-[38px] z-20 w-[260px] rounded-card border border-line bg-surface p-3 shadow-lg">
          <div className="max-h-[320px] overflow-y-auto">
            {ORDER_COLUMN_OPTIONS.filter((c) => c.id !== "actions").map((column) => (
              <Checkbox
                key={column.id}
                label={column.label}
                checked={chosen.has(column.id)}
                onChange={() => {
                  const next = new Set(chosen);
                  if (next.has(column.id)) next.delete(column.id);
                  else next.add(column.id);
                  onChange(ORDER_COLUMN_OPTIONS.map((c) => c.id).filter((id) => next.has(id)));
                }}
              />
            ))}
          </div>
          <div className="mt-2 flex justify-end">
            <Button size="sm" onClick={() => setOpen(false)}>
              Done
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default function OrderUnits() {
  const [view, setView] = useState<"units" | "csv">("units");
  const [searchParams, setSearchParams] = useSearchParams();
  const jobParam = jobFromParams(searchParams);
  const orderParam = orderFromParams(searchParams);
  const outcomeParam = outcomeFromParams(searchParams);
  const deliveryParam = deliveryFromParams(searchParams);
  const rowParam = rowFromParams(searchParams);
  /** The delivery states as one value, for the effects to compare. */
  const deliveryKey = deliveryParam?.join(" ") ?? null;

  const [draft, setDraft] = useState<OrderFilters>(() =>
    linkedFilters(jobParam, orderParam, outcomeParam, deliveryParam, rowParam),
  );
  const [applied, setApplied] = useState<OrderFilters>(() =>
    linkedFilters(jobParam, orderParam, outcomeParam, deliveryParam, rowParam),
  );
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<Order[]>([]);
  const [total, setTotal] = useState(0);
  const [pages, setPages] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [options, setOptions] = useState<OrderFilterOptions | null>(null);
  const [columns, setColumns] = useState<string[]>(DEFAULT_ORDER_COLUMNS);
  const [viewSavedAt, setViewSavedAt] = useState<string | null>(null);
  /** True while what is applied IS the saved view — restored, or just saved. */
  const [showingSavedView, setShowingSavedView] = useState(false);
  const [viewBusy, setViewBusy] = useState(false);
  const [detail, setDetail] = useState<Order | null>(null);
  /** Unit row ids ticked for a cancel / reschedule request (spec FR-020). */
  const [selected, setSelected] = useState<ReadonlySet<number>>(() => new Set());
  const [requestDraft, setRequestDraft] = useState<UnitRequestDraft | null>(null);
  const [exporting, setExporting] = useState<ExportKind | null>(null);
  const [syncOpen, setSyncOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [reportOpen, setReportOpen] = useState(false);
  const [invoicesOpen, setInvoicesOpen] = useState(false);
  const { toasts, push, dismiss } = useToasts();
  const requestNo = useRef(0);
  const jobAtMount = useRef(jobParam);
  const lastJob = useRef(jobParam);
  const orderAtMount = useRef(orderParam);
  const lastOrder = useRef(orderParam);
  /** Opened from the dashboard's "To be delivered" card (either half of its link). */
  const deliveryLinkAtMount = useRef(outcomeParam !== null || deliveryKey !== null);
  const lastOutcome = useRef(outcomeParam);
  const lastDelivery = useRef(deliveryKey);
  /** Only the newest filter-options request may land, like the rows'. */
  const optionsNo = useRef(0);
  const runActive = useOrderingRunActive(uploadOpen || syncOpen);

  useEffect(() => {
    document.title = "Order Units History — Ordering Suite";
  }, []);

  const load = useCallback(async (filters: OrderFilters, pageNumber: number) => {
    const current = ++requestNo.current;
    setLoading(true);
    try {
      const result = await listOrders({ ...filters, page: pageNumber, size: PAGE_SIZE });
      if (current !== requestNo.current) return;
      setItems(result.items);
      setTotal(result.total);
      setPages(result.pages);
      setError(null);
      // The total can shrink under us (a settle, a narrower filter): never
      // stay on a page past the last one, showing nothing with no way back.
      if (pageNumber > result.pages) setPage(Math.max(1, result.pages));
    } catch (err) {
      if (current !== requestNo.current) return;
      setError(errorMessage(err));
    } finally {
      if (current === requestNo.current) setLoading(false);
    }
  }, []);

  /** The status picker's choices with their counts. Fetched again on every
   *  reload: a read-back or an upload can bring a wording the page has not
   *  seen yet, and it must be pickable without reopening the page (spec
   *  FR-009). A failed fetch keeps the choices already shown. */
  const loadOptions = useCallback(() => {
    const current = ++optionsNo.current;
    void orderFilterOptions()
      .then((next) => {
        if (current === optionsNo.current) setOptions(next);
      })
      .catch(() => undefined);
  }, []);

  // Filter options and the saved view, once. A saved view replaces the
  // starting filters unless the page was opened for one run or one order, or
  // from the dashboard's "To be delivered" card.
  useEffect(() => {
    loadOptions();
    void getOrderView()
      .then((view) => {
        if (!view) return;
        const known = view.columns.filter((id) => KNOWN_COLUMNS.has(id));
        if (known.length > 0) setColumns(known);
        setViewSavedAt(view.saved_at);
        const filters = restoredFilters(view.filters);
        if (
          jobAtMount.current === null &&
          orderAtMount.current === null &&
          !deliveryLinkAtMount.current &&
          Object.keys(filters).length > 0
        ) {
          const restored = { ...EMPTY, ...filters };
          setDraft(restored);
          setApplied(restored);
          setShowingSavedView(true);
        }
      })
      .catch(() => undefined);
  }, [loadOptions]);

  // The run deep link can change without a remount: the sidebar leads to this
  // route without it, and a second run's link arrives while already here.
  useEffect(() => {
    if (lastJob.current === jobParam) return;
    lastJob.current = jobParam;
    setDraft((d) => ({ ...d, job_id: jobParam }));
    setApplied((a) => ({ ...a, job_id: jobParam }));
    setPage(1);
  }, [jobParam]);

  // The one-order deep link (an alert's "View order"), the same way.
  const lastRow = useRef<number | null>(rowParam);
  useEffect(() => {
    if (lastRow.current === rowParam) return;
    lastRow.current = rowParam;
    setDraft((d) => ({ ...d, order_row: rowParam }));
    setApplied((a) => ({ ...a, order_row: rowParam }));
    setPage(1);
  }, [rowParam]);

  // The order deep link, likewise: a change on the dashboard links to its
  // order's rows, and the sidebar leads back here without the link.
  useEffect(() => {
    if (lastOrder.current === orderParam) return;
    lastOrder.current = orderParam;
    const order_ids = orderParam ? [orderParam] : undefined;
    setDraft((d) => ({ ...d, order_ids }));
    setApplied((a) => ({ ...a, order_ids }));
    setPage(1);
  }, [orderParam]);

  // The "To be delivered" link's two halves, likewise: the outcome and the
  // delivery states follow the address, and the sidebar leads back without them.
  useEffect(() => {
    if (lastOutcome.current === outcomeParam) return;
    lastOutcome.current = outcomeParam;
    const status = outcomeParam ?? undefined;
    setDraft((d) => ({ ...d, status }));
    setApplied((a) => ({ ...a, status }));
    setPage(1);
  }, [outcomeParam]);
  useEffect(() => {
    if (lastDelivery.current === deliveryKey) return;
    lastDelivery.current = deliveryKey;
    const delivery_states = deliveryKey ? (deliveryKey.split(" ") as DeliveryState[]) : undefined;
    setDraft((d) => ({ ...d, delivery_states }));
    setApplied((a) => ({ ...a, delivery_states }));
    setPage(1);
  }, [deliveryKey]);

  /** Take the deep links the filter no longer holds off the address. The
   *  order link's effect is told first, so the address changing does not undo
   *  the filter that was just applied — and the outcome's and the delivery
   *  states' effects the same way. */
  const unlink = useCallback(
    (filters: OrderFilters) => {
      const next = new URLSearchParams(searchParams);
      if (filters.job_id == null) next.delete("job_id");
      if (filters.order_row == null && next.has("row")) {
        next.delete("row");
        lastRow.current = null;
      }
      const keepsOrder = filters.order_ids?.length === 1 && filters.order_ids[0] === orderParam;
      if (!keepsOrder && next.has("order_id")) {
        next.delete("order_id");
        lastOrder.current = null;
      }
      const keepsOutcome = outcomeParam !== null && filters.status === outcomeParam;
      if (!keepsOutcome && next.has("status")) {
        next.delete("status");
        lastOutcome.current = null;
      }
      const keepsDelivery = deliveryKey !== null && sameStates(filters.delivery_states, deliveryKey.split(" "));
      if (!keepsDelivery && next.has("delivery_state")) {
        next.delete("delivery_state");
        lastDelivery.current = null;
      }
      if (next.toString() !== searchParams.toString()) setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams, orderParam, outcomeParam, deliveryKey],
  );

  useEffect(() => {
    void load(applied, page);
  }, [applied, page, load]);

  // A selection belongs to the page it was made on: a new page or filter
  // clears it, and a reload drops ids that are no longer on screen.
  useEffect(() => {
    setSelected(new Set());
  }, [applied, page]);
  useEffect(() => {
    setSelected((current) => {
      const onPage = new Set(items.map((o) => o.id));
      const kept = [...current].filter((id) => onPage.has(id));
      return kept.length === current.size ? current : new Set(kept);
    });
  }, [items]);

  const apply = useCallback(() => {
    setPage(1);
    setApplied(draft);
    setShowingSavedView(false);
    unlink(draft);
  }, [draft, unlink]);

  const reset = useCallback(() => {
    setDraft(EMPTY);
    setApplied(EMPTY);
    setPage(1);
    setShowingSavedView(false);
    unlink(EMPTY);
  }, [unlink]);

  // Refresh, a finished read-back and the two uploads all come through here,
  // and each can bring a wording the status picker has not offered yet.
  const reload = useCallback(() => {
    void load(applied, page);
    loadOptions();
  }, [applied, page, load, loadOptions]);
  const closeDetail = useCallback(() => setDetail(null), []);
  // A reload (a read-back finished, a request recorded) brings fresh rows: an
  // open dialog follows its row rather than keeping the copy it opened with.
  useEffect(() => {
    setDetail((current) => (current ? (items.find((o) => o.id === current.id) ?? current) : current));
  }, [items]);
  const onSettled = useCallback(() => {
    setDetail(null);
    void load(applied, page);
  }, [applied, page, load]);

  // --- cancel / reschedule requests (spec FR-020) ---------------------------
  const onToggleRow = useCallback((id: number, checked: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);
  const onTogglePage = useCallback((ids: number[], checked: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      for (const id of ids) {
        if (checked) next.add(id);
        else next.delete(id);
      }
      return next;
    });
  }, []);
  /** The toolbar's Cancel / Reschedule Order Units: the ticked rows, as the dialog names them. */
  const openRequest = useCallback(
    (kind: UnitRequestDraft["kind"]) => {
      const units = items.filter((o) => selected.has(o.id));
      if (units.length > 0) setRequestDraft({ kind, units });
    },
    [items, selected],
  );
  /** The row's own Cancel: that one unit. */
  const onCancelRow = useCallback((order: Order) => setRequestDraft({ kind: "cancel", units: [order] }), []);
  /** Goods in hand, or not after all: the ticked rows marked at once (spec FR-001). */
  const [marking, setMarking] = useState(false);
  const onMarkReceived = useCallback(
    async (received: boolean) => {
      const ids = items.filter((o) => selected.has(o.id)).map((o) => o.id);
      if (ids.length === 0) return;
      setMarking(true);
      try {
        const out = await markReceived(ids, received);
        push({
          tone: "success",
          title: received ? "Marked received" : "Received mark taken back",
          description: `${formatCount(out.changed)} unit${out.changed === 1 ? "" : "s"} changed.`,
        });
        setSelected(new Set());
        void load(applied, page);
      } catch (err) {
        push({ tone: "error", title: "Could not change the received mark", description: errorMessage(err) });
      } finally {
        setMarking(false);
      }
    },
    [items, selected, push, load, applied, page],
  );
  /** Recorded: the ticks go, and the rows reload so their tags show. */
  const onRequestsRecorded = useCallback(() => {
    setSelected(new Set());
    void load(applied, page);
  }, [applied, page, load]);

  const onDownload = useCallback(
    async (kind: ExportKind) => {
      setExporting(kind);
      // Exports have no row cap (C-9), and the browser has to hold the whole
      // file before it can save it (the download carries the sign-in token,
      // so it cannot be a plain link): for a big one, say so up front rather
      // than leave a spinning button as the only sign for minutes.
      if (total > 20_000) {
        push({
          tone: "info",
          title: "Preparing a big export",
          description: `${formatCount(total)} rows — this can take a while. The file lands in your downloads when it is ready; you can keep working meanwhile.`,
        });
      }
      try {
        if (kind === "xlsx") await downloadOrders(applied);
        else if (kind === "csv") await downloadOrdersCsv(applied);
        else await downloadOrdersCsv(applied, kind);
        push({
          tone: "success",
          // The spec's own words for its CSV (FR-016); the other layouts say which they were.
          title: kind === "xlsx" ? "Workbook downloaded" : kind === "csv" ? "CSV downloaded" : `${EXPORT_LABEL[kind]} downloaded`,
          description: (
            <>
              Your export has been downloaded.
              {total > 20_000 ? (
                <span className="block">
                  {formatCount(total)} rows — a big file; give the spreadsheet a moment to open it.
                </span>
              ) : null}
            </>
          ),
        });
      } catch (err) {
        push({ tone: "error", title: "Export failed", description: errorMessage(err) });
      } finally {
        setExporting(null);
      }
    },
    [applied, push, total],
  );

  const onQuickSave = useCallback(async () => {
    setViewBusy(true);
    try {
      const saved = await saveOrderView({ columns, filters: viewFilters(applied) });
      setViewSavedAt(saved.saved_at);
      setShowingSavedView(true);
      push({ tone: "success", title: "View saved", description: "These columns and filters open by default now." });
    } catch (err) {
      push({ tone: "error", title: "Could not save the view", description: errorMessage(err) });
    } finally {
      setViewBusy(false);
    }
  }, [columns, applied, push]);

  const onDefaultView = useCallback(async () => {
    setViewBusy(true);
    try {
      await clearOrderView();
      setColumns(DEFAULT_ORDER_COLUMNS);
      setViewSavedAt(null);
      reset();
    } catch (err) {
      push({ tone: "error", title: "Could not reset the view", description: errorMessage(err) });
    } finally {
      setViewBusy(false);
    }
  }, [reset, push]);

  const filtered = useMemo(() => isNarrowed(applied), [applied]);
  const nothing = total === 0;
  // No export while a load is in flight or has failed: `total` would still be
  // the previous filter's, so the heads-up and the success toast would name a
  // row count that belongs to a different file than the one downloaded.
  const exportsBlocked = nothing || exporting !== null || loading || error !== null;

  return (
    <div className="flex flex-col gap-5">
      <div role="tablist" aria-label="Order Units views" className="grid grid-cols-2 rounded-control border border-line bg-page p-1">
        {(
          [
            ["units", "Order Units"],
            ["csv", "CSV Downloads"],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            role="tab"
            type="button"
            aria-selected={view === value}
            onClick={() => setView(value)}
            className={`rounded-[6px] px-3 py-2 text-[13px] font-medium ${
              view === value ? "bg-surface text-ink shadow-sm" : "text-ink-2 hover:text-ink"
            }`}
          >
            {label}
          </button>
        ))}
      </div>
      {view === "csv" ? <CsvDownloads /> : null}
      <div hidden={view === "csv"} className="flex flex-col gap-5">
      <RunPageHeader
        title="Order Units History"
        lead="Every order this tool attempted, with what Flipkart later said about it. Filter, pick your columns, export the rows you can see."
        actions={
          <>
            <Button size="sm" variant="secondary" onClick={() => setUploadOpen((o) => !o)} aria-expanded={uploadOpen}>
              {uploadOpen ? "Hide order IDs" : "Upload order IDs"}
            </Button>
            <Button size="sm" variant="secondary" onClick={() => setReportOpen((o) => !o)} aria-expanded={reportOpen}>
              {reportOpen ? "Hide order report" : "Upload order report"}
            </Button>
            <Button size="sm" variant="secondary" onClick={() => setInvoicesOpen((o) => !o)} aria-expanded={invoicesOpen}>
              {invoicesOpen ? "Hide invoices" : "Upload invoices"}
            </Button>
            <Button size="sm" variant="secondary" onClick={() => setSyncOpen((o) => !o)} aria-expanded={syncOpen}>
              {syncOpen ? "Hide read-back" : "Read back from Flipkart"}
            </Button>
            <Button size="sm" onClick={reload} loading={loading && items.length > 0}>
              Refresh
            </Button>
          </>
        }
      />

      {uploadOpen ? <UploadOrderIds onChanged={reload} blocked={runActive} /> : null}
      {reportOpen ? <UploadOrderReport onChanged={reload} /> : null}
      {invoicesOpen ? <UploadInvoices onChanged={reload} /> : null}
      {syncOpen ? <SyncPanel onSynced={reload} blocked={runActive} /> : null}

      <OrderUnitsFilters
        draft={draft}
        onChange={(patch) => setDraft((d) => ({ ...d, ...patch }))}
        onApply={apply}
        onReset={reset}
        appliedNarrowed={filtered}
        options={options}
        applying={loading}
        idPrefix="ou"
      />

      <Card
        title={
          <span>
            Showing {formatCount(Math.min(items.length, total))} of {formatCount(total)} order{" "}
            {total === 1 ? "unit" : "units"}
            {selected.size > 0 ? <span className="font-normal text-ink-2"> · {formatCount(selected.size)} selected</span> : null}
          </span>
        }
        description={showingSavedView && viewSavedAt ? "Showing your saved view." : undefined}
        actions={
          <>
            <Button size="sm" variant="ghost" onClick={() => void onDefaultView()} loading={viewBusy} disabled={viewBusy}>
              Default View
            </Button>
            <Button size="sm" variant="ghost" onClick={() => void onQuickSave()} loading={viewBusy} disabled={viewBusy}>
              Quick Save
            </Button>
            <ColumnsMenu visible={columns} onChange={setColumns} />
            <ExtractInvoicesDataControls filters={applied} total={total} push={push} />
            <CompileInvoicesControls filters={applied} total={total} push={push} />
            <Button
              size="sm"
              onClick={() => void onDownload("xlsx")}
              loading={exporting === "xlsx"}
              disabled={exportsBlocked}
            >
              Export .xlsx
            </Button>
            <DownloadMenu
              busy={exporting !== null && exporting !== "xlsx" ? exporting : null}
              disabled={exportsBlocked}
              onPick={(kind) => void onDownload(kind)}
            />

            {/* Goods in hand, marked by a person (spec FR-001's "Received"). */}
            <Button
              size="sm"
              variant="secondary"
              disabled={selected.size === 0 || marking}
              loading={marking}
              onClick={() => void onMarkReceived(true)}
              title={selected.size === 0 ? "Tick units in the table first" : undefined}
            >
              Mark received
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={selected.size === 0 || marking}
              onClick={() => void onMarkReceived(false)}
              title={selected.size === 0 ? "Tick units in the table first" : "Take the received mark back"}
            >
              Not received
            </Button>

            {/* Spec FR-020 / C-4: a request for a person to action on Flipkart by hand; nothing is sent. */}
            <Button
              size="sm"
              variant="primary"
              disabled={selected.size === 0}
              onClick={() => openRequest("reschedule")}
              title={selected.size === 0 ? "Tick units in the table first" : undefined}
            >
              Reschedule Order Units
            </Button>
            <Button
              size="sm"
              variant="danger"
              disabled={selected.size === 0}
              onClick={() => openRequest("cancel")}
              title={selected.size === 0 ? "Tick units in the table first" : undefined}
            >
              Cancel Order Units
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <OrderTable
            orders={items}
            loading={loading}
            error={error}
            onRetry={reload}
            filtered={filtered}
            onClearFilter={reset}
            onView={setDetail}
            visibleColumns={columns}
            selectedIds={selected}
            onToggleRow={onToggleRow}
            onTogglePage={onTogglePage}
            onCancel={onCancelRow}
            onInvoiceDownloadError={(message) =>
              push({ tone: "error", title: "Could not download the invoice", description: message })
            }
          />
          {pages > 1 || page > 1 ? (
            <AccountsPager
              idPrefix="ou-pager"
              noun="order units"
              page={page}
              size={PAGE_SIZE}
              total={total}
              pages={pages}
              shown={items.length}
              onPageChange={setPage}
              loading={loading}
            />
          ) : null}
        </div>
      </Card>

      </div>

      <OrderDetail order={detail} onClose={closeDetail} onSettled={onSettled} onRefreshed={reload} />
      <UnitRequestDialog draft={requestDraft} onClose={() => setRequestDraft(null)} onRecorded={onRequestsRecorded} />
      <Toaster toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
