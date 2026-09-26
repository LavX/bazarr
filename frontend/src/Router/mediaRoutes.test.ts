import { describe, expect, it } from "vitest";
import { libraryRouteForKind } from "@/Router/mediaRoutes";

describe("library routes", () => {
  it("uses an explicit route for every supported kind", () => {
    expect(libraryRouteForKind("sonarr")).toBe("/series");
    expect(libraryRouteForKind("radarr")).toBe("/movies");
    expect(libraryRouteForKind("sportarr")).toBe("/sports");
    expect(libraryRouteForKind("unknown")).toBeUndefined();
  });
});
