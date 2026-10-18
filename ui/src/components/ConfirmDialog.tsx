import { useEffect, useId, useRef } from "react";
import { Button } from "./ui";

/** A confirmation for a destructive action. Focus starts on "Cancel" (the safe choice), Esc cancels, and focus goes
 *  back to where it was afterwards. */
export function ConfirmDialog({ title, body, confirmLabel, onConfirm, onCancel }: {
  title: string; body: string; confirmLabel: string; onConfirm: () => void; onCancel: () => void;
}) {
  const cancel = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const bodyId = useId();
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    cancel.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onCancel(); };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); before?.focus?.(); };
  }, [onCancel]);
  return (
    <div className="scrim" onClick={onCancel}>
      <div className="dialog" role="alertdialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={bodyId}
        onClick={(e) => e.stopPropagation()}>
        <h2 id={titleId} className="dialog__title">{title}</h2>
        <p id={bodyId}>{body}</p>
        <div className="dialog__actions">
          <Button ref={cancel} variant="secondary" onClick={onCancel}>Cancel</Button>
          <Button variant="danger" onClick={onConfirm}>{confirmLabel}</Button>
        </div>
      </div>
    </div>
  );
}
