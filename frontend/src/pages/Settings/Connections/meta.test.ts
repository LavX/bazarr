import { afterEach, describe, expect, it, vi } from "vitest";
import { buildArrInstanceCreateBody, buildWebhookUrl } from "./meta";

// --------------------------------------------------------------------------
// F3 - buildArrInstanceCreateBody: is_default omission on create
// --------------------------------------------------------------------------

const BASE_FIELDS = {
  name: "Main Sonarr",
  ip: "192.168.1.10",
  port: 8989,
  base_url: "",
  ssl: false,
  verify_ssl: true,
  http_timeout: 60,
  enabled: true,
} as const;

describe("buildArrInstanceCreateBody", () => {
  it("omits is_default when the switch is off (backend auto-promotes)", () => {
    const body = buildArrInstanceCreateBody("sonarr", {
      ...BASE_FIELDS,
      isDefault: false,
    });
    expect(Object.prototype.hasOwnProperty.call(body, "is_default")).toBe(
      false,
    );
  });

  it("includes is_default: true when the switch is on", () => {
    const body = buildArrInstanceCreateBody("sonarr", {
      ...BASE_FIELDS,
      isDefault: true,
    });
    expect(body.is_default).toBe(true);
  });

  it("includes the kind in the body", () => {
    const body = buildArrInstanceCreateBody("radarr", {
      ...BASE_FIELDS,
      isDefault: false,
    });
    expect(body.kind).toBe("radarr");
  });

  it("omits api_key when no key is provided", () => {
    const body = buildArrInstanceCreateBody("sonarr", {
      ...BASE_FIELDS,
      isDefault: false,
    });
    expect(Object.prototype.hasOwnProperty.call(body, "api_key")).toBe(false);
  });

  it("includes api_key when one is provided", () => {
    const body = buildArrInstanceCreateBody("sonarr", {
      ...BASE_FIELDS,
      isDefault: false,
      apiKey: "abc123",
    });
    expect(body.api_key).toBe("abc123");
  });
});

// --------------------------------------------------------------------------
// F6 - buildWebhookUrl: base URL is included in the webhook URL
// --------------------------------------------------------------------------

describe("buildWebhookUrl", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const INSTANCE = { kind: "sonarr" as const, stable_key: "abc-123" };

  it("includes the base URL when Bazarr is mounted under a subpath", () => {
    const url = buildWebhookUrl(INSTANCE, "http://myserver:6767", "/bazarr");
    expect(url).toBe("http://myserver:6767/bazarr/api/webhooks/sonarr/abc-123");
  });

  it("produces a correct URL with no subpath (default install)", () => {
    const url = buildWebhookUrl(INSTANCE, "http://myserver:6767", "");
    expect(url).toBe("http://myserver:6767/api/webhooks/sonarr/abc-123");
  });

  it("works for radarr instances", () => {
    const url = buildWebhookUrl(
      { kind: "radarr", stable_key: "xyz-789" },
      "https://host:443",
      "",
    );
    expect(url).toBe("https://host:443/api/webhooks/radarr/xyz-789");
  });
});
