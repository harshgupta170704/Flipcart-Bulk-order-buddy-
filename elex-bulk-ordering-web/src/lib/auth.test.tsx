// @vitest-environment jsdom
/**
 * The route guards: no token → /login carrying where we were (FR-055); an
 * operator inside RequireAdmin sees the fallback (FR-058); a stored token the
 * server could not check is kept and shown as such, while a refused one is
 * dropped.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getToken: vi.fn(),
  clearToken: vi.fn(),
  setToken: vi.fn(),
  me: vi.fn(),
  login: vi.fn(),
}));

vi.mock("./api", () => ({
  ...mocks,
  TOKEN_KEY: "ordering.token",
  UNAUTHORIZED_EVENT: "ordering:unauthorized",
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

import { ApiError } from "./api";
import { AuthProvider, RequireAdmin, RequireAuth, useAuth } from "./auth";
import { onSessionChange } from "./session";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function Probe() {
  const location = useLocation();
  return <p>at {location.pathname + location.search}</p>;
}

function app(initial: string) {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<Probe />} />
          <Route element={<RequireAuth />}>
            <Route path="/alerts" element={<p>the alerts page</p>} />
            <Route element={<RequireAdmin fallback={<p>admins only</p>} />}>
              <Route path="/users" element={<p>the users page</p>} />
            </Route>
          </Route>
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

const operator = {
  username: "ops@test.com",
  email: "ops@test.com",
  name: "Ops",
  role: "operator",
  must_change_password: false,
  is_env_admin: false,
  expires_at: null,
};

describe("RequireAuth / RequireAdmin", () => {
  it("sends a visitor without a token to /login with where they were", async () => {
    mocks.getToken.mockReturnValue(null);
    app("/alerts?kind=delivery_overdue");
    expect(await screen.findByText("at /login?from=%2Falerts%3Fkind%3Ddelivery_overdue")).toBeTruthy();
  });

  it("shows an operator the fallback inside RequireAdmin", async () => {
    mocks.getToken.mockReturnValue("tok");
    mocks.me.mockResolvedValue(operator);
    app("/users");
    expect(await screen.findByText("admins only")).toBeTruthy();
  });

  it("keeps a token the server could not check, and drops one it refused", async () => {
    mocks.getToken.mockReturnValue("tok");
    mocks.me.mockRejectedValueOnce(new Error("Failed to fetch"));
    app("/alerts");
    expect(await screen.findByText("Could not check your sign-in")).toBeTruthy();
    expect(mocks.clearToken).not.toHaveBeenCalled();
    cleanup();

    mocks.me.mockRejectedValueOnce(new ApiError(401, "Token has expired"));
    app("/alerts");
    expect(await screen.findByText("at /login?from=%2Falerts")).toBeTruthy();
    await waitFor(() => expect(mocks.clearToken).toHaveBeenCalled());
  });
});

describe("a change of who is signed in", () => {
  function SignOut() {
    const { logout, email } = useAuth();
    return <button onClick={logout}>Sign out {email}</button>;
  }

  it("is told to the stores holding one person's answers when someone signs out", async () => {
    mocks.getToken.mockReturnValue("tok");
    mocks.me.mockResolvedValue(operator);
    const changed = vi.fn();
    const stop = onSessionChange(changed);
    try {
      render(
        <MemoryRouter>
          <AuthProvider>
            <SignOut />
          </AuthProvider>
        </MemoryRouter>,
      );
      fireEvent.click(await screen.findByText("Sign out ops@test.com"));
      expect(changed).toHaveBeenCalledTimes(1);
      expect(mocks.clearToken).toHaveBeenCalled();
    } finally {
      stop();
    }
  });
});
