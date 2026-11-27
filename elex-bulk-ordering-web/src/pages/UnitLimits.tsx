/**
 * Unit limits — how many units one account may buy of a category, brand, model
 * or variant inside a rolling window.
 *
 * These are the platform's rules, written down: 20 handsets per 31 days, 16
 * vivo per 31 days, 5 of one model per 30 days. An ordering run checks them
 * before it spends an account's quota, because the alternative is finding out
 * by tripping the cap — which gets orders rejected and accounts flagged.
 *
 * Two things here are deliberate. A rule is disabled rather than deleted, so
 * "we stopped enforcing this" stays readable instead of vanishing; and a blank
 * value is always rendered as "Any brand", because a wildcard rule that looks
 * like a half-filled row is one somebody tidies away.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { deleteUnitLimit, errorMessage, updateUnitLimit } from "../lib/api";
import { useAuth } from "../lib/auth";
import { countLabel, formatCount } from "../lib/format";
import type { UnitLimit } from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { LimitForm } from "../components/LimitForm";
import {
  limitValueLabel,
  scopeLabel,
  useUnitLimits,
} from "../components/LimitsData";
import { RunError, RunLoading } from "../components/RunStates";
import { StatusBadge } from "../components/StatusBadge";
import { Table, type Column } from "../components/Table";

/** Which add/edit panel is open. */
type Editor = { kind: "none" } | { kind: "new" } | { kind: "edit"; id: number };

const CLOSED: Editor = { kind: "none" };

export default function UnitLimits() {
  const limits = useUnitLimits();
  const reload = limits.reload;

  const [editor, setEditor] = useState<Editor>(CLOSED);
  const [deleting, setDeleting] = useState<number | null>(null);
  /** The single row mid-request, by id. */
  const [busyRow, setBusyRow] = useState<number | null>(null);
  /* Operators read the caps a run respects; only an admin sets them (FR-058). */
  const { isAdmin } = useAuth();
  const [note, setNote] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    document.title = "Unit limits — Ordering Suite";
  }, []);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const rows = limits.rows ?? [];

  const begin = (id: number) => {
    setBusyRow(id);
    setActionError(null);
    setNote(null);
  };

  const onSaved = (message: string) => {
    setEditor(CLOSED);
    setNote(message);
    setActionError(null);
    reload();
  };

  /* Enabled is a toggle, not a delete: the operator reads "Disabled" as a
     state of the rule, and retyping a cap from memory is how a wrong one gets
     saved. */
  const toggleEnabled = async (row: UnitLimit) => {
    begin(row.id);
    const name = limitValueLabel(row.scope, row.value);
    try {
      const saved = await updateUnitLimit(row.id, { enabled: !row.enabled });
      if (!alive.current) return;
      setNote(
        saved.enabled
          ? `“${name}” is enforced again — runs will check it before spending an account.`
          : `“${name}” is disabled. Runs will no longer check it, so quota under this rule is spent unchecked.`,
      );
      reload();
    } catch (err) {
      if (alive.current) setActionError(errorMessage(err));
    } finally {
      if (alive.current) setBusyRow(null);
    }
  };

  const removeLimit = async (row: UnitLimit) => {
    begin(row.id);
    const name = limitValueLabel(row.scope, row.value);
    try {
      await deleteUnitLimit(row.id);
      if (!alive.current) return;
      setDeleting(null);
      setNote(`“${name}” is gone — no run will check that cap again.`);
      reload();
    } catch (err) {
      if (alive.current) setActionError(errorMessage(err));
    } finally {
      if (alive.current) setBusyRow(null);
    }
  };

  const columns: Column<UnitLimit>[] = useMemo(
    () => [
      {
        key: "scope",
        header: "Scope",
        width: "130px",
        render: (row) => <span className="font-medium">{scopeLabel(row.scope)}</span>,
      },
      {
        key: "value",
        header: "Value",
        render: (row) =>
          row.value.trim() === "" ? (
            // Spelled out, never blank: this row caps everything in its scope.
            <span className="italic text-ink-2">{limitValueLabel(row.scope, row.value)}</span>
          ) : (
            <span>{row.value}</span>
          ),
      },
      {
        key: "max_units",
        header: "Max units",
        numeric: true,
        width: "120px",
        // Rendered by hand because the table prints 0 as an em dash, and a cap
        // of 0 — which blocks every account — must not read as "not set".
        render: (row) => formatCount(row.max_units),
      },
      {
        key: "window_days",
        header: "Window (days)",
        numeric: true,
        width: "140px",
        render: (row) => formatCount(row.window_days),
      },
      {
        key: "enabled",
        header: "Enabled",
        width: "210px",
        render: (row) => (
          <span className="inline-flex items-center gap-2">
            <StatusBadge
              status={row.enabled ? "done" : "skipped"}
              label={row.enabled ? "Enabled" : "Disabled"}
            />
            {isAdmin ? (
              <Button
                size="sm"
                variant="ghost"
                loading={busyRow === row.id}
                disabled={busyRow !== null}
                onClick={() => void toggleEnabled(row)}
              >
                {row.enabled ? "Disable" : "Enable"}
              </Button>
            ) : null}
          </span>
        ),
      },
      {
        key: "actions",
        header: "",
        align: "right",
        width: "150px",
        render: (row) =>
          isAdmin ? (
            <span className="inline-flex items-center gap-1">
              <Button
                size="sm"
                variant="ghost"
                disabled={busyRow !== null}
                onClick={() => {
                  setEditor({ kind: "edit", id: row.id });
                  setDeleting(null);
                  setActionError(null);
                }}
              >
                Edit
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={busyRow !== null}
                onClick={() => {
                  setDeleting(row.id);
                  setEditor(CLOSED);
                  setActionError(null);
                }}
              >
                Delete
              </Button>
            </span>
          ) : null,
      },
    ],
    // The handlers only read state setters and their own `row`, so `busyRow` —
    // which decides what the buttons render — and the role are the real deps.
    [busyRow, isAdmin],
  );

  const edited = editor.kind === "edit" ? (rows.find((row) => row.id === editor.id) ?? null) : null;
  const toDelete = deleting === null ? null : (rows.find((row) => row.id === deleting) ?? null);

  const enabledCount = rows.filter((row) => row.enabled).length;
  /* A cap below one unit leaves no room for anybody, so every matching account
     is skipped — worth saying out loud rather than leaving in a cell. */
  const blockAll = rows.filter((row) => row.enabled && row.max_units < 1);
  const refreshing = limits.loading && limits.rows !== null;

  /* The four states, decided in one place so an empty table can never stand in
     for a request still in the air. */
  const body = (() => {
    if (limits.rows === null && limits.error !== null) {
      return (
        <RunError
          title="Could not load the unit limits"
          message={limits.error}
          onRetry={reload}
        />
      );
    }
    if (limits.rows === null) {
      return <RunLoading label="Loading unit limits…" />;
    }
    return (
      <Table
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        caption="Unit limits"
        minWidth="920px"
        empty={
          <EmptyState
            title="No unit limit yet"
            description="Platforms cap how many units one account may buy of a category, brand or model in a rolling window — 20 handsets per 31 days, 16 of one brand, 5 of one model. With none here, a run spends every account's quota without checking, and the orders that cross a cap get rejected or flag the account. Add the caps you know about and runs will skip accounts that have nothing left."
            action={
              isAdmin ? (
                <Button variant="primary" onClick={() => setEditor({ kind: "new" })}>
                  Add the first limit
                </Button>
              ) : undefined
            }
          />
        }
      />
    );
  })();

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold leading-tight tracking-tight text-ink">
            Unit limits
          </h1>
          <p className="mt-1 max-w-prose text-[13px] leading-relaxed text-ink-2">
            How many units one account may order of something inside a rolling window. An ordering
            run checks these before it spends an account, so a cap costs you a skipped account
            rather than a rejected order.
          </p>
        </div>
        {/* Only a *re*load reads as refreshing — the first one is the panel's
            own loading state, shown where the data will appear. */}
        <Button onClick={reload} loading={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh"}
        </Button>
      </header>

      <div aria-live="polite" className="flex flex-col gap-2 empty:hidden">
        {note ? <Alert tone="success">{note}</Alert> : null}
        {actionError ? (
          <Alert tone="error" title="That did not work">
            {actionError}
          </Alert>
        ) : null}
        {limits.error !== null && limits.rows !== null ? (
          <p role="status" className="text-[12px] text-ink-2">
            Showing the limits that last loaded — the refresh failed: {limits.error}
          </p>
        ) : null}
      </div>

      {rows.length > 0 && enabledCount === 0 ? (
        <Alert tone="warning" title="Every limit is disabled">
          Nothing on this screen is being enforced, so runs will spend each account's quota without
          checking it. Enable the caps that still apply.
        </Alert>
      ) : null}

      {blockAll.length > 0 ? (
        <Alert tone="warning" title="A cap of 0 units blocks every account">
          {blockAll.map((row) => limitValueLabel(row.scope, row.value)).join(", ")}{" "}
          {blockAll.length === 1 ? "is" : "are"} enabled with no units allowed, so every run that
          matches {blockAll.length === 1 ? "it" : "them"} skips every account. Raise the cap to at
          least 1, or disable the rule.
        </Alert>
      ) : null}

      <Card
        title="Limits"
        description="One row per cap. Scope is what the platform counts — a category, a brand, a model or a single variant — and a blank value means every value in that scope. A variant limit always names one product id: the part after pid= in the product link."
        actions={
          isAdmin ? (
            <Button
              size="sm"
              variant="primary"
              disabled={editor.kind !== "none" || busyRow !== null}
              onClick={() => {
                setEditor({ kind: "new" });
                setDeleting(null);
                setActionError(null);
              }}
            >
              Add limit
            </Button>
          ) : (
            <span className="text-[12px] text-ink-2">Only an admin can change these.</span>
          )
        }
      >
        <div className="flex flex-col gap-4">
          {editor.kind === "new" || edited !== null ? (
            <LimitForm
              key={editor.kind === "edit" ? `limit-${editor.id}` : "limit-new"}
              limit={edited}
              onSaved={onSaved}
              onCancel={() => setEditor(CLOSED)}
            />
          ) : null}

          {toDelete !== null ? (
            <Alert
              tone="warning"
              title={`Delete “${limitValueLabel(toDelete.scope, toDelete.value)}”?`}
            >
              <span className="block">
                No run will check it again, so accounts will be spent against this cap without
                anyone noticing. If you only want to stop enforcing it, disable it instead — the
                numbers stay on file.
              </span>
              <span className="mt-2 flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  variant="danger"
                  loading={busyRow === toDelete.id}
                  onClick={() => void removeLimit(toDelete)}
                >
                  Delete limit
                </Button>
                {toDelete.enabled ? (
                  <Button
                    size="sm"
                    disabled={busyRow !== null}
                    onClick={() => {
                      setDeleting(null);
                      void toggleEnabled(toDelete);
                    }}
                  >
                    Disable it instead
                  </Button>
                ) : null}
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busyRow !== null}
                  onClick={() => setDeleting(null)}
                >
                  Keep it
                </Button>
              </span>
            </Alert>
          ) : null}

          {body}
        </div>
      </Card>

      {rows.length > 0 ? (
        <p className="text-[12px] text-ink-2">
          {countLabel(rows.length, "limit")} on file, {enabledCount} enforced. A run matches them
          against the brand and category you give it — leave those blank on the ordering form and
          only the all-scope limits can apply.
        </p>
      ) : null}
    </div>
  );
}
