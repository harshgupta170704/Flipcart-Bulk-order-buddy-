// @vitest-environment jsdom
/**
 * The Put-invoices-on-file card: it takes PDFs and the zips Flipkart sends,
 * says so, and reports every file's outcome — a zip's members under
 * "<zip>/<member>" the way the server names them.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  uploadInvoices: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  uploadInvoices: mocks.uploadInvoices,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { UploadInvoices } from "./UploadInvoices";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("UploadInvoices", () => {
  it("accepts PDFs and zips and says what a zip is for", () => {
    render(<UploadInvoices onChanged={() => undefined} />);

    const input = screen.getByLabelText("Invoice PDFs or zips") as HTMLInputElement;
    expect(input.accept.split(",")).toEqual(
      expect.arrayContaining(["application/pdf", ".pdf", "application/zip", ".zip"]),
    );
    expect(input.multiple).toBe(true);
    expect(screen.getByText(/the zips Flipkart sends/)).toBeTruthy();
  });

  it("uploads what was chosen and lists each file's outcome, zip members included", async () => {
    mocks.uploadInvoices.mockResolvedValue({
      stored: 2,
      files: [
        { filename: "OD338918803190451100.zip/file_1.pdf", order_id: "OD338918803190451100", status: "stored", reason: null },
        { filename: "OD338918803190451100.zip/file_2.pdf", order_id: "OD338918803190451100", status: "stored", reason: null },
        { filename: "notes.zip", order_id: null, status: "not_pdf", reason: "the zip holds no PDF" },
      ],
    });
    const onChanged = vi.fn();
    render(<UploadInvoices onChanged={onChanged} />);

    const input = screen.getByLabelText("Invoice PDFs or zips") as HTMLInputElement;
    const zip = new File([new Uint8Array([0x50, 0x4b, 0x03, 0x04])], "OD338918803190451100.zip", {
      type: "application/zip",
    });
    const other = new File([new Uint8Array([0x50, 0x4b, 0x03, 0x04])], "notes.zip", { type: "application/zip" });
    fireEvent.change(input, { target: { files: [zip, other] } });
    fireEvent.click(screen.getByRole("button", { name: /Upload 2 files/ }));

    await waitFor(() => expect(mocks.uploadInvoices).toHaveBeenCalledTimes(1));
    expect(mocks.uploadInvoices.mock.calls[0][0]).toHaveLength(2);
    await waitFor(() => expect(screen.getByText(/invoices put on file/)).toBeTruthy());
    expect(screen.getByText("OD338918803190451100.zip/file_1.pdf")).toBeTruthy();
    expect(screen.getByText("OD338918803190451100.zip/file_2.pdf")).toBeTruthy();
    expect(screen.getByText(/Not a PDF: the zip holds no PDF/)).toBeTruthy();
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("says a PDF put on file that could not be read, with the reason (NFR-009)", async () => {
    mocks.uploadInvoices.mockResolvedValue({
      stored: 2,
      files: [
        { filename: "file_1.pdf", order_id: "OD332627468086718100", status: "stored", reading: "read", reason: null },
        {
          filename: "scan.pdf",
          order_id: "OD332627468086718100",
          status: "stored",
          reading: "not_read",
          reason: "too many pages to read: 25",
        },
        {
          filename: "file_2.pdf",
          order_id: "OD332627468086718100",
          status: "not_saved",
          reading: null,
          reason: "could not be saved (OperationalError); the other files went through — upload this one again",
        },
      ],
    });
    render(<UploadInvoices onChanged={() => undefined} />);

    const input = screen.getByLabelText("Invoice PDFs or zips") as HTMLInputElement;
    const pdf = new File([new Uint8Array([0x25, 0x50, 0x44, 0x46])], "scan.pdf", { type: "application/pdf" });
    fireEvent.change(input, { target: { files: [pdf] } });
    fireEvent.click(screen.getByRole("button", { name: /Upload 1 file/ }));

    await waitFor(() => expect(screen.getByText(/invoices put on file/)).toBeTruthy());
    const items = screen.getAllByRole("listitem").map((item) => item.textContent);
    expect(items).toEqual([
      "file_1.pdf — On file (OD332627468086718100)",
      "scan.pdf — Stored, but could not be read (OD332627468086718100): too many pages to read: 25",
      "file_2.pdf — Not saved (OD332627468086718100): could not be saved (OperationalError); the other files went through — upload this one again",
    ]);
    expect(screen.getByText(/invoices put on file/).textContent).toBe("2 invoices put on file. 1 could not be read.");
  });
});
