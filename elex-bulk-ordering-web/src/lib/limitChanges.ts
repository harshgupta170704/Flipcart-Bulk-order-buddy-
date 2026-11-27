/**
 * The order's variant and this order's changes to the unit limits (Smart ID
 * Selection, part 2). Pure functions, so the rules the order form applies,
 * what it sends and what a run's settings say can be tested without a page.
 *
 * A change never edits a saved limit: a change to a saved one is tied to it by
 * its id and touches only its units, days and on/off, for this one order; an
 * added line is a limit for this one order. Both go to the server as
 * `limit_overrides`, with the checks and with the order.
 */
import { LIMIT_SCOPES, type ApplicableLimit, type LimitOverride, type LimitScope } from "./types";

/* --- the variant ----------------------------------------------------------- */

/** A Flipkart product id, the pid in a product link: letters and digits. */
const PRODUCT_ID = /^[A-Za-z0-9]{6,40}$/;

/** The product link's pid, upper-cased, when it is a usable product id; else null. */
export function pidFromLink(link: string): string | null {
  let url: URL;
  try {
    url = new URL(link.trim());
  } catch {
    return null;
  }
  const pid = (url.searchParams.get("pid") ?? "").trim();
  return PRODUCT_ID.test(pid) ? pid.toUpperCase() : null;
}

export interface OrderVariant {
  /** The variant id the checks send; null when the order names none. */
  variant: string | null;
  /** Why not, when there is a product: said on the form in one line. */
  reason: "the link names no variant" | "a size is picked" | "it has several products" | null;
}

/**
 * The exact variant this order buys, as the run will record it: the product
 * link's pid. None when a size is picked (the run then buys another variant
 * than the link names), when the order has several products ("+ Add product
 * URL"), or when the link names no variant (a short link, say). Nothing at all
 * without a product link.
 */
export function orderVariant(input: {
  /** The product link, or "" while there is no usable one. */
  productLink: string;
  extraLinks?: readonly string[];
  size?: string | null;
}): OrderVariant {
  if (input.productLink.trim() === "") return { variant: null, reason: null };
  if ((input.extraLinks ?? []).some((link) => link.trim() !== "")) {
    return { variant: null, reason: "it has several products" };
  }
  if (input.size?.trim()) return { variant: null, reason: "a size is picked" };
  const pid = pidFromLink(input.productLink);
  return pid ? { variant: pid, reason: null } : { variant: null, reason: "the link names no variant" };
}

/** "Variant limits cannot apply to this order: a size is picked." */
export function variantNote(variant: OrderVariant): string | null {
  return variant.reason ? `Variant limits cannot apply to this order: ${variant.reason}.` : null;
}

/* --- this order's changes ----------------------------------------------------- */

/** A change to one saved limit for this order, as typed. */
export interface LimitEdit {
  maxUnits: string;
  windowDays: string;
  enabled: boolean;
}

/**
 * A limit added for this order only, as typed. Only a model line keeps a value
 * of its own (a name the product's title contains); the others take the
 * product's own value as it stands (see `addedValue`).
 */
export interface AddedLimit {
  key: number;
  scope: LimitScope;
  value: string;
  maxUnits: string;
  windowDays: string;
}

export interface LimitChanges {
  /** The product link the changes were made for: they apply to it alone. */
  forLink: string;
  /** Changes to saved limits, by the limit's id. */
  edits: Record<number, LimitEdit>;
  added: AddedLimit[];
}

export const NO_LIMIT_CHANGES: LimitChanges = { forLink: "", edits: {}, added: [] };

/** The longest window a limit may have, for one order as for a saved one. */
export const MAX_WINDOW_DAYS = 3650;

let lastKey = 0;
/** A key for a new added line, unique for the page's life. */
export function addedKey(): number {
  lastKey += 1;
  return lastKey;
}

/** What the product says, for the lines that take its value. */
export interface ProductValues {
  brand: string;
  category: string;
  /** The seller the run buys from ("" for Flipkart's default). */
  seller: string;
  variant: string | null;
  /** The product's title, which a model line must be part of; "" until read. */
  title: string;
}

/** The value an added line goes with: the product's own, or the model name typed. */
export function addedValue(line: Pick<AddedLimit, "scope" | "value">, product: ProductValues): string {
  switch (line.scope) {
    case "model":
      return line.value.trim();
    case "brand":
      return product.brand.trim();
    case "category":
      return product.category.trim();
    case "seller":
      return product.seller.trim();
    case "variant":
      return product.variant ?? "";
  }
}

function whole(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const value = Number(trimmed);
  return Number.isInteger(value) ? value : null;
}

/** Whether a saved limit's line says anything other than the saved limit. */
export function isChanged(saved: ApplicableLimit, edit: LimitEdit | undefined): boolean {
  if (edit === undefined) return false;
  return !edit.enabled || whole(edit.maxUnits) !== saved.max_units || whole(edit.windowDays) !== saved.window_days;
}

/** Changes to saved limits that still apply, and added lines. With the
 *  applicable limits not known yet, every edit counts. */
export function hasChanges(changes: LimitChanges, applicable: ApplicableLimit[] | null): boolean {
  if (changes.added.length > 0) return true;
  if (applicable === null) return Object.keys(changes.edits).length > 0;
  return applicable.some((limit) => isChanged(limit, changes.edits[limit.limit_id]));
}

/* --- checking the box ---------------------------------------------------------- */

export interface LineErrors {
  units?: string;
  days?: string;
  value?: string;
  /** A change only an admin may make. */
  loosen?: string;
}

export interface LimitChangeErrors {
  /** By saved limit id. */
  saved: Record<number, LineErrors>;
  /** By added line key. */
  added: Record<number, LineErrors>;
}

export const ONLY_ADMIN_LOOSENS = "Only an admin can loosen a limit for one order.";

function unitsError(text: string): string | undefined {
  const value = whole(text);
  return value === null || value < 1 ? "Units: a whole number, 1 or more." : undefined;
}

function daysError(text: string): string | undefined {
  const value = whole(text);
  return value === null || value < 1 || value > MAX_WINDOW_DAYS
    ? `Days: a whole number from 1 to ${MAX_WINDOW_DAYS}.`
    : undefined;
}

const NO_VALUE: Record<LimitScope, string> = {
  brand: "Fill in Brand above: a brand line takes the product's brand.",
  category: "Fill in Category above: a category line takes the product's category.",
  seller: "Choose a seller in 1. Product: a seller line takes the run's seller.",
  variant: "This order names no variant, so a variant line cannot apply.",
  model: "Type a name the product's title contains.",
};

/** The applicable saved limit with this scope and value (ignoring case), if any. */
function savedLimitFor(scope: LimitScope, value: string, applicable: ApplicableLimit[] | null): ApplicableLimit | undefined {
  const wanted = value.trim().toLowerCase();
  if (wanted === "") return undefined;
  return (applicable ?? []).find((limit) => limit.scope === scope && limit.value.trim().toLowerCase() === wanted);
}

/**
 * The scopes a line added for this order may take, in the usual order: none
 * another added line already has (`forKey` is the line asking, which keeps its
 * own), and none whose product value a saved limit that applies already
 * covers (a saved "brand vivo" leaves no room for an added brand line for
 * vivo: that one is changed instead). A model name is typed, so only another
 * model line rules a model line out.
 */
export function addableScopes(
  changes: LimitChanges,
  applicable: ApplicableLimit[] | null,
  product: ProductValues,
  forKey?: number,
): LimitScope[] {
  return LIMIT_SCOPES.filter((scope) => {
    if (changes.added.some((line) => line.key !== forKey && line.scope === scope)) return false;
    if (scope === "model") return true;
    return savedLimitFor(scope, addedValue({ scope, value: "" }, product), applicable) === undefined;
  });
}

function valueError(
  line: AddedLimit,
  index: number,
  added: AddedLimit[],
  applicable: ApplicableLimit[] | null,
  product: ProductValues,
): string | undefined {
  const value = addedValue(line, product);
  if (value === "") return NO_VALUE[line.scope];
  if (line.scope === "model") {
    const title = product.title.trim();
    if (title === "") return "The product's title is not read yet, so a model line cannot be checked.";
    if (!title.toLowerCase().includes(value.toLowerCase())) return `The product's title does not contain “${value}”.`;
  }
  // As the server refuses them: a limit that already applies is changed, not added again.
  if (savedLimitFor(line.scope, value, applicable) !== undefined) {
    return `A ${line.scope} limit for '${value}' already applies: change that one instead.`;
  }
  const wanted = value.toLowerCase();
  const earlier = added
    .slice(0, index)
    .some((other) => other.scope === line.scope && addedValue(other, product).toLowerCase() === wanted);
  if (earlier) return `A ${line.scope} limit for '${value}' is already added for this order.`;
  return undefined;
}

function prune(errors: LineErrors): LineErrors | null {
  return errors.units || errors.days || errors.value || errors.loosen ? errors : null;
}

/**
 * What is wrong with this order's changes, line by line; null when nothing is.
 * Units are a whole number, 1 or more, and days 1 to 3650, as for saved
 * limits; a line switched off goes with its saved numbers, so its fields are
 * never checked. An added line needs a value (a model line, a name the
 * product's title contains) and must not repeat a limit that already applies
 * or another added line. Someone who is not an admin may tighten a limit but
 * never loosen it: more units, fewer days, or a line switched off.
 */
export function validateLimitChanges(
  changes: LimitChanges,
  applicable: ApplicableLimit[] | null,
  product: ProductValues,
  isAdmin: boolean,
): LimitChangeErrors | null {
  const out: LimitChangeErrors = { saved: {}, added: {} };
  let found = false;
  const savedById = new Map((applicable ?? []).map((limit) => [limit.limit_id, limit]));

  for (const [id, edit] of Object.entries(changes.edits)) {
    const limitId = Number(id);
    const saved = savedById.get(limitId);
    if (applicable !== null && (saved === undefined || !isChanged(saved, edit))) continue;
    const errors: LineErrors = edit.enabled
      ? { units: unitsError(edit.maxUnits), days: daysError(edit.windowDays) }
      : {};
    if (!isAdmin && saved !== undefined) {
      const units = whole(edit.maxUnits);
      const days = whole(edit.windowDays);
      const looser =
        !edit.enabled || (units !== null && units > saved.max_units) || (days !== null && days < saved.window_days);
      if (looser) errors.loosen = ONLY_ADMIN_LOOSENS;
    }
    const kept = prune(errors);
    if (kept) {
      out.saved[limitId] = kept;
      found = true;
    }
  }

  changes.added.forEach((line, index) => {
    const kept = prune({
      units: unitsError(line.maxUnits),
      days: daysError(line.windowDays),
      value: valueError(line, index, changes.added, applicable, product),
    });
    if (kept) {
      out.added[line.key] = kept;
      found = true;
    }
  });
  return found ? out : null;
}

/* --- on the wire ------------------------------------------------------------------- */

/**
 * The changes as the checks and the order send them: a saved limit that is
 * changed goes by its id with its units, days and on/off (never its value),
 * a line switched off with its saved numbers; an added line with its scope
 * and value. Empty when nothing is changed. Edits to a limit that no longer
 * applies to the product are left out, and so are the lines `errors` marks:
 * the rest still go, so the checks follow every change that holds.
 */
export function limitOverrides(
  changes: LimitChanges,
  applicable: ApplicableLimit[] | null,
  product: ProductValues,
  errors: LimitChangeErrors | null = null,
): LimitOverride[] {
  const out: LimitOverride[] = [];
  const savedById = applicable === null ? null : new Map(applicable.map((limit) => [limit.limit_id, limit]));
  for (const [id, edit] of Object.entries(changes.edits)) {
    const limitId = Number(id);
    if (errors?.saved[limitId]) continue;
    const saved = savedById?.get(limitId);
    if (savedById !== null && (saved === undefined || !isChanged(saved, edit))) continue;
    const off = !edit.enabled;
    out.push({
      limit_id: limitId,
      max_units: off && saved ? saved.max_units : Number(edit.maxUnits.trim()),
      window_days: off && saved ? saved.window_days : Number(edit.windowDays.trim()),
      enabled: edit.enabled,
    });
  }
  for (const line of changes.added) {
    if (errors?.added[line.key]) continue;
    out.push({
      limit_id: null,
      scope: line.scope,
      value: addedValue(line, product),
      max_units: Number(line.maxUnits.trim()),
      window_days: Number(line.windowDays.trim()),
      enabled: true,
    });
  }
  return out;
}

function isScope(value: unknown): value is LimitScope {
  return typeof value === "string" && (LIMIT_SCOPES as readonly string[]).includes(value);
}

/**
 * An earlier run's saved changes, back in the form for "Run again", made for
 * that run's product link. null when it saved none (or none readable).
 */
export function changesFromRun(raw: unknown, forLink: string): LimitChanges | null {
  if (!Array.isArray(raw)) return null;
  const edits: Record<number, LimitEdit> = {};
  const added: AddedLimit[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") continue;
    const saved = entry as Record<string, unknown>;
    if (typeof saved.max_units !== "number" || typeof saved.window_days !== "number") continue;
    const maxUnits = String(saved.max_units);
    const windowDays = String(saved.window_days);
    if (typeof saved.limit_id === "number") {
      edits[saved.limit_id] = { maxUnits, windowDays, enabled: saved.enabled !== false };
    } else if (isScope(saved.scope)) {
      added.push({
        key: addedKey(),
        scope: saved.scope,
        value: saved.scope === "model" && typeof saved.value === "string" ? saved.value : "",
        maxUnits,
        windowDays,
      });
    }
  }
  if (Object.keys(edits).length === 0 && added.length === 0) return null;
  return { forLink: forLink.trim(), edits, added };
}

/* --- in a run's settings ----------------------------------------------------------- */

function rule(units: number, days: number): string {
  return `${units} ${units === 1 ? "unit" : "units"} per ${days} ${days === 1 ? "day" : "days"}`;
}

/**
 * A run's saved `limit_overrides`, one line each, for its settings:
 * "brand 'vivo': 3 units per 30 days (saved: 2 per 30)",
 * "added for this order: model 'T5x 5G' 1 unit per 30 days",
 * "switched off: category 'Mobile'". A change saved without its limit's name
 * is named by the limit's id; the saved numbers show when the run kept them.
 */
export function overrideLines(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const lines: string[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") continue;
    const saved = entry as Record<string, unknown>;
    const units = typeof saved.max_units === "number" ? saved.max_units : null;
    const days = typeof saved.window_days === "number" ? saved.window_days : null;
    const value = typeof saved.value === "string" ? saved.value.trim() : "";
    const name = isScope(saved.scope)
      ? value === ""
        ? `any ${saved.scope}`
        : `${saved.scope} '${value}'`
      : typeof saved.limit_id === "number"
        ? `limit #${saved.limit_id}`
        : "a limit";
    if (typeof saved.limit_id !== "number") {
      if (units !== null && days !== null) lines.push(`added for this order: ${name} ${rule(units, days)}`);
      continue;
    }
    if (saved.enabled === false) {
      lines.push(`switched off: ${name}`);
      continue;
    }
    if (units === null || days === null) continue;
    const savedUnits = typeof saved.saved_max_units === "number" ? saved.saved_max_units : null;
    const savedDays = typeof saved.saved_window_days === "number" ? saved.saved_window_days : null;
    lines.push(
      `${name}: ${rule(units, days)}${savedUnits !== null && savedDays !== null ? ` (saved: ${savedUnits} per ${savedDays})` : ""}`,
    );
  }
  return lines;
}
