// @vitest-environment jsdom
/**
 * The Track-orders-from-a-sheet card lists every row the server refused, each
 * with its reason: the first twenty at once, and all of them one click away —
 * a long sheet can be refused row by row, and each row is one to fix.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { OrderIdsImportResult } from "../lib/types";

const mocks = vi.hoisted(() => ({
  importOrderIds: vi.fn(),
  startSyncJob: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  importOrderIds: mocks.importOrderIds,
  startSyncJob: mocks.startSyncJob,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { UploadOrderIds } from "./UploadOrderIds";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function refused(count: number): OrderIdsImportResult["rejected"] {
  return Array.from({ length: count }, (_, index) => ({
    row: index + 2,
    reason: `unit id '4.39123E+20' was saved as a number (sheet row ${index + 2})`,
  }));
}

async function upload(result: OrderIdsImportResult) {
  mocks.importOrderIds.mockResolvedValue(result);
  render(<UploadOrderIds onChanged={() => undefined} />);
  const sheet = new File(["Email,Order ID\n"], "ids.csv", { type: "text/csv" });
  fireEvent.change(screen.getByLabelText("Order id sheet"), { target: { files: [sheet] } });
  fireEvent.click(screen.getByRole("button", { name: "Upload" }));
  await waitFor(() => expect(screen.getByText(/rows? refused/)).toBeTruthy());
}

function listedRows(): string[] {
  return within(screen.getByRole("list", { name: "Refused rows" }))
    .getAllByRole("listitem")
    .map((item) => item.textContent ?? "");
}

describe("UploadOrderIds", () => {
  it("lists the first twenty refused rows, and every one of them on request", async () => {
    await upload({ created: 3, existing: 0, rejected: refused(45), account_ids: [1], order_ids: ["OD1", "OD2", "OD3"] });

    expect(screen.getByText("45")).toBeTruthy();
    let rows = listedRows();
    expect(rows).toHaveLength(21);
    expect(rows[0]).toBe("Row 2: unit id '4.39123E+20' was saved as a number (sheet row 2)");
    expect(rows[19]).toContain("Row 21:");
    expect(rows[20]).toBe("… and 25 more");

    const showAll = screen.getByRole("button", { name: "Show all 45" });
    expect(showAll.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(showAll);

    rows = listedRows();
    expect(rows).toHaveLength(45);
    expect(rows[44]).toBe("Row 46: unit id '4.39123E+20' was saved as a number (sheet row 46)");
    expect(screen.queryByText(/and 25 more/)).toBeNull();

    const fewer = screen.getByRole("button", { name: "Show the first 20" });
    expect(fewer.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(fewer);
    expect(listedRows()).toHaveLength(21);
  });

  it("offers no extra control when every refused row is already listed", async () => {
    await upload({ created: 0, existing: 1, rejected: refused(20), account_ids: [1], order_ids: ["OD1"] });

    expect(listedRows()).toHaveLength(20);
    expect(screen.queryByRole("button", { name: /^Show / })).toBeNull();
  });

  it("starts the next upload's list folded again", async () => {
    await upload({ created: 0, existing: 0, rejected: refused(30), account_ids: [], order_ids: [] });
    fireEvent.click(screen.getByRole("button", { name: "Show all 30" }));
    expect(listedRows()).toHaveLength(30);

    mocks.importOrderIds.mockResolvedValue({ created: 0, existing: 0, rejected: refused(25), account_ids: [], order_ids: [] });
    const again = new File(["Email,Order ID\n"], "ids-2.csv", { type: "text/csv" });
    fireEvent.change(screen.getByLabelText("Order id sheet"), { target: { files: [again] } });
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));

    expect(await screen.findByRole("button", { name: "Show all 25" })).toBeTruthy();
    expect(listedRows()).toHaveLength(21);
  });
});
