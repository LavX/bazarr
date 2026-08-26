/* eslint-disable camelcase */

import { describe, expect, it } from "vitest";
import {
  foldScoreModifier,
  resolveProviderScoreModifiers,
} from "@/pages/Settings/Providers/components";

/** A saved-settings stand-in carrying only the field under test. */
const saved = (modifiers: unknown): Settings =>
  ({
    general: { provider_score_modifiers: modifiers },
  }) as unknown as Settings;

describe("provider score modifiers", () => {
  describe("reading the current values", () => {
    it("prefers what is staged in this settings session over what is saved", () => {
      const resolved = resolveProviderScoreModifiers(
        {
          "settings-general-provider_score_modifiers": JSON.stringify({
            whisperai: 25,
          }),
        },
        saved(JSON.stringify({ whisperai: 5 })),
      );

      expect(resolved).toEqual({ whisperai: 25 });
    });

    it("falls back to the saved settings when nothing is staged", () => {
      const resolved = resolveProviderScoreModifiers(
        {},
        saved(JSON.stringify({ whisperai: 5 })),
      );

      expect(resolved).toEqual({ whisperai: 5 });
    });

    it("reads an object that was never serialised", () => {
      const resolved = resolveProviderScoreModifiers(
        {
          "settings-general-provider_score_modifiers": { whisperai: 25 },
        },
        null,
      );

      expect(resolved).toEqual({ whisperai: 25 });
    });

    it("gives back nothing rather than throwing on malformed JSON", () => {
      const resolved = resolveProviderScoreModifiers(
        { "settings-general-provider_score_modifiers": "{not json" },
        null,
      );

      expect(resolved).toEqual({});
    });

    it("drops an entry whose value is not a number", () => {
      const resolved = resolveProviderScoreModifiers(
        {
          "settings-general-provider_score_modifiers": JSON.stringify({
            whisperai: "quite a lot",
            subdl: 10,
          }),
        },
        null,
      );

      expect(resolved).toEqual({ subdl: 10 });
    });
  });

  describe("writing a value back", () => {
    it("records the modifier the user set", () => {
      expect(foldScoreModifier({ subdl: 10 }, "whisperai", 25)).toEqual({
        subdl: 10,
        whisperai: 25,
      });
    });

    it("records a negative modifier, which is the demoting case", () => {
      expect(foldScoreModifier({}, "someprovider", -20)).toEqual({
        someprovider: -20,
      });
    });

    it("removes the entry when the user clears it back to zero", () => {
      expect(
        foldScoreModifier({ whisperai: 25, subdl: 10 }, "whisperai", 0),
      ).toEqual({
        subdl: 10,
      });
    });

    it("removes the entry when the field is emptied", () => {
      expect(foldScoreModifier({ whisperai: 25 }, "whisperai", "")).toEqual({});
    });

    it("leaves the other providers alone", () => {
      const before = { subdl: 10, opensubtitles: -5 };
      const after = foldScoreModifier(before, "whisperai", 25);

      expect(after.subdl).toBe(10);
      expect(after.opensubtitles).toBe(-5);
      expect(before).toEqual({ subdl: 10, opensubtitles: -5 });
    });

    it("clamps a value beyond the percentage scale to its ends", () => {
      expect(foldScoreModifier({}, "whisperai", 500)).toEqual({
        whisperai: 100,
      });
      expect(foldScoreModifier({}, "whisperai", -500)).toEqual({
        whisperai: -100,
      });
    });

    it("ignores a value that is not a number", () => {
      expect(foldScoreModifier({ whisperai: 25 }, "whisperai", "abc")).toEqual({
        whisperai: 25,
      });
    });
  });
});
