/**
 * Addresses & GST — the delivery addresses a run can ship to, and the GSTs it
 * can bill under.
 *
 * These two lists used to be four flat values in Settings, which meant every
 * order in the system shipped to one address under one GSTIN. For a business
 * running several entities that is wrong, and wrong *quietly*: the order goes
 * through, it just lands somewhere else billed to someone else. So both lists
 * live on one screen — they are chosen together on every run — and the link
 * between them is a column you can read at a glance.
 */
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import {
  deleteAddress,
  deleteGst,
  errorMessage,
  updateAddress,
  updateGst,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import { countLabel, DASH, formatCount, formatDate } from "../lib/format";
import type { Address, Gst } from "../lib/types";
import {
  addressStreet,
  useAddresses,
  useGsts,
  type ListState,
} from "../components/AddressBookData";
import { AddressForm, GstForm } from "../components/AddressForms";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Dialog } from "../components/Dialog";
import { EmptyState } from "../components/EmptyState";
import { RunError, RunLoading } from "../components/RunStates";
import { Table, type Column } from "../components/Table";

/** Which add/edit panel a section has open. */
type Tab = "addresses" | "gsts";
const TABS: readonly Tab[] = ["addresses", "gsts"];

type Editor = { kind: "none" } | { kind: "new" } | { kind: "edit"; id: number };

const CLOSED: Editor = { kind: "none" };

/** The one default row, marked so it reads as a state and not as a button. */
function DefaultPill() {
  return (
    <span
      className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-control border px-2 py-[3px] text-[12px] font-medium leading-5 text-ink"
      style={{
        borderColor: "color-mix(in oklab, var(--accent) 45%, transparent)",
        backgroundColor: "color-mix(in oklab, var(--accent) 16%, var(--surface-1))",
      }}
    >
      <span aria-hidden="true" className="font-semibold">
        ★
      </span>
      <span>Default</span>
    </span>
  );
}

/**
 * Loading / error / empty, shared by both sections so neither can drift into
 * showing an empty table while a request is still in the air.
 */
function sectionState<T>(
  state: ListState<T>,
  what: string,
  retry: () => void,
): { node: ReactNode } | null {
  if (state.rows === null && state.error !== null) {
    return {
      node: (
        <RunError title={`Could not load the ${what}`} message={state.error} onRetry={retry} />
      ),
    };
  }
  if (state.rows === null) {
    return { node: <RunLoading label={`Loading ${what}…`} /> };
  }
  return null;
}

export default function AddressBook() {
  const addresses = useAddresses();
  const gsts = useGsts();

  const [addressEditor, setAddressEditor] = useState<Editor>(CLOSED);
  const [gstEditor, setGstEditor] = useState<Editor>(CLOSED);
  const [addressDeleting, setAddressDeleting] = useState<number | null>(null);
  const [gstDeleting, setGstDeleting] = useState<number | null>(null);
  /** "address-3" / "gst-2" — whichever single row is mid-request. */
  const [busyRow, setBusyRow] = useState<string | null>(null);
  /* Operators read the book and pick from it for a run; only an admin changes
     it (FR-058). The API refuses an operator's write too; this is the half
     that keeps the buttons honest. */
  const { isAdmin } = useAuth();
  const [note, setNote] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  /** A failed delete's reason, shown inside the confirmation dialog only. */
  const [deleteError, setDeleteError] = useState<string | null>(null);
  /** One list at a time (spec FR-050): addresses, or the GSTs they bill under. */
  const [tab, setTab] = useState<Tab>("addresses");
  /* The tab bar follows the ARIA tabs pattern: one tab in the Tab order, the
     arrow keys move between them, and each panel names the tab it belongs to. */
  const tabsId = useId();
  const tabRefs = useRef<Record<Tab, HTMLButtonElement | null>>({ addresses: null, gsts: null });
  const onTabKey = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = TABS.indexOf(tab);
    let next: Tab | null = null;
    if (event.key === "ArrowRight") next = TABS[(index + 1) % TABS.length];
    else if (event.key === "ArrowLeft") next = TABS[(index - 1 + TABS.length) % TABS.length];
    else if (event.key === "Home") next = TABS[0];
    else if (event.key === "End") next = TABS[TABS.length - 1];
    if (next === null) return;
    event.preventDefault();
    setTab(next);
    tabRefs.current[next]?.focus();
  };

  useEffect(() => {
    document.title = "Addresses & GST — Ordering Suite";
  }, []);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const addressRows = addresses.rows ?? [];
  const gstRows = gsts.rows ?? [];

  const reloadAddresses = addresses.reload;
  const reloadGsts = gsts.reload;

  /* An address edit moves a GST's `address_count`, and a GST edit changes the
     label an address shows — so either change refreshes both lists. */
  const reloadBoth = useCallback(() => {
    reloadAddresses();
    reloadGsts();
  }, [reloadAddresses, reloadGsts]);

  const begin = (row: string) => {
    setBusyRow(row);
    setActionError(null);
    setNote(null);
  };

  /* --- addresses --------------------------------------------------------- */

  const onAddressSaved = (message: string) => {
    setAddressEditor(CLOSED);
    setNote(message);
    setActionError(null);
    reloadBoth();
  };

  const makeAddressDefault = async (row: Address) => {
    begin(`address-${row.id}`);
    try {
      await updateAddress(row.id, { is_default: true });
      if (!alive.current) return;
      setNote(`Runs now open on “${row.label}”.`);
      reloadAddresses();
    } catch (err) {
      if (alive.current) setActionError(errorMessage(err));
    } finally {
      if (alive.current) setBusyRow(null);
    }
  };

  const removeAddress = async (row: Address) => {
    begin(`address-${row.id}`);
    try {
      await deleteAddress(row.id);
      if (!alive.current) return;
      setAddressDeleting(null);
      setNote(`“${row.label}” is gone.`);
      reloadBoth();
    } catch (err) {
      if (alive.current) setDeleteError(errorMessage(err));
    } finally {
      if (alive.current) setBusyRow(null);
    }
  };

  /* --- GSTs -------------------------------------------------------------- */

  const onGstSaved = (message: string) => {
    setGstEditor(CLOSED);
    setNote(message);
    setActionError(null);
    reloadBoth();
  };

  const makeGstDefault = async (row: Gst) => {
    begin(`gst-${row.id}`);
    try {
      await updateGst(row.id, { is_default: true });
      if (!alive.current) return;
      setNote(`Runs now bill under “${row.label}” unless told otherwise.`);
      reloadGsts();
    } catch (err) {
      if (alive.current) setActionError(errorMessage(err));
    } finally {
      if (alive.current) setBusyRow(null);
    }
  };

  const removeGst = async (row: Gst) => {
    begin(`gst-${row.id}`);
    try {
      // The note counts what the server unlinked, not the row's counts: the
      // list was loaded earlier, and an address or account linked or unlinked
      // since — by another admin, in another tab — would make them wrong.
      const { unlinked_addresses: addresses, unlinked_accounts: accounts } = await deleteGst(row.id);
      if (!alive.current) return;
      setGstDeleting(null);
      setNote(
        (addresses > 0
          ? `“${row.label}” is gone — its ${countLabel(addresses, "address", "addresses")} ${
              addresses === 1 ? "is" : "are"
            } no longer linked to a GST.`
          : `“${row.label}” is gone.`) +
          (accounts > 0
            ? ` ${countLabel(accounts, "account")} that had it as ${accounts === 1 ? "its" : "their"} own GST now ${
                accounts === 1 ? "bills" : "bill"
              } under the GST each run picks.`
            : ""),
      );
      reloadBoth();
    } catch (err) {
      if (alive.current) setDeleteError(errorMessage(err));
    } finally {
      if (alive.current) setBusyRow(null);
    }
  };

  /* --- columns ----------------------------------------------------------- */

  const addressColumns: Column<Address>[] = useMemo(
    () => [
      {
        key: "label",
        header: "Label",
        render: (row) => <span className="font-medium">{row.label}</span>,
      },
      { key: "name", header: "Name" },
      {
        key: "phone",
        header: "Phone",
        render: (row) => (row.phone ? <span className="tabular-nums">{row.phone}</span> : DASH),
      },
      {
        key: "line1",
        header: "Address",
        render: (row) => (
          <span className="block max-w-[260px] truncate" title={addressStreet(row)}>
            {addressStreet(row)}
          </span>
        ),
      },
      { key: "floor", header: "Floor", width: "90px", render: (row) => row.floor || DASH },
      { key: "city", header: "City" },
      { key: "state", header: "State" },
      {
        key: "pincode",
        header: "Pincode",
        render: (row) => <span className="tabular-nums">{row.pincode}</span>,
      },
      {
        key: "gst",
        header: "Linked GST",
        render: (row) =>
          row.gst === null ? (
            <span className="text-muted">None</span>
          ) : (
            // The GSTIN in plain sight, not a tooltip: it is what the invoice
            // will carry, and two registrations can share a business name.
            <span className="whitespace-nowrap">
              {row.gst.label}{" "}
              <span className="font-mono text-[12px] tracking-wide text-ink-2">- {row.gst.gstin}</span>
            </span>
          ),
      },
      {
        key: "created_at",
        header: "Created",
        width: "120px",
        render: (row) => <span className="whitespace-nowrap text-ink-2">{formatDate(row.created_at)}</span>,
      },
      {
        key: "is_default",
        header: "Default",
        width: "140px",
        render: (row) =>
          row.is_default ? (
            <DefaultPill />
          ) : isAdmin ? (
            <Button
              size="sm"
              variant="ghost"
              loading={busyRow === `address-${row.id}`}
              disabled={busyRow !== null}
              onClick={() => void makeAddressDefault(row)}
            >
              Make default
            </Button>
          ) : (
            <span className="text-muted">{DASH}</span>
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
                  setAddressEditor({ kind: "edit", id: row.id });
                  setAddressDeleting(null);
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
                  setAddressDeleting(row.id);
                  setAddressEditor(CLOSED);
                  setActionError(null);
                  setDeleteError(null);
                }}
              >
                Delete
              </Button>
            </span>
          ) : null,
      },
    ],
    // The handlers only ever read state setters and their own `row`, so
    // `busyRow` — which decides what the buttons render — and the role are
    // the real deps.
    [busyRow, isAdmin],
  );

  const gstColumns: Column<Gst>[] = useMemo(
    () => [
      {
        key: "label",
        header: "Label",
        render: (row) => <span className="font-medium">{row.label}</span>,
      },
      {
        key: "gstin",
        header: "GSTIN",
        render: (row) => <span className="font-mono text-[12px] tracking-wide">{row.gstin}</span>,
      },
      { key: "business_name", header: "Business name" },
      {
        key: "address_count",
        header: "Addresses using it",
        numeric: true,
        width: "160px",
        render: (row) => formatCount(row.address_count),
      },
      {
        key: "created_at",
        header: "Created",
        width: "120px",
        render: (row) => <span className="whitespace-nowrap text-ink-2">{formatDate(row.created_at)}</span>,
      },
      {
        key: "is_default",
        header: "Default",
        width: "140px",
        render: (row) =>
          row.is_default ? (
            <DefaultPill />
          ) : isAdmin ? (
            <Button
              size="sm"
              variant="ghost"
              loading={busyRow === `gst-${row.id}`}
              disabled={busyRow !== null}
              onClick={() => void makeGstDefault(row)}
            >
              Make default
            </Button>
          ) : (
            <span className="text-muted">{DASH}</span>
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
                  setGstEditor({ kind: "edit", id: row.id });
                  setGstDeleting(null);
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
                  setGstDeleting(row.id);
                  setGstEditor(CLOSED);
                  setActionError(null);
                  setDeleteError(null);
                }}
              >
                Delete
              </Button>
            </span>
          ) : null,
      },
    ],
    [busyRow, isAdmin],
  );

  /* --- bodies ------------------------------------------------------------ */

  const editedAddress =
    addressEditor.kind === "edit"
      ? (addressRows.find((row) => row.id === addressEditor.id) ?? null)
      : null;
  const editedGst =
    gstEditor.kind === "edit" ? (gstRows.find((row) => row.id === gstEditor.id) ?? null) : null;

  const addressToDelete =
    addressDeleting === null ? null : (addressRows.find((row) => row.id === addressDeleting) ?? null);
  const gstToDelete =
    gstDeleting === null ? null : (gstRows.find((row) => row.id === gstDeleting) ?? null);

  const addressFallback = sectionState(addresses, "addresses", reloadAddresses);
  const gstFallback = sectionState(gsts, "GSTs", reloadGsts);

  const addressBody = (() => {
    if (addressFallback !== null) return addressFallback.node;
    return (
      <Table
        columns={addressColumns}
        rows={addressRows}
        rowKey={(row) => row.id}
        caption="Delivery addresses"
        minWidth="1180px"
        empty={
          <EmptyState
            title="No delivery address yet"
            description="Every ordering run ships to an address from this list, so no run can start until there is one. Add the address each entity ships from and a run can choose between them."
            action={
              isAdmin ? (
                <Button variant="primary" onClick={() => setAddressEditor({ kind: "new" })}>
                  Add the first address
                </Button>
              ) : undefined
            }
          />
        }
      />
    );
  })();

  const gstBody = (() => {
    if (gstFallback !== null) return gstFallback.node;
    return (
      <Table
        columns={gstColumns}
        rows={gstRows}
        rowKey={(row) => row.id}
        caption="GSTs"
        minWidth="900px"
        empty={
          <EmptyState
            title="No GST on file yet"
            description="A GST is the entity an order bills under. With none here, orders are placed without GST details. Add one per entity, then link each address to its own."
            action={
              isAdmin ? (
                <Button variant="primary" onClick={() => setGstEditor({ kind: "new" })}>
                  Add the first GST
                </Button>
              ) : undefined
            }
          />
        }
      />
    );
  })();

  const defaultAddress = addressRows.find((row) => row.is_default) ?? null;
  const defaultGst = gstRows.find((row) => row.is_default) ?? null;
  const refreshing =
    (addresses.loading && addresses.rows !== null) || (gsts.loading && gsts.rows !== null);

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold leading-tight tracking-tight text-ink">
            Addresses &amp; GST
          </h1>
          <p className="mt-1 max-w-prose text-[13px] leading-relaxed text-ink-2">
            Where orders are delivered, and which entity they bill under. An ordering run picks one
            of each — get it wrong and the order still succeeds, it just ships to the wrong place
            under the wrong GST.
          </p>
        </div>
        {/* Only a *re*load reads as refreshing — the first one is the panels'
            own loading state, shown where the data will appear. */}
        <Button onClick={reloadBoth} loading={refreshing}>
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
        {addresses.error !== null && addresses.rows !== null ? (
          <p role="status" className="text-[12px] text-ink-2">
            Showing the addresses that last loaded — the refresh failed: {addresses.error}
          </p>
        ) : null}
        {gsts.error !== null && gsts.rows !== null ? (
          <p role="status" className="text-[12px] text-ink-2">
            Showing the GSTs that last loaded — the refresh failed: {gsts.error}
          </p>
        ) : null}
      </div>

      {addressRows.length > 0 && defaultAddress === null ? (
        <Alert tone="warning" title="No address is marked default">
          The Place-orders form opens on the default address. With none marked it opens on nothing,
          and a run cannot start until an address is chosen — mark the one you actually ship from.
        </Alert>
      ) : null}

      <div
        role="tablist"
        aria-label="Addresses and GSTs"
        className="flex gap-1 border-b border-line"
        onKeyDown={onTabKey}
      >
        {TABS.map((key) => (
          <button
            key={key}
            id={`${tabsId}-tab-${key}`}
            ref={(element) => {
              tabRefs.current[key] = element;
            }}
            role="tab"
            type="button"
            aria-selected={tab === key}
            aria-controls={`${tabsId}-panel-${key}`}
            tabIndex={tab === key ? 0 : -1}
            onClick={() => setTab(key)}
            className={`-mb-px border-b-2 px-3 py-2 text-[13px] font-medium transition-colors ${
              tab === key ? "border-ink text-ink" : "border-transparent text-ink-2 hover:text-ink"
            }`}
          >
            {key === "addresses"
              ? `Addresses (${formatCount(addressRows.length)})`
              : `GSTs (${formatCount(gstRows.length)})`}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`${tabsId}-panel-${tab}`} aria-labelledby={`${tabsId}-tab-${tab}`}>
      {tab === "addresses" ? (
        <Card
          title="Addresses"
          description="One row per place you ship from. The linked GST is the entity that address normally bills under; a run can still override it."
          actions={
            isAdmin ? (
              <Button
                size="sm"
                variant="primary"
                disabled={busyRow !== null}
                onClick={() => {
                  setAddressEditor({ kind: "new" });
                  setAddressDeleting(null);
                  setActionError(null);
                }}
              >
                Add address
              </Button>
            ) : (
              <span className="text-[12px] text-ink-2">Only an admin can change these.</span>
            )
          }
        >
          {addressBody}
        </Card>
      ) : (
        <Card
          title="GSTs"
          description="One row per entity. Deleting a GST never deletes an address — the addresses linked to it are simply left without one."
          actions={
            isAdmin ? (
              <Button
                size="sm"
                variant="primary"
                disabled={busyRow !== null}
                onClick={() => {
                  setGstEditor({ kind: "new" });
                  setGstDeleting(null);
                  setActionError(null);
                }}
              >
                Add GST
              </Button>
            ) : (
              <span className="text-[12px] text-ink-2">Only an admin can change these.</span>
            )
          }
        >
          {gstBody}
        </Card>
      )}
      </div>

      {/* Forms and confirmations open over the list rather than pushing it
          down the page (spec FR-050/051). */}
      <Dialog
        open={addressEditor.kind !== "none"}
        onClose={() => setAddressEditor(CLOSED)}
        title={editedAddress ? `Edit “${editedAddress.label}”` : "Add an address"}
        width="760px"
      >
        {addressEditor.kind === "new" || editedAddress !== null ? (
          <AddressForm
            key={addressEditor.kind === "edit" ? `address-${addressEditor.id}` : "address-new"}
            address={editedAddress}
            gsts={gstRows}
            firstEver={addressRows.length === 0}
            onSaved={onAddressSaved}
            onCancel={() => setAddressEditor(CLOSED)}
          />
        ) : null}
      </Dialog>

      <Dialog
        open={gstEditor.kind !== "none"}
        onClose={() => setGstEditor(CLOSED)}
        title={editedGst ? `Edit “${editedGst.label}”` : "Add a GST"}
        width="640px"
      >
        {gstEditor.kind === "new" || editedGst !== null ? (
          <GstForm
            key={gstEditor.kind === "edit" ? `gst-${gstEditor.id}` : "gst-new"}
            gst={editedGst}
            firstEver={gstRows.length === 0}
            onSaved={onGstSaved}
            onCancel={() => setGstEditor(CLOSED)}
          />
        ) : null}
      </Dialog>

      <Dialog
        open={addressToDelete !== null}
        onClose={() => {
          setAddressDeleting(null);
          setDeleteError(null);
        }}
        title={addressToDelete ? `Delete “${addressToDelete.label}”?` : ""}
        width="560px"
        footer={
          addressToDelete ? (
            <>
              <Button
                size="sm"
                variant="ghost"
                disabled={busyRow !== null}
                onClick={() => {
                  setAddressDeleting(null);
                  setDeleteError(null);
                }}
              >
                Keep it
              </Button>
              <Button
                size="sm"
                variant="danger"
                loading={busyRow === `address-${addressToDelete.id}`}
                onClick={() => void removeAddress(addressToDelete)}
              >
                Delete address
              </Button>
            </>
          ) : undefined
        }
      >
        {addressToDelete ? (
          <div className="flex flex-col gap-2">
            {deleteError ? (
              <Alert tone="error" title="That did not work">
                {deleteError}
              </Alert>
            ) : null}
            <p className="text-[13px] text-ink-2">
              {addressToDelete.is_default
                ? "It is the default, so a run that does not pick an address cannot start until you mark another one."
                : "No run can ship there afterwards."}
            </p>
          </div>
        ) : null}
      </Dialog>

      <Dialog
        open={gstToDelete !== null}
        onClose={() => {
          setGstDeleting(null);
          setDeleteError(null);
        }}
        title={gstToDelete ? `Delete “${gstToDelete.label}”?` : ""}
        width="560px"
        footer={
          gstToDelete ? (
            <>
              <Button
                size="sm"
                variant="ghost"
                disabled={busyRow !== null}
                onClick={() => {
                  setGstDeleting(null);
                  setDeleteError(null);
                }}
              >
                Keep it
              </Button>
              <Button
                size="sm"
                variant="danger"
                loading={busyRow === `gst-${gstToDelete.id}`}
                onClick={() => void removeGst(gstToDelete)}
              >
                {gstToDelete.address_count > 0
                  ? `Delete and unlink ${countLabel(gstToDelete.address_count, "address", "addresses")}`
                  : "Delete GST"}
              </Button>
            </>
          ) : undefined
        }
      >
        {gstToDelete ? (
          <div className="flex flex-col gap-2 text-[13px] text-ink-2">
            {deleteError ? (
              <Alert tone="error" title="That did not work">
                {deleteError}
              </Alert>
            ) : null}
            <p>
              {gstToDelete.address_count > 0
                ? `${countLabel(gstToDelete.address_count, "address", "addresses")} ${
                    gstToDelete.address_count === 1 ? "links" : "link"
                  } to it and will be left with no GST. The ${
                    gstToDelete.address_count === 1 ? "address is" : "addresses are"
                  } not deleted — but until you link ${
                    gstToDelete.address_count === 1 ? "it" : "them"
                  } to another GST, a run using ${
                    gstToDelete.address_count === 1 ? "it" : "one of them"
                  } bills under whichever GST is picked for that run.`
                : "No address links to it."}
            </p>
            {(gstToDelete.account_count ?? 0) > 0 ? (
              <p>
                {countLabel(gstToDelete.account_count ?? 0, "account")}{" "}
                {gstToDelete.account_count === 1 ? "has" : "have"} it as{" "}
                {gstToDelete.account_count === 1 ? "its" : "their"} own GST. The accounts are kept, but from then
                on their orders bill under the GST each run picks — set another GST on them on the Accounts page.
              </p>
            ) : null}
            {gstToDelete.is_default ? (
              <p>
                It is also the default GST, so a run that does not pick a GST bills under the chosen
                address&apos;s linked GST — or without GST details — until you mark another one.
              </p>
            ) : null}
          </div>
        ) : null}
      </Dialog>

      {gstRows.length > 0 && defaultGst === null ? (
        <Alert tone="warning" title="No GST is marked default">
          A run whose address is not linked to a GST, and that picks none, is placed without GST
          details. Mark the one you normally bill under, or link each address to its own.
        </Alert>
      ) : null}
    </div>
  );
}
