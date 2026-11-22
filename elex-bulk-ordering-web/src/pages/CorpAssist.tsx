import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  actOnHdfcChildCards,
  addHdfcUserId,
  cancelHdfcBatch,
  createHdfcBatch,
  deleteHdfcUserId,
  downloadHdfcBatchSheet,
  downloadHdfcChildCards,
  errorMessage,
  fetchHdfcCards,
  getHdfcPortal,
  listHdfcBatches,
  listHdfcChildCards,
  listHdfcUserIds,
  retryHdfcBatch,
  syncHdfcPortalUsers,
  pointHdfcUserEmail,
  testHdfcPortalLogin,
  updateHdfcUserId,
} from "../lib/api";
import { formatCurrency, formatDateTime, formatNumber } from "../lib/format";
import type {
  HdfcBatch,
  HdfcBatchSend,
  HdfcBatchStatus,
  HdfcCardFilters,
  HdfcChildCard,
  HdfcFetchResult,
  HdfcPortalStatus,
  HdfcPortalTestResult,
  HdfcUserId,
} from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Dialog } from "../components/Dialog";
import { Field, NumberInput, Select, TextInput } from "../components/Field";
import { StatusBadge } from "../components/StatusBadge";
import { Table, type Column } from "../components/Table";

type Tab = "users" | "create" | "cards";

const TABS: { id: Tab; label: string }[] = [
  { id: "users", label: "User IDs" },
  { id: "create", label: "Create cards" },
  { id: "cards", label: "Child cards" },
];

function SelectBox({ checked, onChange, label }: { checked: boolean; onChange: (on: boolean) => void; label: string }) {
  return <input type="checkbox" className="size-4" aria-label={label} checked={checked} onChange={(e) => onChange(e.target.checked)} />;
}

function toggle(ids: Set<number>, id: number, on: boolean): Set<number> {
  const next = new Set(ids);
  if (on) next.add(id);
  else next.delete(id);
  return next;
}

function fetchSummary(results: HdfcFetchResult[]): string {
  const added = results.reduce((sum, r) => sum + r.added, 0);
  const failed = results.filter((r) => r.error).length;
  const parts = [`${added} new child card${added === 1 ? "" : "s"} from ${results.length} user id${results.length === 1 ? "" : "s"}`];
  if (failed) parts.push(`${failed} with a problem (see Last fetch)`);
  return parts.join("; ") + ".";
}

/* --- User IDs ------------------------------------------------------------- */

const AAN_LABEL = "Aan (parent card's Alternate A/c No.)";
const AAN_HELP = "From HDFC's Card Relationship page. HDFC needs it to create cards.";
const PORTAL_ID_LABEL = "HDFC Virtual Card User ID";

interface UserDraft {
  username: string;
  email: string;
  mobile: string;
  parent: string;
  aan: string;
  portalUserId: string;
}

const EMPTY_DRAFT: UserDraft = { username: "", email: "", mobile: "", parent: "", aan: "", portalUserId: "" };

/** Edit one user id. The parent card is sent only when typed, since only its last four digits are kept. */
function EditUserIdDialog({ row, onClose, onSaved }: { row: HdfcUserId | null; onClose: () => void; onSaved: () => void }) {
  const [draft, setDraft] = useState<UserDraft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!row) return;
    setDraft({
      username: row.username,
      email: row.email,
      mobile: row.mobile ?? "",
      parent: "",
      aan: row.aan ?? "",
      portalUserId: row.portal_user_id ?? "",
    });
    setError(null);
  }, [row]);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!row) return;
    setSaving(true);
    setError(null);
    try {
      await updateHdfcUserId(row.id, {
        username: draft.username.trim(),
        email: draft.email.trim(),
        mobile: draft.mobile.trim() || null,
        ...(draft.parent.trim() ? { parent_card: draft.parent.trim() } : {}),
        aan: draft.aan.trim() || null,
        portal_user_id: draft.portalUserId.trim() || null,
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
      open={row !== null}
      title={row ? `Edit ${row.username}` : "Edit user id"}
      onClose={onClose}
      width="560px"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            form="hdfc-edit-user"
            variant="primary"
            loading={saving}
            disabled={!draft.username.trim() || !draft.email.trim()}
          >
            Save
          </Button>
        </>
      }
    >
      <form id="hdfc-edit-user" className="grid gap-3 sm:grid-cols-2" onSubmit={(e) => void save(e)}>
        {error ? (
          <Alert tone="error" className="sm:col-span-2">
            {error}
          </Alert>
        ) : null}
        <Field label="Username" required>
          <TextInput value={draft.username} maxLength={64} onChange={(e) => setDraft({ ...draft, username: e.target.value })} />
        </Field>
        <Field label="Email" required>
          <TextInput type="email" value={draft.email} onChange={(e) => setDraft({ ...draft, email: e.target.value })} />
        </Field>
        <Field label="Mobile">
          <TextInput inputMode="numeric" value={draft.mobile} onChange={(e) => setDraft({ ...draft, mobile: e.target.value })} />
        </Field>
        <Field
          label="Parent card"
          help={row?.parent_card_last4 ? `Saved: •••• ${row.parent_card_last4}. Leave empty to keep it.` : "Only its last four digits are saved."}
        >
          <TextInput inputMode="numeric" autoComplete="off" value={draft.parent} onChange={(e) => setDraft({ ...draft, parent: e.target.value })} />
        </Field>
        <Field label={AAN_LABEL} help={AAN_HELP}>
          <TextInput inputMode="numeric" autoComplete="off" value={draft.aan} onChange={(e) => setDraft({ ...draft, aan: e.target.value })} />
        </Field>
        <Field label={PORTAL_ID_LABEL}>
          <TextInput
            autoComplete="off"
            value={draft.portalUserId}
            maxLength={64}
            onChange={(e) => setDraft({ ...draft, portalUserId: e.target.value })}
          />
        </Field>
      </form>
    </Dialog>
  );
}

function UserIds({ onFetched, portal }: { onFetched: () => void; portal: HdfcPortalStatus | null }) {
  const [rows, setRows] = useState<HdfcUserId[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [draft, setDraft] = useState<UserDraft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [editing, setEditing] = useState<HdfcUserId | null>(null);

  const load = useCallback(async () => {
    try {
      setRows(await listHdfcUserIds());
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
    setError(null);
    try {
      await addHdfcUserId({
        username: draft.username.trim(),
        email: draft.email.trim(),
        mobile: draft.mobile.trim() || null,
        parent_card: draft.parent.trim() || null,
        aan: draft.aan.trim() || null,
        portal_user_id: draft.portalUserId.trim() || null,
      });
      setDraft(EMPTY_DRAFT);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const runFetch = async (ids: number[] | null) => {
    setFetching(true);
    setNotice(null);
    setError(null);
    try {
      const results = await fetchHdfcCards(ids);
      setNotice(fetchSummary(results));
      setSelected(new Set());
      await load();
      onFetched();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setFetching(false);
    }
  };

  const syncFromHdfc = async () => {
    setSyncing(true);
    setNotice(null);
    setError(null);
    try {
      const result = await syncHdfcPortalUsers();
      setNotice(`${result.added} added, ${result.updated} updated.`);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSyncing(false);
    }
  };

  const all = rows ?? [];
  const columns: Column<HdfcUserId>[] = [
    {
      key: "select",
      header: (
        <SelectBox
          label="Select every user id"
          checked={all.length > 0 && selected.size === all.length}
          onChange={(on) => setSelected(on ? new Set(all.map((r) => r.id)) : new Set())}
        />
      ),
      width: "36px",
      render: (row) => <SelectBox label={`Select ${row.username}`} checked={selected.has(row.id)} onChange={(on) => setSelected((s) => toggle(s, row.id, on))} />,
    },
    { key: "username", header: "Username" },
    { key: "email", header: "Email" },
    { key: "mobile", header: "Mobile" },
    { key: "parent_card_last4", header: "Parent card", render: (row) => (row.parent_card_last4 ? `•••• ${row.parent_card_last4}` : "—") },
    { key: "aan", header: "Aan", render: (row) => (row.aan ? <span className="font-mono tabular-nums">{row.aan}</span> : "—") },
    { key: "card_count", header: "Child cards", numeric: true },
    {
      key: "active",
      header: "Active",
      render: (row) => (
        <SelectBox
          label={`${row.username} active`}
          checked={row.active}
          onChange={(on) => {
            setRows((prev) => prev && prev.map((r) => (r.id === row.id ? { ...r, active: on } : r)));
            void updateHdfcUserId(row.id, { active: on }).then(load, (err) => {
              setError(errorMessage(err));
              void load();
            });
          }}
        />
      ),
    },
    {
      key: "last_fetched_at",
      header: "Last fetch",
      render: (row) =>
        row.last_fetched_at ? (
          <span className={row.last_error ? "text-critical" : undefined} title={row.last_error ?? undefined}>
            {formatDateTime(row.last_fetched_at)}
            {row.last_error ? ` — ${row.last_error}` : ""}
          </span>
        ) : (
          "Never"
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
          {portal?.configured ? (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                if (!window.confirm(`Change ${row.username}'s email on the HDFC portal to ${row.email}? HDFC then mails its cards there.`)) return;
                setNotice(null);
                setError(null);
                void pointHdfcUserEmail(row.id).then(
                  () => setNotice(`${row.username}'s email on HDFC is now ${row.email}.`),
                  (err) => setError(errorMessage(err)),
                );
              }}
            >
              Set email on HDFC
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              if (!window.confirm(`Delete ${row.username} and the ${row.card_count} child card(s) read for it?`)) return;
              void deleteHdfcUserId(row.id).then(() => {
                setSelected((s) => toggle(s, row.id, false));
                void load();
                onFetched();
              }, (err) => setError(errorMessage(err)));
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
      {error ? <Alert tone="error">{error}</Alert> : null}
      {notice ? <Alert tone="success">{notice}</Alert> : null}
      <Card title="Add user id">
        <form className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4" onSubmit={(e) => void add(e)}>
          <Field label="Username" required>
            <TextInput value={draft.username} maxLength={64} onChange={(e) => setDraft({ ...draft, username: e.target.value })} />
          </Field>
          <Field label="Email" required help="The address HDFC mails this user's child cards to. It must reach the shared inbox.">
            <TextInput type="email" value={draft.email} onChange={(e) => setDraft({ ...draft, email: e.target.value })} />
          </Field>
          <Field label="Mobile">
            <TextInput inputMode="numeric" value={draft.mobile} onChange={(e) => setDraft({ ...draft, mobile: e.target.value })} />
          </Field>
          <Field label="Parent card" help="Only its last four digits are saved.">
            <TextInput inputMode="numeric" autoComplete="off" value={draft.parent} onChange={(e) => setDraft({ ...draft, parent: e.target.value })} />
          </Field>
          <Field label={AAN_LABEL} help={AAN_HELP}>
            <TextInput inputMode="numeric" autoComplete="off" value={draft.aan} onChange={(e) => setDraft({ ...draft, aan: e.target.value })} />
          </Field>
          <Field label={PORTAL_ID_LABEL}>
            <TextInput
              autoComplete="off"
              value={draft.portalUserId}
              maxLength={64}
              onChange={(e) => setDraft({ ...draft, portalUserId: e.target.value })}
            />
          </Field>
          <div className="flex items-end">
            <Button type="submit" variant="primary" loading={saving} disabled={!draft.username.trim() || !draft.email.trim()}>
              Add user id
            </Button>
          </div>
        </form>
      </Card>
      <Card
        flush
        title="User IDs"
        actions={
          <div className="flex gap-2">
            {portal?.configured ? (
              <Button size="sm" loading={syncing} onClick={() => void syncFromHdfc()}>
                Sync from HDFC
              </Button>
            ) : null}
            <Button size="sm" loading={fetching} disabled={selected.size === 0} onClick={() => void runFetch([...selected])}>
              Fetch selected
            </Button>
            <Button size="sm" variant="primary" loading={fetching} disabled={all.length === 0} onClick={() => void runFetch(null)}>
              Fetch all
            </Button>
          </div>
        }
      >
        <Table caption="HDFC user ids" columns={columns} rows={all} rowKey={(row) => row.id} dense minWidth="1000px" empty="No user ids yet." />
      </Card>
      <EditUserIdDialog row={editing} onClose={() => setEditing(null)} onSaved={() => void load()} />
    </div>
  );
}

/* --- Create cards ---------------------------------------------------------- */

const LIMITS = {
  amount: [1, 10_000_000],
  count: [1, 500],
  days: [1, 365],
} as const satisfies Record<string, readonly [number, number]>;
/** The most cards one batch may ask for, across every user id. */
const MAX_CARDS = 1000;
/** How often the batches list refreshes while one is still sending or waiting. */
const POLL_MS = 10_000;

/** A batch status drawn with the shared status badge's look. */
const BATCH_BADGE: Record<HdfcBatchStatus, { status: string; label: string }> = {
  sending: { status: "running", label: "Sending" },
  waiting: { status: "pending", label: "Waiting for cards" },
  done: { status: "done", label: "Done" },
  failed: { status: "failed", label: "Failed" },
  cancelled: { status: "cancelled_by_user", label: "Cancelled" },
};

function isOpen(batch: HdfcBatch): boolean {
  return batch.status === "sending" || batch.status === "waiting";
}

/** The whole number typed, or null when it is empty, not whole, or out of range. */
function inRange(text: string, [min, max]: readonly [number, number]): number | null {
  const value = Number(text);
  if (text.trim() === "" || !Number.isInteger(value) || value < min || value > max) return null;
  return value;
}

/** Distinct values across a batch's rows, e.g. "₹5,000" or "₹5,000, ₹10,000". */
function distinct(values: number[], show: (value: number) => string): string {
  return [...new Set(values)].map(show).join(", ") || "—";
}

function PortalLine({ portal, error }: { portal: HdfcPortalStatus | null; error: string | null }) {
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<HdfcPortalTestResult | null>(null);

  if (error) return <Alert tone="error">{error}</Alert>;
  if (!portal) return null;
  if (!portal.configured) {
    return (
      <Alert tone="info">
        HDFC portal not set up: download the sheet and upload it on HDFC's Create Virtual Card (Bulk) page yourself.
      </Alert>
    );
  }

  const test = async () => {
    setTesting(true);
    setResult(null);
    try {
      setResult(await testHdfcPortalLogin());
    } catch (err) {
      setResult({ ok: false, detail: errorMessage(err) });
    } finally {
      setTesting(false);
    }
  };

  const signedIn = `HDFC portal: signed in as ${portal.user_hint ?? "—"}`;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          {portal.otp_ready ? (
            <Alert tone="success">
              {signedIn}, OTP via {portal.otp_via ?? "—"}
            </Alert>
          ) : (
            <Alert tone="warning">
              {signedIn}, but its OTP can't be read: {portal.otp_detail ?? "no way to read it is set up."}
            </Alert>
          )}
        </div>
        <Button size="sm" loading={testing} onClick={() => void test()}>
          Test sign-in
        </Button>
      </div>
      {portal.last_error ? <p className="text-[12px] text-ink-2">Last portal error: {portal.last_error}</p> : null}
      {result ? <Alert tone={result.ok ? "success" : "error"}>{result.detail}</Alert> : null}
    </div>
  );
}

function CreateCards({ portal, portalError }: { portal: HdfcPortalStatus | null; portalError: string | null }) {
  const [users, setUsers] = useState<HdfcUserId[] | null>(null);
  const [batches, setBatches] = useState<HdfcBatch[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [amount, setAmount] = useState("1");
  const [count, setCount] = useState("1");
  const [days, setDays] = useState("7");
  const [sending, setSending] = useState<HdfcBatchSend | null>(null);
  const [report, setReport] = useState<HdfcBatch | null>(null);

  const loadBatches = useCallback(async () => {
    try {
      setBatches(await listHdfcBatches());
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void listHdfcUserIds().then(setUsers, (err) => setError(errorMessage(err)));
    void loadBatches();
  }, [loadBatches]);

  const polling = (batches ?? []).some(isOpen);
  useEffect(() => {
    if (!polling) return;
    const handle = window.setInterval(() => void loadBatches(), POLL_MS);
    return () => window.clearInterval(handle);
  }, [polling, loadBatches]);

  const active = (users ?? []).filter((u) => u.active);
  const chosen = active.filter((u) => selected.has(u.id));
  const noAan = chosen.filter((u) => !u.aan);
  const amountN = inRange(amount, LIMITS.amount);
  const countN = inRange(count, LIMITS.count);
  const daysN = inRange(days, LIMITS.days);
  const totalCards = countN ? chosen.length * countN : 0;
  const tooMany = totalCards > MAX_CARDS;
  const ready = chosen.length > 0 && amountN !== null && countN !== null && daysN !== null && !tooMany;

  const create = async (send: HdfcBatchSend) => {
    if (!ready || amountN === null || countN === null || daysN === null) return;
    setSending(send);
    setError(null);
    setNotice(null);
    try {
      const batch = await createHdfcBatch(
        chosen.map((u) => ({ user_id: u.id, amount: amountN, count: countN, valid_days: daysN })),
        send,
      );
      setSelected(new Set());
      if (send === "download") {
        await downloadHdfcBatchSheet(batch.id);
        setNotice(`Sheet for ${batch.card_count} card(s) downloaded. Upload it on HDFC's Create Virtual Card (Bulk) page.`);
      } else {
        setNotice(`Sending ${batch.card_count} card(s) to HDFC. Each card is read from HDFC's email as it arrives.`);
      }
      await loadBatches();
    } catch (err) {
      setError(errorMessage(err));
      void loadBatches();
    } finally {
      setSending(null);
    }
  };

  const act = (run: () => Promise<unknown>) => {
    setError(null);
    void run().then(loadBatches, (err) => setError(errorMessage(err)));
  };

  const columns: Column<HdfcBatch>[] = [
    { key: "created_at", header: "Created", render: (row) => formatDateTime(row.created_at) },
    { key: "users", header: "Users", render: (row) => row.rows.map((r) => r.username).join(", ") || "—" },
    { key: "cards", header: "Cards", numeric: true, render: (row) => `${row.cards_received}/${row.card_count}` },
    { key: "amount", header: "Amount", numeric: true, render: (row) => distinct(row.rows.map((r) => r.amount), formatCurrency) },
    { key: "valid_days", header: "Valid days", numeric: true, render: (row) => distinct(row.rows.map((r) => r.valid_days), String) },
    { key: "send", header: "Send", render: (row) => (row.send === "portal" ? "HDFC portal" : "Sheet") },
    {
      key: "status",
      header: "Status",
      render: (row) => <StatusBadge status={BATCH_BADGE[row.status]?.status ?? row.status} label={BATCH_BADGE[row.status]?.label} />,
    },
    { key: "error", header: "Error", render: (row) => (row.error ? <span className="text-critical">{row.error}</span> : "—") },
    {
      key: "actions",
      header: "",
      sticky: "right",
      render: (row) => (
        <div className="flex gap-1">
          <Button size="sm" variant="ghost" aria-label={`CSV of batch ${row.id}`} onClick={() => act(() => downloadHdfcBatchSheet(row.id))}>
            CSV
          </Button>
          {row.portal_report ? (
            <Button size="sm" variant="ghost" onClick={() => setReport(row)}>
              Report
            </Button>
          ) : null}
          {row.status === "failed" && row.send === "portal" ? (
            <Button size="sm" variant="ghost" onClick={() => act(() => retryHdfcBatch(row.id))}>
              Retry
            </Button>
          ) : null}
          {isOpen(row) ? (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                if (!window.confirm("Stop waiting for this batch's cards?")) return;
                act(() => cancelHdfcBatch(row.id));
              }}
            >
              Cancel
            </Button>
          ) : null}
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-5">
      <PortalLine portal={portal} error={portalError} />
      {error ? <Alert tone="error">{error}</Alert> : null}
      {notice ? <Alert tone="success">{notice}</Alert> : null}
      <Card title="Create cards" description="Ask HDFC for new virtual cards: each selected user id gets this many cards of this amount.">
        <div className="flex flex-col gap-4">
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1.5 text-[13px] font-medium text-ink">User ids</legend>
            {users === null ? (
              <p className="text-[13px] text-ink-2">Loading…</p>
            ) : active.length === 0 ? (
              <p className="text-[13px] text-ink-2">No active user ids. Add one on the User IDs tab.</p>
            ) : (
              <>
                <label className="flex items-center gap-2 text-[13px] font-medium text-ink">
                  <SelectBox
                    label="Select all"
                    checked={chosen.length === active.length}
                    onChange={(on) => setSelected(on ? new Set(active.map((u) => u.id)) : new Set())}
                  />
                  Select all
                </label>
                <div className="grid max-h-56 gap-1.5 overflow-y-auto rounded-control border border-line p-3 sm:grid-cols-2 lg:grid-cols-3">
                  {active.map((u) => (
                    <label key={u.id} className="flex min-w-0 items-center gap-2 text-[13px] text-ink">
                      <SelectBox label={`Create for ${u.username}`} checked={selected.has(u.id)} onChange={(on) => setSelected((s) => toggle(s, u.id, on))} />
                      <span className="truncate">{u.username}</span>
                      {u.aan ? null : <span className="shrink-0 text-[12px] text-ink-2">(no Aan)</span>}
                    </label>
                  ))}
                </div>
              </>
            )}
          </fieldset>
          {noAan.length ? (
            <Alert tone="warning">
              {noAan.map((u) => u.username).join(", ")} {noAan.length === 1 ? "has" : "have"} no Aan (parent card's Alternate
              A/c No.), so HDFC will refuse {noAan.length === 1 ? "its" : "their"} cards. Add it on the User IDs tab first.
            </Alert>
          ) : null}
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Amount (₹)" required error={amount && amountN === null ? "Whole rupees, 1 to 1,00,00,000." : undefined}>
              <NumberInput min={LIMITS.amount[0]} max={LIMITS.amount[1]} step={1} value={amount} onChange={(e) => setAmount(e.target.value)} />
            </Field>
            <Field label="Cards per user id" required error={count && countN === null ? "1 to 500." : undefined}>
              <NumberInput min={LIMITS.count[0]} max={LIMITS.count[1]} step={1} value={count} onChange={(e) => setCount(e.target.value)} />
            </Field>
            <Field label="Valid for days" required error={days && daysN === null ? "1 to 365." : undefined}>
              <NumberInput min={LIMITS.days[0]} max={LIMITS.days[1]} step={1} value={days} onChange={(e) => setDays(e.target.value)} />
            </Field>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <p className="text-[13px] font-medium text-ink" aria-live="polite">
              Total: {formatNumber(totalCards)} card{totalCards === 1 ? "" : "s"}, {formatCurrency(amountN ? totalCards * amountN : 0)}
            </p>
            {tooMany ? <p className="text-[12px] text-critical">At most {MAX_CARDS} cards in one batch.</p> : null}
            <div className="ml-auto flex flex-wrap gap-2">
              <Button loading={sending === "download"} disabled={!ready || sending !== null} onClick={() => void create("download")}>
                Download HDFC sheet
              </Button>
              <Button
                variant="primary"
                loading={sending === "portal"}
                disabled={!ready || sending !== null || !portal?.configured}
                title={portal?.configured ? undefined : "The HDFC portal sign-in is not set up."}
                onClick={() => void create("portal")}
              >
                Create on HDFC
              </Button>
            </div>
          </div>
        </div>
      </Card>
      <Card flush title={`Batches${batches ? ` (${batches.length})` : ""}`}>
        <Table
          caption="HDFC card batches"
          columns={columns}
          rows={batches ?? []}
          rowKey={(row) => row.id}
          dense
          minWidth="1100px"
          empty="No batches yet."
        />
      </Card>
      <Dialog open={report !== null} title="HDFC portal report" onClose={() => setReport(null)} width="720px">
        <pre className="whitespace-pre-wrap break-words font-mono text-[12px] text-ink">{report?.portal_report}</pre>
      </Dialog>
    </div>
  );
}

/* --- Child cards ---------------------------------------------------------- */

const NO_FILTERS: HdfcCardFilters = { status: "", min_remaining: "", created_from: "", created_to: "", search: "" };

function ChildCards({ reloadKey }: { reloadKey: number }) {
  const [rows, setRows] = useState<HdfcChildCard[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filters, setFilters] = useState<HdfcCardFilters>(NO_FILTERS);
  const [applied, setApplied] = useState<HdfcCardFilters>(NO_FILTERS);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setRows(await listHdfcChildCards(applied));
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [applied]);

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  const shown = rows ?? [];
  const act = async (action: "cancel" | "activate" | "delete") => {
    const ids = shown.map((r) => r.id);
    if (!ids.length) return;
    const verb = { cancel: "Cancel", activate: "Reactivate", delete: "Delete" }[action];
    if (!window.confirm(`${verb} the ${ids.length} child card(s) shown?`)) return;
    setBusy(true);
    setError(null);
    try {
      await actOnHdfcChildCards(ids, action);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<HdfcChildCard>[] = [
    { key: "username", header: "Username" },
    { key: "email", header: "Email" },
    { key: "card_number", header: "Child card", render: (row) => <span className="font-mono tabular-nums">{row.card_number}</span> },
    { key: "parent_card_last4", header: "Parent card", render: (row) => (row.parent_card_last4 ? `•••• ${row.parent_card_last4}` : "—") },
    { key: "opening_amount", header: "Opening Amount", numeric: true, render: (row) => formatCurrency(row.opening_amount) },
    { key: "used", header: "Used", numeric: true, render: (row) => formatCurrency(row.used) },
    { key: "remaining", header: "Remaining", numeric: true, render: (row) => formatCurrency(row.remaining) },
    {
      key: "status",
      header: "Status",
      render: (row) => (
        <button
          type="button"
          className="underline decoration-dotted underline-offset-2"
          title={row.status === "active" ? "Cancel this card" : "Reactivate this card"}
          onClick={() => void actOnHdfcChildCards([row.id], row.status === "active" ? "cancel" : "activate").then(load, (err) => setError(errorMessage(err)))}
        >
          {row.status === "active" ? "Active" : "Cancelled"}
        </button>
      ),
    },
    { key: "error", header: "Error" },
    { key: "created_at", header: "Created", render: (row) => formatDateTime(row.created_at) },
  ];

  return (
    <div className="flex flex-col gap-5">
      {error ? <Alert tone="error">{error}</Alert> : null}
      <Card title="Filters">
        <form
          className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6"
          onSubmit={(e) => {
            e.preventDefault();
            setApplied({ ...filters });
          }}
        >
          <Field label="Search" hint="Username, email or card digits">
            <TextInput value={filters.search} maxLength={64} onChange={(e) => setFilters({ ...filters, search: e.target.value })} />
          </Field>
          <Field label="Status">
            <Select value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value as HdfcCardFilters["status"] })}>
              <option value="">Any</option>
              <option value="active">Active</option>
              <option value="cancelled">Cancelled</option>
            </Select>
          </Field>
          <Field label="Minimum Remaining Amount (₹)">
            <NumberInput min={0} value={filters.min_remaining} onChange={(e) => setFilters({ ...filters, min_remaining: e.target.value })} />
          </Field>
          <Field label="Created from">
            <TextInput type="date" value={filters.created_from} onChange={(e) => setFilters({ ...filters, created_from: e.target.value })} />
          </Field>
          <Field label="Created to">
            <TextInput type="date" value={filters.created_to} onChange={(e) => setFilters({ ...filters, created_to: e.target.value })} />
          </Field>
          <div className="flex items-end gap-2">
            <Button type="submit" variant="primary">
              Apply
            </Button>
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setFilters(NO_FILTERS);
                setApplied(NO_FILTERS);
              }}
            >
              Clear
            </Button>
          </div>
        </form>
      </Card>
      <Card
        flush
        title={`Child cards${rows ? ` (${rows.length})` : ""}`}
        actions={
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={!shown.length || busy} onClick={() => void act("cancel")}>
              Cancel all
            </Button>
            <Button size="sm" variant="danger" disabled={!shown.length || busy} onClick={() => void act("delete")}>
              Delete all
            </Button>
            <Button size="sm" disabled={!shown.length} onClick={() => void downloadHdfcChildCards(applied).catch((err) => setError(errorMessage(err)))}>
              CSV
            </Button>
          </div>
        }
      >
        <Table caption="HDFC child cards" columns={columns} rows={shown} rowKey={(row) => row.id} dense minWidth="1100px" empty="No child cards. Fetch from the User IDs tab." />
      </Card>
    </div>
  );
}

/** CorpAssist (HDFC): user ids, new virtual card requests, and the child cards read from HDFC's emails. */
export default function CorpAssist() {
  const [tab, setTab] = useState<Tab>("users");
  const [reloadKey, setReloadKey] = useState(0);
  const [portal, setPortal] = useState<HdfcPortalStatus | null>(null);
  const [portalError, setPortalError] = useState<string | null>(null);

  useEffect(() => {
    document.title = "CorpAssist — Ordering Suite";
  }, []);

  // Re-read on every tab change, so a sign-in set up meanwhile shows without a reload.
  useEffect(() => {
    void getHdfcPortal().then(
      (status) => {
        setPortal(status);
        setPortalError(null);
      },
      (err) => setPortalError(errorMessage(err)),
    );
  }, [tab]);

  return (
    <div className="flex flex-col gap-5">
      <header>
        <h1 className="text-[20px] font-semibold tracking-tight text-ink">CorpAssist (HDFC)</h1>
        <p className="mt-1 max-w-prose text-[13px] text-ink-2">
          User ids for HDFC virtual card emails, and the child cards read from those emails in the shared inbox. Used is
          what a card paid for in this tool's orders; spending elsewhere is not seen. A card's expiry and CVV are never
          kept, so they still come from the run's card sheet.
        </p>
      </header>
      <div role="tablist" aria-label="CorpAssist" className="flex w-full max-w-lg rounded-control border border-line bg-page p-1">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={`flex-1 rounded-[6px] px-4 py-2 text-[13px] font-medium transition-colors ${
              tab === t.id ? "bg-surface text-ink shadow-sm" : "text-ink-2 hover:text-ink"
            }`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "users" ? (
        <UserIds portal={portal} onFetched={() => setReloadKey((k) => k + 1)} />
      ) : tab === "create" ? (
        <CreateCards portal={portal} portalError={portalError} />
      ) : (
        <ChildCards reloadKey={reloadKey} />
      )}
    </div>
  );
}
