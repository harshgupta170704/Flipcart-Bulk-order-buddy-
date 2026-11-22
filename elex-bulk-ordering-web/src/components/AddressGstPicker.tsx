/**
 * Choosing where an ordering run ships and which GST it bills under.
 *
 * This is the whole point of having an address book: a wrong choice here fails
 * *quietly* — the order succeeds, it just arrives at the wrong place under the
 * wrong GST number. So the picker never settles for showing two labels. It
 * prints the chosen address in full and the GSTIN the run will actually bill
 * under, and it says so out loud when the address belongs to one entity and the
 * run is billing under another.
 *
 * There is no fallback address: a run needs an Address Book address, full stop,
 * and the server refuses one without. The GST may be left on automatic, and the
 * server then walks the ladder `resolveGst` shows here — the address's linked
 * GST, else the default GST, else none.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import type { Address, Gst } from "../lib/types";
import {
  addressOption,
  addressPlace,
  addressStreet,
  defaultRow,
  gstOption,
  resolveGst,
  useAddresses,
  useGsts,
  type ResolvedGst,
} from "./AddressBookData";
import { AddressForm } from "./AddressForms";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Dialog } from "./Dialog";
import { Field, Select } from "./Field";
import { RunError, RunLoading } from "./RunStates";

/** What the host form sends on to POST /jobs/order. */
export interface AddressGstChoice {
  /** null until an address is chosen — the run cannot start without one. */
  address_id: number | null;
  /** null means automatic: the address's linked GST, else the default GST. */
  gst_id: number | null;
}

/** How many of the run's accounts have a GST of their own, when the page knows. */
export interface OwnGstCount {
  /** Accounts with a GST of their own: they bill under it, not the run's. */
  count: number;
  /** Accounts in the run. */
  of: number;
}

export interface AddressGstPickerProps {
  value: AddressGstChoice;
  /** Must be a stable callback — a plain `setState` is ideal. */
  onChange: (next: AddressGstChoice) => void;
  disabled?: boolean;
  className?: string;
  /**
   * Of the accounts the run will use, how many have their own GST — null when
   * the page cannot tell (a selection by filter, or more accounts than it has
   * loaded). The summary says that such accounts bill under their own either way.
   */
  ownGst?: OwnGstCount | null;
  /** Told the chosen address and the GST the run will bill under, whenever
   *  either changes, so a summary elsewhere can show them in full. Must be a
   *  stable callback. */
  onResolved?: (address: Address | null, gst: Gst | null) => void;
}

const LINK_STYLE = "font-medium text-accent underline underline-offset-2";

function SummaryRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-3">
      <span className="shrink-0 text-[12px] font-medium uppercase tracking-wide text-muted sm:w-24 sm:pt-px">
        {label}
      </span>
      <div className="min-w-0 text-[13px] leading-relaxed text-ink">{children}</div>
    </div>
  );
}

/**
 * The run's GST is for the accounts without one of their own: since 26 Oct
 * 2026 an account may carry its own GST (Accounts page), and its orders bill
 * under that one whatever the run picks. Said beside the run's GST, with the
 * count when the page knows it.
 */
function OwnGstNote({ ownGst }: { ownGst: OwnGstCount | null }) {
  let text: string;
  if (ownGst === null) {
    text = "An account with a GST of its own bills under that one instead.";
  } else if (ownGst.count === 0) {
    text = ownGst.of === 1 ? "The account has no GST of its own." : "None of the selected accounts has a GST of its own.";
  } else if (ownGst.count === ownGst.of) {
    text =
      ownGst.of === 1
        ? "The account has a GST of its own and bills under it instead."
        : `All ${ownGst.of} selected accounts have a GST of their own and bill under it instead.`;
  } else {
    text = `${ownGst.count} of the ${ownGst.of} selected accounts ${
      ownGst.count === 1 ? "has a GST of its own and bills" : "have a GST of their own and bill"
    } under it instead.`;
  }
  return <span className="mt-1 block text-[12px] text-muted">{text}</span>;
}

/** Everything the operator should check before starting the run. */
function RunTarget({
  address,
  resolved,
  ownGst,
}: {
  address: Address | null;
  resolved: ResolvedGst;
  ownGst: OwnGstCount | null;
}) {
  return (
    <div className="flex flex-col gap-2.5 rounded-control border border-line bg-page px-3.5 py-3">
      <SummaryRow label="Ships to">
        {address === null ? (
          <span className="text-ink-2">No address chosen yet — the run cannot start until one is.</span>
        ) : (
          <>
            <span className="block font-medium">{address.name}</span>
            <span className="block text-ink-2">{addressStreet(address)}</span>
            <span className="block text-ink-2">{addressPlace(address)}</span>
            {address.phone ? (
              <span className="block text-ink-2 tabular-nums">{address.phone}</span>
            ) : null}
          </>
        )}
      </SummaryRow>
      <SummaryRow label="Bills under">
        {resolved.gst === null ? (
          <span className="text-ink-2">No GST — the orders will be placed without GST details.</span>
        ) : (
          <>
            <span className="block font-medium">{resolved.gst.business_name}</span>
            <span className="block font-mono text-[12px] tracking-wide text-ink-2">
              {resolved.gst.gstin}
            </span>
            {resolved.source === "linked" ? (
              <span className="block text-[12px] text-muted">Linked to the chosen address.</span>
            ) : resolved.source === "default" ? (
              <span className="block text-[12px] text-muted">
                The default GST — the chosen address is not linked to one.
              </span>
            ) : null}
          </>
        )}
        <OwnGstNote ownGst={ownGst} />
      </SummaryRow>
    </div>
  );
}

export function AddressGstPicker({
  value,
  onChange,
  disabled = false,
  className = "",
  ownGst = null,
  onResolved,
}: AddressGstPickerProps) {
  const addresses = useAddresses();
  const gsts = useGsts();

  const addressRows = addresses.rows ?? [];
  const gstRows = gsts.rows ?? [];

  /* The chosen address and the GST it resolves to, for the host's summary. */
  const resolvedAddress = addressRows.find((row) => row.id === value.address_id) ?? null;
  const resolvedGst = resolveGst(
    resolvedAddress,
    gstRows.find((row) => row.id === value.gst_id) ?? null,
    gstRows,
  ).gst;
  useEffect(() => {
    onResolved?.(resolvedAddress, resolvedGst);
  }, [onResolved, resolvedAddress, resolvedGst]);

  /* Open on the address marked default, once, as soon as the list has landed
     — and on nothing when none is marked, so the operator has to choose, just
     as the server refuses a run that names no address. Only once: after that
     the selects are the operator's. The GST is left on automatic so the
     address's own GST applies — the read-back below shows which one that is. */
  const defaulted = useRef(false);
  useEffect(() => {
    if (defaulted.current) return;
    if (addresses.rows === null) return;
    defaulted.current = true;
    if (value.address_id !== null) return;
    const address = defaultRow(addresses.rows);
    if (address === null) return;
    onChange({ ...value, address_id: address.id });
  }, [addresses.rows, value, onChange]);

  const retry = () => {
    addresses.reload();
    gsts.reload();
  };

  /* "+ Add address" right here, as on the order form in the video: the new
     address is saved to the Address Book and chosen for this run. */
  const [adding, setAdding] = useState(false);
  const knownIds = useRef<Set<number> | null>(null);
  useEffect(() => {
    if (knownIds.current === null || addresses.rows === null) return;
    const added = addresses.rows.find((row) => !knownIds.current?.has(row.id));
    if (added === undefined) return;
    knownIds.current = null;
    onChange({ ...value, address_id: added.id });
  }, [addresses.rows, value, onChange]);
  const addButton = (
    <Button type="button" size="sm" variant="secondary" disabled={disabled} onClick={() => setAdding(true)}>
      + Add address
    </Button>
  );
  const addDialog = (
    <Dialog open={adding} title="Add address" onClose={() => setAdding(false)} width="720px">
      {adding ? (
        <AddressForm
          address={null}
          gsts={gstRows}
          firstEver={addressRows.length === 0}
          onSaved={() => {
            knownIds.current = new Set(addressRows.map((row) => row.id));
            setAdding(false);
            addresses.reload();
            gsts.reload();
          }}
          onCancel={() => setAdding(false)}
        />
      ) : null}
    </Dialog>
  );

  const loadFailed =
    (addresses.rows === null && addresses.error !== null) ||
    (gsts.rows === null && gsts.error !== null);

  if (loadFailed) {
    return (
      <div className={className}>
        <RunError
          title="Could not load the addresses"
          message={
            <>
              {addresses.error ?? gsts.error}
              <span className="mt-1 block">
                Until this loads the run cannot be aimed at a saved address, so it cannot start.
              </span>
            </>
          }
          onRetry={retry}
        />
      </div>
    );
  }

  if (addresses.rows === null || gsts.rows === null) {
    return (
      <div className={className}>
        <RunLoading label="Loading addresses and GSTs…" />
      </div>
    );
  }

  if (addressRows.length === 0) {
    return (
      <div className={className}>
        <Alert tone="warning" title="No saved address — the run cannot start">
          Every ordering run ships to an address from the Address Book, and there is none yet. Add one
          here, or add your address and GST on the{" "}
          <Link to="/addresses" className={LINK_STYLE}>
            Addresses page
          </Link>
          .
        </Alert>
        <div className="mt-2">{addButton}</div>
        {addDialog}
      </div>
    );
  }

  const chosenAddress = addressRows.find((row) => row.id === value.address_id) ?? null;
  const chosenGst = gstRows.find((row) => row.id === value.gst_id) ?? null;
  const linked = chosenAddress?.gst ?? null;
  const resolved = resolveGst(chosenAddress, chosenGst, gstRows);
  /* Allowed — one entity's address can bill under another's GST — but never
     silently: this is the mistake the phase exists to make visible. */
  const mismatch = linked !== null && chosenGst !== null && linked.id !== chosenGst.id;

  return (
    <div className={`flex flex-col gap-4 ${className}`}>
      {addDialog}
      <div className="flex justify-end">{addButton}</div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          label="Delivery address"
          required
          help="Every order in this run ships here. A run cannot start without one."
        >
          <Select
            name="address_id"
            value={value.address_id === null ? "" : String(value.address_id)}
            disabled={disabled}
            onChange={(event) =>
              onChange({
                ...value,
                address_id: event.target.value === "" ? null : Number(event.target.value),
              })
            }
          >
            <option value="" disabled>
              Choose an address…
            </option>
            {addressRows.map((row) => (
              <option key={row.id} value={String(row.id)}>
                {addressOption(row)}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label="GST"
          help={
            gstRows.length === 0
              ? "No GST on file — the orders are placed without GST details."
              : "The entity this run bills under. Leave it on automatic to follow the address."
          }
        >
          <Select
            name="gst_id"
            value={value.gst_id === null ? "" : String(value.gst_id)}
            disabled={disabled || gstRows.length === 0}
            onChange={(event) =>
              onChange({
                ...value,
                gst_id: event.target.value === "" ? null : Number(event.target.value),
              })
            }
          >
            <option value="">Automatic — the address’s linked GST, else the default GST</option>
            {gstRows.map((row) => (
              <option key={row.id} value={String(row.id)}>
                {gstOption(row)}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      <RunTarget address={chosenAddress} resolved={resolved} ownGst={ownGst} />

      {mismatch ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <p className="text-[12px] leading-snug text-ink-2">
            “{chosenAddress?.label}” is linked to{" "}
            <span className="font-medium text-ink">{linked.label}</span>, but this run bills under{" "}
            <span className="font-medium text-ink">{chosenGst?.label}</span>. That is allowed —
            worth a second look.
          </p>
          <Button
            size="sm"
            variant="ghost"
            disabled={disabled}
            onClick={() => onChange({ ...value, gst_id: linked.id })}
          >
            Bill under {linked.label}
          </Button>
        </div>
      ) : null}

      {gstRows.length === 0 ? (
        <Alert tone="warning" title="No GST on file — this run places orders without GST details">
          <Link to="/addresses" className={LINK_STYLE}>
            Add a GST per entity
          </Link>{" "}
          and link each address to its own, and the run bills under it.
        </Alert>
      ) : chosenAddress !== null && chosenAddress.gst === null && chosenGst === null ? (
        resolved.gst !== null ? (
          <p className="text-[12px] leading-snug text-ink-2">
            “{chosenAddress.label}” is not linked to a GST, so this run bills under the default GST,{" "}
            <span className="font-medium text-ink">{resolved.gst.label}</span>.{" "}
            <Link to="/addresses" className={LINK_STYLE}>
              Link it
            </Link>{" "}
            to make the pairing explicit.
          </p>
        ) : (
          <Alert tone="warning" title="This run would place orders without GST details">
            “{chosenAddress.label}” is not linked to a GST and none is marked default. Pick a GST
            above, or{" "}
            <Link to="/addresses" className={LINK_STYLE}>
              link the address
            </Link>{" "}
            to one.
          </Alert>
        )
      ) : chosenAddress !== null && chosenAddress.gst === null ? (
        <p className="text-[12px] leading-snug text-ink-2">
          “{chosenAddress.label}” is not linked to a GST, so this run bills under the one picked
          above.{" "}
          <Link to="/addresses" className={LINK_STYLE}>
            Link it
          </Link>{" "}
          to make the pairing automatic.
        </p>
      ) : null}
    </div>
  );
}

export default AddressGstPicker;
