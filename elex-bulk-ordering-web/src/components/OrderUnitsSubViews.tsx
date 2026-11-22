import { useCallback, useEffect, useState } from "react";
import { cancelJob, errorMessage, listJobs, listJobTasks } from "../lib/api";
import { formatDateTime } from "../lib/format";
import type { Job, JobTaskItem } from "../lib/types";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Card } from "./Card";
import { Field, Select, TextInput } from "./Field";
import { StatusBadge } from "./StatusBadge";
import { Table, type Column } from "./Table";

function durationText(start: string | null, end: string | null): string {
  if (!start || !end) return "—";
  const ms = Math.max(0, new Date(end).getTime() - new Date(start).getTime());
  const secs = Math.round(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  return `${mins}m ${secs % 60}s`;
}

export function BatchRequestsView() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setJobs(await listJobs(200));
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const filtered = jobs.filter((j) => {
    if (typeFilter && j.type !== typeFilter) return false;
    if (statusFilter && j.status !== statusFilter) return false;
    return true;
  });

  const columns: Column<Job>[] = [
    { key: "id", header: "Request", render: (j) => `#${j.id} · ${j.type}` },
    {
      key: "source",
      header: "Source",
      render: (j) => {
        const p = (j.payload ?? {}) as Record<string, unknown>;
        return String(p.mode ?? p.source ?? "manual");
      },
    },
    { key: "status", header: "Status", render: (j) => <StatusBadge status={j.status} /> },
    {
      key: "progress",
      header: "Progress",
      render: (j) => `${j.succeeded + j.failed}/${j.total} (${j.succeeded} ok, ${j.failed} fail)`,
    },
    { key: "total", header: "Tasks", numeric: true },
    { key: "started_at", header: "Started", render: (j) => (j.started_at ? formatDateTime(j.started_at) : "—") },
    { key: "finished_at", header: "Finished", render: (j) => (j.finished_at ? formatDateTime(j.finished_at) : "—") },
    { key: "duration", header: "Duration", render: (j) => durationText(j.started_at, j.finished_at) },
    {
      key: "actions",
      header: "Actions",
      render: (j) =>
        j.status === "running" || j.status === "queued" ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => void cancelJob(j.id).then(load, (e) => setError(errorMessage(e)))}
          >
            Cancel
          </Button>
        ) : (
          <span className="text-[12px] text-muted">—</span>
        ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      {error ? <Alert tone="error">{error}</Alert> : null}
      <Card title="Filters">
        <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-4">
          <Field label="Task type">
            <Select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
              <option value="">All types</option>
              <option value="order">Order</option>
              <option value="sync">Sync / Read-back</option>
              <option value="login">Login</option>
              <option value="coupon">Coupon</option>
              <option value="compile">Compile invoices</option>
            </Select>
          </Field>
          <Field label="Status">
            <Select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              <option value="">All statuses</option>
              <option value="queued">Queued</option>
              <option value="running">Running</option>
              <option value="completed">Completed</option>
              <option value="failed">Failed</option>
            </Select>
          </Field>
          <div className="flex items-end">
            <Button size="sm" loading={loading} onClick={() => void load()}>
              Refresh
            </Button>
          </div>
        </div>
      </Card>
      <Card flush title={`Batch requests (${filtered.length})`}>
        <Table
          caption="Batch requests"
          columns={columns}
          rows={filtered}
          rowKey={(j) => j.id}
          dense
          minWidth="950px"
          empty="No batch requests found."
        />
      </Card>
    </div>
  );
}

export function AllTasksView() {
  const [items, setItems] = useState<JobTaskItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [email, setEmail] = useState("");
  const [jobType, setJobType] = useState("");
  const [status, setStatus] = useState("");
  const [errorQ, setErrorQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listJobTasks({ page, size: 50, email, job_type: jobType, status, error_q: errorQ });
      setItems(res.items);
      setTotal(res.total);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [page, email, jobType, status, errorQ]);

  useEffect(() => {
    void load();
  }, [load]);

  const columns: Column<JobTaskItem>[] = [
    { key: "id", header: "Task", render: (r) => `#${r.id} (${r.job_type})` },
    { key: "source", header: "Source" },
    { key: "email", header: "Email", render: (r) => r.email || "—" },
    { key: "status", header: "Status", render: (r) => <StatusBadge status={r.status} /> },
    { key: "started_at", header: "Started", render: (r) => (r.started_at ? formatDateTime(r.started_at) : "—") },
    { key: "finished_at", header: "Finished", render: (r) => (r.finished_at ? formatDateTime(r.finished_at) : "—") },
    { key: "duration", header: "Duration", render: (r) => durationText(r.started_at, r.finished_at) },
    { key: "message", header: "Message / Error", render: (r) => r.message || "—" },
    { key: "triggered_by", header: "Triggered by", render: (r) => r.triggered_by || "—" },
    { key: "job_id", header: "Request", render: (r) => `#${r.job_id}` },
  ];

  const totalPages = Math.max(1, Math.ceil(total / 50));

  return (
    <div className="flex flex-col gap-4">
      {error ? <Alert tone="error">{error}</Alert> : null}
      <Card title="Task Filters">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Field label="Email">
            <TextInput value={email} onChange={(e) => { setEmail(e.target.value); setPage(1); }} placeholder="Filter by email…" />
          </Field>
          <Field label="Task type">
            <Select value={jobType} onChange={(e) => { setJobType(e.target.value); setPage(1); }}>
              <option value="">All types</option>
              <option value="order">Order</option>
              <option value="sync">Sync</option>
              <option value="login">Login</option>
              <option value="coupon">Coupon</option>
            </Select>
          </Field>
          <Field label="Status">
            <Select value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }}>
              <option value="">All statuses</option>
              <option value="pending">Pending</option>
              <option value="running">Running</option>
              <option value="done">Done</option>
              <option value="failed">Failed</option>
              <option value="skipped">Skipped</option>
              <option value="unconfirmed">Unconfirmed</option>
            </Select>
          </Field>
          <Field label="Error / Message">
            <TextInput value={errorQ} onChange={(e) => { setErrorQ(e.target.value); setPage(1); }} placeholder="Search error text…" />
          </Field>
          <div className="flex items-end">
            <Button size="sm" loading={loading} onClick={() => void load()}>
              Refresh
            </Button>
          </div>
        </div>
      </Card>
      <Card
        flush
        title={`All tasks (${total})`}
        actions={
          <div className="flex items-center gap-2 text-[12.5px] text-ink-2">
            <Button size="sm" variant="ghost" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
              Previous
            </Button>
            <span>
              Page {page} of {totalPages}
            </span>
            <Button size="sm" variant="ghost" disabled={page >= totalPages} onClick={() => setPage((p) => Math.min(totalPages, p + 1))}>
              Next
            </Button>
          </div>
        }
      >
        <Table
          caption="All tasks"
          columns={columns}
          rows={items}
          rowKey={(r) => r.id}
          dense
          minWidth="1100px"
          empty="No task items found."
        />
      </Card>
    </div>
  );
}
