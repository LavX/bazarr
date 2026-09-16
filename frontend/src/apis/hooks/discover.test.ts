/* eslint-disable camelcase -- API fixture fields keep their transport names. */
import { describe, expect, it } from "vitest";
import { isDiscoverSummary } from "./discover";

const component = {
  availability: "available",
  observed_at: "2026-09-01T12:00:00Z",
  complete: true,
};

describe("isDiscoverSummary", () => {
  it("accepts a body carrying the components the page reads", () => {
    expect(
      isDiscoverSummary({
        generated_at: "2026-09-01T12:00:00Z",
        state: "new_installation",
        activity: component,
        attention: component,
        wanted: component,
        arrivals: [],
      }),
    ).toBe(true);
  });

  it("rejects the authentication message a 401 answers with", () => {
    expect(
      isDiscoverSummary({
        message:
          "The server could not verify that you are authorized to access the URL requested.",
      }),
    ).toBe(false);
  });

  it("rejects a body missing any single component", () => {
    const full = {
      activity: component,
      attention: component,
      wanted: component,
      arrivals: [],
    };
    for (const key of Object.keys(full)) {
      const partial: Record<string, unknown> = { ...full };
      delete partial[key];
      expect(isDiscoverSummary(partial)).toBe(false);
    }
  });

  it("rejects the page the server sends when the API route was not matched", () => {
    expect(isDiscoverSummary("<!doctype html><html></html>")).toBe(false);
    expect(isDiscoverSummary(null)).toBe(false);
    expect(isDiscoverSummary(undefined)).toBe(false);
  });
});
