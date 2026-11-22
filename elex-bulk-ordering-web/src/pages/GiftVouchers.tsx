import { useCallback, useEffect, useState, type FormEvent } from "react";
import { addGiftVoucher, deleteGiftVoucher, errorMessage, listGiftVouchers, updateGiftVoucher } from "../lib/api";
import { formatDate } from "../lib/format";
import type { GiftVoucher } from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Field, NumberInput, TextInput } from "../components/Field";
import { Table, type Column } from "../components/Table";

const rupees = (value: number) => `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

/** Gift Vouchers: the register of vouchers, their balance and who holds them. */
export default function GiftVouchers() {
  const [rows, setRows] = useState<GiftVoucher[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState({ label: "", code: "", value: "", account: "", expires: "" });
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    document.title = "Gift Vouchers — Ordering Suite";
  }, []);

  const load = useCallback(async () => {
    try {
      setRows(await listGiftVouchers());
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const add = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    try {
      await addGiftVoucher({
        label: draft.label.trim(),
        code: draft.code.trim() || null,
        value: Number(draft.value || 0),
        account_email: draft.account.trim() || null,
        expires_on: draft.expires || null,
      });
      setDraft({ label: "", code: "", value: "", account: "", expires: "" });
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const columns: Column<GiftVoucher>[] = [
    { key: "label", header: "Label" },
    { key: "code_last4", header: "Code", render: (row) => (row.code_last4 ? `•••• ${row.code_last4}` : "—") },
    { key: "value", header: "Value", numeric: true, render: (row) => rupees(row.value) },
    {
      key: "balance",
      header: "Balance",
      numeric: true,
      render: (row) => (
        <button
          type="button"
          className="tabular-nums underline decoration-dotted underline-offset-2"
          title="Change the balance"
          onClick={() => {
            const next = window.prompt(`Balance left on ${row.label}`, String(row.balance));
            if (next === null || next.trim() === "" || Number.isNaN(Number(next))) return;
            void updateGiftVoucher(row.id, { balance: Number(next) }).then(load, (err) => setError(errorMessage(err)));
          }}
        >
          {rupees(row.balance)}
        </button>
      ),
    },
    { key: "account_email", header: "Account", render: (row) => row.account_email ?? "—" },
    { key: "expires_on", header: "Expires", render: (row) => (row.expires_on ? formatDate(row.expires_on) : "—") },
    { key: "created_at", header: "Created", render: (row) => formatDate(row.created_at) },
    {
      key: "actions",
      header: "",
      render: (row) => (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            if (!window.confirm(`Delete ${row.label} from the register?`)) return;
            void deleteGiftVoucher(row.id).then(load, (err) => setError(errorMessage(err)));
          }}
        >
          Delete
        </Button>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-5">
      <header>
        <h1 className="text-[20px] font-semibold tracking-tight text-ink">Gift Vouchers</h1>
        <p className="mt-1 max-w-prose text-[13px] text-ink-2">
          The vouchers on hand, what is left on each, and the account holding it. Only the last four characters of a
          code are kept — never the full code or its PIN.
        </p>
      </header>
      {error ? <Alert tone="error">{error}</Alert> : null}
      <Card title="Add a gift voucher">
        <form className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6" onSubmit={(e) => void add(e)}>
          <Field label="Label" required>
            <TextInput value={draft.label} maxLength={64} onChange={(e) => setDraft({ ...draft, label: e.target.value })} />
          </Field>
          <Field label="Code" help="Only its last four characters are saved.">
            <TextInput value={draft.code} autoComplete="off" onChange={(e) => setDraft({ ...draft, code: e.target.value })} />
          </Field>
          <Field label="Value (₹)" required>
            <NumberInput min={0} value={draft.value} onChange={(e) => setDraft({ ...draft, value: e.target.value })} />
          </Field>
          <Field label="Account email">
            <TextInput value={draft.account} onChange={(e) => setDraft({ ...draft, account: e.target.value })} />
          </Field>
          <Field label="Expires">
            <TextInput type="date" value={draft.expires} onChange={(e) => setDraft({ ...draft, expires: e.target.value })} />
          </Field>
          <div className="flex items-end">
            <Button type="submit" variant="primary" loading={saving} disabled={!draft.label.trim() || draft.value === ""}>
              Add voucher
            </Button>
          </div>
        </form>
      </Card>
      <Card flush>
        <Table caption="Gift vouchers" columns={columns} rows={rows ?? []} rowKey={(row) => row.id} dense />
      </Card>
    </div>
  );
}
