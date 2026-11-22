import { useEffect, useState, type FormEvent } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { authOptions, errorMessage } from "../lib/api";
import { useAuth } from "../lib/auth";
import { safeReturnTo } from "../lib/returnTo";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Field, TextInput } from "../components/Field";

interface FromState {
  from?: string;
}

export default function Login() {
  const { login, token, ready, role, mustChangePassword } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resetByMail, setResetByMail] = useState(false);

  useEffect(() => {
    document.title = "Sign in — Ordering Suite";
  }, []);

  // Whether "Forgot password?" can do anything: only with a mail server.
  useEffect(() => {
    let cancelled = false;
    void authOptions()
      .then((options) => {
        if (!cancelled) setResetByMail(Boolean(options.password_reset_by_email));
      })
      .catch(() => {
        /* no answer: offer nothing extra */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /* Where to go afterwards: the `?from=` of the sign-in link (spec FR-055),
     else the place the guard sent us from, else the dashboard — and only ever
     a path inside this app. */
  const target =
    safeReturnTo(new URLSearchParams(location.search).get("from")) ??
    safeReturnTo((location.state as FromState | null)?.from) ??
    "/";

  // Already signed in (and checked)? Don't sit on the login screen. A user on
  // a temporary password goes to change it first, and still lands on
  // `target` afterwards.
  if (ready && token && role !== null) {
    return mustChangePassword ? (
      <Navigate to="/change-password" replace state={{ from: target }} />
    ) : (
      <Navigate to={target} replace />
    );
  }

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      const me = await login(email.trim(), password);
      if (me.must_change_password) {
        navigate("/change-password", { replace: true, state: { from: target } });
      } else {
        navigate(target, { replace: true });
      }
    } catch (err) {
      setError(errorMessage(err));
      setSubmitting(false);
    }
  };

  return (
    <main className="flex min-h-dvh items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-5 flex items-center justify-center gap-2">
          <span
            aria-hidden="true"
            className="inline-flex size-7 items-center justify-center rounded-[8px] text-[13px] font-bold"
            style={{ backgroundColor: "var(--accent)", color: "var(--accent-ink)" }}
          >
            O
          </span>
          <span className="text-[15px] font-semibold tracking-tight text-ink">Ordering Suite</span>
        </div>

        <section className="rounded-card border border-line bg-surface p-6">
          <h1 className="text-[17px] font-semibold leading-tight text-ink">Sign in</h1>
          <p className="mt-1 text-[13px] leading-relaxed text-ink-2">
            With the email and password an admin gave you.
            {resetByMail ? null : " Forgotten the password? An admin can set a temporary one."}
          </p>

          <form className="mt-5 flex flex-col gap-4" onSubmit={(e) => void onSubmit(e)} noValidate>
            <Field label="Email" required help="The admin set up on the server signs in with that name instead.">
              <TextInput
                name="username"
                type="text"
                value={email}
                autoFocus
                autoComplete="username"
                inputMode="email"
                spellCheck={false}
                required
                disabled={submitting}
                onChange={(e) => setEmail(e.target.value)}
              />
            </Field>

            <Field label="Password" required>
              <TextInput
                name="password"
                type="password"
                value={password}
                autoComplete="current-password"
                required
                disabled={submitting}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>

            <div aria-live="polite">
              {error ? <Alert tone="error">{error}</Alert> : null}
            </div>

            <Button
              type="submit"
              variant="primary"
              loading={submitting}
              disabled={email.trim() === "" || password === ""}
              className="w-full"
            >
              {submitting ? "Signing in…" : "Sign in"}
            </Button>

            {resetByMail ? (
              <p className="text-center text-[13px] text-ink-2">
                <Link to="/forgot-password" className="font-medium text-ink underline-offset-2 hover:underline">
                  Forgot password?
                </Link>
              </p>
            ) : null}
          </form>
        </section>
      </div>
    </main>
  );
}
