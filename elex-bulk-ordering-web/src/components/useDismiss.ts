import { useEffect, useRef, type RefObject } from "react";

/**
 * Close a popover the ways people expect: Escape, or a press anywhere outside
 * it. `onClose` is read through a ref so an inline arrow does not re-subscribe
 * the listeners on every render.
 */
export function useDismiss(ref: RefObject<HTMLElement | null>, open: boolean, onClose: () => void) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeRef.current();
    };
    const onDown = (event: MouseEvent) => {
      const el = ref.current;
      if (el && !el.contains(event.target as Node)) closeRef.current();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [ref, open]);
}
