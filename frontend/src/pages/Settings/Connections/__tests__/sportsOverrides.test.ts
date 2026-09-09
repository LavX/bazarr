/* eslint-disable camelcase */
import { describe, expect, it } from "vitest";
import type { ArrSportsSettings } from "@/apis/raw/arrInstances";
import {
  isSportsOverridden,
  setSportsOverride,
  SPORTS_OVERRIDE_FIELDS,
  sportsOverrideDefault,
} from "../sportsOverrides";

describe("sports overrides", () => {
  it("covers every resolvable key and uses the renamed names", () => {
    const keys = SPORTS_OVERRIDE_FIELDS.map((field) => field.key).sort();
    expect(keys).toEqual(
      [
        "excluded_sports",
        "excluded_tags",
        "full_update",
        "full_update_day",
        "full_update_hour",
        "minimum_score",
        "only_monitored",
        "search_on_sync",
        "sports_sync",
        "sync_only_monitored_events",
        "sync_only_monitored_leagues",
        "use_ffprobe_cache",
        "wanted_search_frequency",
      ].sort(),
    );
  });

  it("reports a key as overridden only when the blob carries it", () => {
    expect(isSportsOverridden({}, "only_monitored")).toBe(false);
    // False is a real override, not an absent one.
    expect(isSportsOverridden({ only_monitored: false }, "only_monitored")).toBe(
      true,
    );
  });

  it("sets and clears a single override without touching its neighbours", () => {
    let blob: ArrSportsSettings = {};
    blob = setSportsOverride(blob, "only_monitored", true);
    blob = setSportsOverride(blob, "minimum_score", 85);
    expect(blob).toEqual({ only_monitored: true, minimum_score: 85 });

    blob = setSportsOverride(blob, "only_monitored", undefined);
    expect(blob).toEqual({ minimum_score: 85 });
    expect(isSportsOverridden(blob, "only_monitored")).toBe(false);
  });

  it("does not mutate the blob it is given", () => {
    const original: ArrSportsSettings = { minimum_score: 70 };
    const next = setSportsOverride(original, "only_monitored", true);
    expect(original).toEqual({ minimum_score: 70 });
    expect(next).not.toBe(original);
  });

  it("starts each override kind at a value the backend validator accepts", () => {
    expect(sportsOverrideDefault("bool")).toBe(true);
    expect(sportsOverrideDefault("percent")).toBe(70);
    expect(sportsOverrideDefault("tags")).toEqual([]);
    expect(sportsOverrideDefault("fullUpdate")).toBe("Daily");
    expect(sportsOverrideDefault("syncInterval")).toBe(60);
    expect(sportsOverrideDefault("searchFrequency")).toBe(6);
    expect(sportsOverrideDefault("day")).toBe(6);
    expect(sportsOverrideDefault("hour")).toBe(4);
  });
});
