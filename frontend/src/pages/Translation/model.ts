/* eslint-disable camelcase -- transport names. */
import {
  choiceOf,
  isSecretField,
  PluginField,
} from "@/components/plugin-settings";
import type { Tone } from "@/pages/Settings/Providers/hub/utils";
import type {
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

export interface Status {
  tone: Tone;
  label: string;
  /** What to do about it. Empty when nothing needs doing. */
  next: string;
}

/** Whether an engine can take a job, and when it cannot, the reason and the next step. */
export function engineStatus(engine: TranslationEngine): Status {
  if (!engine.plugin?.active) {
    return {
      tone: "danger",
      label: "Plugin not running",
      next: "Its plugin is not installed or not running. Repair or reinstall it in the Subtitle Hub.",
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
      return waiting
        ? {
            tone: "warning",
            label: "Needs its key again",
            next: "A back end's address changed. Open it and enter its key again.",
          }
        : {
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
      next: "Fill in its settings and save them.",
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
  if (item.awaiting_secrets) {
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
