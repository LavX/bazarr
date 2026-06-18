import { describe, expect, it } from "vitest";
import {
  isOverridden,
  OVERRIDE_FIELDS,
  setOverride,
} from "./subtitleOverrides";

describe("setOverride", () => {
  it("adds a key under its section", () => {
    expect(setOverride({}, "subsync", "subsync_threshold", 80)).toEqual({
      subsync: { subsync_threshold: 80 },
    });
  });

  it("removes the key and prunes the empty section when value is undefined", () => {
    expect(
      setOverride(
        { subsync: { subsync_threshold: 80 } },
        "subsync",
        "subsync_threshold",
        undefined,
      ),
    ).toEqual({});
  });

  it("keeps sibling keys when removing one", () => {
    const blob = { subsync: { subsync_threshold: 80, use_subsync: true } };
    expect(setOverride(blob, "subsync", "use_subsync", undefined)).toEqual({
      subsync: { subsync_threshold: 80 },
    });
  });

  it("does not mutate the input blob", () => {
    const blob = { general: { use_postprocessing: true } };
    setOverride(blob, "general", "postprocessing_cmd", "/run.sh");
    expect(blob).toEqual({ general: { use_postprocessing: true } });
  });
});

describe("isOverridden", () => {
  it("is true only when the key is present (regardless of value)", () => {
    const blob = { general: { use_postprocessing: false } };
    expect(isOverridden(blob, "general", "use_postprocessing")).toBe(true);
    expect(isOverridden(blob, "general", "postprocessing_cmd")).toBe(false);
    expect(isOverridden({}, "subsync", "use_subsync")).toBe(false);
  });
});

describe("OVERRIDE_FIELDS", () => {
  it("covers the general and subsync sections", () => {
    const general = OVERRIDE_FIELDS.filter((f) => f.section === "general");
    const subsync = OVERRIDE_FIELDS.filter((f) => f.section === "subsync");
    expect(general.map((f) => f.key)).toContain("use_postprocessing");
    expect(general.map((f) => f.key)).toContain("subzero_mods");
    expect(subsync.map((f) => f.key)).toContain("use_subsync");
    expect(subsync.map((f) => f.key)).toContain("max_offset_seconds");
  });
});
