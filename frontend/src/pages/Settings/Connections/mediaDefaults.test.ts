/* eslint-disable camelcase */
import { describe, expect, it } from "vitest";
import {
  GLOBAL_DEFAULT,
  hasMediaDefaultProfile,
  mediaDefaultsToValue,
  NO_PROFILE,
  valueToMediaDefaults,
} from "./mediaDefaults";

describe("mediaDefaultsToValue", () => {
  it("maps a missing block to the explicit global-default choice", () => {
    // The reverse-failure case in the UI: nothing stored must READ as
    // "use the global default", never as a picked profile.
    expect(mediaDefaultsToValue(undefined)).toBe(GLOBAL_DEFAULT);
    expect(mediaDefaultsToValue({})).toBe(GLOBAL_DEFAULT);
  });

  it("maps a disabled override to the no-profile choice", () => {
    expect(mediaDefaultsToValue({ default_enabled: false })).toBe(NO_PROFILE);
  });

  it("maps an enabled override to its profile id", () => {
    expect(
      mediaDefaultsToValue({ default_enabled: true, default_profile: 3 }),
    ).toBe("3");
  });
});

describe("valueToMediaDefaults", () => {
  it("clears the block for the global-default choice", () => {
    expect(valueToMediaDefaults(GLOBAL_DEFAULT)).toEqual({});
  });

  it("stores a disabled override for the no-profile choice", () => {
    expect(valueToMediaDefaults(NO_PROFILE)).toEqual({
      default_enabled: false,
    });
  });

  it("stores an enabled override with a numeric profile id", () => {
    expect(valueToMediaDefaults("3")).toEqual({
      default_enabled: true,
      default_profile: 3,
    });
  });

  it("round-trips every state", () => {
    for (const blob of [
      {},
      { default_enabled: false },
      { default_enabled: true, default_profile: 7 },
    ]) {
      expect(valueToMediaDefaults(mediaDefaultsToValue(blob))).toEqual(blob);
    }
  });
});

describe("hasMediaDefaultProfile", () => {
  it("is true only when a profile is actually assigned", () => {
    expect(
      hasMediaDefaultProfile({ default_enabled: true, default_profile: 3 }),
    ).toBe(true);
    expect(hasMediaDefaultProfile({ default_enabled: false })).toBe(false);
    expect(hasMediaDefaultProfile({})).toBe(false);
    expect(hasMediaDefaultProfile(undefined)).toBe(false);
  });
});
