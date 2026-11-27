/**
 * Swap Email Address: upload a CSV of (old email, new email) pairs and the
 * server changes each Flipkart account's login email to the new one — signing
 * in with the account's stored session and filling the verification codes
 * Flipkart sends (both go to mailboxes we already read for these accounts, so
 * no phone is involved). Watch each row walk pending → running → done / failed.
 *
 * CSV shape (matches the sheet): a header row "old email,new email", then one
 * pair per line. The header and blank lines are ignored.
 */
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  checkEmailSwap,
  errorMessage,
  getEmailSwapSummary,
  listAccounts,
  listEmailSwaps,
  retryFailedEmailSwaps,
  runEmailSwaps,
  swapOneEmail,
  uploadEmailSwaps,
} from "../lib/api";
import { formatDateTime } from "../lib/format";
import type { Account, EmailSwapAddressCheck, EmailSwapCheck, EmailSwapRow, EmailSwapSummary } from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { StatusBadge } from "../components/StatusBadge";
import { Table, type Column } from "../components/Table";

const EMPTY_SUMMARY: EmailSwapSummary = {
  total: 0,
  pending: 0,
  running: 0,
  done: 0,
  failed: 0,
  worker_running: false,
};

function Tile({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="flex flex-col rounded-card border border-line bg-surface px-4 py-3">
      <span className="text-[12px] font-medium text-ink-2">{label}</span>
      <span className="text-[22px] font-semibold tabular-nums" style={tone ? { color: tone } : undefined}>
        {value}
      </span>
    </div>
  );
}

const INPUT =
  "w-full rounded-control border border-line bg-surface px-3 py-1.5 text-[13px] text-ink placeholder:text-ink-2 focus:outline-none focus:ring-2 focus:ring-[var(--focus,#2563eb)]";

function AddressLine({ label, check }: { label: string; check: EmailSwapAddressCheck | null | undefined }) {
  if (!check) return null;
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 text-[12px]">
      <span className="font-medium text-ink">{label}</span>
      <span className="text-ink-2">{check.email}</span>
      {check.readable ? (
        <span style={{ color: "var(--status-good)" }}>✓ codes readable{check.inbox ? ` — ${check.inbox}` : ""}</span>
      ) : (
        <span style={{ color: "var(--status-bad)" }}>✗ {check.problem || "codes cannot be read"}</span>
      )}
    </li>
  );
}

/**
 * Swap ONE account: pick it by typing part of its email, type the new email,
 * and the page asks the server whether both codes can really be read (it opens
 * the inboxes — InstAddr for catbar/apmail, Microsoft 365 for askcaayush.com)
 * before the Swap button is enabled.
 */
function SwapOneCard({ onQueued }: { onQueued: (row: EmailSwapRow) => void }) {
  const [oldEmail, setOldEmail] = useState("");
  const [newEmail, setNewEmail] = useState("");
  const [matches, setMatches] = useState<Account[]>([]);
  const [check, setCheck] = useState<EmailSwapCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [swapping, setSwapping] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Account suggestions while typing (one small page, never the whole table).
  useEffect(() => {
    const q = oldEmail.trim();
    if (q.length < 2) {
      setMatches([]);
      return;
    }
    let live = true;
    const id = window.setTimeout(() => {
      listAccounts({ q, size: 10, sort: "id" })
        .then((page) => live && setMatches(page.items))
        .catch(() => live && setMatches([]));
    }, 250);
    return () => {
      live = false;
      window.clearTimeout(id);
    };
  }, [oldEmail]);

  // A changed pair invalidates the last check.
  useEffect(() => setCheck(null), [oldEmail, newEmail]);

  const runCheck = async () => {
    setChecking(true);
    setError(null);
    try {
      setCheck(await checkEmailSwap(oldEmail.trim(), newEmail.trim()));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setChecking(false);
    }
  };

  const swap = async (e: FormEvent) => {
    e.preventDefault();
    setSwapping(true);
    setError(null);
    try {
      const row = await swapOneEmail(oldEmail.trim(), newEmail.trim());
      onQueued(row);
      setOldEmail("");
      setNewEmail("");
      setCheck(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSwapping(false);
    }
  };

  const ready = Boolean(oldEmail.trim() && newEmail.trim());

  return (
    <Card title="Swap one account">
      <form className="flex flex-col gap-3" onSubmit={(e) => void swap(e)}>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="flex flex-col gap-1 text-[12px] font-medium text-ink-2">
            Account (current email)
            <input
              className={INPUT}
              list="swap-one-accounts"
              value={oldEmail}
              onChange={(e) => setOldEmail(e.target.value)}
              placeholder="Type part of the email…"
              autoComplete="off"
            />
            <datalist id="swap-one-accounts">
              {matches.map((a) => (
                <option key={a.id} value={a.email}>
                  {a.status}
                </option>
              ))}
            </datalist>
          </label>
          <label className="flex flex-col gap-1 text-[12px] font-medium text-ink-2">
            New email
            <input
              className={INPUT}
              type="email"
              value={newEmail}
              onChange={(e) => setNewEmail(e.target.value)}
              placeholder="e.g. someone@askcaayush.com"
              autoComplete="off"
            />
          </label>
        </div>

        {check ? (
          <div className="rounded-control border border-line bg-page px-3 py-2">
            <ul className="flex flex-col gap-1">
              <AddressLine label="Old" check={check.old} />
              <AddressLine label="New" check={check.new} />
            </ul>
            {check.ok ? (
              <p className="mt-1 text-[12px]" style={{ color: "var(--status-good)" }}>
                Ready — account #{check.account_id} has a saved session and both codes can be read.
              </p>
            ) : (
              <ul className="mt-1 list-disc pl-5 text-[12px]" style={{ color: "var(--status-bad)" }}>
                {check.reasons.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            )}
          </div>
        ) : null}
        {error ? <Alert tone="error">{error}</Alert> : null}

        <div className="flex justify-end gap-2">
          <Button type="button" size="sm" variant="ghost" loading={checking} disabled={!ready} onClick={() => void runCheck()}>
            Check
          </Button>
          <Button type="submit" size="sm" variant="primary" loading={swapping} disabled={!check?.ok}>
            Swap now
          </Button>
        </div>
      </form>
    </Card>
  );
}

export default function SwapEmail() {
  const [csv, setCsv] = useState("");
  const [fileName, setFileName] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [summary, setSummary] = useState<EmailSwapSummary>(EMPTY_SUMMARY);
  const [rows, setRows] = useState<EmailSwapRow[]>([]);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, list] = await Promise.all([getEmailSwapSummary(), listEmailSwaps()]);
      setSummary(s);
      setRows(list);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Poll while anything is in flight so the table stays live.
  useEffect(() => {
    if (!summary.worker_running && summary.pending === 0 && summary.running === 0) return;
    const id = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(id);
  }, [summary.worker_running, summary.pending, summary.running, refresh]);

  const onFile = async (file: File | null) => {
    if (!file) return;
    setFileName(file.name);
    setCsv(await file.text());
  };

  const upload = async (e: FormEvent) => {
    e.preventDefault();
    setUploading(true);
    setError(null);
    setNotice(null);
    try {
      const result = await uploadEmailSwaps(csv);
      const bits = [`${result.created} queued`];
      if (result.skipped) bits.push(`${result.skipped} skipped (already queued)`);
      if (result.errors.length) bits.push(`${result.errors.length} line error(s)`);
      setNotice(`Batch ${result.batch}: ${bits.join(", ")}.`);
      if (result.errors.length) setError(result.errors.slice(0, 8).join("\n"));
      setCsv("");
      setFileName(null);
      if (fileRef.current) fileRef.current.value = "";
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setUploading(false);
    }
  };

  const columns: Column<EmailSwapRow>[] = [
    { key: "old_email", header: "Old email", render: (r) => <span className="text-[12.5px]">{r.old_email}</span> },
    { key: "new_email", header: "New email", render: (r) => <span className="text-[12.5px] font-medium">{r.new_email}</span> },
    {
      key: "status",
      header: "Status",
      render: (r) => <StatusBadge status={r.status} label={r.status[0].toUpperCase() + r.status.slice(1)} />,
    },
    { key: "stage", header: "Stage", render: (r) => <span className="text-[12px] text-ink-2">{r.stage || "—"}</span> },
    {
      key: "message",
      header: "Detail",
      render: (r) => <span className="line-clamp-2 max-w-[320px] text-[12px] text-ink-2">{r.message || "—"}</span>,
    },
    { key: "attempts", header: "Tries", numeric: true, render: (r) => r.attempts },
    { key: "updated_at", header: "Updated", render: (r) => formatDateTime(r.updated_at) },
  ];

  const busy = summary.worker_running || summary.pending > 0 || summary.running > 0;

  return (
    <div className="flex flex-col gap-5">
      <header>
        <h1 className="text-[20px] font-semibold leading-tight tracking-tight text-ink">Swap Email Address</h1>
        <p className="mt-1 max-w-prose text-[13px] leading-relaxed text-ink-2">
          Upload a CSV with two columns — <strong>old email</strong> and <strong>new email</strong>. The server signs
          into each account with its saved session, changes its Flipkart login email to the new one, and fills the
          verification codes automatically. Flipkart sends a code to <em>both</em> the new and the old address (no
          phone needed for these accounts), and both are read from our mailboxes.
        </p>
        <p className="mt-2 max-w-prose text-[12px] leading-relaxed text-ink-2">
          Two things to know: the <strong>new email must be an address we can actually read</strong> — a registered
          InstAddr alias (catbar.net / apmail.org) or one of the askcaayush.com addresses, which are read from the
          Microsoft 365 inbox. A random unseen address never receives its code, so the swap can't complete. And a
          successful change <strong>signs the old session out</strong>, so the account is marked for re-login
          automatically once its email is swapped.
        </p>
      </header>

      {error ? <Alert tone="error">{error}</Alert> : null}
      {notice ? <Alert tone="success">{notice}</Alert> : null}

      <SwapOneCard
        onQueued={(row) => {
          setNotice(`Swap queued: ${row.old_email} → ${row.new_email}. It starts within a few seconds.`);
          void refresh();
        }}
      />

      <Card title="Or upload a CSV">
        <form className="flex flex-col gap-3" onSubmit={(e) => void upload(e)}>
          <div className="flex flex-wrap items-center gap-3">
            <input
              ref={fileRef}
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => void onFile(e.target.files?.[0] ?? null)}
              className="text-[13px] text-ink-2 file:mr-3 file:rounded-control file:border file:border-line file:bg-page file:px-3 file:py-1.5 file:text-[13px] file:font-medium file:text-ink hover:file:bg-surface"
            />
            {fileName ? <span className="text-[12px] text-ink-2">{fileName}</span> : null}
          </div>
          <details className="text-[12px] text-ink-2">
            <summary className="cursor-pointer select-none">Or paste CSV text</summary>
            <textarea
              value={csv}
              onChange={(e) => setCsv(e.target.value)}
              rows={5}
              placeholder={"old email,new email\nold1@catbar.net,new1@catbar.net"}
              className="mt-2 w-full rounded-control border border-line bg-surface p-2 font-mono text-[12px] text-ink"
            />
          </details>
          <div className="flex justify-end">
            <Button type="submit" variant="primary" size="sm" loading={uploading} disabled={!csv.trim()}>
              Queue swaps
            </Button>
          </div>
        </form>
      </Card>

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <Tile label="Total" value={summary.total} />
        <Tile label="Pending" value={summary.pending} />
        <Tile label="Running" value={summary.running} tone="var(--status-warning)" />
        <Tile label="Done" value={summary.done} tone="var(--status-good)" />
        <Tile label="Failed" value={summary.failed} tone="var(--status-bad)" />
      </section>

      <Card
        flush
        title="Swaps"
        actions={
          <div className="flex gap-2">
            <Button size="sm" variant="ghost" onClick={() => void refresh()}>
              Refresh
            </Button>
            <Button size="sm" variant="ghost" onClick={() => void runEmailSwaps().then(refresh, (e) => setError(errorMessage(e)))}>
              Run pending
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={summary.failed === 0}
              onClick={() => void retryFailedEmailSwaps().then(refresh, (e) => setError(errorMessage(e)))}
            >
              Retry failed
            </Button>
          </div>
        }
      >
        {busy ? (
          <div className="px-4 pb-2 pt-1 text-[12px] text-ink-2">
            Working… {summary.worker_running ? "a swap is in progress" : "queued"}. This page refreshes automatically.
          </div>
        ) : null}
        <Table
          caption="Email swaps"
          columns={columns}
          rows={rows}
          rowKey={(r) => r.id}
          dense
          minWidth="900px"
          empty="No swaps yet. Upload a CSV above to begin."
        />
      </Card>
    </div>
  );
}
