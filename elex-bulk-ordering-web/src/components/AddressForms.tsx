/**
 * The add/edit panels for an address and for a GST.
 *
 * Both validate before they ask the server, because the two mistakes that
 * matter here are silent ones: a wrong pincode still places the order, it just
 * delivers somewhere else, and a wrong GSTIN still bills, just under the wrong
 * entity. A duplicate label comes back as a 409 and lands on the label field,
 * where the duplicate is.
 *
 * A panel owns its own save: it reports back only once the row exists.
 */
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  createAddress,
  createGst,
  errorMessage,
  updateAddress,
  updateGst,
} from "../lib/api";
import {
  GSTIN_LENGTH,
  PINCODE_LENGTH,
  type Address,
  type AddressInput,
  type Gst,
  type GstInput,
} from "../lib/types";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Checkbox, Field, Select, TextInput } from "./Field";

const PINCODE_RE = /^\d{6}$/;

/** Exactly 6 digits — the server's rule, checked here so a 422 is the rare case. */
export function isPincode(value: string): boolean {
  return PINCODE_RE.test(value.trim());
}

/** Exactly 15 characters. The server owns the real format; this is the shape. */
export function isGstin(value: string): boolean {
  return value.trim().length === GSTIN_LENGTH;
}

const PANEL = "flex flex-col gap-4 rounded-card border border-line bg-page p-4";

/** A save closes the panel, so the reply lands after unmount — ignore it then. */
function useAlive(): { current: boolean } {
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return alive;
}

/* ===========================================================================
   Address
   =========================================================================== */

interface AddressDraft {
  label: string;
  name: string;
  phone: string;
  line1: string;
  floor: string;
  city: string;
  state: string;
  pincode: string;
  /** "" means "not linked to a GST". */
  gstId: string;
  isDefault: boolean;
}

type AddressErrors = Partial<Record<keyof AddressDraft, string>>;

function addressDraft(address: Address | null, firstEver: boolean): AddressDraft {
  if (address === null) {
    return {
      label: "",
      name: "",
      phone: "",
      line1: "",
      floor: "",
      city: "",
      state: "",
      pincode: "",
      gstId: "",
      // The very first address becomes the default: a list with no default is
      // a list every run silently falls back out of.
      isDefault: firstEver,
    };
  }
  return {
    label: address.label,
    name: address.name,
    phone: address.phone ?? "",
    line1: address.line1,
    floor: address.floor ?? "",
    city: address.city ?? "",
    state: address.state ?? "",
    pincode: address.pincode,
    gstId: address.gst === null ? "" : String(address.gst.id),
    isDefault: address.is_default,
  };
}

function validateAddress(draft: AddressDraft): AddressErrors {
  const errors: AddressErrors = {};
  if (draft.label.trim() === "") {
    errors.label = "Give this address a short label — it is how a run picks it.";
  }
  if (draft.name.trim() === "") {
    errors.name = "Enter the name the parcel is addressed to.";
  }
  if (draft.line1.trim() === "") {
    errors.line1 = "Enter the street address.";
  }
  if (draft.pincode.trim() === "") {
    errors.pincode = "Enter the pincode.";
  } else if (!isPincode(draft.pincode)) {
    errors.pincode = `A pincode is exactly ${PINCODE_LENGTH} digits.`;
  }
  return errors;
}

export interface AddressFormProps {
  /** The row being edited, or null to add one. */
  address: Address | null;
  /** Offered in the "Linked GST" select. */
  gsts: Gst[];
  /** True when no address exists yet — the first one is pre-set as default. */
  firstEver: boolean;
  /** Called with a sentence for the page's status line once the row is saved. */
  onSaved: (message: string) => void;
  onCancel: () => void;
  className?: string;
}

export function AddressForm({
  address,
  gsts,
  firstEver,
  onSaved,
  onCancel,
  className = "",
}: AddressFormProps) {
  const editing = address !== null;
  const [draft, setDraft] = useState<AddressDraft>(() => addressDraft(address, firstEver));
  const [showErrors, setShowErrors] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [labelTaken, setLabelTaken] = useState<string | null>(null);

  const alive = useAlive();

  const errors = useMemo(() => validateAddress(draft), [draft]);
  const shown = showErrors ? errors : {};
  /** The default row cannot un-default itself — another row has to take over. */
  const defaultLocked = editing && address.is_default;

  const set = <K extends keyof AddressDraft>(key: K, value: AddressDraft[K]) => {
    setDraft((prev) => ({ ...prev, [key]: value }));
    setSaveError(null);
    if (key === "label") setLabelTaken(null);
  };

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saving) return;
    setShowErrors(true);
    setSaveError(null);
    setLabelTaken(null);
    if (Object.keys(errors).length > 0) return;

    const label = draft.label.trim();
    const payload: AddressInput = {
      label,
      name: draft.name.trim(),
      // null, not "": these columns are nullable, and on an edit null is the
      // only value that actually clears one.
      phone: draft.phone.trim() || null,
      line1: draft.line1.trim(),
      floor: draft.floor.trim() || null,
      city: draft.city.trim() || null,
      state: draft.state.trim() || null,
      pincode: draft.pincode.trim(),
      gst_id: draft.gstId === "" ? null : Number(draft.gstId),
      is_default: defaultLocked ? true : draft.isDefault,
    };

    setSaving(true);
    try {
      const saved =
        address === null
          ? await createAddress(payload)
          : await updateAddress(address.id, payload);
      onSaved(
        editing
          ? `“${saved.label}” saved.`
          : `“${saved.label}” is ready — an ordering run can ship there now.`,
      );
    } catch (err) {
      if (!alive.current) return;
      if (err instanceof ApiError && err.status === 409) {
        setLabelTaken(`An address called “${label}” already exists.`);
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
        {editing ? `Edit “${address.label}”` : "New address"}
      </p>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          label="Label"
          required
          error={labelTaken ?? shown.label}
          help="How this address is named in a run, e.g. the entity it belongs to."
        >
          <TextInput
            value={draft.label}
            disabled={saving}
            autoComplete="off"
            spellCheck={false}
            maxLength={64}
            placeholder="GLOBE COMMERCE HQ"
            onChange={(event) => set("label", event.target.value)}
          />
        </Field>

        <Field label="Name" required error={shown.name} help="Who the parcel is addressed to.">
          <TextInput
            value={draft.name}
            disabled={saving}
            autoComplete="off"
            maxLength={120}
            onChange={(event) => set("name", event.target.value)}
          />
        </Field>

        <Field label="Phone" help="For the courier. Optional.">
          <TextInput
            value={draft.phone}
            disabled={saving}
            type="tel"
            inputMode="tel"
            autoComplete="off"
            maxLength={20}
            onChange={(event) => set("phone", event.target.value)}
          />
        </Field>

        <Field label="Floor / landmark" help="Optional second line.">
          <TextInput
            value={draft.floor}
            disabled={saving}
            autoComplete="off"
            maxLength={120}
            placeholder="2nd floor"
            onChange={(event) => set("floor", event.target.value)}
          />
        </Field>

        <Field label="Address" required error={shown.line1} className="sm:col-span-2">
          <TextInput
            value={draft.line1}
            disabled={saving}
            autoComplete="off"
            maxLength={255}
            placeholder="12A Nehru Road, Indiranagar"
            onChange={(event) => set("line1", event.target.value)}
          />
        </Field>

        <Field label="City">
          <TextInput
            value={draft.city}
            disabled={saving}
            autoComplete="off"
            maxLength={80}
            onChange={(event) => set("city", event.target.value)}
          />
        </Field>

        <Field label="State">
          <TextInput
            value={draft.state}
            disabled={saving}
            autoComplete="off"
            maxLength={80}
            onChange={(event) => set("state", event.target.value)}
          />
        </Field>

        <Field
          label="Pincode"
          required
          error={shown.pincode}
          help={`Exactly ${PINCODE_LENGTH} digits. It decides where the order actually goes.`}
        >
          <TextInput
            value={draft.pincode}
            disabled={saving}
            inputMode="numeric"
            autoComplete="off"
            maxLength={PINCODE_LENGTH}
            placeholder="560038"
            className="tabular-nums"
            onChange={(event) => set("pincode", event.target.value.replace(/\D/g, ""))}
          />
        </Field>

        <Field
          label="Linked GST"
          help={
            gsts.length === 0
              ? "No GST on file yet. Add one below, then come back and link it."
              : "The entity this address normally bills under. A run can still pick another."
          }
        >
          <Select
            value={draft.gstId}
            disabled={saving || gsts.length === 0}
            onChange={(event) => set("gstId", event.target.value)}
          >
            <option value="">None</option>
            {gsts.map((gst) => (
              <option key={gst.id} value={String(gst.id)}>
                {gst.label} — {gst.gstin}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      <Checkbox
        label="Make this the default delivery address"
        checked={defaultLocked ? true : draft.isDefault}
        disabled={saving || defaultLocked}
        help={
          defaultLocked
            ? "This is the default. Make another address the default to move it."
            : "Every ordering run opens on the default address. Exactly one address holds it."
        }
        onChange={(event) => set("isDefault", event.target.checked)}
      />

      <div aria-live="polite" className="empty:hidden">
        {saveError ? (
          <Alert tone="error" title="Could not save this address">
            {saveError}
          </Alert>
        ) : null}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" variant="primary" loading={saving}>
          {saving ? "Saving…" : editing ? "Save address" : "Add address"}
        </Button>
        <Button variant="ghost" disabled={saving} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

/* ===========================================================================
   GST
   =========================================================================== */

interface GstDraft {
  label: string;
  gstin: string;
  businessName: string;
  isDefault: boolean;
}

type GstErrors = Partial<Record<keyof GstDraft, string>>;

function gstDraft(gst: Gst | null, firstEver: boolean): GstDraft {
  if (gst === null) {
    return { label: "", gstin: "", businessName: "", isDefault: firstEver };
  }
  return {
    label: gst.label,
    gstin: gst.gstin,
    businessName: gst.business_name,
    isDefault: gst.is_default,
  };
}

function validateGst(draft: GstDraft): GstErrors {
  const errors: GstErrors = {};
  if (draft.label.trim() === "") {
    errors.label = "Give this GST a short label — usually the entity name.";
  }
  if (draft.gstin.trim() === "") {
    errors.gstin = "Enter the GSTIN.";
  } else if (!isGstin(draft.gstin)) {
    errors.gstin = `A GSTIN is exactly ${GSTIN_LENGTH} characters — this one has ${draft.gstin.trim().length}.`;
  }
  if (draft.businessName.trim() === "") {
    errors.businessName = "Enter the registered business name.";
  }
  return errors;
}

export interface GstFormProps {
  /** The row being edited, or null to add one. */
  gst: Gst | null;
  /** True when no GST exists yet — the first one is pre-set as default. */
  firstEver: boolean;
  onSaved: (message: string) => void;
  onCancel: () => void;
  className?: string;
}

/**
 * What the server's refusal of a GSTIN means, in words: its own answer is a
 * pattern ("String should match pattern '^[0-9]{2}[A-Z]{5}…'"), which tells
 * a person nothing (spec FR-053; review of 27 Oct 2026).
 */
export const GSTIN_REFUSED =
  "This is not a GSTIN. One reads: 2 digits for the state, the 10 characters of a PAN " +
  "(5 letters, 4 digits, a letter), a digit or letter, a Z, then one more — for example 09AAAAA0000A1Z5.";

/** Whether a 422's detail names this field (FastAPI's `loc` ends with it). */
function refusesField(detail: unknown, field: string): boolean {
  return (
    Array.isArray(detail) &&
    detail.some((item) => {
      const loc = (item as { loc?: unknown })?.loc;
      return Array.isArray(loc) && loc[loc.length - 1] === field;
    })
  );
}

export function GstForm({ gst, firstEver, onSaved, onCancel, className = "" }: GstFormProps) {
  const editing = gst !== null;
  const [draft, setDraft] = useState<GstDraft>(() => gstDraft(gst, firstEver));
  const [showErrors, setShowErrors] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [labelTaken, setLabelTaken] = useState<string | null>(null);
  const [gstinRefused, setGstinRefused] = useState<string | null>(null);

  const alive = useAlive();

  const errors = useMemo(() => validateGst(draft), [draft]);
  const shown = showErrors ? errors : {};
  const defaultLocked = editing && gst.is_default;

  const set = <K extends keyof GstDraft>(key: K, value: GstDraft[K]) => {
    setDraft((prev) => ({ ...prev, [key]: value }));
    setSaveError(null);
    if (key === "label") setLabelTaken(null);
    if (key === "gstin") setGstinRefused(null);
  };

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saving) return;
    setShowErrors(true);
    setSaveError(null);
    setLabelTaken(null);
    if (Object.keys(errors).length > 0) return;

    const label = draft.label.trim();
    const payload: GstInput = {
      label,
      // GSTINs are written upper-case everywhere they appear on an invoice.
      gstin: draft.gstin.trim().toUpperCase(),
      business_name: draft.businessName.trim(),
      is_default: defaultLocked ? true : draft.isDefault,
    };

    setSaving(true);
    try {
      const saved = gst === null ? await createGst(payload) : await updateGst(gst.id, payload);
      onSaved(editing ? `“${saved.label}” saved.` : `“${saved.label}” is ready to bill under.`);
    } catch (err) {
      if (!alive.current) return;
      if (err instanceof ApiError && err.status === 409) {
        setLabelTaken(`A GST called “${label}” already exists.`);
      } else if (err instanceof ApiError && err.status === 422 && refusesField(err.detail, "gstin")) {
        setGstinRefused(GSTIN_REFUSED);
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
        {editing ? `Edit “${gst.label}”` : "New GST"}
      </p>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          label="Label"
          required
          error={labelTaken ?? shown.label}
          help="How this GST is named in a run — usually the entity, e.g. BLAZECOM."
        >
          <TextInput
            value={draft.label}
            disabled={saving}
            autoComplete="off"
            spellCheck={false}
            maxLength={64}
            placeholder="BLAZECOM"
            onChange={(event) => set("label", event.target.value)}
          />
        </Field>

        <Field
          label="GSTIN"
          required
          error={gstinRefused ?? shown.gstin}
          help={`Exactly ${GSTIN_LENGTH} characters. This is the number the order bills under.`}
        >
          <TextInput
            value={draft.gstin}
            disabled={saving}
            autoComplete="off"
            spellCheck={false}
            maxLength={GSTIN_LENGTH}
            placeholder="29ABCDE1234F1Z5"
            className="font-mono uppercase"
            onChange={(event) => set("gstin", event.target.value.toUpperCase())}
          />
        </Field>

        <Field
          label="Business name"
          required
          error={shown.businessName}
          help="The registered name, as it should read on the invoice."
          className="sm:col-span-2"
        >
          <TextInput
            value={draft.businessName}
            disabled={saving}
            autoComplete="off"
            maxLength={160}
            placeholder="Blazecom Retail Private Limited"
            onChange={(event) => set("businessName", event.target.value)}
          />
        </Field>
      </div>

      <Checkbox
        label="Make this the default GST"
        checked={defaultLocked ? true : draft.isDefault}
        disabled={saving || defaultLocked}
        help={
          defaultLocked
            ? "This is the default. Make another GST the default to move it."
            : "Every ordering run opens on the default GST. Exactly one GST holds it."
        }
        onChange={(event) => set("isDefault", event.target.checked)}
      />

      <div aria-live="polite" className="empty:hidden">
        {saveError ? (
          <Alert tone="error" title="Could not save this GST">
            {saveError}
          </Alert>
        ) : null}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" variant="primary" loading={saving}>
          {saving ? "Saving…" : editing ? "Save GST" : "Add GST"}
        </Button>
        <Button variant="ghost" disabled={saving} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

export default AddressForm;
