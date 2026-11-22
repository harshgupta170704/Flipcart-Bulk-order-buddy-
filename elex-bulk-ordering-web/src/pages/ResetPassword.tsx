/**
 * The page the mail's link opens (spec FR-059, C-13): choose a new password
 * with the code in the link, and be signed in. The code works for an hour and
 * once; a spent or stale one sends the person back to ask for another.
 *
 * The code rides after the link's '#' (audit F-29): a browser never sends
 * that part to a server, so no access log on the way can keep it. A link
 * mailed before that move carries it after the '?', and still works. Once
 * read, it is cleared from the address bar, and so from the history too.
 */
import { useEffect, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import * as api from "../lib/api";
import { useAuth } from "../lib/auth";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Field, TextInput } from "../components/Field";
import { MIN_PASSWORD_CHARS } from "./ChangePassword";

/** The code in the link: after the '#', or else after the '?' of an older link. */
export function resetTokenFrom(location: { hash: string; search: string }): string {
  const fromHash = new URLSearchParams(location.hash.replace(/^#/, "")).get("token");
  return fromHash || new URLSearchParams(location.search).get("token") || "";
}

export default function ResetPassword() {
  const { applySession } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  // Read when the page opens, and again whenever the router's address
  // changes (below); never from the address bar on a later render, which is
  // cleared straight after.
  const [token, setToken] = useState(() => resetTokenFrom(location));

  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    document.title = "Choose a new password — Ordering Suite";
  }, []);

  useEffect(() => {
    // A second link opened in this tab changes only the part after '#',
    // which the browser treats as the same page: the page is not opened
    // afresh, so the code is read here again. Then the path stays and the
    // '#…' and '?…' go. history.state is handed back as it was: the router
    // keeps its own bookkeeping there. Clearing the bar does not change the
    // router's address, so this does not run again because of it.
    const fresh = resetTokenFrom(location);
    if (fresh) setToken(fresh);
    if (window.location.hash || window.location.search) {
      window.history.replaceState(window.history.state, "", window.location.pathname);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the address parts are what matter
  }, [location.hash, location.search]);

  const tooShort = next !== "" && next.length < MIN_PASSWORD_CHARS;
  const mismatch = again !== "" && again !== next;
  const canSubmit = token !== "" && next.length >= MIN_PASSWORD_CHARS && again === next && !submitting;

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await api.resetPassword(token, next);
      api.setToken(res.access_token);
      const me = await api.me();
      applySession(res.access_token, me);
      navigate("/", { replace: true });
    } catch (err) {
      setError(api.errorMessage(err));
      setSubmitting(false);
    }
  };

  return (
    <main className="flex min-h-dvh items-center justify-center px-4 py-10">
      <section className="w-full max-w-sm rounded-card border border-line bg-surface p-6">
        <h1 className="text-[17px] font-semibold leading-tight text-ink">Choose a new password</h1>
        {token === "" ? (
          <div className="mt-3 flex flex-col gap-4">
            <Alert tone="error" title="This link is missing its code">
              Open the link from the mail as it was sent, or ask for a new one.
            </Alert>
            <Link to="/forgot-password" className="text-[13px] font-medium text-ink underline-offset-2 hover:underline">
              Ask for a new link
            </Link>
          </div>
        ) : (
          <form className="mt-3 flex flex-col gap-4" onSubmit={(e) => void onSubmit(e)} noValidate>
            <p className="text-[13px] leading-relaxed text-ink-2">
              At least {MIN_PASSWORD_CHARS} characters, not your email address, and not one of the
              usual guesses. Saving signs you in here and out everywhere else.
            </p>
            <Field label="New password" required error={tooShort ? `Use at least ${MIN_PASSWORD_CHARS} characters.` : undefined}>
              <TextInput type="password" value={next} autoFocus autoComplete="new-password" disabled={submitting} onChange={(e) => setNext(e.target.value)} />
            </Field>
            <Field label="New password, again" required error={mismatch ? "The two do not match." : undefined}>
              <TextInput type="password" value={again} autoComplete="new-password" disabled={submitting} onChange={(e) => setAgain(e.target.value)} />
            </Field>
            <div aria-live="polite">
              {error ? (
                <Alert tone="error">
                  {error}{" "}
                  <Link to="/forgot-password" className="font-medium underline-offset-2 hover:underline">
                    Ask for a new link
                  </Link>
                </Alert>
              ) : null}
            </div>
            <Button type="submit" variant="primary" loading={submitting} disabled={!canSubmit} className="w-full">
              {submitting ? "Saving…" : "Save and sign in"}
            </Button>
          </form>
        )}
      </section>
    </main>
  );
}
