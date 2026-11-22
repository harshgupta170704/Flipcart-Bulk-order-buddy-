import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import * as api from "./api";
import type { MeResponse, UserRole } from "./types";
import { hereAsReturnTo } from "./returnTo";
import { sessionChanged } from "./session";

export interface AuthState {
  /** The signed-in email (the API's `username`), or null. */
  username: string | null;
  email: string | null;
  name: string | null;
  role: UserRole | null;
  isAdmin: boolean;
  /** The configured admin: the password set on the server stays a second key. */
  isEnvAdmin: boolean;
  /** Signed in with a temporary password: the app insists on a new one first. */
  mustChangePassword: boolean;
  /** When the session ends, as an ISO string, or null when unknown. */
  expiresAt: string | null;
  /** The bearer token, or null. */
  token: string | null;
  /** False until the stored token has been checked against /auth/me. */
  ready: boolean;
  /**
   * The stored token could not be checked for a reason other than being
   * refused (the server was unreachable, say). The token is kept; `retry`
   * asks again. Null when the check went through, or was never needed.
   */
  loadError: string | null;
  /** Check the stored token again — after a load error, or when another tab signed in. */
  retry: () => Promise<void>;
  /** Throws an ApiError when the credentials are rejected. */
  login: (username: string, password: string) => Promise<MeResponse>;
  logout: () => void;
  /** Re-read /auth/me — after a profile change, say. */
  refresh: () => Promise<void>;
  /** Adopt a session the server just issued: after a password change or a reset link. */
  applySession: (token: string, me: MeResponse) => void;
}

const AuthContext = createContext<AuthState | null>(null);

interface Who {
  email: string;
  name: string;
  role: UserRole;
  isEnvAdmin: boolean;
  mustChangePassword: boolean;
  expiresAt: string | null;
}

function whoFrom(me: MeResponse): Who {
  return {
    email: me.email || me.username,
    name: me.name,
    role: me.role,
    isEnvAdmin: Boolean(me.is_env_admin),
    mustChangePassword: me.must_change_password,
    expiresAt: me.expires_at,
  };
}

/** A refusal (401) means the token is dead; anything else means we could not tell. */
function refused(err: unknown): boolean {
  return err instanceof api.ApiError && err.status === 401;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(() => api.getToken());
  const [who, setWho] = useState<Who | null>(null);
  const [ready, setReady] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const mounted = useRef(true);
  const tokenRef = useRef<string | null>(token);
  tokenRef.current = token;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  /**
   * Check the token in storage against the server and adopt it. A refused
   * token is dropped; a check that could not be made keeps the token and
   * reports why, so a server hiccup does not sign everybody out.
   */
  const check = useCallback(async () => {
    const stored = api.getToken();
    if (!stored) {
      if (mounted.current) {
        setTokenState(null);
        setWho(null);
        setLoadError(null);
      }
      return;
    }
    try {
      const me = await api.me();
      if (!mounted.current) return;
      setWho(whoFrom(me));
      setTokenState(stored);
      setLoadError(null);
    } catch (err) {
      if (!mounted.current) return;
      if (refused(err)) {
        api.clearToken();
        sessionChanged();
        setTokenState(null);
        setWho(null);
        setLoadError(null);
      } else {
        setTokenState(stored);
        setLoadError(api.errorMessage(err));
      }
    }
  }, []);

  // Validate whatever token we found in storage before letting the app in.
  useEffect(() => {
    void check().finally(() => {
      if (mounted.current) setReady(true);
    });
  }, [check]);

  const logout = useCallback(() => {
    api.clearToken();
    // What one person's stores hold (the change feed's list) goes with them.
    sessionChanged();
    setTokenState(null);
    setWho(null);
    setLoadError(null);
  }, []);

  // api.ts fires this whenever the server rejects our token mid-session.
  useEffect(() => {
    const onUnauthorized = () => logout();
    window.addEventListener(api.UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(api.UNAUTHORIZED_EVENT, onUnauthorized);
  }, [logout]);

  // Another tab signed in, out, or changed its password: follow it, so a
  // stale tab never clears a newer token or keeps a dead one.
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== null && event.key !== api.TOKEN_KEY) return;
      const stored = api.getToken();
      if (stored === tokenRef.current) return;
      sessionChanged();
      if (!stored) {
        setTokenState(null);
        setWho(null);
        return;
      }
      void check();
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [check]);

  const refresh = useCallback(async () => {
    const me = await api.me();
    if (mounted.current) setWho(whoFrom(me));
  }, []);

  const applySession = useCallback((fresh: string, me: MeResponse) => {
    api.setToken(fresh);
    sessionChanged();
    if (!mounted.current) return;
    setTokenState(fresh);
    setWho(whoFrom(me));
    setLoadError(null);
  }, []);

  const login = useCallback(async (user: string, password: string) => {
    const res = await api.login(user, password);
    api.setToken(res.access_token);
    sessionChanged();
    let me: MeResponse;
    try {
      me = await api.me();
    } catch (first) {
      if (refused(first)) throw first;
      try {
        me = await api.me();
      } catch {
        // Nothing is committed until we know who signed in: a guessed
        // identity would show the wrong menus and the wrong password rules.
        api.clearToken();
        throw new Error("Signed in, but your account details could not be loaded. Try again.");
      }
    }
    if (mounted.current) {
      setWho(whoFrom(me));
      setTokenState(res.access_token);
      setLoadError(null);
      setReady(true);
    }
    return me;
  }, []);

  const value = useMemo<AuthState>(
    () => ({
      username: who?.email ?? null,
      email: who?.email ?? null,
      name: who?.name ?? null,
      role: who?.role ?? null,
      isAdmin: who?.role === "admin",
      isEnvAdmin: who?.isEnvAdmin ?? false,
      mustChangePassword: who?.mustChangePassword ?? false,
      expiresAt: who?.expiresAt ?? null,
      token,
      ready,
      loadError,
      retry: check,
      login,
      logout,
      refresh,
      applySession,
    }),
    [who, token, ready, loadError, check, login, logout, refresh, applySession],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>.");
  return ctx;
}

/** Shown instead of the app when the stored token could not be checked. */
function CouldNotCheck({ error, retry, logout }: { error: string; retry: () => Promise<void>; logout: () => void }) {
  const [busy, setBusy] = useState(false);
  const again = async () => {
    setBusy(true);
    try {
      await retry();
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="flex min-h-dvh items-center justify-center px-4 py-10">
      <section role="alert" className="w-full max-w-sm rounded-card border border-line bg-surface p-6">
        <h1 className="text-[17px] font-semibold leading-tight text-ink">Could not check your sign-in</h1>
        <p className="mt-2 text-[13px] leading-relaxed text-ink-2">
          The server did not answer: {error}. Your sign-in is kept; try again in a moment.
        </p>
        <div className="mt-4 flex items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => void again()}
            className="rounded-control bg-accent px-3 py-1.5 text-[13px] font-medium text-accent-ink disabled:opacity-60"
          >
            {busy ? "Checking…" : "Try again"}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={logout}
            className="rounded-control border border-line px-3 py-1.5 text-[13px] font-medium text-ink-2 hover:bg-page hover:text-ink"
          >
            Sign out
          </button>
        </div>
      </section>
    </main>
  );
}

/**
 * Route guard. Renders nothing while the stored token is being checked, then
 * either the nested routes or a redirect to /login carrying where we were
 * (spec FR-055), and — for a user still on a temporary password — the
 * change-password page before anything else.
 */
export function RequireAuth() {
  const { token, ready, role, mustChangePassword, loadError, retry, logout } = useAuth();
  const location = useLocation();

  if (!ready) return null;
  if (!token) {
    const from = hereAsReturnTo(location);
    return <Navigate to={`/login?from=${encodeURIComponent(from)}`} replace />;
  }
  if (role === null) {
    // A token we hold but could not check: say so rather than guessing a role.
    return <CouldNotCheck error={loadError ?? "no answer"} retry={retry} logout={logout} />;
  }
  if (mustChangePassword && location.pathname !== "/change-password") {
    return <Navigate to="/change-password" replace state={{ from: hereAsReturnTo(location) }} />;
  }
  return <Outlet />;
}

/** Inside RequireAuth: the nested routes for an admin, a plain note otherwise. */
export function RequireAdmin({ fallback }: { fallback: ReactNode }) {
  const { isAdmin } = useAuth();
  return isAdmin ? <Outlet /> : <>{fallback}</>;
}
