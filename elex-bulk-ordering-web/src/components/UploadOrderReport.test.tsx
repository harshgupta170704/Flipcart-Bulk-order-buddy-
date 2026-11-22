// @vitest-environment jsdom
/**
 * The order report card: it says what the report did — units added, updated,
 * unchanged, every refused row with its reason, the columns not kept — reloads
 * the ledger when something changed, and offers the template.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { OrderReportImportResult } from "../lib/types";

const mocks = vi.hoisted(() => ({
  importOrderReport: vi.fn(),
  downloadReportTemplate: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  importOrderReport: mocks.importOrderReport,
  downloadReportTemplate: mocks.downloadReportTemplate,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { UploadOrderReport } from "./UploadOrderReport";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function upload(result: OrderReportImportResult | Error, onChanged = vi.fn()) {
  if (result instanceof Error) mocks.importOrderReport.mockRejectedValue(result);
  else mocks.importOrderReport.mockResolvedValue(result);
  render(<UploadOrderReport onChanged={onChanged} />);
  const report = new File(["platform,order_id\n"], "report.csv", { type: "text/csv" });
  fireEvent.change(screen.getByLabelText("Order report"), { target: { files: [report] } });
  fireEvent.click(screen.getByRole("button", { name: "Upload report" }));
  await waitFor(() => expect(mocks.importOrderReport).toHaveBeenCalledWith(report));
  return onChanged;
}

describe("UploadOrderReport", () => {
  it("says what the report did, row by row, and reloads the ledger", async () => {
    const onChanged = await upload({
      created: 2,
      updated: 1,
      unchanged: 3,
      kept_platform: 0,
      rejected: [{ row: 4, reason: "platform 'elex' is not supported yet: Flipkart only for now" }],
      not_kept: ["imei", "refund_status"],
    });
    expect(await screen.findByText(/units added/)).toBeTruthy();
    expect(screen.getByText(/Row 4: platform 'elex' is not supported yet/)).toBeTruthy();
    expect(screen.getByText(/Not kept .* imei, refund_status/)).toBeTruthy();
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Flipkart's own page says otherwise/)).toBeNull();
  });

  it("reloads nothing when the report changed nothing", async () => {
    const onChanged = await upload({ created: 0, updated: 0, unchanged: 5, kept_platform: 0, rejected: [], not_kept: [] });
    expect(await screen.findByText(/already as the report says/)).toBeTruthy();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("says when Flipkart's own reading was kept over the report's", async () => {
    // "1 updated" alone would read as if the report's status had gone in.
    await upload({ created: 0, updated: 1, unchanged: 0, kept_platform: 1, rejected: [], not_kept: [] });
    expect(await screen.findByText(/Flipkart's own page says otherwise for 1 unit \(/)).toBeTruthy();
    expect(screen.getByText(/its reading was kept, and only what it left empty was filled/)).toBeTruthy();
  });

  it("shows the server's refusal of the whole file", async () => {
    await upload(new Error("the header row needs the columns platform, order_id, unit_id, order_date, status, product (missing: unit_id)"));
    expect(await screen.findByText(/missing: unit_id/)).toBeTruthy();
  });

  it("offers the template", async () => {
    mocks.downloadReportTemplate.mockResolvedValue(undefined);
    render(<UploadOrderReport onChanged={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Download the template" }));
    await waitFor(() => expect(mocks.downloadReportTemplate).toHaveBeenCalledTimes(1));
  });
});
