/**
 * Loading the two lists behind the address book, plus the handful of display
 * helpers the page and the ordering form both need.
 *
 * Both lists are short and hand-managed (one row per business entity), so each
 * is fetched whole and read from exactly one place: the page edits them, the
 * ordering form picks from them, and nothing anywhere derives a delivery
 * address by itself.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage, listAddresses, listGsts } from "../lib/api";
import type { Address, Gst } from "../lib/types";

export interface ListState<T> {
  /** null until a load has succeeded — "not loaded yet" is not "empty". */
  rows: T[] | null;
  loading: boolean;
  /** Set on a failed load. `rows` keeps whatever last arrived. */
  error: string | null;
  reload: () => void;
}

/**
 * `fetcher` must be a stable function (a module-level one) — it is the only
 * dependency of `reload`, which the first-load effect runs.
 */
function useList<T>(fetcher: () => Promise<T[]>): ListState<T> {
  const [rows, setRows] = useState<T[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  /* Editing a row reloads both lists, so two loads can be in the air at once.
     Only the newest one is allowed to land — otherwise a slow earlier reply
     overwrites the fresh list with what the table looked like before the edit. */
  const ticket = useRef(0);

  const reload = useCallback(() => {
    ticket.current += 1;
    const mine = ticket.current;
    setLoading(true);
    fetcher().then(
      (next) => {
        if (!alive.current || ticket.current !== mine) return;
        setRows(next);
        setError(null);
        setLoading(false);
      },
      (err: unknown) => {
        if (!alive.current || ticket.current !== mine) return;
        setError(errorMessage(err));
        setLoading(false);
      },
    );
  }, [fetcher]);

  useEffect(() => {
    reload();
  }, [reload]);

  return { rows, loading, error, reload };
}

export function useAddresses(): ListState<Address> {
  return useList(listAddresses);
}

export function useGsts(): ListState<Gst> {
  return useList(listGsts);
}

/* --- display -------------------------------------------------------------- */

/**
 * The row a run should start from: the one marked default, and nothing else.
 * With none marked the answer is null — the picker then waits for a choice,
 * exactly as the server refuses a run that names no address. Quietly opening
 * on the first row would re-create the fallback the server was rid of.
 */
export function defaultRow<T extends { is_default: boolean }>(rows: T[]): T | null {
  return rows.find((row) => row.is_default) ?? null;
}

/** How the GST a run bills under was arrived at. */
export type GstSource = "chosen" | "linked" | "default";

export interface ResolvedGst {
  gst: Gst | null;
  source: GstSource | null;
}

/**
 * The GST a run bills under, as the server will resolve it: the one chosen for
 * the run, else the one the address is linked to, else the one marked default,
 * else none. Mirrors `resolve_destination` in the backend, so what the operator
 * reads back before starting is what the order will carry. The last rung is
 * like `defaultRow`'s: an unmarked list yields nothing — the server never
 * guesses a GST, it places the order without GST details and says so.
 */
export function resolveGst(address: Address | null, chosen: Gst | null, gsts: Gst[]): ResolvedGst {
  if (chosen !== null) return { gst: chosen, source: "chosen" };
  const linkedId = address?.gst?.id;
  if (linkedId !== undefined) {
    const linked = gsts.find((row) => row.id === linkedId);
    if (linked !== undefined) return { gst: linked, source: "linked" };
  }
  const marked = gsts.find((row) => row.is_default);
  return marked === undefined ? { gst: null, source: null } : { gst: marked, source: "default" };
}

/** "12A Nehru Road, 2nd floor" — the street line plus the floor when set. */
export function addressStreet(address: Address): string {
  const floor = address.floor?.trim() ?? "";
  return floor === "" ? address.line1 : `${address.line1}, ${floor}`;
}

/** "Bengaluru, Karnataka 560001" — whichever parts exist, in postal order. */
export function addressPlace(address: Address): string {
  const parts = [address.city?.trim() ?? "", address.state?.trim() ?? ""].filter(
    (part) => part !== "",
  );
  const where = parts.join(", ");
  return where === "" ? address.pincode : `${where} ${address.pincode}`;
}

/** One line, for a <select> option. The default is marked so it reads as one. */
export function addressOption(address: Address): string {
  const place = address.city?.trim() ?? "";
  const tail = place === "" ? address.pincode : place;
  return `${address.label} — ${address.name}, ${tail}${address.is_default ? " (default)" : ""}`;
}

/** One line for a GST <select> option: label, the GSTIN itself, default mark. */
export function gstOption(gst: Gst): string {
  return `${gst.label} — ${gst.gstin}${gst.is_default ? " (default)" : ""}`;
}
