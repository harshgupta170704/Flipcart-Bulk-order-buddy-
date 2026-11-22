import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  addOtpPhone,
  bulkImportOtpPhones,
  deleteOtpPhone,
  errorMessage,
  getOtpPhoneSettings,
  listOtpPhoneMessages,
  listOtpPhones,
  newOtpPhoneToken,
  setOtpPhoneSettings,
  updateOtpPhone,
} from "../lib/api";
import { formatDateTime } from "../lib/format";
import type { OtpPhone, OtpPhoneBulkResult, OtpPhoneCreated, OtpPhoneMessage } from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Dialog } from "../components/Dialog";
import { Checkbox, Field, TextInput } from "../components/Field";
import { StatusBadge } from "../components/StatusBadge";
import { Table, type Column } from "../components/Table";

/** How often the list refreshes, so Online and Last OTP stay current. */
const REFRESH_MS = 15_000;

interface PhoneDraft {
  label: string;
  phone: string;
  employeeId: string;
  corporateId: string;
}

const EMPTY_DRAFT: PhoneDraft = { label: "", phone: "", employeeId: "", corporateId: "" };

/** A read-only value with a Copy button. */
function CopyField({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    const done = () => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    };
    try {
      void navigator.clipboard.writeText(value).then(done, () => undefined);
    } catch {
      /* no clipboard (an http page): the value is still selectable */
    }
  };
  return (
    <Field label={label}>
      {(control) => (
        <div className="flex gap-2">
          <input
            id={control.id}
            readOnly
            value={value}
            onFocus={(e) => e.target.select()}
            className="h-[38px] w-full min-w-0 rounded-control border border-line bg-page px-3 font-mono text-[12.5px] text-ink"
          />
          <Button aria-label={`Copy ${label}`} onClick={copy}>
            {copied ? "Copied" : "Copy"}
          </Button>
        </div>
      )}
    </Field>
  );
}

/** The token, shown this once, and the two addresses the forwarder app calls. */
function TokenDialog({ phone, onClose }: { phone: OtpPhoneCreated | null; onClose: () => void }) {
  const origin = window.location.origin;
  return (
    <Dialog
      open={phone !== null}
      title={phone ? `Set up ${phone.label}` : "Set up phone"}
      description="The token is shown only now. Copy it into the forwarder app before closing."
      onClose={onClose}
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      {phone ? (
        <div className="flex flex-col gap-3">
          <CopyField label="Token" value={phone.token} />
          <CopyField label="URL" value={origin + phone.webhook_path.split("?")[0]} />
          <CopyField label="Request headers" value={`{"Authorization": "Bearer ${phone.token}"}`} />
          <CopyField label="Webhook URL" value={origin + phone.webhook_path} />
          <CopyField label="Ping URL" value={origin + phone.ping_path} />
          <div className="text-[12.5px] leading-relaxed text-ink-2">
            <p className="font-medium text-ink">SMS forwarder (GawkAr, Play Store)</p>
            <ol className="mt-1 list-decimal space-y-0.5 pl-5">
              <li>Menu → Settings: turn on “Through our server”.</li>
              <li>Synchronization → + → Advanced Settings: turn on “Forward OTP from banks”, then Next.</li>
              <li>
                + → Sync with “URL”: paste the URL above. Request template: leave the body as it is, paste the request
                headers above, Save.
              </li>
              <li>Next through the remaining steps, then ✓.</li>
              <li>Phone settings → Apps → SMS forwarder: battery Unrestricted, unrestricted data, and “pause if unused” off.</li>
            </ol>
            <p className="mt-2">
              Dual SIM: add each number here as its own phone, and give each its own filter in the app, set to that SIM.
              Other forwarder apps: POST each text to the webhook URL as JSON, e.g.{" "}
              <code className="font-mono">{'{"from": "%from%", "text": "%text%"}'}</code>.
            </p>
          </div>
        </div>
      ) : null}
    </Dialog>
  );
}

function EditPhoneDialog({ phone, onClose, onSaved }: { phone: OtpPhone | null; onClose: () => void; onSaved: () => void }) {
  const [draft, setDraft] = useState<PhoneDraft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!phone) return;
    setDraft({ label: phone.label, phone: phone.phone_number, employeeId: phone.employee_id ?? "", corporateId: phone.corporate_id ?? "" });
    setError(null);
  }, [phone]);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!phone) return;
    setSaving(true);
    setError(null);
    try {
      await updateOtpPhone(phone.id, {
        label: draft.label.trim(),
        employee_id: draft.employeeId.trim() || null,
        corporate_id: draft.corporateId.trim() || null,
      });
      onSaved();
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open={phone !== null}
      title={phone ? `Edit ${phone.label}` : "Edit phone"}
      onClose={onClose}
      width="520px"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" form="otp-phone-edit" variant="primary" loading={saving} disabled={!draft.label.trim()}>
            Save
          </Button>
        </>
      }
    >
      <form id="otp-phone-edit" className="grid gap-3 sm:grid-cols-2" onSubmit={(e) => void save(e)}>
        {error ? (
          <Alert tone="error" className="sm:col-span-2">
            {error}
          </Alert>
        ) : null}
        <Field label="Label" required>
          <TextInput value={draft.label} maxLength={64} onChange={(e) => setDraft({ ...draft, label: e.target.value })} />
        </Field>
        <Field label="Phone Number" help="Can't be changed. Add the phone again for a new number.">
          <TextInput value={draft.phone} disabled />
        </Field>
        <Field label="Employee ID">
          <TextInput value={draft.employeeId} maxLength={64} onChange={(e) => setDraft({ ...draft, employeeId: e.target.value })} />
        </Field>
        <Field label="Corporate ID">
          <TextInput value={draft.corporateId} maxLength={64} onChange={(e) => setDraft({ ...draft, corporateId: e.target.value })} />
        </Field>
      </form>
    </Dialog>
  );
}

function MessagesDialog({ phone, onClose }: { phone: OtpPhone | null; onClose: () => void }) {
  const [messages, setMessages] = useState<OtpPhoneMessage[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!phone) return;
    setMessages(null);
    setError(null);
    void listOtpPhoneMessages(phone.id).then(setMessages, (err) => setError(errorMessage(err)));
  }, [phone]);

  const columns: Column<OtpPhoneMessage>[] = [
    { key: "received_at", header: "Received", render: (row) => formatDateTime(row.received_at) },
    { key: "sender", header: "From", render: (row) => row.sender ?? "—" },
    { key: "otp", header: "OTP", render: (row) => (row.otp ? <span className="font-mono tabular-nums">{row.otp}</span> : "—") },
    { key: "body", header: "Message", render: (row) => <span className="whitespace-pre-wrap break-words">{row.body}</span> },
  ];

  return (
    <Dialog
      open={phone !== null}
      title={phone ? `Texts from ${phone.label}` : "Texts"}
      description="What this phone forwarded in the last day, newest first."
      onClose={onClose}
      width="820px"
    >
      {error ? <Alert tone="error">{error}</Alert> : null}
      {messages === null && !error ? <p className="text-[13px] text-ink-2">Loading…</p> : null}
      {messages ? (
        <Table caption="Forwarded texts" columns={columns} rows={messages} rowKey={(row) => row.id} dense minWidth="640px" empty="No texts in the last day." />
      ) : null}
    </Dialog>
  );
}

/** Employee Phones: phones that forward the OTP texts they receive, for hands-free sign-ins. */
export default function EmployeePhones() {
  const [rows, setRows] = useState<OtpPhone[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState<PhoneDraft>(EMPTY_DRAFT);
  const [addError, setAddError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [created, setCreated] = useState<OtpPhoneCreated | null>(null);
  const [editing, setEditing] = useState<OtpPhone | null>(null);
  const [reading, setReading] = useState<OtpPhone | null>(null);
  const [assumeOnline, setAssumeOnline] = useState(false);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkCorp, setBulkCorp] = useState("");
  const [bulkCsv, setBulkCsv] = useState("");
  const [bulkResult, setBulkResult] = useState<OtpPhoneBulkResult | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);

  useEffect(() => {
    document.title = "Employee Phones — Ordering Suite";
  }, []);

  const load = useCallback(async () => {
    try {
      const [list, cfg] = await Promise.all([
        listOtpPhones(),
        typeof getOtpPhoneSettings === "function" ? getOtpPhoneSettings().catch(() => null) : Promise.resolve(null),
      ]);
      setRows(list);
      if (cfg && typeof cfg.assume_online === "boolean") setAssumeOnline(cfg.assume_online);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  const toggleAssumeOnline = async (next: boolean) => {
    setAssumeOnline(next);
    try {
      await setOtpPhoneSettings(next);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const runBulk = async (e: FormEvent) => {
    e.preventDefault();
    setBulkBusy(true);
    setError(null);
    try {
      const res = await bulkImportOtpPhones({ corporate_id: bulkCorp.trim() || null, csv_text: bulkCsv });
      setBulkResult(res);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBulkBusy(false);
    }
  };

  useEffect(() => {
    void load();
    const handle = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(handle);
  }, [load]);

  const add = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setAddError(null);
    try {
      const phone = await addOtpPhone({
        label: draft.label.trim(),
        phone_number: draft.phone.trim(),
        employee_id: draft.employeeId.trim() || null,
        corporate_id: draft.corporateId.trim() || null,
      });
      setAdding(false);
      setDraft(EMPTY_DRAFT);
      setCreated(phone);
      await load();
    } catch (err) {
      setAddError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const columns: Column<OtpPhone>[] = [
    {
      key: "online",
      header: "Status",
      render: (row) =>
        row.online ? <StatusBadge status="done" label="Online" /> : <StatusBadge status="skipped" label="Offline" />,
    },
    { key: "label", header: "Label" },
    { key: "phone_number", header: "Phone Number", render: (row) => <span className="font-mono tabular-nums">{row.phone_number}</span> },
    { key: "employee_id", header: "Employee ID", render: (row) => row.employee_id ?? "—" },
    { key: "corporate_id", header: "Corporate ID", render: (row) => row.corporate_id ?? "—" },
    { key: "last_ping_at", header: "Last Ping", render: (row) => formatDateTime(row.last_ping_at) },
    {
      key: "last_otp",
      header: "Last OTP",
      render: (row) =>
        row.last_otp ? (
          <span className="font-mono tabular-nums" title={row.last_sender ? `From ${row.last_sender}` : undefined}>
            {row.last_otp}
          </span>
        ) : (
          "—"
        ),
    },
    { key: "last_otp_at", header: "Last OTP Time", render: (row) => formatDateTime(row.last_otp_at) },
    {
      key: "active",
      header: "Active",
      render: (row) => (
        <input
          type="checkbox"
          aria-label={`${row.label} active`}
          className="size-4 accent-accent"
          checked={row.active}
          onChange={(e) => {
            const on = e.target.checked;
            setRows((prev) => prev && prev.map((r) => (r.id === row.id ? { ...r, active: on } : r)));
            void updateOtpPhone(row.id, { active: on }).then(load, (err) => {
              setError(errorMessage(err));
              void load();
            });
          }}
        />
      ),
    },
    {
      key: "actions",
      header: "",
      sticky: "right",
      render: (row) => (
        <div className="flex gap-1">
          <Button size="sm" variant="ghost" onClick={() => setEditing(row)}>
            Edit
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              if (!window.confirm(`Give ${row.label} a new token? The old one stops working, so the app must be set up again.`)) return;
              void newOtpPhoneToken(row.id).then((phone) => {
                setCreated(phone);
                void load();
              }, (err) => setError(errorMessage(err)));
            }}
          >
            New token
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setReading(row)}>
            Messages
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              if (!window.confirm(`Delete ${row.label}? Its texts will no longer be accepted.`)) return;
              void deleteOtpPhone(row.id).then(load, (err) => setError(errorMessage(err)));
            }}
          >
            Delete
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-5">
      <header>
        <h1 className="text-[20px] font-semibold tracking-tight text-ink">Employee Phones</h1>
        <p className="mt-1 max-w-prose text-[13px] text-ink-2">
          Phones running an SMS forwarder app. Each forwards the OTP texts it receives here, so sign-ins that send an OTP
          to a mobile run hands-free.
        </p>
      </header>
      {error ? <Alert tone="error">{error}</Alert> : null}
      <Card
        flush
        title={`Phones${rows ? ` (${rows.length})` : ""}`}
        actions={
          <div className="flex flex-wrap items-center gap-3">
            <Checkbox
              checked={assumeOnline}
              onChange={(checked) => void toggleAssumeOnline(checked)}
              label="Assume all numbers online"
            />
            <Button
              size="sm"
              onClick={() => {
                setBulkResult(null);
                setBulkOpen(true);
              }}
            >
              Configure New Employee IDs
            </Button>
            <Button
              size="sm"
              variant="primary"
              onClick={() => {
                setAddError(null);
                setAdding(true);
              }}
            >
              Add phone
            </Button>
          </div>
        }
      >
        <Table caption="Employee phones" columns={columns} rows={rows ?? []} rowKey={(row) => row.id} dense minWidth="1100px" empty="No phones yet." />
      </Card>

      <Dialog
        open={bulkOpen}
        title="Configure New Employee IDs"
        description="Upload or paste employee_id,phone_number[,label] CSV rows. New phones receive relay tokens right away."
        onClose={() => setBulkOpen(false)}
        width="560px"
        footer={
          <>
            <Button variant="ghost" onClick={() => setBulkOpen(false)}>
              Close
            </Button>
            <Button type="submit" form="otp-phone-bulk" variant="primary" loading={bulkBusy} disabled={!bulkCsv.trim()}>
              Import CSV
            </Button>
          </>
        }
      >
        <form id="otp-phone-bulk" className="flex flex-col gap-3" onSubmit={(e) => void runBulk(e)}>
          <Field label="Corporate ID (optional)">
            <TextInput value={bulkCorp} maxLength={64} onChange={(e) => setBulkCorp(e.target.value)} placeholder="e.g. HDFC-CORP-01" />
          </Field>
          <input
            type="file"
            accept=".csv,text/csv,text/plain"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (!file) return;
              void file.text().then((txt) => setBulkCsv(txt));
            }}
            className="text-[13px] text-ink"
          />
          <textarea
            rows={6}
            value={bulkCsv}
            onChange={(e) => setBulkCsv(e.target.value)}
            placeholder={"employee_id,phone_number,label\nEMP01,9876543210,Ravi's phone"}
            className="w-full rounded-control border border-line bg-surface p-2.5 font-mono text-[12.5px] text-ink"
          />
          {bulkResult ? (
            <Alert tone="info">
              Added: {bulkResult.added.length} · Updated: {bulkResult.updated} · Skipped: {bulkResult.skipped.length}
              {bulkResult.added.length > 0 ? (
                <div className="mt-2 flex flex-col gap-1">
                  {bulkResult.added.map((p) => (
                    <button
                      key={p.id}
                      type="button"
                      className="text-left font-mono text-[12px] underline"
                      onClick={() => setCreated(p)}
                    >
                      View setup token for {p.label} ({p.phone_number})
                    </button>
                  ))}
                </div>
              ) : null}
            </Alert>
          ) : null}
        </form>
      </Dialog>

      <Dialog
        open={adding}
        title="Add phone"
        onClose={() => setAdding(false)}
        width="520px"
        footer={
          <>
            <Button variant="ghost" onClick={() => setAdding(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              form="otp-phone-add"
              variant="primary"
              loading={saving}
              disabled={!draft.label.trim() || !draft.phone.trim()}
            >
              Add phone
            </Button>
          </>
        }
      >
        <form id="otp-phone-add" className="grid gap-3 sm:grid-cols-2" onSubmit={(e) => void add(e)}>
          {addError ? (
            <Alert tone="error" className="sm:col-span-2">
              {addError}
            </Alert>
          ) : null}
          <Field label="Label" required>
            <TextInput value={draft.label} maxLength={64} onChange={(e) => setDraft({ ...draft, label: e.target.value })} />
          </Field>
          <Field label="Phone Number" required>
            <TextInput inputMode="tel" value={draft.phone} onChange={(e) => setDraft({ ...draft, phone: e.target.value })} />
          </Field>
          <Field label="Employee ID">
            <TextInput value={draft.employeeId} maxLength={64} onChange={(e) => setDraft({ ...draft, employeeId: e.target.value })} />
          </Field>
          <Field label="Corporate ID">
            <TextInput value={draft.corporateId} maxLength={64} onChange={(e) => setDraft({ ...draft, corporateId: e.target.value })} />
          </Field>
        </form>
      </Dialog>

      <TokenDialog phone={created} onClose={() => setCreated(null)} />
      <EditPhoneDialog phone={editing} onClose={() => setEditing(null)} onSaved={() => void load()} />
      <MessagesDialog phone={reading} onClose={() => setReading(null)} />
    </div>
  );
}
