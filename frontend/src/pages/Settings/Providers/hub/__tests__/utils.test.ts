/* eslint-disable camelcase */
import { describe, expect, it } from "vitest";
import type {
  ProviderHubCatalog,
  ProviderHubInstallation,
} from "@/apis/raw/providerHub";
import {
  formatAbsoluteTime,
  formatRelativeTime,
  getJobStateMeta,
  getLatestCatalogEntry,
  getProviderStateMeta,
  isUpdateAvailable,
  parseGitHubUrl,
  parseManifest,
  summarizeUpdates,
} from "@/pages/Settings/Providers/hub/utils";

describe("parseManifest", () => {
  it("returns null for null/undefined/empty inputs", () => {
    expect(parseManifest(null)).toBeNull();
    expect(parseManifest(undefined)).toBeNull();
    expect(parseManifest({})).toBeNull();
  });

  it("returns object when manifest is already an object", () => {
    const m = { provider_id: "x", name: "X" };
    expect(parseManifest({ manifest: m })).toEqual(m);
  });

  it("parses manifest from JSON string", () => {
    const m = { provider_id: "x" };
    expect(parseManifest({ manifest: JSON.stringify(m) })).toEqual(m);
  });

  it("falls back to manifest_json field", () => {
    const m = { provider_id: "y" };
    expect(parseManifest({ manifest_json: m })).toEqual(m);
  });

  it("returns null for invalid JSON string", () => {
    expect(parseManifest({ manifest: "{not json" })).toBeNull();
  });
});

describe("getProviderStateMeta", () => {
  it("returns success tone for active", () => {
    expect(getProviderStateMeta("active").tone).toBe("success");
  });

  it("returns warning tone for staged", () => {
    expect(getProviderStateMeta("staged").tone).toBe("warning");
  });

  it("returns danger tone for failed", () => {
    expect(getProviderStateMeta("failed").tone).toBe("danger");
  });

  it("falls back to info tone for unknown states", () => {
    expect(getProviderStateMeta("weird-state").tone).toBe("info");
    expect(getProviderStateMeta("weird-state").label).toBe("weird-state");
  });

  it("falls back to Unknown for undefined", () => {
    expect(getProviderStateMeta(undefined).label).toBe("Unknown");
  });
});

describe("getJobStateMeta", () => {
  it("maps known job states", () => {
    expect(getJobStateMeta("completed").tone).toBe("success");
    expect(getJobStateMeta("pending").tone).toBe("info");
    expect(getJobStateMeta("running").tone).toBe("warning");
    expect(getJobStateMeta("failed").tone).toBe("danger");
  });

  it("falls back for unknown", () => {
    expect(getJobStateMeta(undefined).label).toBe("Unknown");
  });
});

describe("getLatestCatalogEntry", () => {
  const catalog: ProviderHubCatalog = {
    sources: [],
    entries: [
      { provider_id: "a", version: "1.2.0", trusted: true },
      { provider_id: "a", version: "1.10.0", trusted: true },
      { provider_id: "a", version: "1.2.3", trusted: true },
      { provider_id: "b", version: "0.1.0", trusted: false },
    ],
  };

  it("returns highest semver for provider_id", () => {
    expect(getLatestCatalogEntry(catalog, "a")?.version).toBe("1.10.0");
  });

  it("returns null when provider not in catalog", () => {
    expect(getLatestCatalogEntry(catalog, "missing")).toBeNull();
  });

  it("handles empty/null catalog", () => {
    expect(getLatestCatalogEntry(undefined, "a")).toBeNull();
    expect(getLatestCatalogEntry({ sources: [], entries: [] }, "a")).toBeNull();
  });
});

describe("isUpdateAvailable / summarizeUpdates", () => {
  const catalog: ProviderHubCatalog = {
    sources: [],
    entries: [
      { provider_id: "a", version: "2.0.0", trusted: true },
      { provider_id: "b", version: "1.0.0", trusted: true },
    ],
  };

  const providers: ProviderHubInstallation[] = [
    { provider_id: "a", state: "active", active_version: "1.0.0" },
    { provider_id: "b", state: "active", active_version: "1.0.0" },
    {
      provider_id: "c",
      state: "staged",
      active_version: "1.0.0",
      pending_restart: true,
    },
  ];

  it("flags providers with a newer version in catalog", () => {
    expect(isUpdateAvailable(providers[0], catalog)).toBe(true);
    expect(isUpdateAvailable(providers[1], catalog)).toBe(false);
  });

  it("does not flag providers awaiting restart", () => {
    expect(isUpdateAvailable(providers[2], catalog)).toBe(false);
  });

  it("does not flag providers with no active_version", () => {
    expect(
      isUpdateAvailable({ provider_id: "x", state: "inactive" }, catalog),
    ).toBe(false);
  });

  it("summarizes both available and pending-restart counts", () => {
    const s = summarizeUpdates(providers, catalog);
    expect(s.available.map((p) => p.provider_id)).toEqual(["a"]);
    expect(s.pendingRestart.map((p) => p.provider_id)).toEqual(["c"]);
  });
});

describe("parseGitHubUrl", () => {
  it("parses standard github.com URLs", () => {
    const r = parseGitHubUrl(
      "https://github.com/owner/repo/blob/main/catalog.json",
    );
    expect(r).toEqual({
      owner: "owner",
      repo: "repo",
      suggestedName: "owner/repo",
    });
  });

  it("parses raw.githubusercontent.com URLs", () => {
    const r = parseGitHubUrl(
      "https://raw.githubusercontent.com/o/r/main/catalog.json",
    );
    expect(r?.owner).toBe("o");
    expect(r?.repo).toBe("r");
  });

  it("strips .git suffix", () => {
    expect(parseGitHubUrl("https://github.com/foo/bar.git")?.repo).toBe("bar");
  });

  it("returns null for non-github hosts", () => {
    expect(parseGitHubUrl("https://gitlab.com/o/r")).toBeNull();
  });

  it("returns null for invalid URLs", () => {
    expect(parseGitHubUrl("not a url")).toBeNull();
    expect(parseGitHubUrl("")).toBeNull();
  });
});

describe("formatRelativeTime", () => {
  const now = new Date("2026-05-17T12:00:00Z");

  it("returns 'just now' for <5s", () => {
    expect(formatRelativeTime("2026-05-17T11:59:58Z", now)).toBe("just now");
  });

  it("uses seconds, minutes, hours, days, months, years buckets", () => {
    expect(formatRelativeTime("2026-05-17T11:59:30Z", now)).toMatch(/s ago$/);
    expect(formatRelativeTime("2026-05-17T11:15:00Z", now)).toBe("45m ago");
    expect(formatRelativeTime("2026-05-17T06:00:00Z", now)).toBe("6h ago");
    expect(formatRelativeTime("2026-05-10T12:00:00Z", now)).toBe("7d ago");
    expect(formatRelativeTime("2026-02-17T12:00:00Z", now)).toBe("3mo ago");
    expect(formatRelativeTime("2024-05-17T12:00:00Z", now)).toBe("2y ago");
  });

  it("returns empty string for null/invalid", () => {
    expect(formatRelativeTime(null, now)).toBe("");
    expect(formatRelativeTime("not a date", now)).toBe("");
  });
});

describe("formatAbsoluteTime", () => {
  it("formats as ISO without milliseconds, with UTC suffix", () => {
    expect(formatAbsoluteTime("2026-05-17T14:02:11Z")).toBe(
      "2026-05-17 14:02:11 UTC",
    );
  });

  it("returns empty for null/invalid", () => {
    expect(formatAbsoluteTime(null)).toBe("");
    expect(formatAbsoluteTime("nope")).toBe("");
  });
});
