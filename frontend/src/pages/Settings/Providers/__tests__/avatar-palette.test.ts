import { describe, expect, it } from "vitest";
import {
  AVATAR_PALETTE_SIZE,
  getAvatarPaletteIndex,
} from "@/pages/Settings/Providers/avatar-palette";

describe("getAvatarPaletteIndex", () => {
  it("returns a stable index for the same key", () => {
    expect(getAvatarPaletteIndex("opensubtitles")).toBe(
      getAvatarPaletteIndex("opensubtitles"),
    );
  });

  it("returns an index inside the palette bounds", () => {
    for (const key of [
      "embedded_subtitles",
      "opensubtitles",
      "opensubtitlescom",
      "subdl",
      "subtitulamos",
      "tvsubtitles",
      "gestdown",
      "supersubtitles",
      "wizdom",
      "yifysubtitles",
    ]) {
      const index = getAvatarPaletteIndex(key);
      expect(index).toBeGreaterThanOrEqual(0);
      expect(index).toBeLessThan(AVATAR_PALETTE_SIZE);
    }
  });

  it("uses the full palette across realistic provider keys", () => {
    const seen = new Set<number>();
    const keys = [
      "embedded_subtitles",
      "opensubtitles",
      "opensubtitlescom",
      "subdl",
      "subtitulamos",
      "tvsubtitles",
      "gestdown",
      "supersubtitles",
      "wizdom",
      "yifysubtitles",
      "animetosho",
      "animekalesi",
      "napiprojekt",
      "betaseries",
      "addic7ed",
      "podnapisi",
    ];
    for (const key of keys) {
      seen.add(getAvatarPaletteIndex(key));
    }
    expect(seen.size).toBeGreaterThanOrEqual(AVATAR_PALETTE_SIZE / 2);
  });
});
