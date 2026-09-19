import { describe, expect, it } from "vitest";
import { groupRoutes } from "@/App/Navbar";
import type { CustomRouteObject } from "@/Router/type";

// Sports is a media type, not a leftover. It was landing in the catch-all
// "Other" group at the bottom of the sidebar because its path was missing from
// the Media section, which is a different place from where Series and Movies
// appear and reads as a second-class feature.
function mediaRoutes(hidden: Partial<Record<string, boolean>> = {}) {
  return [
    { name: "Series", path: "series", hidden: hidden.series },
    { name: "Movies", path: "movies", hidden: hidden.movies },
    { name: "Sports", path: "sports", hidden: hidden.sports },
    { name: "History", path: "history" },
    { name: "Settings", path: "settings" },
  ] as CustomRouteObject[];
}

describe("navbar grouping", () => {
  it("lists Sports in Media beside Series and Movies", () => {
    const groups = groupRoutes(mediaRoutes());
    const media = groups.find((g) => g.label === "Media");

    expect(media?.items.map((i) => i.path)).toEqual([
      "series",
      "movies",
      "sports",
    ]);
  });

  it("never drops Sports into the catch-all Other group", () => {
    const groups = groupRoutes(mediaRoutes());
    expect(groups.find((g) => g.label === "Other")).toBeUndefined();
  });

  it("keeps Sports in Media even when it is the only visible media type", () => {
    // The case that surfaced this: an install with Sonarr and Radarr off, so
    // Series and Movies are hidden and Sports is the only media route left.
    const groups = groupRoutes(mediaRoutes({ series: true, movies: true }));

    expect(groups.find((g) => g.label === "Media")?.items).toHaveLength(1);
    expect(groups.find((g) => g.label === "Media")?.items[0].path).toBe(
      "sports",
    );
    expect(groups.find((g) => g.label === "Other")).toBeUndefined();
  });
});
