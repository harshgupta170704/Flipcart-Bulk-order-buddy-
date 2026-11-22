// @vitest-environment jsdom
/**
 * The chosen seller in the order run's multipart body (wave 2, package A).
 * POST /jobs/order reads a missing `seller` as Flipkart's default seller, so
 * the field travels only when there is one — never as an empty string.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { startOrderJob } from "./api";
import type { OrderJobInput } from "./types";

const base: OrderJobInput = {
  product_link: "https://www.flipkart.com/pigeon-pressure-cooker/p/itm0000000000001",
  qty: 1,
  target: 18250,
  no_cap: false,
  account_ids: [7, 8],
  payment_mode: "cod",
  cards_file: null,
  address_id: 1,
  gst_id: null,
};

describe("startOrderJob's seller field", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ id: 52 }), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function sent(): FormData {
    const init = fetchMock.mock.calls[fetchMock.mock.calls.length - 1][1] as RequestInit;
    return init.body as FormData;
  }

  it("sends a chosen seller, trimmed", async () => {
    await startOrderJob({ ...base, seller: "  SAPHEDretail " });

    expect(sent().get("seller")).toBe("SAPHEDretail");
  });

  it.each([null, undefined, "", "   "])("leaves the field out for %j", async (seller) => {
    await startOrderJob({ ...base, seller });

    expect(sent().has("seller")).toBe(false);
  });
});
