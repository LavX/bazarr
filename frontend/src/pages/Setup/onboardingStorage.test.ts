import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  onboardingKey,
  readOnboardingValue,
  removeOnboardingValue,
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

  it.each(NAMES)(
    "does not adopt the legacy %s again after this install cleared its own",
    (name) => {
      // Finishing, leaving and Run first-time setup all clear the wizard's
      // state. Removing the key left this install with nothing of its own,
      // which is exactly when the legacy value is adopted, so the wizard came
      // back on the old step with the old answers after every clear.
      localStorage.setItem(`bazarr.onboarding.${name}`, "kept");
      underBaseUrl("/bazarr");
      expect(readOnboardingValue(name)).toBe("kept");

      removeOnboardingValue(name);

      expect(readOnboardingValue(name)).toBeNull();
      expect(readOnboardingValue(name)).toBeNull();
      // Still there for the install served from the root, which uses it.
      expect(localStorage.getItem(`bazarr.onboarding.${name}`)).toBe("kept");
    },
  );

  it.each(NAMES)(
    "does not adopt a legacy %s that turns up after this install cleared its own",
    (name) => {
      // An install served from the root on the same origin keeps writing the
      // un-namespaced key, so a clear here has to hold against a value that
      // was not there yet when it happened.
      underBaseUrl("/bazarr");
      writeOnboardingValue(name, "mine");
      removeOnboardingValue(name);
      localStorage.setItem(`bazarr.onboarding.${name}`, "root install");

      expect(readOnboardingValue(name)).toBeNull();
    },
  );

  it("writes over a cleared value like any other", () => {
    underBaseUrl("/bazarr");
    removeOnboardingValue("step");
    writeOnboardingValue("step", "seerr");

    expect(readOnboardingValue("step")).toBe("seerr");
  });

  it("removes the key outright for an install without a base URL", () => {
    // The root install reads the un-namespaced key as its own, so there is no
    // legacy value to hold off and nothing needs to stay behind.
    localStorage.setItem("bazarr.onboarding.step", "seerr");

    removeOnboardingValue("step");

    expect(localStorage.getItem("bazarr.onboarding.step")).toBeNull();
    expect(readOnboardingValue("step")).toBeNull();
  });

  it("never adopts the connection tests of another install", () => {
    // The Test results were first stored after the keys were namespaced, so
    // no earlier version of this install ever wrote them under the plain key.
    // What is there belongs to an install served from the root on the same
    // origin, and its row ids are the same small numbers: adopting them
    // reported rows that were never tested here as connected.
    const rootResults = JSON.stringify({ "arr:1": "passed" });
    localStorage.setItem("bazarr.onboarding.connection-tests", rootResults);
    underBaseUrl("/bazarr");

    expect(readOnboardingValue("connection-tests")).toBeNull();
    expect(localStorage.getItem(onboardingKey("connection-tests"))).toBeNull();
    expect(localStorage.getItem("bazarr.onboarding.connection-tests")).toBe(
      rootResults,
    );
  });

  it("reports nothing when there is nothing under either key", () => {
    underBaseUrl("/bazarr");

    expect(readOnboardingValue("step")).toBeNull();
    expect(localStorage.getItem(onboardingKey("step"))).toBeNull();
  });
});
