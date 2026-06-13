import { describe, expect, it } from "vitest";
import {
  CONNECTION_TABS,
  DEFAULT_CONNECTION_TAB,
  isConnectionTab,
  parseTabFromHash,
} from "./tabs";

describe("connection tabs", () => {
  it("lists the four services in order", () => {
    expect(CONNECTION_TABS).toEqual(["sonarr", "radarr", "plex", "jellyfin"]);
  });

  it("recognises valid tab keys", () => {
    expect(isConnectionTab("plex")).toBe(true);
    expect(isConnectionTab("nope")).toBe(false);
  });

  it("parses a leading-hash fragment to its tab", () => {
    expect(parseTabFromHash("#plex")).toBe("plex");
    expect(parseTabFromHash("#jellyfin")).toBe("jellyfin");
  });

  it("parses a bare fragment without a hash", () => {
    expect(parseTabFromHash("radarr")).toBe("radarr");
  });

  it("falls back to the default tab for empty or unknown fragments", () => {
    expect(parseTabFromHash("")).toBe(DEFAULT_CONNECTION_TAB);
    expect(parseTabFromHash("#bogus")).toBe(DEFAULT_CONNECTION_TAB);
    expect(DEFAULT_CONNECTION_TAB).toBe("sonarr");
  });
});
