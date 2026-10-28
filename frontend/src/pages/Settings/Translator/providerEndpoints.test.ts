import { describe, expect, it } from "vitest";
import {
  endpointMatchesSlug,
  parseProviderEndpoints,
} from "./providerEndpoints";

describe("endpointMatchesSlug", () => {
  // OpenRouter accepts a bare provider slug where the endpoint tag is qualified, and
  // AI Subtitle Translator resolves the two namespaces the same way. Comparing tags
  // alone told users a provider that works does not serve their model, in the one mode
  // that excludes everything outside the list.
  it.each([
    ["deepinfra/fp8", "deepinfra", true],
    ["parasail/fp8", "parasail", true],
    ["novita", "novita", true],
    ["DeepInfra/FP8", "deepinfra", true],
    ["deepinfra/fp8", "deepinfra/fp8", true],
  ])("matches %s against %s", (tag, slug, expected) => {
    expect(endpointMatchesSlug(tag, slug)).toBe(expected);
  });

  it.each([
    // A qualified slug must not be satisfied by a different variant.
    ["deepinfra/fp4", "deepinfra/fp8"],
    // A prefix that is not a whole path segment is not a match.
    ["deepinfra-turbo/fp8", "deepinfra"],
    ["novita", "nov"],
    ["deepinfra/fp8", "fp8"],
  ])("does not match %s against %s", (tag, slug) => {
    expect(endpointMatchesSlug(tag, slug)).toBe(false);
  });
});

describe("parseProviderEndpoints availability", () => {
  const payload = (endpoint: Record<string, unknown>) => ({
    data: { endpoints: [{ tag: "deepinfra/fp8", ...endpoint }] },
  });

  // OpenRouter reports 0 for a healthy endpoint and a non-zero code for a degraded or
  // down one. A payload with no status at all says nothing about health, so it must not
  // read as "unavailable": that would disable every option and every row at once while
  // nothing has actually failed.
  it.each([
    ["is 0", { status: 0 }, true],
    ["is a negative degraded code", { status: -2 }, false],
    ["is a positive code", { status: 1 }, false],
    ["is missing", {}, true],
    ["is null", { status: null }, true],
    ["is not a number", { status: "ok" }, true],
  ])("when status %s", (_label, extra, available) => {
    const [endpoint] = parseProviderEndpoints(payload(extra));
    expect(endpoint.available).toBe(available);
  });
});
