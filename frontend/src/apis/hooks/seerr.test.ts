import { describe, expect, it } from "vitest";
import { seerrMediaKey } from "./seerr";

describe("seerr query keys", () => {
  it("keys movie status by tmdb id", () => {
    expect(seerrMediaKey({ kind: "movie", tmdbId: 550 })).toEqual([
      "seerr",
      "media",
      "movie",
      "tmdb",
      550,
    ]);
  });
  it("keys tvdb-only shows separately from tmdb ones", () => {
    expect(seerrMediaKey({ kind: "tv", tvdbId: 121361 })).toEqual([
      "seerr",
      "media",
      "tv",
      "tvdb",
      121361,
    ]);
  });
});
