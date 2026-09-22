import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  onboardingKey,
  readOnboardingValue,
  writeOnboardingValue,
} from "./onboardingStorage";

// The three things the wizard remembers. All of them were written under the
// un-namespaced key before the keys were namespaced by base URL, and a reader
// mid-setup has whichever of them they had reached.
const NAMES = ["step", "intent", "media-servers"];

function underBaseUrl(base: string) {
  vi.stubGlobal("Bazarr", { baseUrl: base });
}

describe("onboarding storage", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it.each(NAMES)(
    "adopts the legacy %s of an install under a base URL",
    (name) => {
      localStorage.setItem(`bazarr.onboarding.${name}`, "kept");
      underBaseUrl("/bazarr");

      expect(readOnboardingValue(name)).toBe("kept");
      expect(localStorage.getItem(onboardingKey(name))).toBe("kept");
      // Taken as it is adopted: two instances behind one proxy share this key,
      // and the second to load must not inherit the first one's place.
      expect(localStorage.getItem(`bazarr.onboarding.${name}`)).toBeNull();
      // Nothing left to adopt a second time.
      expect(readOnboardingValue(name)).toBe("kept");
    },
  );

  it.each(NAMES)("never adopts a legacy %s over this install's own", (name) => {
    localStorage.setItem(`bazarr.onboarding.${name}`, "someone else");
    underBaseUrl("/bazarr");
    writeOnboardingValue(name, "mine");

    expect(readOnboardingValue(name)).toBe("mine");
    // Left for the instance it belongs to, which has not read it yet.
    expect(localStorage.getItem(`bazarr.onboarding.${name}`)).toBe(
      "someone else",
    );
  });

  it("leaves an install without a base URL on the key it always used", () => {
    localStorage.setItem("bazarr.onboarding.step", "seerr");

    expect(onboardingKey("step")).toBe("bazarr.onboarding.step");
    expect(readOnboardingValue("step")).toBe("seerr");
    expect(localStorage.getItem("bazarr.onboarding.step")).toBe("seerr");
  });

  it("reports nothing when there is nothing under either key", () => {
    underBaseUrl("/bazarr");

    expect(readOnboardingValue("step")).toBeNull();
    expect(localStorage.getItem(onboardingKey("step"))).toBeNull();
  });
});
