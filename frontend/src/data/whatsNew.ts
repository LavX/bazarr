import type { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import {
  faLayerGroup,
  faStore,
  faTowerBroadcast,
  faWandMagicSparkles,
} from "@fortawesome/free-solid-svg-icons";

export interface WhatsNewSlide {
  /** Short headline for the change. */
  title: string;
  /** One to three lines describing it. */
  body: string;
  /** Optional imported asset URL; takes priority over `icon`. */
  image?: string;
  /** Optional FontAwesome icon shown when there is no image. */
  icon?: IconDefinition;
  /** Optional deep-link to the relevant page ("Take me there"). */
  cta?: { label: string; to: string };
}

/**
 * The release being announced. The maintainer bumps this (and adds an entry below) when
 * cutting a release. Kept as an explicit token so the wizard never has to parse the
 * fork's `version + YYMMDD` runtime string.
 */
export const latestWhatsNewVersion = "2.4.0";

export const whatsNew: Record<string, WhatsNewSlide[]> = {
  "2.4.0": [
    {
      title: "Distribution Hub",
      body: "Serve subtitles through a multi-tenant API with named keys, tiers, and per-key usage metering.",
      icon: faTowerBroadcast,
      cta: { label: "Open Distribution Hub", to: "/distribution-hub" },
    },
    {
      title: "Provider Hub auto-install",
      body: "Opt in to automatically replace built-in providers with their Provider Hub catalog versions at startup. Off by default; manual install from the Marketplace always works.",
      icon: faStore,
      cta: { label: "Open General settings", to: "/settings/general" },
    },
    {
      title: "Combined subtitles",
      body: "Merge subtitles from multiple languages into a single track for side-by-side viewing.",
      icon: faLayerGroup,
      cta: { label: "Open Subtitles settings", to: "/settings/subtitles" },
    },
    {
      title: "Smarter subtitle matching",
      body: "When a release name can't be parsed, Bazarr now falls back to the on-disk filename instead of giving up, so more searches succeed.",
      icon: faWandMagicSparkles,
    },
  ],
};

export function getWhatsNewSlides(version: string): WhatsNewSlide[] {
  return whatsNew[version] ?? [];
}
