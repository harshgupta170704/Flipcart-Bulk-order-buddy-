// @vitest-environment jsdom
/**
 * A GSTIN the server refuses is explained at its field, in words, not as the
 * server's pattern (spec FR-053; review of 27 Oct 2026).
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ createGst: vi.fn() }));

vi.mock("../lib/api", () => {
  class ApiError extends Error {
    constructor(
      readonly status: number,
      message: string,
      readonly detail?: unknown,
    ) {
      super(message);
    }
  }
  return {
    ApiError,
    createGst: mocks.createGst,
    updateGst: vi.fn(),
    createAddress: vi.fn(),
    updateAddress: vi.fn(),
    errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
  };
});

import { ApiError } from "../lib/api";
import { GSTIN_REFUSED, GstForm } from "./AddressForms";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function fill() {
  fireEvent.change(screen.getByLabelText(/Label/), { target: { value: "SONIC" } });
  fireEvent.change(screen.getByLabelText(/GSTIN/), { target: { value: "33AAECS4321F1XK" } });
  fireEvent.change(screen.getByLabelText(/Business name/i), { target: { value: "Sonic Kommerce" } });
}

describe("the GST form", () => {
  it("explains a GSTIN the server refuses, at the field", async () => {
    const pattern = "String should match pattern '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'";
    mocks.createGst.mockRejectedValue(
      new (ApiError as unknown as new (s: number, m: string, d: unknown) => Error)(422, `gstin: ${pattern}`, [
        { loc: ["body", "gstin"], msg: pattern, type: "string_pattern_mismatch" },
      ]),
    );
    render(<GstForm gst={null} firstEver={false} onSaved={() => undefined} onCancel={() => undefined} />);
    fill();
    fireEvent.submit(screen.getByLabelText(/GSTIN/).closest("form")!);

    expect(await screen.findByText(GSTIN_REFUSED)).toBeTruthy();
    expect(screen.queryByText(/should match pattern/)).toBeNull();

    // Typing again clears it.
    fireEvent.change(screen.getByLabelText(/GSTIN/), { target: { value: "33AAECS4321F1ZK" } });
    expect(screen.queryByText(GSTIN_REFUSED)).toBeNull();
  });

  it("keeps any other refusal as the server words it", async () => {
    mocks.createGst.mockRejectedValue(new Error("the server is down"));
    render(<GstForm gst={null} firstEver={false} onSaved={() => undefined} onCancel={() => undefined} />);
    fill();
    fireEvent.submit(screen.getByLabelText(/GSTIN/).closest("form")!);
    expect(await screen.findByText(/the server is down/)).toBeTruthy();
  });
});
