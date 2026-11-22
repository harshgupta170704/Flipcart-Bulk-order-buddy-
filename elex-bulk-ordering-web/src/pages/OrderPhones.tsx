import { useCallback, useEffect, useState, type FormEvent } from "react";
import { addOrderPhone, deleteOrderPhone, errorMessage, listOrderPhones, updateOrderPhone } from "../lib/api";
import { formatDate } from "../lib/format";
import type { OrderPhoneNumber } from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Field, TextInput } from "../components/Field";
import { Table, type Column } from "../components/Table";

/** Order Phone Numbers: the numbers a run may put on the delivery address in turn. */
export default function OrderPhones() {
  const [rows, setRows] = useState<OrderPhoneNumber[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [number, setNumber] = useState("");
  const [label, setLabel] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    document.title = "Order Phone Numbers — Ordering Suite";
  }, []);

  const load = useCallback(async () => {
    try {
      setRows(await listOrderPhones());
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
      await addOrderPhone({ number, label: label.trim() || null });
      setNumber("");
      setLabel("");
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const columns: Column<OrderPhoneNumber>[] = [
    { key: "number", header: "Phone", render: (row) => <span className="font-mono tabular-nums">{row.number}</span> },
    { key: "label", header: "Label", render: (row) => row.label ?? "—" },
    {
      key: "active",
      header: "In use",
      render: (row) => (
        <input
          type="checkbox"
          aria-label={`Use ${row.number} on orders`}
          className="size-4 accent-accent"
          checked={row.active}
          onChange={(e) => void updateOrderPhone(row.id, { active: e.target.checked }).then(load, (err) => setError(errorMessage(err)))}
        />
      ),
    },
    { key: "created_at", header: "Created", render: (row) => formatDate(row.created_at) },
    {
      key: "actions",
      header: "",
      render: (row) => (
        <Button size="sm" variant="ghost" onClick={() => void deleteOrderPhone(row.id).then(load, (err) => setError(errorMessage(err)))}>
          Delete
        </Button>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-5">
      <header>
        <h1 className="text-[20px] font-semibold tracking-tight text-ink">Order Phone Numbers</h1>
        <p className="mt-1 max-w-prose text-[13px] text-ink-2">
          Numbers a run puts on the delivery address in turn. On Smart Bulk Order, “Use saved numbers” fills the
          Address Phone Numbers box with the ones marked in use.
        </p>
      </header>
      {error ? <Alert tone="error">{error}</Alert> : null}
      <Card title="Add a number">
        <form className="grid gap-3 sm:grid-cols-[1fr_1fr_auto]" onSubmit={(e) => void add(e)}>
          <Field label="Phone" required help="10 digits, without the country code.">
            <TextInput inputMode="tel" value={number} onChange={(e) => setNumber(e.target.value)} />
          </Field>
          <Field label="Label">
            <TextInput value={label} maxLength={64} onChange={(e) => setLabel(e.target.value)} />
          </Field>
          <div className="flex items-end">
            <Button type="submit" variant="primary" loading={saving} disabled={number.trim() === ""}>
              Add number
            </Button>
          </div>
        </form>
      </Card>
      <Card flush>
        <Table caption="Order phone numbers" columns={columns} rows={rows ?? []} rowKey={(row) => row.id} dense />
      </Card>
    </div>
  );
}
