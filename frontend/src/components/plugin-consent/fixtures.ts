/* eslint-disable camelcase -- transport names. */
import type {
  PluginConsentMember,
  PluginConsentSummary,
  PluginUpdateDelta,
} from "@/apis/raw/plugins";

const official = {
  id: "official",
  name: "Official catalog",
  official: true,
  trusted: true,
  development: false,
};

const community = {
  id: "community",
  name: "Some community source",
  official: false,
  trusted: false,
  development: false,
};

export const translatorMember: PluginConsentMember = {
  id: "ai_translator",
  name: "AI Subtitle Translator",
  version: "2.0.0",
  kind: "translator",
  reason: "root",
  source: official,
  permissions: [
    { name: "network", mark: "enforced" },
    { name: "data_dir", mark: "declared" },
  ],
  destinations: ["https://openrouter.ai:443"],
  binaries: [],
  download_size: 3_250_000,
  memory_request: null,
};

export const backendMember: PluginConsentMember = {
  id: "opencode_backend",
  name: "Opencode back end",
  version: "0.4.1",
  kind: "translator_backend",
  reason: "requires ai_translator.backend.v1",
  source: community,
  permissions: [
    { name: "binaries", mark: "declared" },
    { name: "docker", mark: "declared" },
  ],
  destinations: ["https://api.example.org:443"],
  binaries: [{ artifact: "oci", process: "daemon" }],
  download_size: null,
  memory_request: null,
};

/** An install of the translator with one more plugin in its closure. */
export const installSummary: PluginConsentSummary = {
  plugin: {
    id: "ai_translator",
    name: "AI Subtitle Translator",
    version: "2.0.0",
    kind: "translator",
  },
  members: [backendMember, translatorMember],
  already_installed: [{ id: "host_cache", capability: "cache.v1" }],
  external_requirements: ["flaresolverr.v1"],
  permissions: [
    { name: "binaries", mark: "declared" },
    { name: "data_dir", mark: "declared" },
    { name: "docker", mark: "declared" },
    { name: "network", mark: "enforced" },
  ],
  destinations: ["https://api.example.org:443", "https://openrouter.ai:443"],
  binaries: [{ artifact: "oci", process: "daemon" }],
  download_size: 3_250_000,
  memory_request: 1_610_612_736,
  digest: "sha256:aaaa",
};

/** An install with nothing in it that needs saying beyond the plugin itself. */
export const plainSummary: PluginConsentSummary = {
  plugin: {
    id: "google_translate",
    name: "Google Translate",
    version: "1.0.0",
    kind: "translator",
  },
  members: [
    {
      ...translatorMember,
      id: "google_translate",
      name: "Google Translate",
      version: "1.0.0",
      permissions: [],
      destinations: [],
      download_size: null,
    },
  ],
  already_installed: [],
  external_requirements: [],
  permissions: [],
  destinations: [],
  binaries: [],
  download_size: null,
  memory_request: null,
  digest: "sha256:plain",
};

export const updateDelta: PluginUpdateDelta = {
  plugin: {
    id: "ai_translator",
    name: "AI Subtitle Translator",
    from_version: "1.0.0",
    to_version: "1.1.0",
    kind: "translator",
  },
  needs_consent: true,
  added: {
    destinations: ["https://api.deepl.com:443"],
    destination_fields: [],
    permissions: [{ name: "media_read", mark: "enforced" }],
    binaries: [],
    plugins: [],
  },
  digest: "sha256:delta",
};

export const quietDelta: PluginUpdateDelta = {
  ...updateDelta,
  needs_consent: false,
  added: {
    destinations: [],
    destination_fields: [],
    permissions: [],
    binaries: [],
    plugins: [],
  },
  digest: "sha256:quiet",
};

/** The installed release the update starts from. */
export const installedTranslator = {
  provider_id: "ai_translator",
  name: "AI Subtitle Translator",
  kind: "translator",
  state: "active",
  active_version: "1.0.0",
  manifest: {
    schema_version: 2,
    id: "ai_translator",
    kind: "translator",
    permissions: ["network"],
    endpoints: ["https://openrouter.ai:443"],
  },
};
