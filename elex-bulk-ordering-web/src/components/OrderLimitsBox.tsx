/**
 * "Limits for this order", inside the order form's Platform Unit Limits: every
 * saved limit that applies to the chosen product, each on its own line, with
 * its units, days and an on/off tick that change it for this one order, and
 * lines added for this order only. Nothing here edits a saved limit; the
 * Unit Limits page does that.
 *
 * Changes are made only with Smart ID Selection on, and never in an AI run:
 * otherwise the saved limits are shown greyed, as they will be used. Someone
 * who is not an admin may tighten a limit but not loosen it (more units, fewer
 * days, or a line switched off); the server refuses that too.
 */
import { LIMIT_SCOPES, type ApplicableLimit, type LimitScope } from "../lib/types";
import {
  addableScopes,
  addedKey,
  addedValue,
  hasChanges,
  isChanged,
  MAX_WINDOW_DAYS,
  ONLY_ADMIN_LOOSENS,
  type AddedLimit,
  type LimitChangeErrors,
  type LimitChanges,
  type LimitEdit,
  type LineErrors,
  type ProductValues,
} from "../lib/limitChanges";
import type { RunCheckState } from "../lib/useRunCheck";
import { Button } from "./Button";
import { NumberInput, Select, TextInput } from "./Field";
import { limitName, limitRule, scopeLabel, scopeWord } from "./LimitsData";
import { Spinner } from "./Spinner";

export interface OrderLimitsBoxProps {
  /** The saved limits that apply to the product; null until they have been read. */
  applicable: ApplicableLimit[] | null;
  /** The form's room check, for what is said while the limits are read. */
  check: RunCheckState;
  onRetry: () => void;
  /** This order's changes, for the product as it stands. */
  changes: LimitChanges;
  onChange: (next: Pick<LimitChanges, "edits" | "added">) => void;
  /** Changes can be made and are sent: Smart ID Selection on, not an AI run. */
  active: boolean;
  /** Why not, said over the greyed saved limits when not active. */
  inactiveLine: string;
  isAdmin: boolean;
  product: ProductValues;
  /** "Variant limits cannot apply to this order: …", or null. */
  variantLine: string | null;
  errors: LimitChangeErrors | null;
  /** Every error is shown once Submit has been pressed; a loosening at once. */
  showErrors: boolean;
  disabled?: boolean;
}

/** Where a new line starts: a level with no saved limit that the product can
 *  fill, else a model name to type. */
const NEW_LINE_SCOPES: LimitScope[] = ["brand", "category", "variant", "seller", "model"];

function savedEdit(limit: ApplicableLimit): LimitEdit {
  return { maxUnits: String(limit.max_units), windowDays: String(limit.window_days), enabled: true };
}

function errorText(errors: LineErrors | undefined, showAll: boolean): string | null {
  if (!errors) return null;
  const parts = showAll ? [errors.value, errors.units, errors.days, errors.loosen] : [errors.loosen];
  const shown = parts.filter((part): part is string => Boolean(part));
  return shown.length ? [...new Set(shown)].join(" ") : null;
}

export function OrderLimitsBox({
  applicable,
  check,
  onRetry,
  changes,
  onChange,
  active,
  inactiveLine,
  isAdmin,
  product,
  variantLine,
  errors,
  showErrors,
  disabled = false,
}: OrderLimitsBoxProps) {
  const changed = hasChanges(changes, applicable);

  const editSaved = (limit: ApplicableLimit, patch: Partial<LimitEdit>) => {
    const current = changes.edits[limit.limit_id] ?? savedEdit(limit);
    onChange({ edits: { ...changes.edits, [limit.limit_id]: { ...current, ...patch } }, added: changes.added });
  };
  const editAdded = (key: number, patch: Partial<AddedLimit>) => {
    onChange({
      edits: changes.edits,
      added: changes.added.map((line) => (line.key === key ? { ...line, ...patch } : line)),
    });
  };
  /* A level no other added line has, and no saved limit already covers. */
  const addable = addableScopes(changes, applicable, product);
  const addLine = () => {
    const scope =
      NEW_LINE_SCOPES.find(
        (candidate) =>
          addable.includes(candidate) &&
          (candidate === "model" || addedValue({ scope: candidate, value: "" }, product) !== ""),
      ) ?? addable[0];
    if (scope === undefined) return;
    onChange({
      edits: changes.edits,
      added: [...changes.added, { key: addedKey(), scope, value: "", maxUnits: "", windowDays: "30" }],
    });
  };
  /* The server's own refusal of these changes: the order would meet it too. */
  const refused = check.kind === "error" && check.refusedChanges ? check.message : null;

  /* What is said while the applicable limits are not known: nothing to ask
     about yet, a failed check, or a check on its way (an answer still held
     about the product before this one counts as that too). */
  const reading =
    applicable !== null || refused !== null ? null : check.kind === "idle" ? (
      <p className="text-[12px] text-ink-2">The limits that apply show once there is a product and at least one ID.</p>
    ) : check.kind === "error" ? (
      <div className="flex flex-wrap items-center gap-2 text-[12px] text-ink-2">
        <span>Could not read the limits that apply: {check.message}</span>
        <Button size="sm" variant="ghost" onClick={onRetry}>
          Check again
        </Button>
      </div>
    ) : (
      <p className="flex items-center gap-2 text-[12px] text-ink-2">
        <Spinner size={12} />
        Reading the limits that apply to this product…
      </p>
    );

  return (
    <section
      aria-label="Limits for this order"
      className="flex flex-col gap-2.5 rounded-control border border-line bg-page px-3 py-3"
    >
      <h3 className="text-[13px] font-semibold text-ink">Limits for this order</h3>
      {variantLine ? <p className="text-[12px] leading-snug text-ink-2">{variantLine}</p> : null}
      {active ? (
        !isAdmin ? <p className="text-[12px] leading-snug text-ink-2">{ONLY_ADMIN_LOOSENS}</p> : null
      ) : (
        <p className="text-[12px] leading-snug text-ink-2">{inactiveLine}</p>
      )}
      {active && refused !== null ? (
        <p role="alert" className="text-[12px] leading-snug text-ink">
          {refused}
        </p>
      ) : null}

      {reading}

      {applicable !== null && applicable.length === 0 ? (
        <p className="text-[12px] text-ink-2">No saved limit applies to this product.</p>
      ) : null}

      {applicable !== null && applicable.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {applicable.map((limit) => {
            const name = limitName(limit.scope, limit.value);
            if (!active) {
              return (
                <li key={limit.limit_id} className="text-[12px] text-ink-2 opacity-60">
                  <span className="font-medium">{name}</span> ({limitRule(limit.max_units, limit.window_days)})
                </li>
              );
            }
            const edit = changes.edits[limit.limit_id] ?? savedEdit(limit);
            const lineChanged = isChanged(limit, changes.edits[limit.limit_id]);
            const problem = errorText(errors?.saved[limit.limit_id], showErrors);
            return (
              <li key={limit.limit_id} aria-label={name} className="flex flex-col gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  <label className="flex min-w-[150px] flex-1 items-center gap-2 text-[13px] text-ink">
                    <input
                      type="checkbox"
                      className="size-4 accent-accent"
                      aria-label={`Use ${name} for this order`}
                      checked={edit.enabled}
                      // Switching a line off loosens it: an admin's call.
                      disabled={disabled || (!isAdmin && edit.enabled)}
                      onChange={(event) => editSaved(limit, { enabled: event.target.checked })}
                    />
                    <span className={`font-medium ${edit.enabled ? "" : "text-muted line-through"}`}>{name}</span>
                    {lineChanged ? (
                      <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[11px] font-semibold text-accent">Changed</span>
                    ) : null}
                  </label>
                  <div className="w-[84px] shrink-0">
                    <NumberInput
                      aria-label={`Units: ${name}`}
                      min={1}
                      // Not past the saved cap for someone who may only tighten it.
                      max={isAdmin ? undefined : limit.max_units}
                      step={1}
                      value={edit.maxUnits}
                      disabled={disabled || !edit.enabled}
                      onChange={(event) => editSaved(limit, { maxUnits: event.target.value })}
                    />
                  </div>
                  <span className="text-[12px] text-ink-2">units per</span>
                  <div className="w-[84px] shrink-0">
                    <NumberInput
                      aria-label={`Days: ${name}`}
                      min={isAdmin ? 1 : limit.window_days}
                      max={MAX_WINDOW_DAYS}
                      step={1}
                      value={edit.windowDays}
                      disabled={disabled || !edit.enabled}
                      onChange={(event) => editSaved(limit, { windowDays: event.target.value })}
                    />
                  </div>
                  <span className="text-[12px] text-ink-2">days</span>
                </div>
                {lineChanged ? (
                  <p className="text-[11.5px] text-muted">Saved: {limitRule(limit.max_units, limit.window_days)}</p>
                ) : null}
                {problem ? (
                  <p role="alert" className="text-[12px] text-ink">
                    {problem}
                  </p>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}

      {active && changes.added.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {changes.added.map((line, index) => {
            const value = addedValue(line, product);
            const problem = errorText(errors?.added[line.key], showErrors);
            const label = `Added limit ${index + 1}`;
            return (
              <li key={line.key} aria-label={label} className="flex flex-col gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[11px] font-semibold text-accent">
                    This order only
                  </span>
                  <div className="w-[120px] shrink-0">
                    <Select
                      aria-label={`Scope: ${label}`}
                      value={line.scope}
                      disabled={disabled}
                      onChange={(event) => editAdded(line.key, { scope: event.target.value as LimitScope, value: "" })}
                    >
                      {/* Its own level, and the ones no other line has and no saved limit covers. */}
                      {LIMIT_SCOPES.filter(
                        (scope) => scope === line.scope || addableScopes(changes, applicable, product, line.key).includes(scope),
                      ).map((scope) => (
                        <option key={scope} value={scope}>
                          {scopeLabel(scope)}
                        </option>
                      ))}
                    </Select>
                  </div>
                  {line.scope === "model" ? (
                    <div className="w-[180px] shrink-0">
                      <TextInput
                        aria-label={`Model name: ${label}`}
                        placeholder="A name in the title"
                        autoComplete="off"
                        spellCheck={false}
                        maxLength={128}
                        value={line.value}
                        disabled={disabled}
                        onChange={(event) => editAdded(line.key, { value: event.target.value })}
                      />
                    </div>
                  ) : (
                    <span className="min-w-[120px] text-[13px] text-ink" title={`The product's ${scopeWord(line.scope)}`}>
                      {value || <span className="text-muted">No {scopeWord(line.scope)} yet</span>}
                    </span>
                  )}
                  <div className="w-[84px] shrink-0">
                    <NumberInput
                      aria-label={`Units: ${label}`}
                      min={1}
                      step={1}
                      placeholder="1"
                      value={line.maxUnits}
                      disabled={disabled}
                      onChange={(event) => editAdded(line.key, { maxUnits: event.target.value })}
                    />
                  </div>
                  <span className="text-[12px] text-ink-2">units per</span>
                  <div className="w-[84px] shrink-0">
                    <NumberInput
                      aria-label={`Days: ${label}`}
                      min={1}
                      max={MAX_WINDOW_DAYS}
                      step={1}
                      value={line.windowDays}
                      disabled={disabled}
                      onChange={(event) => editAdded(line.key, { windowDays: event.target.value })}
                    />
                  </div>
                  <span className="text-[12px] text-ink-2">days</span>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Remove ${label}`}
                    disabled={disabled}
                    onClick={() =>
                      onChange({ edits: changes.edits, added: changes.added.filter((other) => other.key !== line.key) })
                    }
                  >
                    ✕
                  </Button>
                </div>
                {problem ? (
                  <p role="alert" className="text-[12px] text-ink">
                    {problem}
                  </p>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}

      {active ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="secondary"
            disabled={disabled || addable.length === 0}
            title={addable.length === 0 ? "Every level already has a line for this order." : undefined}
            onClick={addLine}
          >
            Add a limit for this order
          </Button>
          {changed ? (
            <Button size="sm" variant="ghost" disabled={disabled} onClick={() => onChange({ edits: {}, added: [] })}>
              Use saved limits
            </Button>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

export default OrderLimitsBox;
