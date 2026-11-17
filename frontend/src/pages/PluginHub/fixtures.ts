/* eslint-disable camelcase -- transport names. */
import type {
  PluginCatalog,
  PluginCatalogEntry,
  PluginInstallation,
  PluginJob,
} from "@/apis/raw/plugins";

const FUTURE = "2999-01-01T00:00:00Z";

export const officialSource = {
  id: "official",
  name: "Official",
  type: "github",
  url: "https://github.com/LavX/bazarr-provider-catalog",
  enabled: true,
  trusted: true,
  official: true,
  last_checked_at: new Date(Date.now() - 5 * 60_000).toISOString(),
  last_error: null,
  catalog_signature: "verified",
  signature_verified: true,
  catalog_channel: "beta",
  catalog_expires_at: FUTURE,
};

export const communitySource = {
  id: "community",
  name: "Community",
  type: "github",
  url: "https://github.com/someone/catalog",
  enabled: true,
  trusted: false,
  official: false,
  last_checked_at: null,
  last_error: null,
};

export function entry(
  id: string,
  kind: string,
  extra: Record<string, unknown> = {},
  over: Partial<PluginCatalogEntry> = {},
): PluginCatalogEntry {
  return {
    source: "official",
    source_name: "Official",
    provider_id: id,
    name: id,
    version: "1.0.0",
    trusted: true,
    manifest: {
      schema_version: 2,
      id,
      kind,
      name: id,
      version: over.version ?? "1.0.0",
      ...extra,
    },
    catalog_list: "plugins",
    ...over,
  };
}

export const aiTranslator = entry(
  "ai_translator",
  "translator",
  {
    description: "Translates subtitles with the model you pick.",
    permissions: ["network", "data_dir"],
    provides: ["ai_translator.v1"],
    endpoints: ["https://openrouter.ai:443"],
  },
  { name: "AI Subtitle Translator", version: "2.0.0" },
);

export const googleTranslate = entry(
  "google_translate",
  "translator",
  {
    description: "Translates with Google Translate.",
    permissions: ["network"],
    endpoints: ["https://translate.googleapis.com:443"],
  },
  { name: "Google Translate" },
);

export const opencodeBackend = entry(
  "opencode_backend",
  "translator_backend",
  {
    description: "Reaches a model through the opencode command line tool.",
    permissions: ["binaries"],
    extends: { id: "ai_translator", capability: "ai_translator.backend.v1" },
  },
  { name: "Opencode back end" },
);

export const flaresolverr = entry(
  "flaresolverr",
  "service",
  {
    description: "Gets providers past browser checks.",
    permissions: ["docker", "network"],
    provides: ["flaresolverr.v1"],
    runtime: { artifact: "oci", process: "daemon" },
  },
  { name: "FlareSolverr" },
);

/** A subtitle provider: the Subtitle Hub's, never listed on this page. */
export const subdl: PluginCatalogEntry = {
  source: "official",
  source_name: "Official",
  provider_id: "subdl",
  name: "SubDL",
  version: "1.0.0",
  trusted: true,
  manifest: {
    schema_version: 1,
    provider_id: "subdl",
    name: "SubDL",
    version: "1.0.0",
  },
};

/** Another source listing the official plugin's id, with a name of its own choosing in the manifest. */
export const lookalike = entry(
  "ai_translator",
  "translator",
  {
    description: "Not the one Bazarr+ ships.",
    source: { name: "Official" },
  },
  {
    source: "community",
    source_name: "Community",
    name: "AI Subtitle Translator",
    trusted: false,
    blocked_reason:
      "New kinds of plugin install only from the official catalog.",
  },
);

export function catalog(
  entries: PluginCatalogEntry[],
  sources: PluginCatalog["sources"] = [officialSource, communitySource],
): PluginCatalog {
  return { sources, entries };
}

export function installation(
  id: string,
  kind: string,
  extra: Record<string, unknown> = {},
  over: Partial<PluginInstallation> = {},
): PluginInstallation {
  return {
    provider_id: id,
    name: id,
    kind,
    state: "active",
    active_version: "1.0.0",
    enabled: true,
    trusted: true,
    origin: "catalog",
    source_id: "official",
    source_name: "Official",
    source_bound: true,
    manifest: { schema_version: 2, id, kind, name: id, ...extra },
    ...over,
  };
}

export const installedTranslator = installation(
  "ai_translator",
  "translator",
  { permissions: ["network", "data_dir"], provides: ["ai_translator.v1"] },
  { name: "AI Subtitle Translator" },
);

export const installedBackend = installation(
  "opencode_backend",
  "translator_backend",
  {
    permissions: ["binaries"],
    extends: { id: "ai_translator", capability: "ai_translator.backend.v1" },
  },
  { name: "Opencode back end", active_version: "1.0.0" },
);

export const jobs: PluginJob[] = [
  {
    id: "1",
    action: "install",
    state: "completed",
    target_kind: "provider",
    target_id: "ai_translator",
    target_name: "AI Subtitle Translator",
    message: "Installed AI Subtitle Translator",
    created_at: "2026-11-16T10:00:00Z",
  },
  {
    id: "2",
    action: "install",
    state: "completed",
    target_kind: "provider",
    target_id: "subdl",
    target_name: "SubDL",
    message: "Installed SubDL",
    created_at: "2026-11-16T11:00:00Z",
  },
  {
    id: "3",
    action: "refresh_catalog",
    state: "completed",
    target_kind: "system",
    message: "Catalog refreshed",
    created_at: "2026-11-16T12:00:00Z",
  },
];
