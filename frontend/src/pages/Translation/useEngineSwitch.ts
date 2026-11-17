/* eslint-disable camelcase -- transport names. */
import { useRef, useState } from "react";
import {
  afterStateDelivered,
  readRefusal,
  useSaveEngine,
} from "@/apis/hooks/translation";
import type { TranslationEngine, TranslationNotice } from "@/types/translation";
import type { useDraftRevision } from "./useDraftRevision";

/**
 * An engine's on and off switch. It saves at once, one write at a time: the
 * switch shows where it was put while the write is on its way, and goes back
 * when the write is refused.
 *
 * It carries the revision the engine has now. The settings form beside it
 * keeps a draft of its own, so a form that was current before the switch is
 * moved on with it and can still be saved afterwards.
 *
 * Turning an engine on is refused while what it has saved does not fit its
 * plugin. The server then names the fields: they are handed to the form, and
 * the sentence beside the switch says where to look.
 */
export function useEngineSwitch(
  engine: TranslationEngine,
  revision: ReturnType<typeof useDraftRevision>,
) {
  const mutation = useSaveEngine();
  // Where the switch was put, until the saved state says the same or the write is refused.
  const [asked, setAsked] = useState<boolean | null>(null);
  const [problem, setProblem] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [notices, setNotices] = useState<TranslationNotice[]>([]);
  const writing = useRef(false);

  const dismiss = () => {
    setProblem("");
    setFieldErrors({});
  };

  const set = async (enabled: boolean) => {
    // A second press while the first is on its way changes nothing.
    if (writing.current) {
      return;
    }
    writing.current = true;
    const draftIsCurrent = revision.forSave() === engine.revision;
    setAsked(enabled);
    setNotices([]);
    dismiss();
    try {
      const saved = await mutation.mutateAsync({
        id: engine.id,
        body: { enabled, expected_revision: engine.revision },
      });
      if (draftIsCurrent) {
        revision.reload(saved.engine.revision);
      }
      // Turning off the engine that translates leaves no engine picked: the server says so.
      setNotices(saved.notices ?? []);
    } catch (error) {
      const refusal = readRefusal(error);
      const named = Object.keys(refusal?.fieldErrors ?? {});
      const shown = (engine.schema?.fields ?? []).map((field) => field.key);
      if (refusal && named.some((key) => shown.includes(key))) {
        setFieldErrors(refusal.fieldErrors);
        setProblem(
          "It cannot be turned on yet: a setting below needs fixing. Save the settings, then turn it on.",
        );
      } else {
        setProblem(
          refusal?.message ||
            `The engine could not be turned ${enabled ? "on" : "off"}. Try again.`,
        );
      }
    } finally {
      afterStateDelivered(() => {
        writing.current = false;
        setAsked(null);
      });
    }
  };

  return {
    /** Where the switch stands: where it was just put, else what is saved. */
    checked: asked ?? engine.enabled,
    busy: asked !== null,
    problem,
    /** The fields a refused turn-on named, for the settings form to mark. */
    fieldErrors,
    notices,
    set,
    /** Forget the last refusal, once the settings it named have been saved. */
    dismiss,
  };
}
