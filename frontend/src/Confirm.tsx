import { useEffect, useState } from "react";

interface Request {
  title: string;
  text: string;
  danger: boolean;
  resolve: (confirmed: boolean) => void;
}

let show: ((request: Request | null) => void) | null = null;

/** Asks before anything with consequences. Resolves to false if there is nobody to ask. */
export function confirmDialog(title: string, text: string, danger = false): Promise<boolean> {
  return new Promise((resolve) => {
    if (show === null) resolve(false);
    else show({ title, text, danger, resolve });
  });
}

export function ConfirmHost() {
  const [request, setRequest] = useState<Request | null>(null);

  useEffect(() => {
    show = setRequest;
    return () => {
      show = null;
    };
  }, []);

  useEffect(() => {
    if (!request) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") answer(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (!request) return null;

  function answer(confirmed: boolean) {
    request?.resolve(confirmed);
    setRequest(null);
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label={request.title}>
      <div className={`modal ${request.danger ? "danger" : ""}`}>
        <h2>{request.title}</h2>
        <p>{request.text}</p>
        <div className="modal-actions">
          {/* Cancel has the focus: Enter pressed by accident must not fly anything. */}
          <button autoFocus onClick={() => answer(false)}>
            Anuluj
          </button>
          <button className={request.danger ? "danger" : "primary"} onClick={() => answer(true)}>
            Potwierdź
          </button>
        </div>
      </div>
    </div>
  );
}
