// @vitest-environment jsdom
/**
 * A streamed export can fail after its first bytes (spec NFR-009): the server
 * has already said 200, so the only sign is a body that breaks off. The
 * browser then rejects the read with its own wording ("network error",
 * "Failed to fetch", "Load failed"). The download must say what happened in
 * plain words and hand nothing half-written to the browser.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, downloadInvoiceData, downloadOrders, downloadOrdersCsv } from "./api";

/** A 200 whose body sends a first chunk, then breaks off. */
function brokenOff(): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("Unit ID,Status\n"));
      controller.error(new TypeError("terminated"));
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Disposition": 'attachment; filename="order_units.csv"' },
  });
}

describe("a download that breaks off part-way", () => {
  const fetchMock = vi.fn();
  // jsdom can neither mint a blob: URL nor follow one.
  const urlStatics = URL as unknown as Record<string, unknown>;
  const createObjectURL = vi.fn(() => "blob:test");
  let click: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockImplementation(async () => brokenOff());
    vi.stubGlobal("fetch", fetchMock);
    createObjectURL.mockClear();
    urlStatics.createObjectURL = createObjectURL;
    urlStatics.revokeObjectURL = () => undefined;
    click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    delete urlStatics.createObjectURL;
    delete urlStatics.revokeObjectURL;
  });

  it("says so in plain words and saves nothing (the ledger CSV)", async () => {
    const failure = await downloadOrdersCsv({ q: "vivo" }).catch((err: unknown) => err);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).message).toBe("The download stopped part-way, so nothing was saved. Try again.");
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(click).not.toHaveBeenCalled();
  });

  it("does the same for the workbook and the invoice data", async () => {
    for (const run of [() => downloadOrders({}), () => downloadInvoiceData({}, ["product"])]) {
      const failure = await run().catch((err: unknown) => err);
      expect(failure).toBeInstanceOf(ApiError);
      expect((failure as ApiError).message).toMatch(/stopped part-way/);
    }
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(click).not.toHaveBeenCalled();
  });

  it("still hands a whole file to the browser", async () => {
    fetchMock.mockImplementation(async () => new Response("Unit ID,Status\n1,placed\n", { status: 200 }));

    await downloadOrdersCsv({});

    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(click).toHaveBeenCalledTimes(1);
  });
});
