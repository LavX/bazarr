import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { WizardStepDef } from "./steps/types";
import { useWizardStep } from "./useWizardStep";

const STORAGE_KEY = "bazarr.onboarding.step";

const Blank = () => null;

function defs(...keys: string[]): WizardStepDef[] {
  return keys.map((key) => ({
    key,
    label: key,
    phase: "connect" as const,
    Component: Blank,
  }));
}

const BASE = defs("welcome", "intent", "media-servers", "seerr", "finish");

// The two real lists, in order. The media server picker is on both paths now;
// when the cursor was still a number it was library-only, which is what makes
// the Discover migration below a different list rather than the same one.
const DISCOVER = defs(
  "welcome",
  "intent",
  "media-servers",
  "seerr",
  "languages",
  "providers",
  "translator",
  "general",
  "finish",
);
const LIBRARY = defs(
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
);

describe("useWizardStep", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("starts on the first step and persists nothing yet", () => {
    const { result } = renderHook(() => useWizardStep(BASE));

    expect(result.current.index).toBe(0);
    expect(result.current.step.key).toBe("welcome");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("next and back move by key, not by index", () => {
    const { result } = renderHook(() => useWizardStep(BASE));

    act(() => result.current.next());
    expect(result.current.step.key).toBe("intent");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("intent");

    act(() => result.current.back());
    expect(result.current.step.key).toBe("welcome");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("welcome");
  });

  it("never walks off either end", () => {
    const { result } = renderHook(() => useWizardStep(BASE));

    act(() => result.current.back());
    expect(result.current.index).toBe(0);

    act(() => result.current.goTo("finish"));
    act(() => result.current.next());
    expect(result.current.step.key).toBe("finish");
  });

  it("rehydrates the key from localStorage", () => {
    localStorage.setItem(STORAGE_KEY, "seerr");

    const { result } = renderHook(() => useWizardStep(BASE));

    expect(result.current.step.key).toBe("seerr");
  });

  it("migrates a stored index against the list that version built", () => {
    // A Discover reader who upgrades mid-wizard. The old list on this path had
    // no media server step, so 4 was Providers; reading 4 out of the current
    // list would land them on Languages, a step they already answered, and
    // shift every later cursor with it.
    localStorage.setItem("bazarr.onboarding.intent", "discover");
    localStorage.setItem(STORAGE_KEY, "4");

    const { result } = renderHook(() => useWizardStep(DISCOVER));

    expect(result.current.step.key).toBe("providers");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("providers");
  });

  it("migrates a library index, where the order did not move", () => {
    localStorage.setItem("bazarr.onboarding.intent", "library");
    localStorage.setItem(STORAGE_KEY, "8");

    const { result } = renderHook(() => useWizardStep(LIBRARY));

    expect(result.current.step.key).toBe("providers");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("providers");
  });

  it("discards an index naming a step this run does not walk", () => {
    localStorage.setItem("bazarr.onboarding.intent", "library");
    localStorage.setItem(STORAGE_KEY, "2");

    const { result } = renderHook(() =>
      useWizardStep(defs("welcome", "intent", "finish")),
    );

    expect(result.current.step.key).toBe("welcome");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("discards a stored index that is past the end of the list", () => {
    localStorage.setItem(STORAGE_KEY, "42");

    const { result } = renderHook(() => useWizardStep(BASE));

    expect(result.current.step.key).toBe("welcome");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("falls back to the nearest surviving earlier step, not a blank screen", () => {
    const withGenerated = defs(
      "welcome",
      "intent",
      "media-servers",
      "media-server-a",
      "media-server-b",
      "seerr",
      "finish",
    );
    const { result, rerender } = renderHook(
      ({ steps }) => useWizardStep(steps),
      { initialProps: { steps: withGenerated } },
    );

    act(() => result.current.goTo("media-server-b"));
    expect(result.current.step.key).toBe("media-server-b");

    // The reader unticks that server on the picker while standing on its step.
    rerender({
      steps: defs(
        "welcome",
        "intent",
        "media-servers",
        "media-server-a",
        "seerr",
        "finish",
      ),
    });

    expect(result.current.step.key).toBe("media-server-a");
    // Corrected in state, not only for the render.
    expect(localStorage.getItem(STORAGE_KEY)).toBe("media-server-a");
  });

  it("reset clears the persisted key", () => {
    const { result } = renderHook(() => useWizardStep(BASE));

    act(() => result.current.goTo("seerr"));
    expect(localStorage.getItem(STORAGE_KEY)).toBe("seerr");

    act(() => result.current.reset());

    expect(result.current.index).toBe(0);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });
});
