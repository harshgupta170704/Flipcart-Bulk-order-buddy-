/**
 * Users (spec FR-058, FR-059) — admin only. Who may sign in and as what.
 *
 * An admin adds a user with a temporary password (shown once, here, to hand
 * over), changes a name or role, takes a user out (their next request is
 * refused, not their next sign-in), and resets a forgotten password the same
 * way it was first set, which signs that user out everywhere. There is no
 * delete: a user who ran things stays on file as who ran them. The
 * configured admin's password is not reset here: the one set on the server
 * signs them in whatever happens.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { createUser, errorMessage, listUsers, resetUserPassword, updateUser } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatDateTime, formatRelative } from "../lib/format";
import type { AppUser, UserRole } from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Dialog } from "../components/Dialog";
import { EmptyState } from "../components/EmptyState";
import { Field, Select, TextInput } from "../components/Field";
import { RunError, RunLoading } from "../components/RunStates";
import { StatusBadge } from "../components/StatusBadge";
import { Table, type Column } from "../components/Table";
import { MIN_PASSWORD_CHARS } from "./ChangePassword";

const ROLE_LABEL: Record<UserRole, string> = { admin: "Admin", operator: "Operator" };
const ROLE_HELP: Record<UserRole, string> = {
  admin: "Manages users, addresses, GSTs, unit limits and settings — and everything an operator does.",
  operator: "Places orders, reads them back, uploads, compiles invoices and resolves alerts. Reads everything.",
};

/** A temporary password nobody has to think up: 14 unambiguous characters. */
function suggestPassword(): string {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789";
  const bytes = new Uint8Array(14);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

type Editor = { kind: "none" } | { kind: "new" } | { kind: "edit"; id: number } | { kind: "reset"; id: number };

/** The one-time reveal of a temporary password. */
interface Handover {
  email: string;
  password: string;
}

export default function Users() {
  const { email: myEmail } = useAuth();
  const [rows, setRows] = useState<AppUser[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editor, setEditor] = useState<Editor>({ kind: "none" });
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [handover, setHandover] = useState<Handover | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    document.title = "Users — Ordering Suite";
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await listUsers();
      if (!alive.current) return;
      setRows(next);
      setError(null);
    } catch (err) {
      if (alive.current) setError(errorMessage(err));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const edited = editor.kind === "edit" || editor.kind === "reset" ? (rows?.find((r) => r.id === editor.id) ?? null) : null;

  const setActive = async (row: AppUser, active: boolean) => {
    setBusy(true);
    setActionError(null);
    try {
      await updateUser(row.id, { active });
      if (!alive.current) return;
      setNote(active ? `${row.email} can sign in again.` : `${row.email} can no longer sign in — their next request is refused.`);
      await load();
    } catch (err) {
      if (alive.current) setActionError(errorMessage(err));
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const columns = useMemo<Column<AppUser>[]>(
    () => [
      {
        key: "email",
        header: "Email",
        render: (row) => (
          <span className="block">
            <span className="block font-medium text-ink">{row.email}</span>
            {row.name ? <span className="block text-[12px] text-ink-2">{row.name}</span> : null}
          </span>
        ),
      },
      {
        key: "role",
        header: "Role",
        width: "120px",
        render: (row) => (
          <span className="block">
            <span className="block">{ROLE_LABEL[row.role]}</span>
            {row.is_env_admin ? (
              <span className="block text-[12px] text-muted" title="Signs in with the password set on the server; that is their reset.">
                configured admin
              </span>
            ) : null}
          </span>
        ),
      },
      {
        key: "status",
        header: "Status",
        width: "170px",
        render: (row) =>
          !row.active ? (
            <StatusBadge status="failed" label="Deactivated" />
          ) : row.must_change_password ? (
            <StatusBadge status="pending" label="Temporary password" />
          ) : (
            <StatusBadge status="done" label="Active" />
          ),
      },
      {
        key: "last_login_at",
        header: "Last sign-in",
        width: "150px",
        render: (row) =>
          row.last_login_at ? (
            <span className="text-ink-2" title={formatDateTime(row.last_login_at)}>
              {formatRelative(row.last_login_at)}
            </span>
          ) : (
            <span className="text-muted">never</span>
          ),
      },
      {
        key: "actions",
        header: "",
        align: "right",
        width: "280px",
        render: (row) => (
          <span className="inline-flex items-center gap-1">
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditor({ kind: "edit", id: row.id })}>
              Edit
            </Button>
            {row.is_env_admin ? null : (
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditor({ kind: "reset", id: row.id })}>
                Reset password
              </Button>
            )}
            {row.email === myEmail || row.is_env_admin ? null : row.active ? (
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => void setActive(row, false)}>
                Deactivate
              </Button>
            ) : (
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => void setActive(row, true)}>
                Reactivate
              </Button>
            )}
          </span>
        ),
      },
    ],
    // The handlers read only their own row and the setters; `busy` and the
    // signed-in email decide what renders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [busy, myEmail],
  );

  const body = (() => {
    if (rows === null && error !== null) return <RunError title="Could not load the users" message={error} onRetry={load} />;
    if (rows === null) return <RunLoading label="Loading users…" />;
    return (
      <Table
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        caption="Users"
        minWidth="900px"
        empty={<EmptyState title="No users yet" description="Only the configured admin can sign in. Add the people who run orders." />}
      />
    );
  })();

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold leading-tight tracking-tight text-ink">Users</h1>
          <p className="mt-1 max-w-prose text-[13px] leading-relaxed text-ink-2">
            Who may sign in, and as what. Admins manage the setup; operators run the orders. Taking a
            user out keeps them on file as whoever ran what they ran.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={() => void load()} loading={loading && rows !== null}>
            Refresh
          </Button>
          <Button variant="primary" disabled={busy} onClick={() => setEditor({ kind: "new" })}>
            Add user
          </Button>
        </div>
      </header>

      <div aria-live="polite" className="flex flex-col gap-2 empty:hidden">
        {handover ? (
          <Alert tone="success" title={`Temporary password for ${handover.email}`}>
            <span className="block">
              Hand this over once — it is not shown again:{" "}
              <code className="rounded bg-page px-1.5 py-0.5 font-mono text-[13px] text-ink">{handover.password}</code>
            </span>
            <span className="mt-1 block text-ink-2">They will be asked to choose their own password when they sign in.</span>
            <Button size="sm" className="mt-2" variant="ghost" onClick={() => setHandover(null)}>
              Done
            </Button>
          </Alert>
        ) : null}
        {note ? <Alert tone="success">{note}</Alert> : null}
        {actionError ? (
          <Alert tone="error" title="That did not work">
            {actionError}
          </Alert>
        ) : null}
        {error !== null && rows !== null ? (
          <p role="status" className="text-[12px] text-ink-2">
            Showing the users that last loaded — the refresh failed: {error}
          </p>
        ) : null}
      </div>

      <Card title="Users" description="Everyone who can sign in to this deployment.">
        {body}
      </Card>

      <UserDialog
        editor={editor}
        user={edited}
        myEmail={myEmail}
        onClose={() => setEditor({ kind: "none" })}
        onDone={(message, reveal) => {
          setEditor({ kind: "none" });
          setNote(message);
          setActionError(null);
          if (reveal) setHandover(reveal);
          void load();
        }}
      />
    </div>
  );
}

/** Add, edit, or reset — one dialog, three shapes. */
function UserDialog({
  editor,
  user,
  myEmail,
  onClose,
  onDone,
}: {
  editor: Editor;
  user: AppUser | null;
  myEmail: string | null;
  onClose: () => void;
  onDone: (message: string, reveal?: Handover) => void;
}) {
  const open = editor.kind !== "none" && (editor.kind === "new" || user !== null);
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<UserRole>("operator");
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A fresh draft every time the dialog opens for something — and only then:
  // the list reloading behind an open dialog (the 30-second refresh, say)
  // hands in a new row object for the same user, which must not wipe what
  // the admin has typed.
  const userId = user?.id ?? null;
  useEffect(() => {
    if (!open) return;
    setEmail(user?.email ?? "");
    setName(user?.name ?? "");
    setRole(user?.role ?? "operator");
    setPassword(editor.kind === "new" || editor.kind === "reset" ? suggestPassword() : "");
    setError(null);
    setSaving(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editor.kind, userId]);

  const isSelf = user !== null && user.email === myEmail;
  const roleLocked = editor.kind === "edit" && (isSelf || (user?.is_env_admin ?? false));
  const title =
    editor.kind === "new"
      ? "Add a user"
      : editor.kind === "reset"
        ? `Reset the password of ${user?.email ?? ""}`
        : `Edit ${user?.email ?? ""}`;

  const canSave = (() => {
    if (saving) return false;
    if (editor.kind === "new") return email.trim() !== "" && password.length >= MIN_PASSWORD_CHARS;
    if (editor.kind === "reset") return password.length >= MIN_PASSWORD_CHARS;
    return true;
  })();

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!canSave) return;
    setSaving(true);
    setError(null);
    try {
      if (editor.kind === "new") {
        const created = await createUser({ email: email.trim(), name: name.trim(), role, temporary_password: password });
        onDone(`${created.email} added as ${ROLE_LABEL[created.role].toLowerCase()}.`, { email: created.email, password });
      } else if (editor.kind === "reset" && user) {
        await resetUserPassword(user.id, password);
        onDone(`The password of ${user.email} was reset.`, { email: user.email, password });
      } else if (editor.kind === "edit" && user) {
        const patch: { name: string; role?: UserRole } = { name: name.trim() };
        if (!roleLocked && role !== user.role) patch.role = role;
        const saved = await updateUser(user.id, patch);
        onDone(`${saved.email} saved.`);
      }
    } catch (err) {
      setError(errorMessage(err));
      setSaving(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={() => {
        if (!saving) onClose();
      }}
      title={title}
      width="560px"
      footer={
        <>
          <Button size="sm" variant="ghost" disabled={saving} onClick={onClose}>
            Cancel
          </Button>
          <Button size="sm" variant="primary" loading={saving} disabled={!canSave} onClick={() => void submit()}>
            {editor.kind === "new" ? "Add user" : editor.kind === "reset" ? "Reset password" : "Save"}
          </Button>
        </>
      }
    >
      <form className="flex flex-col gap-4" onSubmit={(e) => void submit(e)} noValidate>
        {error ? (
          <Alert tone="error" title="That did not work">
            {error}
          </Alert>
        ) : null}

        {editor.kind === "new" ? (
          <Field label="Email" required help="What they sign in with. Case does not matter.">
            <TextInput type="email" value={email} autoFocus autoComplete="off" disabled={saving} onChange={(e) => setEmail(e.target.value)} />
          </Field>
        ) : null}

        {editor.kind !== "reset" ? (
          <Field label="Name" help="Shown in the header and on the runs they start.">
            <TextInput value={name} autoComplete="off" disabled={saving} onChange={(e) => setName(e.target.value)} />
          </Field>
        ) : null}

        {editor.kind !== "reset" ? (
          <Field
            label="Role"
            help={
              roleLocked
                ? isSelf
                  ? "You cannot change your own role — another admin can."
                  : "The configured admin stays an admin."
                : ROLE_HELP[role]
            }
          >
            <Select value={role} disabled={saving || roleLocked} onChange={(e) => setRole(e.target.value as UserRole)}>
              <option value="operator">Operator</option>
              <option value="admin">Admin</option>
            </Select>
          </Field>
        ) : null}

        {editor.kind === "new" || editor.kind === "reset" ? (
          <Field
            label="Temporary password"
            required
            help={
              editor.kind === "reset"
                ? `At least ${MIN_PASSWORD_CHARS} characters. Shown once after saving, to hand over; they choose their own at their next sign-in. Resetting signs them out everywhere.`
                : `At least ${MIN_PASSWORD_CHARS} characters. Shown once after saving, to hand over; they choose their own at their next sign-in.`
            }
          >
            <div className="flex items-center gap-2">
              <TextInput value={password} autoComplete="off" spellCheck={false} disabled={saving} onChange={(e) => setPassword(e.target.value)} />
              <Button type="button" size="sm" variant="ghost" disabled={saving} onClick={() => setPassword(suggestPassword())}>
                Suggest
              </Button>
            </div>
          </Field>
        ) : null}
      </form>
    </Dialog>
  );
}
