// @vitest-environment jsdom
/**
 * The sign-in page keeps `?from=` through a forced password change (the
 * change page sends the user on to it afterwards), and offers "Forgot
 * password?" only when the server says a mail server is configured.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  login: vi.fn(),
  authOptions: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  authOptions: mocks.authOptions,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

vi.mock("../lib/auth", () => ({
  useAuth: () => ({ login: mocks.login, token: null, ready: true, role: null, mustChangePassword: false }),
}));

import Login from "./Login";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function Probe() {
  const location = useLocation();
  const state = location.state as { from?: string } | null;
  return <p>at {location.pathname} from {state?.from ?? "nowhere"}</p>;
}

function app(initial: string) {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/change-password" element={<Probe />} />
        <Route path="/alerts" element={<Probe />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Login", () => {
  it("carries ?from= into a forced password change", async () => {
    mocks.authOptions.mockResolvedValue({ password_reset_by_email: false });
    mocks.login.mockResolvedValue({ must_change_password: true, role: "operator" });
    app("/login?from=%2Falerts");
    fireEvent.change(screen.getByLabelText(/Email/), { target: { value: "ops@test.com" } });
    fireEvent.change(screen.getByLabelText(/Password/), { target: { value: "temporary-pass-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("at /change-password from /alerts")).toBeTruthy();
    expect(mocks.login).toHaveBeenCalledWith("ops@test.com", "temporary-pass-1");
  });

  it("lands on ?from= straight away when no change is needed", async () => {
    mocks.authOptions.mockResolvedValue({ password_reset_by_email: false });
    mocks.login.mockResolvedValue({ must_change_password: false, role: "operator" });
    app("/login?from=%2Falerts");
    fireEvent.change(screen.getByLabelText(/Email/), { target: { value: "ops@test.com" } });
    fireEvent.change(screen.getByLabelText(/Password/), { target: { value: "a-proper-password-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("at /alerts from nowhere")).toBeTruthy();
  });

  it("offers the reset link only when a mail server is configured", async () => {
    mocks.authOptions.mockResolvedValue({ password_reset_by_email: true });
    app("/login");
    expect(await screen.findByRole("link", { name: "Forgot password?" })).toBeTruthy();
    cleanup();

    mocks.authOptions.mockResolvedValue({ password_reset_by_email: false });
    app("/login");
    expect(await screen.findByText(/An admin can set a temporary one/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Forgot password?" })).toBeNull();
  });
});
