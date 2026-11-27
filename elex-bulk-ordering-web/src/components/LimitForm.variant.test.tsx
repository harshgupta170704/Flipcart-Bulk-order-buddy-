// @vitest-environment jsdom
/**
 * A variant limit in the limit editor (Smart ID, part 2): its value is the
 * product's Flipkart id, the pid in its link, and never blank (a blank one
 * would count every order of the account). The form says what the value is,
 * checks its shape before saving, keeps it upper-case, and shows the server's
 * own refusal as it shows any other.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { UnitLimit } from "../lib/types";

const mocks = vi.hoisted(() => ({
  createUnitLimit: vi.fn(),
  updateUnitLimit: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { ApiError } from "../lib/api";
import { LimitForm } from "./LimitForm";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const HINT = "The product's Flipkart id: the part after pid= in its link, for example MOBHNDTD57UJUK9Q.";
const BLANK = "A variant limit needs the product's Flipkart id (the pid in its link).";

function saved(over: Partial<UnitLimit> = {}): UnitLimit {
  return {
    id: 12,
    scope: "variant",
    value: "MOBHNDTD57UJUK9Q",
    max_units: 2,
    window_days: 30,
    enabled: true,
    created_at: "2026-11-24T10:00:00",
    ...over,
  };
}

function newLimit(scope: string, value: string) {
  render(<LimitForm limit={null} onSaved={vi.fn()} onCancel={vi.fn()} />);
  fireEvent.change(screen.getByLabelText(/^Scope/), { target: { value: scope } });
  fireEvent.change(screen.getByLabelText(/^Value/), { target: { value } });
  fireEvent.change(screen.getByLabelText(/^Max units/), { target: { value: "2" } });
  fireEvent.change(screen.getByLabelText(/^Window/), { target: { value: "30" } });
}

describe("LimitForm: a variant limit", () => {
  it("says what the value is", () => {
    render(<LimitForm limit={null} onSaved={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.queryByText(HINT)).toBeNull();
    fireEvent.change(screen.getByLabelText(/^Scope/), { target: { value: "variant" } });
    expect(screen.getByText(HINT)).toBeTruthy();
    expect((screen.getByLabelText(/^Value/) as HTMLInputElement).placeholder).toBe("MOBHNDTD57UJUK9Q");
  });

  it("refuses a blank value, and saves nothing", async () => {
    newLimit("variant", "  ");
    fireEvent.click(screen.getByRole("button", { name: "Add limit" }));

    expect(await screen.findByText(BLANK)).toBeTruthy();
    expect(mocks.createUnitLimit).not.toHaveBeenCalled();
  });

  it("refuses a value that is not a Flipkart product id", async () => {
    for (const value of ["MOB-HNDTD57", "AB12", "A".repeat(41)]) {
      newLimit("variant", value);
      fireEvent.click(screen.getByRole("button", { name: "Add limit" }));
      expect(
        await screen.findByText("That is not a Flipkart product id: 6 to 40 letters and digits, like MOBHNDTD57UJUK9Q."),
      ).toBeTruthy();
      cleanup();
    }
    expect(mocks.createUnitLimit).not.toHaveBeenCalled();
  });

  it("saves a product id upper-case", async () => {
    mocks.createUnitLimit.mockResolvedValue(saved());
    newLimit("variant", " mobhndtd57ujuk9q ");
    fireEvent.click(screen.getByRole("button", { name: "Add limit" }));

    await waitFor(() => expect(mocks.createUnitLimit).toHaveBeenCalledTimes(1));
    expect(mocks.createUnitLimit.mock.calls[0][0]).toEqual({
      scope: "variant",
      value: "MOBHNDTD57UJUK9Q",
      max_units: 2,
      window_days: 30,
      enabled: true,
    });
  });

  it("shows the server's refusal as any other", async () => {
    mocks.updateUnitLimit.mockRejectedValue(new ApiError(422, BLANK));
    render(<LimitForm limit={saved()} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Save limit" }));

    expect(await screen.findByText("Could not save this limit")).toBeTruthy();
    expect(screen.getByText(BLANK)).toBeTruthy();
  });

  it("still lets other scopes be blank, for every value of the scope", async () => {
    mocks.createUnitLimit.mockResolvedValue(saved({ scope: "brand", value: "" }));
    newLimit("brand", "");
    fireEvent.click(screen.getByRole("button", { name: "Add limit" }));

    await waitFor(() => expect(mocks.createUnitLimit).toHaveBeenCalledTimes(1));
    expect(mocks.createUnitLimit.mock.calls[0][0]).toMatchObject({ scope: "brand", value: "" });
  });
});
