// @vitest-environment jsdom
/**
 * The bank's step finished by hand in the order run's multipart body
 * (27 Oct 2026). The field travels only when ticked: POST /jobs/order reads a
 * missing one as "the tool does the bank's step", as before.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { startOrderJob } from "./api";
import type { OrderJobInput } from "./types";

const base: OrderJobInput = {
  product_link: "https://www.flipkart.com/pigeon-pressure-cooker/p/itm0000000000001",
  qty: 1,
  target: 700,
  no_cap: false,
  account_ids: [7, 8],
  payment_mode: "card",
  cards_file: new File(["Email,Card Number,Expiry,CVV"], "cards.csv", { type: "text/csv" }),
  address_id: 1,
  gst_id: null,
};

describe("startOrderJob's bank_step_by_hand field", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ id: 66 }), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function sent(): FormData {
    const init = fetchMock.mock.calls[fetchMock.mock.calls.length - 1][1] as RequestInit;
    return init.body as FormData;
  }

  it("sends it when ticked", async () => {
    await startOrderJob({ ...base, bank_step_by_hand: true });

    expect(sent().get("bank_step_by_hand")).toBe("true");
  });

  it.each([false, undefined])("leaves the field out for %j", async (value) => {
    await startOrderJob({ ...base, bank_step_by_hand: value });

    expect(sent().has("bank_step_by_hand")).toBe(false);
  });
});
