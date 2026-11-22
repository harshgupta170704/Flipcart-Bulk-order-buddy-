// @vitest-environment jsdom
/**
 * The query string is the contract with backend/app/api/orders.py: one builder
 * feeds the page, the count and both exports, and its names must be the ones
 * parse_filter() reads. A rename on either side fails here, not in a download
 * that quietly holds different rows from the screen that offered it. The two
 * downloads are exercised too, because `sheet` is theirs alone: it picks the
 * owner's layout and must never leak into the ledger CSV or the workbook.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { downloadOrders, downloadOrdersCsv, orderParams } from "./api";

describe("orderParams", () => {
  it("sends nothing for an empty filter", () => {
    expect(orderParams({}).toString()).toBe("");
  });

  it("uses the backend's parameter names, repeating list values", () => {
    const params = orderParams({
      q: " vivo ",
      status: "placed",
      statuses: ["unconfirmed"],
      platform_statuses: ["Cancelled - Order shipped"],
      processing_stages: ["Card OTP Pending"],
      error_codes: ["card_otp_incorrect_or_unfetched"],
      delivery_states: ["unknown"],
      order_ids: ["OD1", " OD2 ", ""],
      emails: ["a@x.com"],
      tracking_ids: ["TRK1"],
      card: "4111 1111",
      account_id: 3,
      job_id: 7,
      date_field: "delivery_date",
      date_from: "2026-10-01",
      date_to: "2026-10-24",
    });
    expect(params.get("q")).toBe("vivo");
    expect(params.getAll("status")).toEqual(["placed", "unconfirmed"]);
    expect(params.getAll("platform_status")).toEqual(["Cancelled - Order shipped"]);
    expect(params.getAll("processing_stage")).toEqual(["Card OTP Pending"]);
    expect(params.getAll("error_code")).toEqual(["card_otp_incorrect_or_unfetched"]);
    expect(params.getAll("delivery_state")).toEqual(["unknown"]);
    expect(params.getAll("order_id")).toEqual(["OD1", "OD2"]);
    expect(params.getAll("email")).toEqual(["a@x.com"]);
    expect(params.getAll("tracking_id")).toEqual(["TRK1"]);
    expect(params.get("card")).toBe("4111 1111");
    expect(params.get("account_id")).toBe("3");
    expect(params.get("job_id")).toBe("7");
    expect(params.get("date_field")).toBe("delivery_date");
    expect(params.get("date_from")).toBe("2026-10-01");
    expect(params.get("date_to")).toBe("2026-10-24");
  });

  it("sends the card filter as typed, only trimmed — the server picks the digits out", () => {
    // Spaces between the groups stay; so would letters. Which characters count
    // is the server's rule (app.core.order_filter.card_digits), not repeated here.
    expect(orderParams({ card: "  4111 1111  " }).get("card")).toBe("4111 1111");
    expect(orderParams({ card: "abcd" }).get("card")).toBe("abcd");
    expect(orderParams({ card: "   " }).has("card")).toBe(false);
  });

  it("omits what is unset, blank or null", () => {
    const params = orderParams({
      q: "  ",
      status: "",
      account_id: null,
      job_id: null,
      date_from: null,
      date_to: null,
      order_ids: [" "],
    });
    expect(params.toString()).toBe("");
  });

  it("never carries the page into an export", () => {
    // page/size are the list's business (listOrders adds them); an export of
    // "the rows you can see" means every matching row, not one page of them.
    expect(orderParams({ page: 3, size: 50 }).toString()).toBe("");
  });
});

describe("the export downloads", () => {
  const fetchMock = vi.fn();
  // jsdom can neither mint a blob: URL nor follow one; the request is what is under test.
  const urlStatics = URL as unknown as Record<string, unknown>;

  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockImplementation(async () => new Response("a,b\n", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    urlStatics.createObjectURL = () => "blob:test";
    urlStatics.revokeObjectURL = () => undefined;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    delete urlStatics.createObjectURL;
    delete urlStatics.revokeObjectURL;
  });

  /** The URL the last download asked the server for. */
  function requested(): URL {
    return new URL(String(fetchMock.mock.calls[fetchMock.mock.calls.length - 1][0]), "http://test");
  }

  it("names the sheet in the query string only for the owner's two sheets", async () => {
    await downloadOrdersCsv({ q: "vivo", card: "4111" }, "out_for_delivery");
    expect(requested().pathname).toBe("/api/orders/export.csv");
    expect(requested().searchParams.get("sheet")).toBe("out_for_delivery");
    // The sheet rides along with the same filter as the screen, not instead of it.
    expect(requested().searchParams.get("q")).toBe("vivo");
    expect(requested().searchParams.get("card")).toBe("4111");

    await downloadOrdersCsv({ q: "vivo" }, "order_data");
    expect(requested().searchParams.get("sheet")).toBe("order_data");

    await downloadOrdersCsv({ q: "vivo" });
    expect(requested().pathname).toBe("/api/orders/export.csv");
    expect(requested().searchParams.has("sheet")).toBe(false);

    await downloadOrders({ q: "vivo" });
    expect(requested().pathname).toBe("/api/orders/export");
    expect(requested().searchParams.has("sheet")).toBe(false);
  });
});
