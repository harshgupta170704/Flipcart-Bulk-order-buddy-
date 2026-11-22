/**
 * "Forgot password?" (spec FR-059, C-13): ask for a reset link by mail. The
 * server answers the same whoever the email belongs to, and only sends when a
 * mail server is configured — the sign-in page links here only then.
 */
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { errorMessage, forgotPassword } from "../lib/api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Field, TextInput } from "../components/Field";

export default function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    document.title = "Reset password — Ordering Suite";
  }, []);

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting || email.trim() === "") return;
    setSubmitting(true);
    setError(null);
    try {
      await forgotPassword(email.trim());
      setSent(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="flex min-h-dvh items-center justify-center px-4 py-10">
      <section className="w-full max-w-sm rounded-card border border-line bg-surface p-6">
        <h1 className="text-[17px] font-semibold leading-tight text-ink">Reset your password</h1>
        {sent ? (
          <div className="mt-3 flex flex-col gap-4">
            <Alert tone="success" title="Check your mail">
              If that email has an account here, a link to choose a new password is on its way. It
              works for an hour, once. Nothing shows up? Check the spam folder, or ask an admin to
              set a temporary password.
            </Alert>
            <Link to="/login" className="text-[13px] font-medium text-ink underline-offset-2 hover:underline">
              Back to sign in
            </Link>
          </div>
        ) : (
          <form className="mt-3 flex flex-col gap-4" onSubmit={(e) => void onSubmit(e)} noValidate>
            <p className="text-[13px] leading-relaxed text-ink-2">
              Type the email you sign in with. You get a mail with a link to choose a new password.
            </p>
            <Field label="Email" required>
              <TextInput
                type="email"
                value={email}
                autoFocus
                autoComplete="username"
                inputMode="email"
                required
                disabled={submitting}
                onChange={(e) => setEmail(e.target.value)}
              />
            </Field>
            <div aria-live="polite">{error ? <Alert tone="error">{error}</Alert> : null}</div>
            <Button type="submit" variant="primary" loading={submitting} disabled={email.trim() === ""} className="w-full">
              {submitting ? "Sending…" : "Send the link"}
            </Button>
            <Link to="/login" className="text-center text-[13px] font-medium text-ink underline-offset-2 hover:underline">
              Back to sign in
            </Link>
          </form>
        )}
      </section>
    </main>
  );
}
