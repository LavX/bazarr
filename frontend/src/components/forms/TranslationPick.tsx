import { FunctionComponent, useMemo, useState } from "react";
import { Select } from "@mantine/core";
import {
  useTranslationRuntime,
  useTranslationState,
} from "@/apis/hooks/translation";
import {
  pickableConnections,
  type PickChoice,
} from "@/pages/Translation/model";

/** The value of the choice that picks nothing: the selected engine, with its fallback. */
const AUTOMATIC = "automatic";

export const AUTOMATIC_LABEL = "Automatic";

export interface ManualPick {
  /** Whether the new translator is on. With it off nothing here is offered. */
  runtime: boolean;
  /** The back ends that can take a job now. */
  choices: PickChoice[];
  /** The back end picked, or null for the selected engine. Null again once the back end is gone. */
  connectionId: number | null;
  /** The picked back end as a label says it, or null. */
  label: string | null;
  setConnectionId: (id: number | null) => void;
}

/**
 * One manual pick of a back end, for a place that starts a translation by hand. It reads the
 * Translation page's state, and only while the new translator is on: with it off the state's route does
 * not exist, and the pick offers nothing.
 */
export function useManualPick(): ManualPick {
  const runtime = useTranslationRuntime();
  const state = useTranslationState(runtime);
  const choices = useMemo(
    () => (runtime && state.data ? pickableConnections(state.data) : []),
    [runtime, state.data],
  );
  const [picked, setPicked] = useState<number | null>(null);
  // A back end that was removed, turned off or lost its key while the form was open is no longer a pick.
  const chosen = choices.find((item) => item.id === picked) ?? null;
  return {
    runtime,
    choices,
    connectionId: chosen?.id ?? null,
    label: chosen?.label ?? null,
    setConnectionId: setPicked,
  };
}

/** What a pick says it does, for the line under the control. */
export const PICK_HELP =
  "Automatic uses the engine selected on the Translation page and moves to its fallback when a back end fails. A back end you pick does the whole job alone, and its error is the result.";

/** "Translate with": automatic, or one usable back end. Draws nothing with the new translator off. */
const TranslationPick: FunctionComponent<{
  pick: ManualPick;
  disabled?: boolean;
}> = ({ pick, disabled }) => {
  const { runtime, choices, connectionId, setConnectionId } = pick;
  const data = useMemo(() => {
    const groups = new Map<string, { value: string; label: string }[]>();
    for (const item of choices) {
      groups.set(item.engine, [
        ...(groups.get(item.engine) ?? []),
        { value: String(item.id), label: item.label },
      ]);
    }
    return [
      { value: AUTOMATIC, label: AUTOMATIC_LABEL },
      ...[...groups].map(([group, items]) => ({ group, items })),
    ];
  }, [choices]);
  if (!runtime) {
    return null;
  }
  return (
    <Select
      label="Translate with"
      description={PICK_HELP}
      data={data}
      value={connectionId === null ? AUTOMATIC : String(connectionId)}
      allowDeselect={false}
      disabled={disabled}
      onChange={(next) =>
        setConnectionId(
          next === null || next === AUTOMATIC ? null : Number(next),
        )
      }
    />
  );
};

export default TranslationPick;
