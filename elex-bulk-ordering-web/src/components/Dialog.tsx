import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

export interface DialogProps {
  open: boolean;
  title: ReactNode;
  description?: ReactNode;
  onClose: () => void;
  children?: ReactNode;
  /** Buttons along the bottom, right-aligned. */
  footer?: ReactNode;
  /** CSS max-width of the panel. */
  width?: string;
}

/** What Tab moves between inside the panel. */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * A modal panel over the page. Escape and the backdrop close it; focus lands
 * on the panel when it opens so a keyboard user is inside it, not behind it,
 * Tab and Shift+Tab stay inside it, and when it closes focus goes back to
 * whatever opened it — the Resolve button, the Delete button — rather than
 * falling to the top of the page. Rendered into <body> so no ancestor's
 * overflow or stacking can clip it.
 */
export function Dialog({ open, title, description, onClose, children, footer, width = "640px" }: DialogProps) {
  const titleId = useId();
  const descriptionId = useId();
  const panel = useRef<HTMLDivElement>(null);

  // The latest onClose, read when Escape is pressed. Putting the callback in
  // the effect's deps would re-run it — and refocus the panel — on every
  // parent render (a toast dismissing, a sync finishing), stealing the keys
  // from whatever field inside the dialog was being typed in.
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeRef.current();
        return;
      }
      if (event.key !== "Tab" || !panel.current) return;
      const focusable = Array.from(panel.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (focusable.length === 0) {
        event.preventDefault();
        panel.current.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      const inside = active instanceof Node && panel.current.contains(active);
      if (event.shiftKey) {
        if (!inside || active === first || active === panel.current) {
          event.preventDefault();
          last.focus();
        }
      } else if (!inside || active === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    panel.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
      // Back to the control that opened it, if it is still on the page.
      if (opener && document.contains(opener)) opener.focus();
    };
  }, [open]);

  if (!open) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        className="flex max-h-[90vh] w-full flex-col rounded-card border border-line bg-surface shadow-xl outline-none"
        style={{ maxWidth: width }}
      >
        <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
          <div>
            <h2 id={titleId} className="text-[15px] font-semibold text-ink">
              {title}
            </h2>
            {description ? (
              <p id={descriptionId} className="mt-1 text-[13px] text-ink-2">
                {description}
              </p>
            ) : null}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-control px-2 py-1 text-[16px] leading-none text-ink-2 hover:bg-page hover:text-ink"
          >
            ×
          </button>
        </div>
        <div className="overflow-y-auto px-5 py-4">{children}</div>
        {footer ? (
          <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line px-5 py-3">
            {footer}
          </div>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}

export default Dialog;
