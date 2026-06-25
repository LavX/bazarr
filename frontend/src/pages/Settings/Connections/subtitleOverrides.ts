import type { ArrSubtitleSettings } from "@/apis/raw/arrInstances";

// Per-instance subtitle setting overrides (#227). Each field maps to a key under
// options.subtitle_settings[<section>]; a present key overrides the global value,
// an absent key inherits it.
export type OverrideSection = "general" | "subsync";

export type OverrideKind =
  | "bool"
  | "percent"
  | "text"
  | "engines"
  | "mods"
  | "offset";

export interface OverrideField {
  section: OverrideSection;
  key: string;
  label: string;
  description?: string;
  kind: OverrideKind;
}

// The set mirrors the backend ALLOWED dict (arr_instances/subtitle_settings.py).
export const OVERRIDE_FIELDS: OverrideField[] = [
  {
    section: "general",
    key: "use_postprocessing",
    label: "Use custom post-processing",
    kind: "bool",
  },
  {
    section: "general",
    key: "postprocessing_cmd",
    label: "Post-processing command",
    kind: "text",
  },
  {
    section: "general",
    key: "use_postprocessing_threshold",
    label: "Limit post-processing by score (series)",
    kind: "bool",
  },
  {
    section: "general",
    key: "postprocessing_threshold",
    label: "Post-processing score threshold (series)",
    kind: "percent",
  },
  {
    section: "general",
    key: "use_postprocessing_threshold_movie",
    label: "Limit post-processing by score (movies)",
    kind: "bool",
  },
  {
    section: "general",
    key: "postprocessing_threshold_movie",
    label: "Post-processing score threshold (movies)",
    kind: "percent",
  },
  {
    section: "general",
    key: "subzero_mods",
    label: "Subtitle modifications",
    kind: "mods",
  },
  {
    section: "general",
    key: "subzero_mods_keep_lyrics",
    label: "Keep lyrics when removing hearing-impaired text",
    kind: "bool",
  },
  {
    section: "subsync",
    key: "use_subsync",
    label: "Automatic subtitle synchronization",
    kind: "bool",
  },
  {
    section: "subsync",
    key: "use_subsync_threshold",
    label: "Limit sync by score (series)",
    kind: "bool",
  },
  {
    section: "subsync",
    key: "subsync_threshold",
    label: "Sync score threshold (series)",
    kind: "percent",
  },
  {
    section: "subsync",
    key: "use_subsync_movie_threshold",
    label: "Limit sync by score (movies)",
    kind: "bool",
  },
  {
    section: "subsync",
    key: "subsync_movie_threshold",
    label: "Sync score threshold (movies)",
    kind: "percent",
  },
  {
    section: "subsync",
    key: "enabled_engines",
    label: "Sync engines",
    kind: "engines",
  },
  {
    section: "subsync",
    key: "max_offset_seconds",
    label: "Maximum offset (seconds)",
    kind: "offset",
  },
];

export function isOverridden(
  blob: ArrSubtitleSettings,
  section: OverrideSection,
  key: string,
): boolean {
  const sec = blob[section] as Record<string, unknown> | undefined;
  return sec !== undefined && key in sec;
}

// Immutably set (or, when value is undefined, remove) a single override. An
// emptied section is pruned so the persisted blob never carries dangling
// sections, matching what the backend merge helper expects.
export function setOverride(
  blob: ArrSubtitleSettings,
  section: OverrideSection,
  key: string,
  value: unknown,
): ArrSubtitleSettings {
  const current = blob[section] as Record<string, unknown> | undefined;
  const sec: Record<string, unknown> = { ...(current ?? {}) };
  if (value === undefined) {
    delete sec[key];
  } else {
    sec[key] = value;
  }
  const next = { ...blob } as Record<string, unknown>;
  if (Object.keys(sec).length > 0) {
    next[section] = sec;
  } else {
    delete next[section];
  }
  return next as ArrSubtitleSettings;
}

// The value an override starts at when the user first enables it.
export function overrideDefault(kind: OverrideKind): unknown {
  switch (kind) {
    case "bool":
      return true;
    case "percent":
      return 90;
    case "text":
      return "";
    case "engines":
      return ["ffsubsync"];
    case "mods":
      return [];
    case "offset":
      return 120;
    default:
      return undefined;
  }
}
