/* eslint-disable camelcase */
import { describe, expect, it } from "vitest";
import type {
  ProviderHubCatalog,
  ProviderHubInstallation,
} from "@/apis/raw/providerHub";
import {
  formatAbsoluteTime,
  formatDuration,
  formatRelativeTime,
  getActionLabel,
  getJobStateMeta,
  getLatestCatalogEntry,
  getProviderStateMeta,
  isUpdateAvailable,
  parseGitHubUrl,
  parseManifest,
  requiresAntiCaptcha,
  requiresFlaresolverr,
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

  it("orders prereleases below final releases", () => {
    const prereleaseCatalog: ProviderHubCatalog = {
      sources: [],
      entries: [
        { provider_id: "a", version: "1.0.0-rc.1", trusted: true },
        { provider_id: "a", version: "1.0.0", trusted: true },
      ],
    };

    expect(getLatestCatalogEntry(prereleaseCatalog, "a")?.version).toBe(
      "1.0.0",
    );
  });

  it("orders alphanumeric prerelease core suffixes below final releases", () => {
    const prereleaseCatalog: ProviderHubCatalog = {
      sources: [],
      entries: [
        { provider_id: "a", version: "1.0.0rc1", trusted: true },
        { provider_id: "a", version: "1.0.0", trusted: true },
      ],
    };

    expect(getLatestCatalogEntry(prereleaseCatalog, "a")?.version).toBe(
      "1.0.0",
    );
    expect(
      isUpdateAvailable(
        { provider_id: "a", state: "active", active_version: "1.0.0rc1" },
        prereleaseCatalog,
      ),
    ).toBe(true);
  });

  it("sorts alphanumeric prerelease suffix numbers", () => {
    const prereleaseCatalog: ProviderHubCatalog = {
      sources: [],
      entries: [
        { provider_id: "a", version: "1.0.0rc1", trusted: true },
        { provider_id: "a", version: "1.0.0rc2", trusted: true },
      ],
    };

    expect(getLatestCatalogEntry(prereleaseCatalog, "a")?.version).toBe(
      "1.0.0rc2",
    );
  });

  it("ignores build metadata for update comparisons", () => {
    const buildCatalog: ProviderHubCatalog = {
      sources: [],
      entries: [{ provider_id: "a", version: "1.2.3+foo", trusted: true }],
    };

    expect(
      isUpdateAvailable(
        { provider_id: "a", state: "active", active_version: "1.2.3" },
        buildCatalog,
      ),
    ).toBe(false);
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

describe("formatDuration", () => {
  it("returns empty for null/undefined", () => {
    expect(formatDuration(null)).toBe("");
    expect(formatDuration(undefined)).toBe("");
  });

  it("formats sub-millisecond and millisecond values", () => {
    expect(formatDuration(0)).toBe("<1ms");
    expect(formatDuration(120)).toBe("120ms");
  });

  it("formats seconds with one decimal under 10s and integer above", () => {
    expect(formatDuration(1500)).toBe("1.5s");
    expect(formatDuration(45000)).toBe("45s");
  });

  it("formats minutes with optional remainder seconds", () => {
    expect(formatDuration(60000)).toBe("1m");
    expect(formatDuration(90000)).toBe("1m 30s");
  });
});

describe("getActionLabel", () => {
  it("returns friendly labels for known actions", () => {
    expect(getActionLabel("install")).toBe("Install plugin");
    expect(getActionLabel("stage_update")).toBe("Stage update");
    expect(getActionLabel("refresh_catalog")).toBe("Refresh catalog");
    expect(getActionLabel("check_updates")).toBe("Check updates");
  });

  it("title-cases unknown snake_case actions", () => {
    expect(getActionLabel("frobnicate_thing")).toBe("Frobnicate Thing");
  });

  it("falls back to 'Unknown action' for empty input", () => {
    expect(getActionLabel(undefined)).toBe("Unknown action");
    expect(getActionLabel(null)).toBe("Unknown action");
    expect(getActionLabel("")).toBe("Unknown action");
  });
});

describe("requiresAntiCaptcha", () => {
  it("returns false for missing/empty manifests", () => {
    expect(requiresAntiCaptcha(null)).toBe(false);
    expect(requiresAntiCaptcha(undefined)).toBe(false);
    expect(requiresAntiCaptcha({})).toBe(false);
    expect(requiresAntiCaptcha({ manifest: { provider_id: "x" } })).toBe(false);
  });

  it("detects a captcha-solver config field", () => {
    expect(
      requiresAntiCaptcha({
        manifest: {
          config_schema: {
            properties: { captcha_solver_url: { title: "Captcha solver URL" } },
          },
        },
      }),
    ).toBe(true);
  });

  it("does not treat a FlareSolverr-only provider as anti-captcha", () => {
    expect(
      requiresAntiCaptcha({
        manifest: {
          config_schema: {
            properties: { flaresolverr_url: { title: "FlareSolverr URL" } },
          },
        },
      }),
    ).toBe(false);
  });

  it("ignores captcha mentions in description text without a config field", () => {
    expect(
      requiresAntiCaptcha({
        manifest: {
          description: "Use cookies when login is blocked by captcha.",
          config_schema: { properties: { cookies: { secret: true } } },
        },
      }),
    ).toBe(false);
  });

  it("honours an explicit requires_anti_captcha flag over field detection", () => {
    expect(
      requiresAntiCaptcha({
        manifest: {
          requires_anti_captcha: false,
          config_schema: { properties: { captcha_solver_url: {} } },
        },
      }),
    ).toBe(false);
    expect(
      requiresAntiCaptcha({ manifest: { requires_anti_captcha: true } }),
    ).toBe(true);
  });
});

describe("requiresFlaresolverr", () => {
  it("returns false for missing/empty manifests", () => {
    expect(requiresFlaresolverr(null)).toBe(false);
    expect(requiresFlaresolverr(undefined)).toBe(false);
    expect(requiresFlaresolverr({})).toBe(false);
  });

  it("detects a flaresolverr config field", () => {
    expect(
      requiresFlaresolverr({
        manifest: {
          config_schema: {
            properties: { flaresolverr_url: { title: "FlareSolverr URL" } },
          },
        },
      }),
    ).toBe(true);
  });

  it("does not treat a captcha-solver-only provider as FlareSolverr", () => {
    expect(
      requiresFlaresolverr({
        manifest: {
          config_schema: { properties: { captcha_solver_url: {} } },
        },
      }),
    ).toBe(false);
  });

  it("honours an explicit requires_flaresolverr flag over field detection", () => {
    expect(
      requiresFlaresolverr({
        manifest: {
          requires_flaresolverr: false,
          config_schema: { properties: { flaresolverr_url: {} } },
        },
      }),
    ).toBe(false);
    expect(
      requiresFlaresolverr({ manifest: { requires_flaresolverr: true } }),
    ).toBe(true);
  });
});
