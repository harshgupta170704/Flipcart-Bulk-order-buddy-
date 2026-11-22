// @vitest-environment jsdom
/**
 * The run summary's "Bills under": the run's GST applies to the accounts
 * without one of their own — since 26 Oct 2026 an account may carry its own,
 * and its orders bill under that one. The summary says so, with the count
 * when the page knows which accounts the run will use.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Address, Gst } from "../lib/types";

const GST: Gst = {
  id: 1,
  label: "Akansha Traders",
  gstin: "09AAAAA0000A1Z5",
  business_name: "Akansha Traders",
  is_default: true,
  address_count: 1,
  account_count: 2,
  created_at: "2026-10-26T00:00:00",
};

const ADDRESS: Address = {
  id: 3,
  label: "Lucknow",
  name: "N",
  phone: null,
  line1: "1 Hazratganj",
  floor: null,
  city: "Lucknow",
  state: "Uttar Pradesh",
  pincode: "226001",
  is_default: true,
  gst: null,
  created_at: "2026-10-26T00:00:00",
};

vi.mock("./AddressBookData", async () => {
  const actual = await vi.importActual<typeof import("./AddressBookData")>("./AddressBookData");
  return {
    ...actual,
    useAddresses: () => ({ rows: [ADDRESS], loading: false, error: null, reload: () => undefined }),
    useGsts: () => ({ rows: [GST], loading: false, error: null, reload: () => undefined }),
  };
});

import { AddressGstPicker, type OwnGstCount } from "./AddressGstPicker";

afterEach(cleanup);

function show(ownGst?: OwnGstCount | null) {
  render(
    <MemoryRouter>
      <AddressGstPicker value={{ address_id: ADDRESS.id, gst_id: null }} onChange={() => undefined} ownGst={ownGst} />
    </MemoryRouter>,
  );
}

describe("AddressGstPicker's Bills under", () => {
  it("says an account with its own GST bills under that one, when the page cannot count them", () => {
    show(null);
    expect(screen.getByText("09AAAAA0000A1Z5")).toBeTruthy();
    expect(screen.getByText("An account with a GST of its own bills under that one instead.")).toBeTruthy();
  });

  it("counts the selected accounts that have one", () => {
    show({ count: 2, of: 5 });
    expect(screen.getByText("2 of the 5 selected accounts have a GST of their own and bill under it instead.")).toBeTruthy();
  });

  it("words one account, all of them and none of them plainly", () => {
    show({ count: 1, of: 3 });
    expect(screen.getByText("1 of the 3 selected accounts has a GST of its own and bills under it instead.")).toBeTruthy();
    cleanup();
    show({ count: 4, of: 4 });
    expect(screen.getByText("All 4 selected accounts have a GST of their own and bill under it instead.")).toBeTruthy();
    cleanup();
    show({ count: 0, of: 4 });
    expect(screen.getByText("None of the selected accounts has a GST of its own.")).toBeTruthy();
  });
});
