import type { ArrSportsSettings } from "@/apis/raw/arrInstances";

// Per-instance Sportarr overrides. Each field maps to a key in the instance's
// options.sports_settings blob: a present key overrides the global value from
// Connections and Scheduler settings, an absent key inherits it. The set
// mirrors GLOBAL_SOURCES in bazarr/sportarr/settings.py, which is also the
// backend's allow-list, so adding a field here without adding it there yields a
// 400 on save.
export type SportsOverrideKind =
  | "bool"
  | "percent"
  | "tags"
  | "fullUpdate"
  | "syncInterval"
  | "searchFrequency"
  | "day"
  | "hour";

export interface SportsOverrideField {
  key: keyof ArrSportsSettings;
  label: string;
  kind: SportsOverrideKind;
}

export const SPORTS_OVERRIDE_FIELDS: SportsOverrideField[] = [
  { key: "sports_sync", label: "Library sync interval", kind: "syncInterval" },
  { key: "full_update", label: "Full subtitle scan", kind: "fullUpdate" },
  { key: "full_update_day", label: "Full scan day", kind: "day" },
  { key: "full_update_hour", label: "Full scan hour", kind: "hour" },
  { key: "minimum_score", label: "Minimum score", kind: "percent" },
  {
    key: "wanted_search_frequency",
    label: "Search for missing subtitles",
    kind: "searchFrequency",
  },
  { key: "only_monitored", label: "Download only monitored", kind: "bool" },
  {
    key: "sync_only_monitored_leagues",
    label: "Sync only monitored leagues",
    kind: "bool",
  },
  {
    key: "sync_only_monitored_events",
    label: "Sync only monitored events",
    kind: "bool",
  },
  { key: "excluded_tags", label: "Excluded tags", kind: "tags" },
  { key: "excluded_sports", label: "Excluded sports", kind: "tags" },
  { key: "search_on_sync", label: "Search after sync", kind: "bool" },
  { key: "use_ffprobe_cache", label: "Cache media analysis", kind: "bool" },
];

// Presence, not truthiness: an override set to false or 0 is still an override.
export function isSportsOverridden(
  blob: ArrSportsSettings,
  key: keyof ArrSportsSettings,
): boolean {
  return key in blob;
}

// Immutably set (or, when value is undefined, remove) a single override, so an
// inherited key is absent from the persisted blob rather than present with a
// frozen copy of the global value that would stop tracking it.
export function setSportsOverride(
  blob: ArrSportsSettings,
  key: keyof ArrSportsSettings,
  value: unknown,
): ArrSportsSettings {
  const next = { ...blob } as Record<string, unknown>;
  if (value === undefined) {
    delete next[key];
  } else {
    next[key] = value;
  }
  return next as ArrSportsSettings;
}

// The value an override starts at when first enabled. Matches the backend
// validator bounds so a freshly enabled override is never rejected on save.
export function sportsOverrideDefault(kind: SportsOverrideKind): unknown {
  switch (kind) {
    case "bool":
      return true;
    case "percent":
      return 70;
    case "tags":
      return [];
    case "fullUpdate":
      return "Daily";
    case "syncInterval":
      return 60;
    case "searchFrequency":
      return 6;
    case "day":
      return 6;
    case "hour":
      return 4;
    default:
      return undefined;
  }
}
