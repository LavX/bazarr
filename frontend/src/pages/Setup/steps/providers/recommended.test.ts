import { describe, expect, it } from "vitest";
import { isRecommendedProvider } from "./recommended";

/**
 * Manifests below are copied from the real catalog (LavX/bazarr-provider-catalog,
 * catalog.json on main) trimmed to the fields the rule reads, so a change in
 * what the rule looks at shows up here as a failure rather than as a silently
 * different set.
 */
function candidate(manifest: LooseObject) {
  return { providerId: manifest.provider_id as string, manifest };
}

describe("isRecommendedProvider", () => {
  it("accepts a provider that asks for nothing", () => {
    // Gestdown: public API, one optional delay field.
    expect(
      isRecommendedProvider(
        candidate({
          provider_id: "gestdown",
          config_schema: {
            type: "object",
            properties: {
              locked_retry_delay_ms: { type: "integer", default: 30000 },
              request_timeout_seconds: { type: "integer", default: 30 },
            },
          },
        }),
      ),
    ).toBe(true);
  });

  it("accepts a provider whose paths have defaults", () => {
    // Embedded Subtitles: ffprobe and ffmpeg paths ship with defaults, so a
    // fresh install can extract embedded tracks without answering anything.
    expect(
      isRecommendedProvider(
        candidate({
          provider_id: "embeddedsubtitles",
          config_schema: {
            type: "object",
            properties: {
              ffmpeg_path: { type: "string", default: "ffmpeg" },
              ffprobe_path: { type: "string", default: "ffprobe" },
            },
          },
        }),
      ),
    ).toBe(true);
  });

  it("rejects a provider that needs an account", () => {
    // Titlovi: username and password, both required and neither marked secret.
    expect(
      isRecommendedProvider(
        candidate({
          provider_id: "titlovi",
          config_schema: {
            type: "object",
            required: ["username", "password"],
            properties: {
              username: { type: "string" },
              password: { type: "string" },
            },
          },
        }),
      ),
    ).toBe(false);
  });

  it("rejects a provider that needs an API key named like one", () => {
    // SubDL: api_key, secret and required.
    expect(
      isRecommendedProvider(
        candidate({
          provider_id: "subdl",
          secret_fields: ["api_key"],
          config_schema: {
            type: "object",
            required: ["api_key"],
            properties: { api_key: { type: "string", secret: true } },
          },
        }),
      ),
    ).toBe(false);
  });

  it("rejects a key that is required but not named like one", () => {
    // BetaSeries calls its API key "token" and does not mark it secret, so the
    // auth read alone says "No signup". The required key is what catches it.
    expect(
      isRecommendedProvider(
        candidate({
          provider_id: "betaseries",
          config_schema: {
            type: "object",
            required: ["token"],
            properties: { token: { type: "string" } },
          },
        }),
      ),
    ).toBe(false);
  });

  it("rejects a provider that needs a service to run", () => {
    // Subsarr is an API the reader hosts themselves.
    expect(
      isRecommendedProvider(
        candidate({
          provider_id: "subsarr",
          config_schema: {
            type: "object",
            required: ["base_url"],
            properties: { base_url: { type: "string" } },
          },
        }),
      ),
    ).toBe(false);
    // WhisperAI is a web service the reader supplies.
    expect(
      isRecommendedProvider(
        candidate({
          provider_id: "whisperai",
          config_schema: {
            type: "object",
            required: ["endpoint", "ffmpeg_path"],
            properties: {
              endpoint: { type: "string" },
              ffmpeg_path: { type: "string", default: "ffmpeg" },
            },
          },
        }),
      ),
    ).toBe(false);
  });

  it("rejects a provider that reaches out to a helper service", () => {
    // OpenSubtitles.org: no account, nothing required, and a FlareSolverr
    // fallback the manifest cannot tell apart from a requirement.
    expect(
      isRecommendedProvider(
        candidate({
          provider_id: "opensubtitles",
          flaresolverr: true,
          config_schema: {
            type: "object",
            properties: {
              flaresolverr_url: { type: "string", default: "" },
              request_delay_ms: { type: "integer", default: 1000 },
            },
          },
        }),
      ),
    ).toBe(false);
    // Zimuku: an anti-captcha solver, declared the same way.
    expect(
      isRecommendedProvider(
        candidate({
          provider_id: "zimuku",
          anti_captcha: true,
          config_schema: {
            type: "object",
            properties: {
              captcha_solver_url: { type: "string", default: "" },
            },
          },
        }),
      ),
    ).toBe(false);
  });

  it("reads the helper capability off a config field when there is no flag", () => {
    expect(
      isRecommendedProvider(
        candidate({
          provider_id: "some_scraper",
          config_schema: {
            type: "object",
            properties: { flaresolverr_url: { type: "string" } },
          },
        }),
      ),
    ).toBe(false);
  });

  it("rejects a manifest it cannot read", () => {
    expect(isRecommendedProvider(null)).toBe(false);
    expect(
      isRecommendedProvider({ providerId: "x", manifest: undefined as never }),
    ).toBe(false);
  });
});
