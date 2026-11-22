// @vitest-environment jsdom
/**
 * The Users page: the edit dialog keeps what the admin typed when the list
 * reloads behind it, and the configured admin's row offers no password reset
 * (the password set on the server is their reset).
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AppUser } from "../lib/types";

const mocks = vi.hoisted(() => ({
  listUsers: vi.fn(),
  updateUser: vi.fn(),
  createUser: vi.fn(),
  resetUserPassword: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));
vi.mock("../lib/auth", () => ({ useAuth: () => ({ email: "owner@example.com" }) }));

import Users from "./Users";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function row(over: Partial<AppUser> = {}): AppUser {
  return {
    id: 2,
    email: "ops@test.com",
    name: "Ops",
    role: "operator",
    active: true,
    must_change_password: false,
    created_at: "2026-10-24T10:00:00",
    last_login_at: null,
    is_env_admin: false,
    ...over,
  };
}

const owner = row({ id: 1, email: "owner@example.com", name: "Administrator", role: "admin", is_env_admin: true });

describe("Users", () => {
  it("keeps the dialog's draft when the list reloads behind it", async () => {
    // Every load hands back new row objects, as a real refresh does.
    mocks.listUsers.mockImplementation(async () => [owner, row()]);
    render(<Users />);
    const edits = await screen.findAllByRole("button", { name: "Edit" });
    fireEvent.click(edits[1]);
    const name = (await screen.findByLabelText(/^Name/)) as HTMLInputElement;
    expect(name.value).toBe("Ops");
    fireEvent.change(name, { target: { value: "Ops Renamed" } });

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(mocks.listUsers).toHaveBeenCalledTimes(2));
    expect((screen.getByLabelText(/^Name/) as HTMLInputElement).value).toBe("Ops Renamed");
  });

  it("offers no password reset on the configured admin's row", async () => {
    mocks.listUsers.mockResolvedValue([owner, row()]);
    render(<Users />);
    await screen.findByText("ops@test.com");
    expect(screen.getAllByRole("button", { name: "Reset password" })).toHaveLength(1);
    expect(screen.getByText("configured admin")).toBeTruthy();
  });
});
