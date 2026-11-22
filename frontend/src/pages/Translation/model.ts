/* eslint-disable camelcase -- transport names. */
import type { Tone } from "@/components/hub/utils";
import {
  choiceOf,
  isSecretField,
  PluginField,
} from "@/components/plugin-settings";
import type {
  RestartChange,
  TranslationBackendType,
  TranslationConnection,
  TranslationEngine,
  TranslationJobStatus,
  TranslationState,
} from "@/types/translation";

/** The official engine first, the rest in the order they were installed. */
export const orderedEngines = (engines: TranslationEngine[]) =>
  [...engines].sort(
    (a, b) => Number(b.official) - Number(a.official) || a.id - b.id,
  );

export const engineName = (engine: TranslationEngine) =>
  engine.name || engine.plugin?.name || engine.plugin_id;

export const backendTypes = (engine: TranslationEngine) =>
  engine.schema?.backend_types ?? {};

export const hasBackends = (engine: TranslationEngine) =>
  Object.keys(backendTypes(engine)).length > 0;

const presetField = (type: TranslationBackendType) =>
  type.fields.find(
    (field) =>
      field.type === "choice" &&
      (field.choices ?? []).some((item) => choiceOf(item).sets !== undefined),
  );

/**
 * What a back end is called: its type, or the preset it was started from when its type has presets
 * (an OpenAI-compatible back end started from LM Studio is "LM Studio").
 */
export function backendLabel(
  engine: TranslationEngine,
  item: TranslationConnection,
): string {
  const type = backendTypes(engine)[item.backend_type];
  if (!type) {
    return item.backend_type;
  }
  const field = presetField(type);
  const picked = field
    ? (field.choices ?? [])
        .map(choiceOf)
        .find((choice) => choice.value === item.values[field.key])
    : undefined;
  return picked?.sets ? (picked.label ?? picked.value) : type.label;
}

/** The name a back end goes by in labels: its type, and its model when it has one. */
export const backendName = (
  engine: TranslationEngine,
  item: TranslationConnection,
) => `${backendLabel(engine, item)}${item.model ? `, ${item.model}` : ""}`;

export const working = (item: TranslationConnection) =>
  item.enabled && item.usable && !item.awaiting_secrets;

export interface PickChoice {
  /** Tells choices apart: `connection:<id>` or `engine:<id>`. */
  key: string;
  /** The back end's id, for a pick of one back end. */
  connectionId?: number;
  /** The engine's id, for a pick of an engine that has no back ends. */
  engineId?: number;
  /** What the control says: a back end as a label says it, or the engine's name. */
  label: string;
  /** The group it is listed under: its engine, or "Engines" for an engine picked as a whole. */
  engine: string;
}

/** The group the engines picked as a whole are listed under. */
export const ENGINES_GROUP = "Engines";

/**
 * What a person can pick for one translation. A back end that is on, usable and has its key, served by an
 * engine that is on and whose plugin runs. And an engine that has no back ends and is ready to take a job (the
 * free engine with nothing to choose). The server asks the same of a pick and refuses any other, so what is
 * held back or off is not offered rather than offered and refused. An engine that has back ends is picked
 * through them, and a legacy engine is set up and used where it always was.
 */
export function pickableChoices(
  state: TranslationState,
  installed?: ReadonlySet<string>,
): PickChoice[] {
  const present = (pluginId: string) => installed?.has(pluginId) ?? true;
  const running = orderedEngines(state.instances)
    .filter(
      (engine) =>
        engine.enabled &&
        !engine.awaiting_secrets &&
        (engine.legacy || present(engine.plugin_id)),
    )
    .filter(canRun);
  const backEnds = running.flatMap((engine) =>
    engine.connections
      .filter(
        (item) => working(item) && (!item.plugin_id || present(item.plugin_id)),
      )
      .map((item) => ({
        key: `connection:${item.id}`,
        connectionId: item.id,
        label: backendName(engine, item),
        engine: engineName(engine),
      })),
  );
  const engines = running
    .filter(
      (engine) =>
        !engine.legacy &&
        !hasBackends(engine) &&
        engine.connections.length === 0 &&
        engineStatus(engine).tone === "success",
    )
    .map((engine) => ({
      key: `engine:${engine.id}`,
      engineId: engine.id,
      label: engineName(engine),
      engine: ENGINES_GROUP,
    }));
  return [...backEnds, ...engines];
}

/** Whether there is something to run: an engine's plugin, or Bazarr+ itself for an engine it runs. */
export const canRun = (engine: TranslationEngine) =>
  engine.legacy === true ||
  (engine.plugin?.active === true && engine.removing !== true);

export interface Status {
  tone: Tone;
  label: string;
  /** What to do about it. Empty when nothing needs doing. */
  next: string;
}

/**
 * Off, and flagged for the person to look at, with no saved key that the flag could be about. A plugin
 * update leaves an engine or a back end like this when what it has saved no longer fits the new
 * release. The flag is the one a changed address sets too, and there the server marks the saved key
 * as stale: that one is a key to enter again, and is not held back. The server sends no reason of its
 * own, so the page says "usually", never "an update did this".
 */
export const heldBack = (
  owner: Pick<
    TranslationEngine | TranslationConnection,
    "enabled" | "awaiting_secrets" | "secrets"
  >,
) =>
  !owner.enabled &&
  owner.awaiting_secrets &&
  !Object.values(owner.secrets).some((entry) => entry.stale === true);

/**
 * What stands between a held-back back end and its switch: settings to check and save, or a type the
 * engine's plugin no longer has, which leaves nothing to check and only the removal.
 */
export function backendHold(
  engine: TranslationEngine,
  item: TranslationConnection,
): "review" | "gone" | null {
  if (!heldBack(item)) {
    return null;
  }
  return backendTypes(engine)[item.backend_type] ? "review" : "gone";
}

/** What a restart will do, in words, by the change that waits for it. */
export const RESTART_LINES: Record<RestartChange, string> = {
  install: "It is installed. Restart Bazarr+ to start it.",
  update: "An update is staged. Restart Bazarr+ to apply it.",
  removal: "It is being removed. Restart Bazarr+ to finish the removal.",
};

/** Whether an engine can take a job, and when it cannot, the reason and the next step. */
export function engineStatus(engine: TranslationEngine): Status {
  // Removed while Bazarr+ runs: it takes no job, and there is nothing to repair or reinstall.
  if (engine.removing) {
    return {
      tone: "warning",
      label: "Removing",
      next: "This plugin is being removed. It goes when the calls it is running end, or after a few minutes.",
    };
  }
  // A removal waits on the restart whether or not the plugin still runs until then.
  if (
    engine.awaiting_restart === "removal" ||
    (!canRun(engine) && engine.awaiting_restart)
  ) {
    return {
      tone: "warning",
      label: "Pending restart",
      next: RESTART_LINES[engine.awaiting_restart!],
    };
  }
  if (!canRun(engine)) {
    return {
      tone: "danger",
      label: "Plugin not running",
      next: "Its plugin is not installed or not running. Repair or reinstall it in the Plugin Hub.",
    };
  }
  // An engine that is off takes no job whatever else is true of it, so this comes before what it lacks.
  if (heldBack(engine)) {
    return (engine.schema?.fields ?? []).length > 0
      ? {
          tone: "warning",
          label: "Needs a review",
          next: "Turned off because its saved settings no longer fit, usually after a plugin update. Check the settings and save them, then turn it on.",
        }
      : {
          tone: "warning",
          label: "Turned off",
          next: "Turned off, usually by a plugin update. Turn it on to translate with it.",
        };
  }
  if (!engine.enabled) {
    return {
      tone: "neutral",
      label: "Off",
      next: "This engine is off. Turn it on to translate with it.",
    };
  }
  if (engine.awaiting_secrets) {
    return {
      tone: "warning",
      label: "Needs its key again",
      next: "An address changed, so the saved key no longer applies. Enter it again in the settings.",
    };
  }
  if (hasBackends(engine)) {
    if (engine.connections.length === 0) {
      return {
        tone: "warning",
        label: "Needs a back end",
        next: "Add a back end so there is somewhere to send the text.",
      };
    }
    if (!engine.connections.some(working)) {
      const waiting = engine.connections.some(
        (item) => item.enabled && item.awaiting_secrets,
      );
      if (waiting) {
        return {
          tone: "warning",
          label: "Needs its key again",
          next: "A back end's address changed. Open it and enter its key again.",
        };
      }
      // None is on, and some are off for a reason the person did not choose: that comes before "turn
      // one on". What to do with each is said above the list, so this only points there.
      const held = engine.connections.filter(
        (item) => backendHold(engine, item) === "review",
      ).length;
      if (held > 0) {
        return {
          tone: "warning",
          label: "Needs a review",
          next:
            held === 1
              ? "No back end is on. One was turned off until its settings are checked, usually after a plugin update. Open it under Back ends."
              : `No back end is on. ${held} were turned off until their settings are checked, usually after a plugin update. Open them under Back ends.`,
        };
      }
      return {
        tone: "warning",
        label: "No back end is on",
        next: "Turn a back end on, or finish setting one up.",
      };
    }
  }
  if (!engine.configured) {
    return {
      tone: "warning",
      label: "Not set up",
      // Its settings may already show values, as an address that is only a default does: saving is the step.
      next: "Check its settings and save them.",
    };
  }
  return { tone: "success", label: "Ready", next: "" };
}

/**
 * First run: nothing is selected and nobody has added a back end. An engine that needs no set-up
 * counts as installed, not as set up: installing one from the first-run panel leaves the panel up,
 * with that engine ready to pick.
 */
export const isFirstRun = (state: TranslationState) =>
  state.engine === null &&
  !state.instances.some((engine) => engine.connections.length > 0);

export interface StartingPoint {
  key: string;
  typeId: string;
  label: string;
  /** The back-end type's label, for a starting point that is one of its presets. */
  group: string;
  /** The preset choice this starting point applies. */
  preset?: { field: PluginField; choice: string };
}

/**
 * What "Add a back end" offers: every back-end type, and for a type whose schema has a preset
 * choice, one entry per preset. Read from the schema alone.
 */
export function startingPoints(engine: TranslationEngine): StartingPoint[] {
  const points: StartingPoint[] = [];
  for (const [typeId, type] of Object.entries(backendTypes(engine))) {
    const field = presetField(type);
    if (!field) {
      points.push({ key: typeId, typeId, label: type.label, group: "" });
      continue;
    }
    for (const item of field.choices ?? []) {
      const choice = choiceOf(item);
      points.push({
        key: `${typeId}:${choice.value}`,
        typeId,
        label: choice.label ?? choice.value,
        group: type.label,
        preset: { field, choice: choice.value },
      });
    }
  }
  return points;
}

/**
 * The back-end types a key alone sets up: one secret field, and no address to type. These are the
 * ones the first-run panel offers; the rest need the full form.
 */
export function keyOnlyTypes(engine: TranslationEngine) {
  return Object.entries(backendTypes(engine))
    .map(([typeId, type]) => ({
      typeId,
      type,
      secret: type.fields.find(isSecretField),
    }))
    .filter(
      (
        item,
      ): item is {
        typeId: string;
        type: TranslationBackendType;
        secret: PluginField;
      } =>
        item.secret !== undefined &&
        !item.type.fields.some((field) => field.destination === true),
    );
}

const dollars = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});

/** A dollar amount. Under ten cents it keeps four decimals, so a cheap job does not read as free. */
export function money(value: number): string {
  if (value > 0 && value < 0.1) {
    return `$${value.toFixed(4)}`;
  }
  return dollars.format(value);
}

/** A price per million tokens, as a back end's row states it. */
export function pricePair(input: unknown, output: unknown): string {
  const a = typeof input === "number" ? input : 0;
  const b = typeof output === "number" ? output : 0;
  if (a === 0 && b === 0) {
    return "Free";
  }
  // Two decimals, or three when the price needs them ($0.075).
  const one = (value: number) =>
    "$" +
    (Number(value.toFixed(2)) === value ? value.toFixed(2) : value.toFixed(3));
  return `${one(a)} in, ${one(b)} out per million tokens`;
}

export function tokens(value: number): string {
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1)}M`;
  }
  if (value >= 1000) {
    return `${(value / 1000).toFixed(1)}k`;
  }
  return String(value);
}

export const JOB_STATUS: Record<
  TranslationJobStatus,
  { tone: Tone; label: string }
> = {
  running: { tone: "info", label: "Running" },
  awaiting_retry: { tone: "warning", label: "Waiting to retry" },
  completed: { tone: "success", label: "Completed" },
  partial: { tone: "warning", label: "Partial" },
  failed: { tone: "danger", label: "Failed" },
  cancelled: { tone: "neutral", label: "Cancelled" },
  superseded: { tone: "neutral", label: "Replaced" },
  declined: { tone: "neutral", label: "Declined" },
};

/** What a stored secret may be said to be, in a few words. Never its value. */
export function keyState(item: TranslationConnection): string {
  const entries = Object.values(item.secrets);
  // A held-back back end is flagged for its settings: its key is as it was.
  if (item.awaiting_secrets && !heldBack(item)) {
    return "Key needed again";
  }
  const set = entries.find((entry) => entry.configured && !entry.stale);
  if (!set) {
    return "No key";
  }
  if (set.count !== undefined) {
    return `${set.count} ${set.count === 1 ? "key" : "keys"}`;
  }
  return set.last4 ? `Key ending ${set.last4}` : "Key saved";
}
