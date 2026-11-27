import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  createInventoryBatch,
  createInventoryTransfer,
  deleteInventoryBatch,
  deleteInventoryTransfer,
  downloadInventoryMismatchReport,
  errorMessage,
  listInventoryBatches,
  listInventoryStock,
  listInventoryTransfers,
  updateCodCards,
  updateInventoryBatch,
} from "../lib/api";
import { formatDateTime } from "../lib/format";
import type { CodCardsUpdateResult, InventoryBatch, InventoryStockRow, InventoryTransfer } from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Dialog } from "../components/Dialog";
import { Field, NumberInput, Select, TextInput } from "../components/Field";
import { StatusBadge } from "../components/StatusBadge";
import { Table, type Column } from "../components/Table";

type Tab = "active" | "completed" | "batches" | "transfers";

const TABS: { id: Tab; label: string }[] = [
  { id: "active", label: "Active / Pending Stock" },
  { id: "completed", label: "Completed Stock" },
  { id: "batches", label: "Batches" },
  { id: "transfers", label: "Transfer Notes" },
];

/** Rows listed per group in the COD cards result; the rest are counted. */
export const COD_ROWS_SHOWN = 30;

/** What "Update orders" did: the counts, then each row left out and why, and
 *  the order ids not found — so a refused row is seen, not just counted. */
export function CodCardsResult({ result }: { result: CodCardsUpdateResult }) {
  const leftOut = result.skipped.length + result.not_found.length;
  return (
    <Alert
      tone={leftOut ? "warning" : "info"}
      title={`Updated: ${result.updated} · Not found: ${result.not_found.length} · Skipped: ${result.skipped.length}`}
    >
      {result.skipped.length ? (
        <ul className="mt-1 list-disc pl-5">
          {result.skipped.slice(0, COD_ROWS_SHOWN).map((line, index) => (
            <li key={index}>{line}</li>
          ))}
          {result.skipped.length > COD_ROWS_SHOWN ? <li>And {result.skipped.length - COD_ROWS_SHOWN} more.</li> : null}
        </ul>
      ) : null}
      {result.not_found.length ? (
        <p className="mt-1 break-words">
          Not found: {result.not_found.slice(0, COD_ROWS_SHOWN).join(", ")}
          {result.not_found.length > COD_ROWS_SHOWN ? ` and ${result.not_found.length - COD_ROWS_SHOWN} more` : ""}.
        </p>
      ) : null}
    </Alert>
  );
}

export default function Inventory() {
  const [tab, setTab] = useState<Tab>("active");
  const [search, setSearch] = useState("");
  const [stock, setStock] = useState<InventoryStockRow[]>([]);
  const [batches, setBatches] = useState<InventoryBatch[]>([]);
  const [transfers, setTransfers] = useState<InventoryTransfer[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Create Batch modal
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchDraft, setBatchDraft] = useState({
    name: "",
    platform: "flipkart",
    product_name: "",
    opening_qty: "0",
    received_qty: "0",
    approved_qty: "0",
    notes: "",
  });

  // Create Transfer Note modal
  const [transferOpen, setTransferOpen] = useState(false);
  const [transferDraft, setTransferDraft] = useState({
    reference: "",
    platform: "flipkart",
    product_name: "",
    quantity: "1",
    destination: "",
    notes: "",
  });

  // Update COD Card Details modal
  const [codOpen, setCodOpen] = useState(false);
  const [codCsv, setCodCsv] = useState("");
  const [codResult, setCodResult] = useState<CodCardsUpdateResult | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    document.title = "Inventory — Ordering Suite";
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [s, b, t] = await Promise.all([
        listInventoryStock(tab === "completed" ? "completed" : "active", search),
        listInventoryBatches(),
        listInventoryTransfers(),
      ]);
      setStock(s);
      setBatches(b);
      setTransfers(t);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [tab, search]);

  useEffect(() => {
    void load();
  }, [load]);

  const submitBatch = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await createInventoryBatch({
        name: batchDraft.name.trim(),
        platform: batchDraft.platform,
        product_name: batchDraft.product_name.trim(),
        opening_qty: Math.max(0, Number(batchDraft.opening_qty) || 0),
        received_qty: Math.max(0, Number(batchDraft.received_qty) || 0),
        approved_qty: Math.max(0, Number(batchDraft.approved_qty) || 0),
        notes: batchDraft.notes.trim() || null,
      });
      setBatchOpen(false);
      setBatchDraft({ name: "", platform: "flipkart", product_name: "", opening_qty: "0", received_qty: "0", approved_qty: "0", notes: "" });
      setNotice("Batch created.");
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const submitTransfer = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await createInventoryTransfer({
        reference: transferDraft.reference.trim(),
        platform: transferDraft.platform,
        product_name: transferDraft.product_name.trim(),
        quantity: Math.max(1, Number(transferDraft.quantity) || 1),
        destination: transferDraft.destination.trim() || null,
        notes: transferDraft.notes.trim() || null,
      });
      setTransferOpen(false);
      setTransferDraft({ reference: "", platform: "flipkart", product_name: "", quantity: "1", destination: "", notes: "" });
      setNotice("Transfer note recorded.");
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const submitCod = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const res = await updateCodCards(codCsv);
      setCodResult(res);
      setNotice(`Updated ${res.updated} Cash-on-Delivery order row(s).`);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const stockColumns: Column<InventoryStockRow>[] = [
    { key: "platform", header: "Platform", render: (r) => r.platform.toUpperCase() },
    { key: "product_name", header: "Product Name" },
    { key: "opening_qty", header: "Opening Quantity", numeric: true },
    { key: "delivered_qty", header: "Quantity Delivered from Platform", numeric: true },
    { key: "expected_qty", header: "Expected Quantity", numeric: true },
    { key: "received_qty", header: "Quantity Received", numeric: true },
    { key: "approved_qty", header: "Quantity Approved", numeric: true },
    { key: "transferred_qty", header: "Quantity Transferred", numeric: true },
    {
      key: "mismatch_qty",
      header: "Mismatch Quantity",
      numeric: true,
      render: (r) => (
        <span className={r.mismatch_qty !== 0 ? "font-semibold text-[var(--status-bad)]" : ""}>{r.mismatch_qty}</span>
      ),
    },
    { key: "to_be_transferred_qty", header: "Quantity To be Transferred", numeric: true },
  ];

  const batchColumns: Column<InventoryBatch>[] = [
    { key: "name", header: "Batch" },
    { key: "platform", header: "Platform", render: (r) => r.platform.toUpperCase() },
    { key: "product_name", header: "Product Name" },
    { key: "opening_qty", header: "Opening", numeric: true },
    { key: "received_qty", header: "Received", numeric: true },
    { key: "approved_qty", header: "Approved", numeric: true },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <StatusBadge status={r.status === "completed" ? "done" : "running"} label={r.status === "completed" ? "Completed" : "Active"} />
      ),
    },
    { key: "created_at", header: "Created", render: (r) => formatDateTime(r.created_at) },
    {
      key: "actions",
      header: "Actions",
      render: (r) => (
        <div className="flex gap-1">
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              void updateInventoryBatch(r.id, { status: r.status === "active" ? "completed" : "active" }).then(load, (err) =>
                setError(errorMessage(err)),
              )
            }
          >
            {r.status === "active" ? "Mark completed" : "Reopen"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              if (!window.confirm(`Delete batch ${r.name}?`)) return;
              void deleteInventoryBatch(r.id).then(load, (err) => setError(errorMessage(err)));
            }}
          >
            Delete
          </Button>
        </div>
      ),
    },
  ];

  const transferColumns: Column<InventoryTransfer>[] = [
    { key: "reference", header: "Reference" },
    { key: "platform", header: "Platform", render: (r) => r.platform.toUpperCase() },
    { key: "product_name", header: "Product Name" },
    { key: "quantity", header: "Quantity", numeric: true },
    { key: "destination", header: "Destination", render: (r) => r.destination || "—" },
    { key: "notes", header: "Notes", render: (r) => r.notes || "—" },
    { key: "created_at", header: "Created", render: (r) => formatDateTime(r.created_at) },
    {
      key: "actions",
      header: "Actions",
      render: (r) => (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            if (!window.confirm(`Delete transfer note ${r.reference}?`)) return;
            void deleteInventoryTransfer(r.id).then(load, (err) => setError(errorMessage(err)));
          }}
        >
          Delete
        </Button>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-[20px] font-semibold tracking-tight text-ink">Inventory</h1>
          <p className="mt-1 max-w-prose text-[13px] text-ink-2">
            Reconcile delivered platform orders against received stock batches and outbound transfer notes.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={() => void downloadInventoryMismatchReport().catch((e) => setError(errorMessage(e)))}>
            Download Mismatch Report
          </Button>
          <Button
            size="sm"
            onClick={() => {
              setCodResult(null);
              setCodOpen(true);
            }}
          >
            Update COD Card Details
          </Button>
          <Button size="sm" variant="primary" onClick={() => setBatchOpen(true)}>
            Create New Batch
          </Button>
          <Button size="sm" variant="primary" onClick={() => setTransferOpen(true)}>
            Create Transfer Note
          </Button>
        </div>
      </header>

      {error ? <Alert tone="error">{error}</Alert> : null}
      {notice ? <Alert tone="success">{notice}</Alert> : null}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div role="tablist" aria-label="Inventory" className="flex flex-wrap rounded-control border border-line bg-page p-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              className={`rounded-[6px] px-4 py-2 text-[13px] font-medium transition-colors ${
                tab === t.id ? "bg-surface text-ink shadow-sm" : "text-ink-2 hover:text-ink"
              }`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
        {(tab === "active" || tab === "completed") && (
          <div className="flex items-center gap-2">
            <TextInput
              placeholder="Search product…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-60"
            />
            <Button size="sm" loading={loading} onClick={() => void load()}>
              Refresh
            </Button>
          </div>
        )}
      </div>

      {(tab === "active" || tab === "completed") && (
        <Card flush title={tab === "active" ? `Active / Pending Stock (${stock.length})` : `Completed Stock (${stock.length})`}>
          <Table
            caption="Inventory stock"
            columns={stockColumns}
            rows={stock}
            rowKey={(r) => `${r.platform}:${r.product_name}`}
            dense
            minWidth="1150px"
            empty="No stock records match yet. Delivered orders or receiving batches appear here automatically."
          />
        </Card>
      )}

      {tab === "batches" && (
        <Card flush title={`Batches (${batches.length})`}>
          <Table
            caption="Inventory batches"
            columns={batchColumns}
            rows={batches}
            rowKey={(r) => r.id}
            dense
            minWidth="950px"
            empty="No batches yet. Create a batch to record opening, received, and approved warehouse counts."
          />
        </Card>
      )}

      {tab === "transfers" && (
        <Card flush title={`Transfer Notes (${transfers.length})`}>
          <Table
            caption="Transfer notes"
            columns={transferColumns}
            rows={transfers}
            rowKey={(r) => r.id}
            dense
            minWidth="900px"
            empty="No transfer notes yet."
          />
        </Card>
      )}

      <Dialog
        open={batchOpen}
        title="Create New Batch"
        onClose={() => setBatchOpen(false)}
        width="560px"
        footer={
          <>
            <Button variant="ghost" onClick={() => setBatchOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" form="inv-batch-form" variant="primary" loading={saving} disabled={!batchDraft.name.trim() || !batchDraft.product_name.trim()}>
              Create batch
            </Button>
          </>
        }
      >
        <form id="inv-batch-form" className="grid gap-3 sm:grid-cols-2" onSubmit={(e) => void submitBatch(e)}>
          <Field label="Batch name" required>
            <TextInput value={batchDraft.name} onChange={(e) => setBatchDraft({ ...batchDraft, name: e.target.value })} placeholder="e.g. Batch Nov-22" />
          </Field>
          <Field label="Platform">
            <Select value={batchDraft.platform} onChange={(e) => setBatchDraft({ ...batchDraft, platform: e.target.value })}>
              <option value="flipkart">Flipkart</option>
              <option value="amazon">Amazon</option>
            </Select>
          </Field>
          <Field label="Product Name" required className="sm:col-span-2">
            <TextInput value={batchDraft.product_name} onChange={(e) => setBatchDraft({ ...batchDraft, product_name: e.target.value })} />
          </Field>
          <Field label="Opening Quantity">
            <NumberInput min={0} value={batchDraft.opening_qty} onChange={(e) => setBatchDraft({ ...batchDraft, opening_qty: e.target.value })} />
          </Field>
          <Field label="Quantity Received">
            <NumberInput min={0} value={batchDraft.received_qty} onChange={(e) => setBatchDraft({ ...batchDraft, received_qty: e.target.value })} />
          </Field>
          <Field label="Quantity Approved">
            <NumberInput min={0} value={batchDraft.approved_qty} onChange={(e) => setBatchDraft({ ...batchDraft, approved_qty: e.target.value })} />
          </Field>
          <Field label="Notes">
            <TextInput value={batchDraft.notes} onChange={(e) => setBatchDraft({ ...batchDraft, notes: e.target.value })} />
          </Field>
        </form>
      </Dialog>

      <Dialog
        open={transferOpen}
        title="Create Transfer Note"
        onClose={() => setTransferOpen(false)}
        width="560px"
        footer={
          <>
            <Button variant="ghost" onClick={() => setTransferOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" form="inv-transfer-form" variant="primary" loading={saving} disabled={!transferDraft.reference.trim() || !transferDraft.product_name.trim()}>
              Create transfer
            </Button>
          </>
        }
      >
        <form id="inv-transfer-form" className="grid gap-3 sm:grid-cols-2" onSubmit={(e) => void submitTransfer(e)}>
          <Field label="Transfer Reference" required>
            <TextInput value={transferDraft.reference} onChange={(e) => setTransferDraft({ ...transferDraft, reference: e.target.value })} placeholder="e.g. TN-001" />
          </Field>
          <Field label="Platform">
            <Select value={transferDraft.platform} onChange={(e) => setTransferDraft({ ...transferDraft, platform: e.target.value })}>
              <option value="flipkart">Flipkart</option>
              <option value="amazon">Amazon</option>
            </Select>
          </Field>
          <Field label="Product Name" required className="sm:col-span-2">
            <TextInput value={transferDraft.product_name} onChange={(e) => setTransferDraft({ ...transferDraft, product_name: e.target.value })} />
          </Field>
          <Field label="Quantity" required>
            <NumberInput min={1} value={transferDraft.quantity} onChange={(e) => setTransferDraft({ ...transferDraft, quantity: e.target.value })} />
          </Field>
          <Field label="Destination">
            <TextInput value={transferDraft.destination} onChange={(e) => setTransferDraft({ ...transferDraft, destination: e.target.value })} placeholder="Warehouse / Buyer" />
          </Field>
          <Field label="Notes" className="sm:col-span-2">
            <TextInput value={transferDraft.notes} onChange={(e) => setTransferDraft({ ...transferDraft, notes: e.target.value })} />
          </Field>
        </form>
      </Dialog>

      <Dialog
        open={codOpen}
        title="Update COD Card Details"
        description="Upload or paste CSV lines as order_id,card_number to record the card a Cash-on-Delivery order was paid with at the door. Only Cash-on-Delivery orders are changed: a card-paid order keeps the card its run recorded."
        onClose={() => setCodOpen(false)}
        width="560px"
        footer={
          <>
            <Button variant="ghost" onClick={() => setCodOpen(false)}>
              Close
            </Button>
            <Button type="submit" form="cod-csv-form" variant="primary" loading={saving} disabled={!codCsv.trim()}>
              Update orders
            </Button>
          </>
        }
      >
        <form id="cod-csv-form" className="flex flex-col gap-3" onSubmit={(e) => void submitCod(e)}>
          <input
            type="file"
            accept=".csv,text/csv,text/plain"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (!file) return;
              void file.text().then((txt) => setCodCsv(txt));
            }}
            className="text-[13px] text-ink"
          />
          <textarea
            rows={6}
            value={codCsv}
            onChange={(e) => setCodCsv(e.target.value)}
            placeholder={"order_id,card_number\nOD433123456789012300,4111222233334444"}
            className="w-full rounded-control border border-line bg-surface p-2.5 font-mono text-[12.5px] text-ink"
          />
          {codResult ? <CodCardsResult result={codResult} /> : null}
        </form>
      </Dialog>
    </div>
  );
}
