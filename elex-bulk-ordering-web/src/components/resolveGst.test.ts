/**
 * The GST ladder the picker reads back must be the one the server walks
 * (`resolve_destination` in app/jobs/tasks.py): chosen, then the address's
 * linked GST, then the default, then none.
 */
import { describe, expect, it } from "vitest";
import type { Address, Gst } from "../lib/types";
import { resolveGst } from "./AddressBookData";

function gst(id: number, label: string, is_default = false): Gst {
  return {
    id,
    label,
    gstin: `29${label.padEnd(10, "X").slice(0, 10)}1Z5`,
    business_name: `${label} LLP`,
    is_default,
    address_count: 0,
    created_at: "2026-10-24T00:00:00",
  };
}

function address(linked: Gst | null): Address {
  return {
    id: 1,
    label: "hub",
    name: "Hub",
    phone: null,
    line1: "1 Hub Road",
    floor: null,
    city: "Pune",
    state: null,
    pincode: "411001",
    is_default: true,
    gst:
      linked === null
        ? null
        : { id: linked.id, label: linked.label, gstin: linked.gstin, business_name: linked.business_name },
    created_at: "2026-10-24T00:00:00",
  };
}

describe("resolveGst", () => {
  const digidrive = gst(1, "DIGIDRIVE");
  const globe = gst(2, "GLOBE", true);
  const rows = [digidrive, globe];

  it("a chosen GST wins over the address and the default", () => {
    expect(resolveGst(address(digidrive), globe, rows)).toEqual({ gst: globe, source: "chosen" });
  });

  it("an unchosen GST follows the address before the default", () => {
    expect(resolveGst(address(digidrive), null, rows)).toEqual({ gst: digidrive, source: "linked" });
  });

  it("an unlinked address falls to the default GST", () => {
    expect(resolveGst(address(null), null, rows)).toEqual({ gst: globe, source: "default" });
  });

  it("with nothing marked default there is no GST — the server never guesses", () => {
    expect(resolveGst(address(null), null, [digidrive])).toEqual({ gst: null, source: null });
    expect(resolveGst(null, null, [])).toEqual({ gst: null, source: null });
  });

  it("a linked GST that is no longer on file is ignored", () => {
    expect(resolveGst(address(gst(9, "GONE")), null, rows)).toEqual({ gst: globe, source: "default" });
  });
});
