import { describe, expect, it } from "vitest";
import { ONBOARDING_STEPS, stepsForIntent } from "./index";

// Every step body under steps/, source and all, keyed by module path. The raw
// text is what we assert on: a skip control is a thing a step renders, and the
// only way to catch one in a step this suite does not otherwise mount (a
// provider sub-stage, say) is to read the file.
const stepSources = import.meta.glob<string>(
  ["./**/*.tsx", "!./**/*.test.tsx", "!./index.tsx"],
  { query: "?raw", import: "default", eager: true },
);

// <Button ...>children</Button> and its Mantine siblings. Non-greedy, because
// these never nest, and deliberately blind to <Text>/<Alert> prose: a step is
// free to say "you can skip this later", it just may not render the control.
const CONTROL_WITH_CHILDREN =
  /<(Button|UnstyledButton|ActionIcon|Anchor)\b[^>]*>([\s\S]*?)<\/\1>/g;

function skipControlsIn(source: string): string[] {
  const found: string[] = [];
  for (const match of source.matchAll(CONTROL_WITH_CHILDREN)) {
    if (/skip/i.test(match[2])) {
      found.push(match[0].replace(/\s+/g, " ").trim());
    }
  }
  return found;
}

describe("onboarding step registry", () => {
  it("no step renders its own skip control", () => {
    // The shell renders one skip control from step.optional, with one label.
    // A step that rolls its own puts a second, differently worded affordance
    // on the same page, which is how "Skip for now" and "Skip" ended up
    // meaning the same thing while General, declared optional, had neither.
    const offenders = Object.entries(stepSources)
      .map(([path, source]) => [path, skipControlsIn(source)] as const)
      .filter(([, controls]) => controls.length > 0);

    expect(offenders).toEqual([]);
  });

  it("every step declares a skip or says why it has none", () => {
    // Welcome and Finish are the bookends: they configure nothing, so there is
    // nothing to skip and nothing to explain. Every step between them has to
    // answer for itself one way or the other.
    const bookends = new Set(["welcome", "finish"]);

    for (const step of ONBOARDING_STEPS) {
      // Never both: a step that can be skipped has no reason to explain why it
      // cannot be.
      expect(step.optional === true && step.requiredReason !== undefined).toBe(
        false,
      );
      if (bookends.has(step.key)) {
        continue;
      }
      expect(
        step.optional === true || typeof step.requiredReason === "string",
      ).toBe(true);
    }
  });

  it("the library path keeps the arr steps and drops nothing they need", () => {
    const keys = stepsForIntent("library").map((s) => s.key);

    expect(keys).toEqual([
      "welcome",
      "intent",
      "sonarr",
      "radarr",
      "sportarr",
      "media-servers",
      "seerr",
      "languages",
      "providers",
      "translator",
      "general",
      "finish",
    ]);
  });

  it("the discover path drops the arr and media-server steps", () => {
    const keys = stepsForIntent("discover").map((s) => s.key);

    expect(keys).toEqual([
      "welcome",
      "intent",
      "seerr",
      "languages",
      "providers",
      "translator",
      "general",
      "finish",
    ]);
  });

  it("no arr step is required", () => {
    for (const key of ["sonarr", "radarr", "sportarr"]) {
      const step = ONBOARDING_STEPS.find((s) => s.key === key);
      expect(step?.optional).toBe(true);
    }
  });

  it("shows the whole registry before an intent is chosen", () => {
    expect(stepsForIntent(null).map((s) => s.key)).toEqual(
      ONBOARDING_STEPS.map((s) => s.key),
    );
  });
});
