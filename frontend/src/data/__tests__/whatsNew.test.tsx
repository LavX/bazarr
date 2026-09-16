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

// Atlas carries changes a reader has to be told about in the slide itself,
// because the slide is the only place they are told at all.
describe("v2.7.0 Atlas slides", () => {
  const slides = getWhatsNewSlides("2.7.0");

  const findSlide = (needle: string) =>
    slides.find((slide) => slide.title.includes(needle));

  it("announces Atlas at all", () => {
    expect(slides.length).toBeGreaterThan(0);
  });

  it("tells existing installs what SmartFast changes for them, and what it needs", () => {
    const slide = findSlide("SmartFast");
    expect(slide).toBeDefined();
    // Existing installs keep throughput routing, so the slide has to say so
    // before anyone assumes their bill moved.
    expect(slide!.body).toContain("Nothing changes on your install");
    // Where to switch, the measured reason to bother, and the version gate: a
    // switch that then breaks every translation is worse than no slide.
    expect(slide!.body).toContain("Provider Routing");
    expect(slide!.body).toContain("twice the cheapest");
    expect(slide!.body).toContain("2.0.0 or newer");
    expect(slide!.cta?.to).toBe("/settings/translator");
  });

  it("credits the reasoning fix to the contributor who sent it", () => {
    expect(slides.some((slide) => slide.body.includes("wouterrutgers"))).toBe(
      true,
    );
  });

  it("names the Seerr flavours and says approval stays in Seerr", () => {
    const slide = findSlide("Seerr");
    expect(slide).toBeDefined();
    ["Overseerr", "Jellyseerr", "Seerr"].forEach((flavour) =>
      expect(slide!.body).toContain(flavour),
    );
    expect(slide!.body).toContain("approval stays in Seerr");
  });
});
