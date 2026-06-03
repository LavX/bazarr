import type {
  DistKindLimits,
  DistLimitWindows,
} from "@/apis/raw/distributionHub";

export const WINDOWS: (keyof DistLimitWindows)[] = [
  "hour",
  "day",
  "week",
  "month",
];

export const WINDOW_LABELS: Record<keyof DistLimitWindows, string> = {
  hour: "Hourly",
  day: "Daily",
  week: "Weekly",
  month: "Monthly",
};

export function emptyWindows(): DistLimitWindows {
  return { hour: 0, day: 0, week: 0, month: 0 };
}

export function emptyKindLimits(): DistKindLimits {
  return { search: emptyWindows(), download: emptyWindows() };
}

/** A 0 limit means "unlimited" for that window. */
export function formatLimit(value: number): string {
  return value > 0 ? value.toLocaleString() : "∞";
}
