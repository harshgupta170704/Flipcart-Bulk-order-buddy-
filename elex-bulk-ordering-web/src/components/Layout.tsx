import { useCallback, useEffect, useRef, useState, type ReactElement, type ReactNode } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { getToken } from "../lib/api";
import { useAuth } from "../lib/auth";
import { hereAsReturnTo } from "../lib/returnTo";
import { OpenAlertsBadge } from "./AlertsSummary";
import { UnseenChangesBadge } from "./ChangesFeed";

type ThemePref = "light" | "dark" | "system";

const THEME_KEY = "ordering.theme";

function readThemePref(): ThemePref {
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") return stored;
  } catch {
    /* storage blocked — fall back to the OS preference */
  }
  return "system";
}

function applyThemePref(pref: ThemePref): void {
  const root = document.documentElement;
  if (pref === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", pref);
}

/** One entry in the top bar. `match` lists the other paths that light it up:
 *  "Platform Accounts & Configs" is one entry for several setup pages. */
interface NavItem {
  to: string;
  label: string;
  icon: (props: { className?: string }) => ReactElement;
  match?: string[];
  /** A live count shown after the label; renders nothing when there is nothing to count. */
  badge?: () => ReactNode;
}

/* --- tiny hand-rolled icons (16px, currentColor) ------------------------- */

const strokeProps = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.6,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

function IconFrame({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <svg viewBox="0 0 16 16" className={className ?? "size-4 shrink-0"} aria-hidden="true" focusable="false">
      {children}
    </svg>
  );
}

/** The setup pages gathered under "Platform Accounts & Configs" (see ConfigsTabs). */
export const CONFIG_PATHS = ["/accounts", "/gift-vouchers", "/hdfc-corpassist", "/sessions", "/coupons", "/order-phones", "/addresses", "/limits"];

const NAV_ITEMS: NavItem[] = [
  {
    to: "/",
    label: "Dashboard",
    icon: ({ className }) => (
      <IconFrame className={className}>
        <rect x="2" y="2.5" width="5" height="5" rx="1" {...strokeProps} />
        <rect x="9" y="2.5" width="5" height="8" rx="1" {...strokeProps} />
        <rect x="2" y="9.5" width="5" height="4" rx="1" {...strokeProps} />
        <rect x="9" y="12.5" width="5" height="1" rx="0.5" {...strokeProps} />
      </IconFrame>
    ),
    badge: () => <UnseenChangesBadge />,
  },
  {
    to: "/ordering",
    label: "Smart Bulk Order",
    icon: ({ className }) => (
      <IconFrame className={className}>
        <path d="M9 1.8 3.5 9h4l-1 5.2L12.5 7h-4z" {...strokeProps} />
      </IconFrame>
    ),
  },
  {
    to: "/orders",
    label: "Orders",
    icon: ({ className }) => (
      <IconFrame className={className}>
        <path d="M2 3h1.8l1.5 7.3h6.4l1.5-5.1H4.4" {...strokeProps} />
        <circle cx="6.2" cy="13" r="1.1" {...strokeProps} />
        <circle cx="11.4" cy="13" r="1.1" {...strokeProps} />
      </IconFrame>
    ),
  },
  {
    to: "/order-units",
    label: "Order Units History",
    icon: ({ className }) => (
      <IconFrame className={className}>
        <path d="M8 1.8 13.5 4.6v6.8L8 14.2 2.5 11.4V4.6z" {...strokeProps} />
        <path d="M2.5 4.6 8 7.4l5.5-2.8M8 7.4v6.8" {...strokeProps} />
      </IconFrame>
    ),
  },
  {
    to: "/accounts",
    label: "Platform Accounts & Configs",
    match: CONFIG_PATHS,
    icon: ({ className }) => (
      <IconFrame className={className}>
        <circle cx="8" cy="5.5" r="2.6" {...strokeProps} />
        <path d="M3 14c.5-2.8 2.5-4.3 5-4.3s4.5 1.5 5 4.3" {...strokeProps} />
      </IconFrame>
    ),
  },
  {
    to: "/alerts",
    label: "Alerts",
    icon: ({ className }) => (
      <IconFrame className={className}>
        <path d="M8 2.2c-2.4 0-4 1.8-4 4.2v2.5L2.8 11h10.4L12 8.9V6.4c0-2.4-1.6-4.2-4-4.2z" {...strokeProps} />
        <path d="M6.6 13.2a1.5 1.5 0 0 0 2.8 0" {...strokeProps} />
      </IconFrame>
    ),
    badge: () => <OpenAlertsBadge />,
  },
  {
    to: "/requests",
    label: "Requests",
    icon: ({ className }) => (
      <IconFrame className={className}>
        <rect x="3" y="2" width="10" height="12" rx="1.5" {...strokeProps} />
        <path d="M5.5 5.5h5M5.5 8h5M5.5 10.5h3" {...strokeProps} />
      </IconFrame>
    ),
  },
  {
    to: "/agents",
    label: "Agents",
    icon: ({ className }) => (
      <IconFrame className={className}>
        <rect x="2" y="3" width="12" height="8" rx="1.2" {...strokeProps} />
        <path d="M6 14h4M8 11v3" {...strokeProps} />
      </IconFrame>
    ),
  },
];

const ADMIN_ITEMS: { to: string; label: string }[] = [
  { to: "/users", label: "Users" },
  { to: "/settings", label: "Settings" },
];

function isItemActive(item: Pick<NavItem, "to" | "match">, pathname: string): boolean {
  if (item.to === "/") return pathname === "/";
  const paths = item.match ?? [item.to];
  return paths.some((path) => pathname === path || pathname.startsWith(`${path}/`));
}

function topNavClass(isActive: boolean): string {
  const base =
    "relative flex h-14 shrink-0 items-center gap-1.5 whitespace-nowrap px-2 text-[12.5px] font-medium transition-colors";
  return isActive
    ? `${base} text-ink after:absolute after:inset-x-1 after:bottom-0 after:h-0.5 after:rounded-full after:bg-ink`
    : `${base} text-ink-2 hover:text-ink`;
}

function mobileNavClass(isActive: boolean): string {
  const base = "flex items-center gap-2.5 rounded-control px-3 py-2 text-[13px] font-medium transition-colors";
  return isActive ? `${base} bg-page text-ink` : `${base} text-ink-2 hover:bg-page hover:text-ink`;
}

/** A small menu opened from the top bar: the Admin pages, or the signed-in user's own. */
function Menu({ label, children, align = "left" }: { label: ReactNode; children: ReactNode; align?: "left" | "right" }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const location = useLocation();

  useEffect(() => setOpen(false), [location.pathname]);
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((value) => !value)}
        className="flex h-14 items-center gap-1 whitespace-nowrap px-2 text-[12.5px] font-medium text-ink-2 hover:text-ink"
      >
        {label}
        <svg viewBox="0 0 16 16" className="size-3" aria-hidden="true">
          <path d="M4 6l4 4 4-4" {...strokeProps} />
        </svg>
      </button>
      {open ? (
        <div
          role="menu"
          className={`absolute top-[calc(100%-6px)] z-30 min-w-44 rounded-control border border-line bg-surface p-1 shadow-lg ${
            align === "right" ? "right-0" : "left-0"
          }`}
        >
          {children}
        </div>
      ) : null}
    </div>
  );
}

const menuItemClass =
  "flex w-full items-center rounded-[6px] px-2.5 py-1.5 text-left text-[13px] text-ink-2 hover:bg-page hover:text-ink";

function TopNav({ isAdmin }: { isAdmin: boolean }) {
  const { pathname } = useLocation();
  return (
    <nav aria-label="Sections" className="flex min-w-0 items-center gap-0.5">
      {NAV_ITEMS.map((item) => {
        const active = isItemActive(item, pathname);
        return (
          <Link key={item.to} to={item.to} aria-current={active ? "page" : undefined} className={topNavClass(active)}>
            <item.icon className="size-3.5 shrink-0" />
            <span>{item.label}</span>
            {item.badge ? item.badge() : null}
          </Link>
        );
      })}
      {isAdmin ? (
        <Menu label="Admin">
          {ADMIN_ITEMS.map((item) => (
            <Link key={item.to} role="menuitem" to={item.to} className={menuItemClass}>
              {item.label}
            </Link>
          ))}
        </Menu>
      ) : null}
    </nav>
  );
}

function MobileNav({ onNavigate, isAdmin }: { onNavigate?: () => void; isAdmin: boolean }) {
  const { pathname } = useLocation();
  return (
    <nav aria-label="Sections" className="flex flex-col gap-0.5">
      {NAV_ITEMS.map((item) => (
        <Link
          key={item.to}
          to={item.to}
          onClick={onNavigate}
          className={mobileNavClass(isItemActive(item, pathname))}
        >
          <item.icon className="size-4 shrink-0 text-muted" />
          <span>{item.label}</span>
          {item.badge ? item.badge() : null}
        </Link>
      ))}
      {isAdmin
        ? ADMIN_ITEMS.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              onClick={onNavigate}
              className={mobileNavClass(isItemActive(item, pathname))}
            >
              <span className="size-4 shrink-0" />
              <span>{item.label}</span>
            </Link>
          ))
        : null}
      <Link to="/change-password" onClick={onNavigate} className={mobileNavClass(pathname === "/change-password")}>
        <span className="size-4 shrink-0" />
        <span>Change password</span>
      </Link>
    </nav>
  );
}

function Brand() {
  return (
    <Link to="/" className="flex shrink-0 items-center gap-2">
      <span
        aria-hidden="true"
        className="inline-flex size-6 items-center justify-center rounded-[7px] text-[12px] font-bold"
        style={{ backgroundColor: "var(--accent)", color: "var(--accent-ink)" }}
      >
        O
      </span>
      <span className="text-[15px] font-semibold tracking-tight text-ink">Ordering Suite</span>
    </Link>
  );
}

const THEME_ORDER: ThemePref[] = ["light", "dark", "system"];
const THEME_NAMES: Record<ThemePref, string> = { light: "Light", dark: "Dark", system: "System" };

/** One icon button that steps Light, Dark, System; the title says which is on. */
function ThemeToggle() {
  const [pref, setPref] = useState<ThemePref>(() => readThemePref());

  useEffect(() => {
    applyThemePref(pref);
    try {
      window.localStorage.setItem(THEME_KEY, pref);
    } catch {
      /* ignore */
    }
  }, [pref]);

  const next = THEME_ORDER[(THEME_ORDER.indexOf(pref) + 1) % THEME_ORDER.length];
  return (
    <button
      type="button"
      onClick={() => setPref(next)}
      aria-label={`Theme: ${THEME_NAMES[pref]}. Switch to ${THEME_NAMES[next]}`}
      title={`Theme: ${THEME_NAMES[pref]}`}
      className="inline-flex size-8 items-center justify-center rounded-control text-ink-2 hover:bg-page hover:text-ink"
    >
      <svg viewBox="0 0 16 16" className="size-4" aria-hidden="true">
        {pref === "light" ? (
          <>
            <circle cx="8" cy="8" r="2.8" {...strokeProps} />
            <path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M12.6 3.4l-1 1M4.4 11.6l-1 1" {...strokeProps} />
          </>
        ) : pref === "dark" ? (
          <path d="M13 9.6A5.5 5.5 0 0 1 6.4 3a5.5 5.5 0 1 0 6.6 6.6z" {...strokeProps} />
        ) : (
          <>
            <rect x="2" y="3" width="12" height="8" rx="1.2" {...strokeProps} />
            <path d="M6 14h4M8 11v3" {...strokeProps} />
          </>
        )}
      </svg>
    </button>
  );
}

/** From this long before the session ends, the banner says so (spec C-13). */
const SESSION_WARNING_MS = 5 * 60 * 1000;
const SESSION_TICK_MS = 30 * 1000;

/**
 * The session countdown (spec C-13: an eight-hour session). Quiet until five
 * minutes before the token expires; then a banner with the way to a fresh
 * sign-in that lands back here; and at the end, the app signs out itself
 * rather than waiting for the next request to fail — unless another tab has
 * signed in since, in which case this one adopts that newer session instead
 * of throwing it away.
 */
export function SessionCountdown() {
  const { expiresAt, token, logout, retry } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), SESSION_TICK_MS);
    return () => window.clearInterval(id);
  }, []);

  const expiry = expiresAt === null ? Number.NaN : Date.parse(expiresAt);
  const remaining = Number.isNaN(expiry) ? Number.POSITIVE_INFINITY : expiry - now;
  const from = hereAsReturnTo(location);

  const signInAgain = useCallback(() => {
    logout();
    navigate(`/login?from=${encodeURIComponent(from)}`, { replace: true });
  }, [logout, navigate, from]);

  // Once per expiry: the sign-out changes the location, which would otherwise
  // re-run this with the same stale expiry and sign out again, for ever.
  const ended = useRef<string | null>(null);
  useEffect(() => {
    if (remaining > 0) {
      ended.current = null;
      return;
    }
    if (ended.current === expiresAt) return;
    ended.current = expiresAt;
    const stored = getToken();
    if (stored && stored !== token) {
      void retry(); // a newer sign-in from another tab: take it over
      return;
    }
    signInAgain();
  }, [remaining, expiresAt, token, retry, signInAgain]);

  if (remaining > SESSION_WARNING_MS) return null;
  const minutes = Math.max(1, Math.ceil(remaining / 60_000));
  return (
    <div
      role="status"
      className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-2 text-[13px] text-ink md:px-6"
      style={{ backgroundColor: "color-mix(in oklab, var(--status-warning) 14%, var(--surface))" }}
    >
      <span>
        Your session ends in {minutes} minute{minutes === 1 ? "" : "s"}. Sign in again to keep working — you
        come back to this page.
      </span>
      <button
        type="button"
        onClick={signInAgain}
        className="rounded-control border border-line bg-surface px-2.5 py-1 text-[12px] font-medium text-ink hover:bg-page"
      >
        Sign in again
      </button>
    </div>
  );
}

/**
 * The app shell: one top bar with the brand, every section and an Admin menu,
 * and on the right the theme switch and the signed-in user's menu. Below
 * ~1024px the sections fold into a menu button. Admin-only pages are left out
 * of an operator's navigation (FR-058).
 */
export function Layout() {
  const { username, name, role, isAdmin, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();

  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  const closeMenu = useCallback(() => setMenuOpen(false), []);
  const who = name || username || "—";

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="sticky top-0 z-20 shrink-0 border-b border-line bg-surface">
        <div className="flex h-14 items-center gap-4 px-3 md:px-5">
          <button
            type="button"
            onClick={() => setMenuOpen((open) => !open)}
            aria-expanded={menuOpen}
            aria-controls="mobile-nav"
            aria-label={menuOpen ? "Close navigation" : "Open navigation"}
            className="inline-flex size-9 items-center justify-center rounded-control border border-line text-ink xl:hidden"
          >
            <svg viewBox="0 0 16 16" className="size-4" aria-hidden="true" focusable="false">
              {menuOpen ? (
                <path d="M3.5 3.5l9 9M12.5 3.5l-9 9" {...strokeProps} />
              ) : (
                <path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" {...strokeProps} />
              )}
            </svg>
          </button>

          <Brand />

          <div className="hidden min-w-0 flex-1 xl:block">
            <TopNav isAdmin={isAdmin} />
          </div>

          <div className="ml-auto flex shrink-0 items-center gap-1">
            <ThemeToggle />
            <Menu
              align="right"
              label={
                <span className="flex items-center gap-1.5" title={username ?? undefined}>
                  <svg viewBox="0 0 16 16" className="size-3.5" aria-hidden="true">
                    <circle cx="8" cy="5.5" r="2.6" {...strokeProps} />
                    <path d="M3 14c.5-2.8 2.5-4.3 5-4.3s4.5 1.5 5 4.3" {...strokeProps} />
                  </svg>
                  <span className="hidden max-w-40 truncate text-ink sm:inline">{who}</span>
                </span>
              }
            >
              <p className="px-2.5 pb-1 pt-1.5 text-[12px] text-muted">
                Signed in as {who}
                {role ? ` · ${role === "admin" ? "Admin" : "Operator"}` : ""}
              </p>
              <Link role="menuitem" to="/change-password" className={menuItemClass}>
                Change password
              </Link>
              <button role="menuitem" type="button" onClick={logout} className={menuItemClass}>
                Sign out
              </button>
            </Menu>
          </div>
        </div>
      </header>

      {menuOpen ? (
        <div id="mobile-nav" className="border-b border-line bg-surface px-2 py-2 xl:hidden">
          <MobileNav onNavigate={closeMenu} isAdmin={isAdmin} />
        </div>
      ) : null}

      <SessionCountdown />

      <main className="min-w-0 flex-1 px-4 py-5 md:px-6 md:py-6">
        <Outlet />
      </main>
    </div>
  );
}

export default Layout;
