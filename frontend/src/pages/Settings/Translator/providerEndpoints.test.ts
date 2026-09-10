import { describe, expect, it } from "vitest";
import { endpointMatchesSlug } from "./providerEndpoints";

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
