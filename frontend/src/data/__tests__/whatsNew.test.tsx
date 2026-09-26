import { matchRoutes } from "react-router";
import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  getWhatsNewSlides,
  latestWhatsNewVersion,
  whatsNew,
} from "@/data/whatsNew";
import { AllProviders } from "@/providers";
import { useRoutes } from "@/Router";

// The modal auto-opens once per user after an upgrade and is the only release
// note many people read, so a release with no entry for its own version ships a
// silent upgrade, and a CTA naming a route that does not exist drops the reader
// on the 404 page.
describe("what's new content", () => {
  const slides = getWhatsNewSlides(latestWhatsNewVersion);

  it("has slides for the version being announced", () => {
    expect(Object.keys(whatsNew)).toContain(latestWhatsNewVersion);
    expect(slides.length).toBeGreaterThan(0);
  });

  it("gives every slide a title, a body and something to show", () => {
    slides.forEach((slide) => {
      expect(slide.title.trim().length).toBeGreaterThan(0);
      expect(slide.body.trim().length).toBeGreaterThan(0);
      expect(slide.icon ?? slide.image).toBeDefined();
    });
  });

  it("points every CTA at a route the app actually has", () => {
    const { result } = renderHook(() => useRoutes(), { wrapper: AllProviders });
    const targets = slides
      .map((slide) => slide.cta?.to)
      .filter((to): to is string => typeof to === "string");

    expect(targets.length).toBeGreaterThan(0);
    targets.forEach((to) => {
      expect(
        matchRoutes(result.current, to),
        `${to} matches no route`,
      ).not.toBeNull();
    });
  });
});

describe("v2.7.0 Atlas slides", () => {
  const slides = getWhatsNewSlides("2.7.0");

  it("keeps the tour short enough to read", () => {
    expect(slides.length).toBeGreaterThan(0);
    expect(slides.length).toBeLessThanOrEqual(6);
    slides.forEach((slide) => {
      expect(slide.body.trim().split(/\s+/).length).toBeLessThanOrEqual(45);
    });
  });

  it("keeps SmartFast opt-in and explains the translator version it needs", () => {
    const slide = slides.find((slide) => slide.title.includes("SmartFast"));
    expect(slide).toBeDefined();
    expect(slide!.body).toContain("Your current routing stays unchanged");
    expect(slide!.body).toContain("Provider Routing");
    expect(slide!.body).toContain("2.0.0 or newer");
    expect(slide!.cta?.to).toBe("/settings/translator");
  });
});
