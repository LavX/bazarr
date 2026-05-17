import { afterEach, describe, expect, it, vi } from "vitest";
import { Environment } from "./env";

describe("Environment", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("uses backend-injected values when present", () => {
    vi.stubGlobal("Bazarr", {
      apiKey: "runtime-api-key",
      baseUrl: "/bazarr/",
      canUpdate: true,
      hasUpdate: true,
    });

    expect(Environment.apiKey).toBe("runtime-api-key");
    expect(Environment.baseUrl).toBe("/bazarr");
    expect(Environment.canUpdate).toBe(true);
    expect(Environment.hasUpdate).toBe(true);
  });

  it("keeps test defaults without backend injection", () => {
    expect(Environment.apiKey).toBeUndefined();
    expect(Environment.baseUrl).toBe("");
    expect(Environment.canUpdate).toBe(false);
    expect(Environment.hasUpdate).toBe(false);
  });
});
