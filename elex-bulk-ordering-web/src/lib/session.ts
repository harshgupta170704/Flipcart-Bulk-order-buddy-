/**
 * Who is signed in has changed: a sign-in, a sign-out, a session ended by the
 * server, another tab doing any of these. The provider in auth.tsx says so;
 * stores holding one person's answers listen and drop what they hold. The
 * change feed's first page (ChangesFeed) is one: it is what is new to the
 * person who fetched it, and the next person to sign in in the same tab must
 * never see it, nor mark it as seen.
 */
type Listener = () => void;

const listeners = new Set<Listener>();

/** Call `listener` whenever who is signed in changes; returns the way to stop. */
export function onSessionChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Tell every listener that who is signed in has changed. */
export function sessionChanged(): void {
  for (const listener of [...listeners]) listener();
}
