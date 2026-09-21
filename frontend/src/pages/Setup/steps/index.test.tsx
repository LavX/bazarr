import { describe, expect, it } from "vitest";
import type { MediaServerDraftSummary } from "@/pages/Setup/useOnboardingSelection";
import { buildSteps, mediaServerStepKey, ONBOARDING_STEPS } from "./index";

const NO_SERVERS: MediaServerDraftSummary[] = [];

function keysFor(
  intent: "library" | "discover" | null,
  mediaServers: MediaServerDraftSummary[] = NO_SERVERS,
) {
  return buildSteps({ intent, mediaServers }).map((s) => s.key);
}

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
    const keys = keysFor("library");

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

  it("the discover path drops the arr steps but keeps media servers", () => {
    // A Jellyfin user with no Sonarr never saw the media server step at all,
    // though connecting one has nothing to do with running an arr.
    const keys = keysFor("discover");

    expect(keys).toEqual([
      "welcome",
      "intent",
      "media-servers",
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
    expect(keysFor(null)).toEqual(ONBOARDING_STEPS.map((s) => s.key));
  });

  it("generates one configure step per selected media server", () => {
    const keys = keysFor("library", [
      { draftId: "a", kind: "emby" },
      { draftId: "b", kind: "emby" },
      { draftId: "c", kind: "jellyfin" },
    ]);

    expect(keys.slice(5, 9)).toEqual([
      "media-servers",
      mediaServerStepKey("a"),
      mediaServerStepKey("b"),
      mediaServerStepKey("c"),
    ]);
  });

  it("generates nothing for a server that is already saved", () => {
    const keys = keysFor("library", [
      { draftId: "a", kind: "emby", instanceId: "row-1" },
      { draftId: "b", kind: "silo" },
    ]);

    expect(keys).not.toContain(mediaServerStepKey("a"));
    expect(keys).toContain(mediaServerStepKey("b"));
  });

  it("an empty selection yields the picker on its own", () => {
    expect(keysFor("library", [])).toEqual(keysFor("library"));
  });

  it("is pure: the same state gives the same list", () => {
    const input = {
      intent: "library" as const,
      mediaServers: [{ draftId: "a", kind: "silo" as const }],
    };

    expect(buildSteps(input).map((s) => s.key)).toEqual(
      buildSteps(input).map((s) => s.key),
    );
  });

  it("every generated step is optional, like the picker that made it", () => {
    const generated = buildSteps({
      intent: "library",
      mediaServers: [{ draftId: "a", kind: "emby" }],
    }).find((s) => s.key === mediaServerStepKey("a"));

    expect(generated?.optional).toBe(true);
    expect(generated?.requiredReason).toBeUndefined();
  });

  it("every step declares the phase the rail counts", () => {
    for (const step of ONBOARDING_STEPS) {
      expect(["start", "connect", "subtitles", "finish"]).toContain(step.phase);
    }
  });
});
