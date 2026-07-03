import type { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import {
  faFileZipper,
  faLayerGroup,
  faServer,
  faShieldHalved,
  faSliders,
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
export const latestWhatsNewVersion = "2.5.1";

export const whatsNew: Record<string, WhatsNewSlide[]> = {
  "2.5.1": [
    {
      title: "Cleaner multi-server first-run",
      body: "A fresh setup no longer spams connection-refused errors at the default 8989/7878 ports while your real Sonarr/Radarr work. Existing setups self-heal automatically on this upgrade, no action needed.",
      icon: faServer,
      cta: { label: "Open Connections", to: "/settings/connections" },
    },
    {
      title: "Reverse-proxy subpath fixed",
      body: "Running Bazarr+ behind a reverse proxy on a subpath (base_url, e.g. /bazarr) no longer shows a blank page. Static assets now load correctly under the configured prefix.",
      icon: faTowerBroadcast,
    },
    {
      title: "Dependency & security maintenance",
      body: "A round of dependency and security updates across the stack (dynaconf, pillow, apscheduler, numpy, alembic, cloudscraper and more), with no known vulnerabilities outstanding.",
      icon: faShieldHalved,
    },
  ],
  "2.5.0": [
    {
      title: "Multiple Sonarr/Radarr instances",
      body: "Connect any number of Sonarr and Radarr servers to one Bazarr+. Every search, download, sync and webhook stays scoped to the server that owns each show or movie.",
      icon: faServer,
      cta: { label: "Open Connections", to: "/settings/connections" },
    },
    {
      title: "Per-instance subtitle settings",
      body: "Override subzero mods, post-processing, audio sync and keep-lyrics per instance. Bazarr+ resolves the right settings against the media's owning server.",
      icon: faSliders,
      cta: { label: "Open Connections", to: "/settings/connections" },
    },
    {
      title: "Archive uploads & drag-and-drop",
      body: "Drop a .zip, .rar or .7z of subtitles straight into the upload modal, or drag files anywhere onto a show or movie page.",
      icon: faFileZipper,
    },
    {
      title: "Guided first-run setup",
      body: "Fresh installs get a step-by-step wizard: connect Sonarr and Radarr, add Plex or Jellyfin, pick languages, and install subtitle providers, with the provider restart handled and resumed for you. It is skippable and never appears once you are set up.",
      icon: faWandMagicSparkles,
    },
    {
      title: "Hardened and polished",
      body: "SSRF and path-traversal fixes (local/LAN use is unaffected) plus 30+ frontend bug fixes, including a Subtitle Editor crash on plain-HTTP setups.",
      icon: faShieldHalved,
    },
  ],
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
