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
      // Copied, not moved: an install served from the root reads and writes
      // this very key, so taking it would empty that install's wizard.
      expect(localStorage.getItem(`bazarr.onboarding.${name}`)).toBe("kept");
      // Reading again is served by this install's own key.
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

  it("leaves another install's value where that install can still read it", () => {
    // Both installs sit behind one proxy on one origin, so they share
    // localStorage. The first is served from the root and never stopped using
    // the un-namespaced key. The second is served from /bazarr.
    localStorage.setItem("bazarr.onboarding.step", "root install");

    underBaseUrl("/bazarr");
    expect(readOnboardingValue("step")).toBe("root install");
    writeOnboardingValue("step", "subpath install");

    underBaseUrl("");
    expect(onboardingKey("step")).toBe("bazarr.onboarding.step");
    expect(readOnboardingValue("step")).toBe("root install");
    expect(localStorage.getItem("bazarr.onboarding.step::/bazarr")).toBe(
      "subpath install",
    );
  });

  it("reports nothing when there is nothing under either key", () => {
    underBaseUrl("/bazarr");

    expect(readOnboardingValue("step")).toBeNull();
    expect(localStorage.getItem(onboardingKey("step"))).toBeNull();
  });
});
