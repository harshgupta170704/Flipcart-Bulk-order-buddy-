import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  actOnHdfcChildCards,
  addHdfcUserId,
  deleteHdfcUserId,
  downloadHdfcChildCards,
  errorMessage,
  fetchHdfcCards,
  listHdfcChildCards,
  listHdfcUserIds,
  updateHdfcUserId,
} from "../lib/api";
import { formatCurrency, formatDateTime } from "../lib/format";
import type { HdfcCardFilters, HdfcChildCard, HdfcFetchResult, HdfcUserId } from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Field, NumberInput, Select, TextInput } from "../components/Field";
import { Table, type Column } from "../components/Table";

type Tab = "users" | "cards";

const TABS: { id: Tab; label: string }[] = [
  { id: "users", label: "User IDs" },
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

function UserIds({ onFetched }: { onFetched: () => void }) {
  const [rows, setRows] = useState<HdfcUserId[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [draft, setDraft] = useState({ username: "", email: "", mobile: "", parent: "" });
  const [saving, setSaving] = useState(false);
  const [fetching, setFetching] = useState(false);

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
      });
      setDraft({ username: "", email: "", mobile: "", parent: "" });
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
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-5">
      {error ? <Alert tone="error">{error}</Alert> : null}
      {notice ? <Alert tone="success">{notice}</Alert> : null}
      <Card title="Add user id">
        <form className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5" onSubmit={(e) => void add(e)}>
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
            <Button size="sm" loading={fetching} disabled={selected.size === 0} onClick={() => void runFetch([...selected])}>
              Fetch selected
            </Button>
            <Button size="sm" variant="primary" loading={fetching} disabled={all.length === 0} onClick={() => void runFetch(null)}>
              Fetch all
            </Button>
          </div>
        }
      >
        <Table caption="HDFC user ids" columns={columns} rows={all} rowKey={(row) => row.id} dense empty="No user ids yet." />
      </Card>
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

/** CorpAssist (HDFC): user ids whose virtual child-card emails are read, and the cards read from them. */
export default function CorpAssist() {
  const [tab, setTab] = useState<Tab>("users");
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    document.title = "CorpAssist — Ordering Suite";
  }, []);

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
      <div role="tablist" aria-label="CorpAssist" className="flex w-full max-w-md rounded-control border border-line bg-page p-1">
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
      {tab === "users" ? <UserIds onFetched={() => setReloadKey((k) => k + 1)} /> : <ChildCards reloadKey={reloadKey} />}
    </div>
  );
}
