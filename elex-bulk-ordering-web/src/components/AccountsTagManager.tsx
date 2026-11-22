/**
 * Create, rename and delete tags.
 *
 * Tags are the filter vocabulary: everything else on the Accounts page — bulk
 * tagging, the pickers, "select all N matching" — narrows by them, so this is
 * where that vocabulary is managed. Deleting one never deletes an account; the
 * accounts holding it simply become untagged, and the copy says so.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, createTag, deleteTag, errorMessage, listTags, renameTag } from "../lib/api";
import { countLabel, formatCount } from "../lib/format";
import type { Tag } from "../lib/types";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Card } from "./Card";
import { EmptyState } from "./EmptyState";
import { Field, TextInput } from "./Field";
import { Spinner } from "./Spinner";

/** Tags are a managed vocabulary, but never render an unbounded list. */
const RENDER_LIMIT = 200;

export interface AccountsTagManagerProps {
  /** Called after any change, so the table and its filter options refresh. */
  onChanged: () => void;
  className?: string;
}

export function AccountsTagManager({ onChanged, className = "" }: AccountsTagManagerProps) {
  const [tags, setTags] = useState<Tag[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [rowBusyId, setRowBusyId] = useState<number | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(() => {
    setLoading(true);
    listTags().then(
      (rows) => {
        if (!alive.current) return;
        setTags(rows);
        setLoadError(null);
        setLoading(false);
      },
      (err: unknown) => {
        if (!alive.current) return;
        setLoadError(errorMessage(err));
        setLoading(false);
      },
    );
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onCreate = async () => {
    const trimmed = name.trim();
    if (trimmed === "") {
      setCreateError("Give the tag a name.");
      return;
    }
    setCreating(true);
    setCreateError(null);
    setNote(null);
    try {
      const created = await createTag(trimmed);
      if (!alive.current) return;
      setName("");
      setNote(`“${created.name}” is ready to use.`);
      load();
      onChanged();
    } catch (err) {
      if (!alive.current) return;
      setCreateError(
        err instanceof ApiError && err.status === 409
          ? `A tag called “${trimmed}” already exists.`
          : errorMessage(err),
      );
    } finally {
      if (alive.current) setCreating(false);
    }
  };

  const onRename = async (tag: Tag) => {
    const trimmed = editName.trim();
    if (trimmed === "" || trimmed === tag.name) {
      setEditingId(null);
      return;
    }
    setRowBusyId(tag.id);
    setRowError(null);
    try {
      const updated = await renameTag(tag.id, trimmed);
      if (!alive.current) return;
      setEditingId(null);
      setNote(`“${tag.name}” is now “${updated.name}”.`);
      load();
      onChanged();
    } catch (err) {
      if (!alive.current) return;
      setRowError(
        err instanceof ApiError && err.status === 409
          ? `A tag called “${trimmed}” already exists.`
          : errorMessage(err),
      );
    } finally {
      if (alive.current) setRowBusyId(null);
    }
  };

  const onDelete = async (tag: Tag) => {
    setRowBusyId(tag.id);
    setRowError(null);
    try {
      await deleteTag(tag.id);
      if (!alive.current) return;
      setConfirmDeleteId(null);
      setNote(
        tag.account_count > 0
          ? `“${tag.name}” is gone — its ${countLabel(tag.account_count, "account")} ${
              tag.account_count === 1 ? "is" : "are"
            } now untagged.`
          : `“${tag.name}” is gone.`,
      );
      load();
      onChanged();
    } catch (err) {
      if (!alive.current) return;
      setRowError(errorMessage(err));
    } finally {
      if (alive.current) setRowBusyId(null);
    }
  };

  const rows = tags ?? [];
  const visible = rows.slice(0, RENDER_LIMIT);
  const hidden = rows.length - visible.length;

  const body = (() => {
    if (loading && tags === null) {
      return (
        <div role="status" className="flex items-center gap-2.5 py-4 text-[13px] text-ink-2">
          <Spinner size={16} />
          <span>Loading tags…</span>
        </div>
      );
    }
    if (tags === null) {
      return (
        <div className="flex flex-col items-start gap-3">
          <Alert tone="error" title="Could not load the tags" className="w-full">
            {loadError ?? "Something went wrong."}
          </Alert>
          <Button variant="primary" onClick={load} loading={loading}>
            Retry
          </Button>
        </div>
      );
    }
    if (rows.length === 0) {
      return (
        <EmptyState
          title="No tags yet"
          description="A tag is how you pick out a group of accounts later — one entity, one batch, one campaign. Create the first one above."
        />
      );
    }
    return (
      <div className="flex flex-col gap-2">
        <ul className="divide-y divide-line rounded-card border border-line">
          {visible.map((tag) => {
            const busy = rowBusyId === tag.id;
            const editing = editingId === tag.id;
            const confirming = confirmDeleteId === tag.id;
            const editId = `tag-rename-${tag.id}`;
            return (
              <li key={tag.id} className="flex flex-col gap-2 px-3 py-2.5">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                  {editing ? (
                    <>
                      <label htmlFor={editId} className="sr-only">
                        New name for {tag.name}
                      </label>
                      <TextInput
                        id={editId}
                        value={editName}
                        disabled={busy}
                        autoComplete="off"
                        maxLength={64}
                        className="h-8 max-w-[220px] text-[13px]"
                        onChange={(event) => setEditName(event.target.value)}
                      />
                      <Button
                        size="sm"
                        variant="primary"
                        loading={busy}
                        onClick={() => void onRename(tag)}
                      >
                        Save
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() => {
                          setEditingId(null);
                          setRowError(null);
                        }}
                      >
                        Cancel
                      </Button>
                    </>
                  ) : (
                    <>
                      <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink">
                        {tag.name}
                      </span>
                      <span className="whitespace-nowrap text-[12px] tabular-nums text-muted">
                        {formatCount(tag.account_count)}{" "}
                        {tag.account_count === 1 ? "account" : "accounts"}
                      </span>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() => {
                          setEditingId(tag.id);
                          setEditName(tag.name);
                          setConfirmDeleteId(null);
                          setRowError(null);
                        }}
                      >
                        Rename
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() => {
                          setConfirmDeleteId(tag.id);
                          setEditingId(null);
                          setRowError(null);
                        }}
                      >
                        Delete
                      </Button>
                    </>
                  )}
                </div>

                {confirming ? (
                  <Alert tone="warning" title={`Delete “${tag.name}”?`}>
                    <span className="block">
                      {tag.account_count > 0
                        ? `The ${countLabel(tag.account_count, "account")} holding it become untagged. No account is deleted.`
                        : "Nothing is using it."}
                    </span>
                    <span className="mt-2 flex flex-wrap items-center gap-2">
                      <Button
                        size="sm"
                        variant="danger"
                        loading={busy}
                        onClick={() => void onDelete(tag)}
                      >
                        Delete tag
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() => setConfirmDeleteId(null)}
                      >
                        Keep it
                      </Button>
                    </span>
                  </Alert>
                ) : null}
              </li>
            );
          })}
        </ul>
        {hidden > 0 ? (
          <p className="text-[12px] text-ink-2">
            {formatCount(hidden)} more not shown. Tags are meant to be a short list — the filter
            bar's tag menu still offers every one of them.
          </p>
        ) : null}
      </div>
    );
  })();

  return (
    <Card
      title="Tags"
      description="One tag per account. Tags are what “select all matching” selects by, so a run can be aimed at a whole group without listing its accounts."
      className={className}
      actions={
        <Button size="sm" onClick={load} loading={loading && tags !== null}>
          Refresh
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end gap-3">
          <Field
            id="tag-new-name"
            label="New tag"
            className="min-w-[200px] flex-1"
            error={createError ?? undefined}
            help="e.g. an entity name, a batch, or whatever you group runs by."
          >
            <TextInput
              value={name}
              disabled={creating}
              autoComplete="off"
              spellCheck={false}
              maxLength={64}
              placeholder="GLOBE COMMERCE"
              onChange={(event) => {
                setName(event.target.value);
                if (createError) setCreateError(null);
              }}
            />
          </Field>
          <Button variant="primary" loading={creating} onClick={() => void onCreate()}>
            {creating ? "Creating…" : "Create tag"}
          </Button>
        </div>

        <div aria-live="polite" className="flex flex-col gap-2 empty:hidden">
          {note ? <Alert tone="success">{note}</Alert> : null}
          {rowError ? (
            <Alert tone="error" title="That did not work">
              {rowError}
            </Alert>
          ) : null}
          {loadError && tags !== null ? (
            <p role="status" className="text-[12px] text-ink-2">
              Showing the tags that last loaded — the refresh failed: {loadError}
            </p>
          ) : null}
        </div>

        {body}
      </div>
    </Card>
  );
}

export default AccountsTagManager;
