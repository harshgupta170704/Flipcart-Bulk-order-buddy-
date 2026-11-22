/**
 * A password of one's own (spec FR-059, the user's half). Reached by choice
 * from the header, or by force right after signing in with a temporary
 * password an admin set — the guard sends such a user here first, and back
 * to where they were going afterwards.
 */
import { useEffect, useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { changePassword, errorMessage } from "../lib/api";
import { useAuth } from "../lib/auth";
import { safeReturnTo } from "../lib/returnTo";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Field, TextInput } from "../components/Field";

/** The server's floor; mirrored here so the form says so before the round trip. */
export const MIN_PASSWORD_CHARS = 10;

interface FromState {
  from?: string;
}

export default function ChangePassword() {
  const { email, isEnvAdmin, mustChangePassword, applySession } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    document.title = "Change password — Ordering Suite";
  }, []);

  const tooShort = next !== "" && next.length < MIN_PASSWORD_CHARS;
  const mismatch = again !== "" && again !== next;
  const canSubmit = current !== "" && next.length >= MIN_PASSWORD_CHARS && again === next && !submitting;
  // Where the sign-in was headed before the forced change, else the dashboard.
  const afterwards = safeReturnTo((location.state as FromState | null)?.from) ?? "/";

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await changePassword(current, next);
      // The change ended the session that asked; the answer carries the new one.
      applySession(res.access_token, res);
      navigate(afterwards, { replace: true });
    } catch (err) {
      setError(errorMessage(err));
      setSubmitting(false);
    }
  };

  const back = () => {
    // The first entry of a fresh tab has nowhere to go back to.
    if (location.key === "default") navigate("/", { replace: true });
    else navigate(-1);
  };

  return (
    <div className="mx-auto flex w-full max-w-lg flex-col gap-5">
      {mustChangePassword ? (
        <Alert tone="info" title="Choose your own password to continue">
          You signed in with a temporary password an admin set. Pick one of your own and the rest
          of the app opens.
        </Alert>
      ) : null}
      {isEnvAdmin ? (
        <Alert tone="info" title="This is the configured admin account">
          The password set on the server keeps working for this account beside the one you choose
          here. To retire it, change it on the server.
        </Alert>
      ) : null}
      <Card
        title="Change password"
        description={`For ${email ?? "your account"}. At least ${MIN_PASSWORD_CHARS} characters, not your email address, and not one of the usual guesses. Your other sessions are signed out.`}
      >
        <form className="flex flex-col gap-4" onSubmit={(e) => void onSubmit(e)} noValidate>
          <Field label="Current password" required help={mustChangePassword ? "The temporary one you just signed in with." : undefined}>
            <TextInput
              type="password"
              value={current}
              autoComplete="current-password"
              autoFocus
              disabled={submitting}
              onChange={(e) => setCurrent(e.target.value)}
            />
          </Field>
          <Field
            label="New password"
            required
            error={tooShort ? `Use at least ${MIN_PASSWORD_CHARS} characters.` : undefined}
          >
            <TextInput
              type="password"
              value={next}
              autoComplete="new-password"
              disabled={submitting}
              onChange={(e) => setNext(e.target.value)}
            />
          </Field>
          <Field label="New password, again" required error={mismatch ? "The two do not match." : undefined}>
            <TextInput
              type="password"
              value={again}
              autoComplete="new-password"
              disabled={submitting}
              onChange={(e) => setAgain(e.target.value)}
            />
          </Field>
          <div aria-live="polite">{error ? <Alert tone="error">{error}</Alert> : null}</div>
          <div className="flex items-center gap-3">
            <Button type="submit" variant="primary" loading={submitting} disabled={!canSubmit}>
              {submitting ? "Saving…" : "Save new password"}
            </Button>
            {!mustChangePassword ? (
              <Button type="button" variant="ghost" disabled={submitting} onClick={back}>
                Back
              </Button>
            ) : null}
          </div>
        </form>
      </Card>
    </div>
  );
}
