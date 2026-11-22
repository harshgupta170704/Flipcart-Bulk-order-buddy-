// @vitest-environment jsdom
/**
 * The archive list's request (spec FR-038): the newest page with nothing
 * added, and an older page with the server's own `next_before` sent back as
 * `before`, whole — the key holds colons and a comma.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { listArchives } from "./api";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(
    async () =>
      new Response(JSON.stringify({ items: [], total: 0, next_before: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("listArchives", () => {
  it("asks for the newest page, then for the one after the key it was given", async () => {
    expect(await listArchives()).toEqual({ items: [], total: 0, next_before: null });
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/invoices/archives");

    await listArchives("2026-10-24T10:04:00.250000,4");

    const asked = new URL(String(fetchMock.mock.calls[1][0]), "http://test");
    expect(asked.pathname).toBe("/api/invoices/archives");
    expect(asked.searchParams.get("before")).toBe("2026-10-24T10:04:00.250000,4");
  });
});
