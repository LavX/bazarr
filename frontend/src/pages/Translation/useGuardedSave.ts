import { useState } from "react";
import { readRefusal } from "@/apis/hooks/translation";

/**
 * What a form does with the typed API's refusals: field errors stay beside
 * their fields, a conflict or any other refusal becomes one message, and a
 * request for consent holds the save until the user answers.
 *
 * `send(accepted)` makes the request, with the addresses the user has already
 * accepted for it. `onStale` runs when the request is refused because the row
 * changed since the form loaded it.
 */
export function useGuardedSave(
  send: (accepted: string[]) => Promise<unknown>,
  onStale?: () => void,
) {
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");
  const [asking, setAsking] = useState<string[]>([]);
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);

  const attempt = async (accepted: string[]): Promise<boolean> => {
    setBusy(true);
    setMessage("");
    setStale(false);
    setFieldErrors({});
    try {
      await send(accepted);
      setAsking([]);
      return true;
    } catch (error) {
      const refusal = readRefusal(error);
      if (refusal?.code === "consent_required") {
        setAsking(refusal.destinations);
      } else if (refusal) {
        setAsking([]);
        setFieldErrors(refusal.fieldErrors);
        setMessage(refusal.message);
        setStale(refusal.code === "stale_revision");
        if (refusal.code === "stale_revision") {
          onStale?.();
        }
      } else {
        setAsking([]);
        setMessage("The request failed. Try again.");
      }
      return false;
    } finally {
      setBusy(false);
    }
  };

  return {
    fieldErrors,
    message,
    stale,
    asking,
    busy,
    save: () => attempt([]),
    accept: () => attempt(asking),
    decline: () => setAsking([]),
    /** Forget the last refusal, for a form that reloads what is saved. */
    dismiss: () => {
      setMessage("");
      setStale(false);
      setFieldErrors({});
    },
  };
}
