/**
 * The add/edit panel for one unit limit.
 *
 * It validates before asking the server because the mistake that matters here
 * is a silent one: a limit of 0 units would block every account in every run,
 * and a window of 0 days would count nothing and protect nobody. The duplicate
 * (scope, value) pair comes back as a 409 and lands on the value field, which
 * is where the duplicate is.
 */
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { ApiError, createUnitLimit, errorMessage, updateUnitLimit } from "../lib/api";
import { LIMIT_SCOPES, type LimitScope, type UnitLimit, type UnitLimitInput } from "../lib/types";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Checkbox, Field, NumberInput, Select, TextInput } from "./Field";
import { limitValueLabel, scopeLabel, scopeWord } from "./LimitsData";

const PANEL = "flex flex-col gap-4 rounded-card border border-line bg-page p-4";

/** The server's own floors, checked here so a 422 is the rare case. */
export const MIN_MAX_UNITS = 1;
export const MIN_WINDOW_DAYS = 1;

/** A Flipkart product id, the pid in a product link: what a variant limit's
 *  value must be (the server refuses a blank or malformed one). */
export const PRODUCT_ID = /^[A-Za-z0-9]{6,40}$/;
const VARIANT_HINT = "The product's Flipkart id: the part after pid= in its link, for example MOBHNDTD57UJUK9Q.";

interface LimitDraft {
  scope: LimitScope;
  value: string;
  maxUnits: string;
  windowDays: string;
  enabled: boolean;
}

type LimitErrors = Partial<Record<keyof LimitDraft, string>>;

function limitDraft(limit: UnitLimit | null): LimitDraft {
  if (limit === null) {
    // Brand, 31 days: the shape of the caps these rules exist to mirror.
    return { scope: "brand", value: "", maxUnits: "", windowDays: "31", enabled: true };
  }
  return {
    scope: limit.scope,
    value: limit.value,
    maxUnits: String(limit.max_units),
    windowDays: String(limit.window_days),
    enabled: limit.enabled,
  };
}

function validateLimit(draft: LimitDraft): LimitErrors {
  const errors: LimitErrors = {};

  // A blank variant would count every order of the account, so a variant
  // limit always names one product id.
  if (draft.scope === "variant") {
    const value = draft.value.trim();
    if (value === "") {
      errors.value = "A variant limit needs the product's Flipkart id (the pid in its link).";
    } else if (!PRODUCT_ID.test(value)) {
      errors.value = "That is not a Flipkart product id: 6 to 40 letters and digits, like MOBHNDTD57UJUK9Q.";
    }
  }

  const maxUnits = Number(draft.maxUnits);
  if (draft.maxUnits.trim() === "") {
    errors.maxUnits = "Enter how many units one account may order in the window.";
  } else if (!Number.isInteger(maxUnits) || maxUnits < MIN_MAX_UNITS) {
    errors.maxUnits = `A whole number, ${MIN_MAX_UNITS} or more. A cap of 0 would block every account.`;
  }

  const windowDays = Number(draft.windowDays);
  if (draft.windowDays.trim() === "") {
    errors.windowDays = "Enter the rolling window in days.";
  } else if (!Number.isInteger(windowDays) || windowDays < MIN_WINDOW_DAYS) {
    errors.windowDays = `A whole number of days, ${MIN_WINDOW_DAYS} or more. A window of 0 counts nothing.`;
  }

  return errors;
}

export interface LimitFormProps {
  /** The row being edited, or null to add one. */
  limit: UnitLimit | null;
  /** Called with a sentence for the page's status line once the row is saved. */
  onSaved: (message: string) => void;
  onCancel: () => void;
  className?: string;
}

export function LimitForm({ limit, onSaved, onCancel, className = "" }: LimitFormProps) {
  const editing = limit !== null;
  const [draft, setDraft] = useState<LimitDraft>(() => limitDraft(limit));
  const [showErrors, setShowErrors] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [duplicate, setDuplicate] = useState<string | null>(null);

  /* A save closes the panel, so the reply can land after unmount. */
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const errors = useMemo(() => validateLimit(draft), [draft]);
  const shown = showErrors ? errors : {};

  const set = <K extends keyof LimitDraft>(key: K, value: LimitDraft[K]) => {
    setDraft((prev) => ({ ...prev, [key]: value }));
    setSaveError(null);
    // The 409 is about the (scope, value) pair, so either field clears it.
    if (key === "scope" || key === "value") setDuplicate(null);
  };

  const variant = draft.scope === "variant";
  /* A blank value caps every value in its scope, except for a variant: that
     one always names a product id. */
  const wildcard = draft.value.trim() === "" && !variant;

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saving) return;
    setShowErrors(true);
    setSaveError(null);
    setDuplicate(null);
    if (Object.keys(errors).length > 0) return;

    const payload: UnitLimitInput = {
      scope: draft.scope,
      // A product id is kept upper-case, as the run reads it off the link.
      value: variant ? draft.value.trim().toUpperCase() : draft.value.trim(),
      max_units: Number(draft.maxUnits),
      window_days: Number(draft.windowDays),
      enabled: draft.enabled,
    };

    setSaving(true);
    try {
      const saved =
        limit === null
          ? await createUnitLimit(payload)
          : await updateUnitLimit(limit.id, payload);
      const name = limitValueLabel(saved.scope, saved.value);
      onSaved(
        editing
          ? `“${name}” saved — ${saved.max_units} units per ${saved.window_days} days.`
          : `“${name}” is now capped at ${saved.max_units} units per ${saved.window_days} days.`,
      );
    } catch (err) {
      if (!alive.current) return;
      if (err instanceof ApiError && err.status === 409) {
        setDuplicate(
          wildcard
            ? `A limit covering every ${scopeWord(draft.scope)} already exists. Edit that one instead of adding a second.`
            : `A ${scopeWord(draft.scope)} limit for “${draft.value.trim()}” already exists. Edit that one instead of adding a second.`,
        );
      } else {
        setSaveError(errorMessage(err));
      }
    } finally {
      if (alive.current) setSaving(false);
    }
  };

  return (
    <form className={`${PANEL} ${className}`} onSubmit={(event) => void onSubmit(event)} noValidate>
      <p className="text-[13px] font-semibold text-ink">
        {editing ? `Edit “${limitValueLabel(limit.scope, limit.value)}”` : "New unit limit"}
      </p>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Scope" required help="What the platform counts this cap against.">
          <Select
            value={draft.scope}
            disabled={saving}
            onChange={(event) => set("scope", event.target.value as LimitScope)}
          >
            {LIMIT_SCOPES.map((scope) => (
              <option key={scope} value={scope}>
                {scopeLabel(scope)}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label="Value"
          required={variant}
          error={duplicate ?? shown.value}
          help={
            variant
              ? VARIANT_HINT
              : wildcard
                ? `Blank on purpose: this caps every ${scopeWord(draft.scope)}, which is the “20 handsets a month” kind of rule.`
                : `Matched against the order's ${scopeWord(draft.scope)}, ignoring case.`
          }
        >
          <TextInput
            value={draft.value}
            disabled={saving}
            autoComplete="off"
            spellCheck={false}
            maxLength={128}
            placeholder={variant ? "MOBHNDTD57UJUK9Q" : `Any ${scopeWord(draft.scope)}`}
            onChange={(event) => set("value", event.target.value)}
          />
        </Field>

        <Field
          label="Max units"
          required
          error={shown.maxUnits}
          help="How many units ONE account may order inside the window."
        >
          <NumberInput
            min={MIN_MAX_UNITS}
            step={1}
            inputMode="numeric"
            placeholder="16"
            value={draft.maxUnits}
            disabled={saving}
            onChange={(event) => set("maxUnits", event.target.value)}
          />
        </Field>

        <Field
          label="Window (days)"
          required
          error={shown.windowDays}
          help="The rolling window the units are counted over, e.g. 31."
        >
          <NumberInput
            min={MIN_WINDOW_DAYS}
            step={1}
            inputMode="numeric"
            value={draft.windowDays}
            disabled={saving}
            onChange={(event) => set("windowDays", event.target.value)}
          />
        </Field>
      </div>

      <Checkbox
        label="Enforce this limit on every run"
        checked={draft.enabled}
        disabled={saving}
        help={
          draft.enabled
            ? "Accounts with nothing left under it are skipped instead of being spent."
            : "Kept for reference but ignored: runs will spend quota under this rule without checking it."
        }
        onChange={(event) => set("enabled", event.target.checked)}
      />

      <div aria-live="polite" className="empty:hidden">
        {saveError ? (
          <Alert tone="error" title="Could not save this limit">
            {saveError}
          </Alert>
        ) : null}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" variant="primary" loading={saving}>
          {saving ? "Saving…" : editing ? "Save limit" : "Add limit"}
        </Button>
        <Button variant="ghost" disabled={saving} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

export default LimitForm;
