import { FunctionComponent, useMemo, useState } from "react";
import { Select } from "@mantine/core";
import {
  useTranslationRuntime,
  useTranslationState,
} from "@/apis/hooks/translation";
import { pickableChoices, type PickChoice } from "@/pages/Translation/model";

/** The value of the choice that picks nothing: the selected engine, with its fallback. */
const AUTOMATIC = "automatic";

export const AUTOMATIC_LABEL = "Automatic";

/** What a request carries for a pick: one back end, or one engine that has none. Empty for no pick. */
export type PickFields =
  | { connection_id: number }
  | { engine_id: number }
  | Record<string, never>;

export interface ManualPick {
  /** Whether the new translator is on. With it off nothing here is offered. */
  runtime: boolean;
  /** The back ends and engines that can take a job now. */
  choices: PickChoice[];
  /** What is picked, or null for the selected engine. Null again once it is gone. */
  picked: PickChoice | null;
  /** The back end picked, or null. */
  connectionId: number | null;
  /** The pick as a label says it, or null. */
  label: string | null;
  /** What a request carries for the pick. */
  fields: PickFields;
  setKey: (key: string | null) => void;
}

/**
 * One manual pick, for a place that starts a translation by hand. It reads the Translation page's state, and
 * only while the new translator is on: with it off the state's route does not exist, and the pick offers
 * nothing.
 */
export function useManualPick(): ManualPick {
  const runtime = useTranslationRuntime();
  const state = useTranslationState(runtime);
  const choices = useMemo(
    () => (runtime && state.data ? pickableChoices(state.data) : []),
    [runtime, state.data],
  );
  const [key, setKey] = useState<string | null>(null);
  // A pick that was removed, turned off or lost its key while the form was open is no longer a pick.
  const picked = choices.find((item) => item.key === key) ?? null;
  const fields: PickFields =
    picked?.connectionId !== undefined
      ? { connection_id: picked.connectionId }
      : picked?.engineId !== undefined
        ? { engine_id: picked.engineId }
        : {};
  return {
    runtime,
    choices,
    picked,
    connectionId: picked?.connectionId ?? null,
    label: picked?.label ?? null,
    fields,
    setKey,
  };
}

/** What a pick says it does, for the line under the control. */
export const PICK_HELP =
  "Automatic uses the engine selected on the Translation page and moves to its fallback when a back end fails. What you pick does the whole job alone, and its error is the result.";

/** "Translate with": automatic, one usable back end or one engine. Draws nothing with the new translator off. */
const TranslationPick: FunctionComponent<{
  pick: ManualPick;
  disabled?: boolean;
}> = ({ pick, disabled }) => {
  const { runtime, choices, picked, setKey } = pick;
  const data = useMemo(() => {
    const groups = new Map<string, { value: string; label: string }[]>();
    for (const item of choices) {
      groups.set(item.engine, [
        ...(groups.get(item.engine) ?? []),
        { value: item.key, label: item.label },
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
      value={picked?.key ?? AUTOMATIC}
      allowDeselect={false}
      disabled={disabled}
      onChange={(next) => setKey(next === AUTOMATIC ? null : next)}
    />
  );
};

export default TranslationPick;
