/* eslint-disable camelcase -- kind ids are transport names. */
import type {
  PluginBinary,
  PluginCatalog,
  PluginCatalogEntry,
  PluginCatalogSource,
  PluginInstallation,
  PluginPermissionMark,
} from "@/apis/raw/plugins";
import { SUBTITLE_PROVIDER_KIND } from "@/apis/raw/plugins";
import {
  getLatestCatalogEntry,
  isAwaitingRestart,
  isRemoving,
  isUpdateAvailable,
  parseManifest,
} from "@/components/hub/utils";

export { isAwaitingRestart, isRemoving };

/** The official source's id in Bazarr+'s own source records. */
export const OFFICIAL_SOURCE_ID = "official";
/** The one plugin that carries the Official mark, when it comes from the official source. */
export const OFFICIAL_PLUGIN_ID = "ai_translator";

export type TypeFilter =
  | "all"
  | "translator"
  | "translator_backend"
  | "service";

export const TYPE_FILTERS: Array<{ value: TypeFilter; label: string }> = [
  { value: "all", label: "All" },
  { value: "translator", label: "Translator" },
  { value: "translator_backend", label: "Translator back end" },
  { value: "service", label: "Service" },
];

export function typeFilterOf(value: string | null): TypeFilter {
  return TYPE_FILTERS.some((item) => item.value === value)
    ? (value as TypeFilter)
    : "all";
}

/** What the status route says about the profile subtitle provider workers run under. */
export interface ProviderSandboxFacts {
  enabled?: boolean;
  state?: string;
  notice?: string | null;
}

/**
 * Whether this host holds a subtitle provider to a permission. A provider runs under its own profile, not
 * the plugin workers': that profile confines the folders, and only while it applies in full. It lets every
 * outbound connection through but those to Bazarr+'s own ports, so the network is never enforced for one.
 * A status that has not answered marks everything declared.
 */
export function providerPermissionMark(
  name: string,
  providers: ProviderSandboxFacts | null | undefined,
): PluginPermissionMark {
  if (
    (name === "media_read" || name === "data_dir") &&
    providers?.enabled !== false &&
    providers?.state === "enforced"
  ) {
    return "enforced";
  }
  return "declared";
}

/**
 * What a provider card says when the provider profile does not hold in full: the backend's own notice when
 * it has one, else the plain state. Null when the profile holds, or while the status has not answered.
 */
export function providerSandboxNote(
  permissions: string[],
  providers: ProviderSandboxFacts | null | undefined,
): string | null {
  if (permissions.length === 0 || !providers?.state) {
    return null;
  }
  if (providers.state === "enforced") {
    return null;
  }
  if (providers.notice) {
    return providers.notice;
  }
  if (providers.state === "off") {
    return "The provider sandbox is switched off.";
  }
  if (providers.state === "pending") {
    return "Checking the provider sandbox";
  }
  return "Not sandboxed on this host";
}

/** What each kind is for, for the empty state of a kind with nothing to show. */
export const KIND_PURPOSE: Record<TypeFilter, string> = {
  all: "Plugins add engines and helpers to Bazarr+. Subtitle providers stay in the Subtitle Hub.",
  translator:
    "Translators turn subtitle lines into another language. The AI Subtitle Translator and Google Translate are translators.",
  translator_backend:
    "Translator back ends give the AI Subtitle Translator another way to reach a model, for example through a command line tool you already use.",
  service:
    "Services are helpers other plugins can call, for example FlareSolverr, which gets providers past browser checks.",
};

/** The kind a catalog entry has: a schema 2 manifest names it, anything older is a subtitle provider. */
export function entryKind(entry: PluginCatalogEntry): string {
  const manifest = parseManifest(entry);
  if (manifest?.schema_version === 2 && typeof manifest.kind === "string") {
    return manifest.kind;
  }
  return SUBTITLE_PROVIDER_KIND;
}

/** The kind an installation has. A row written before kinds existed is a subtitle provider. */
export function installationKind(installation: PluginInstallation): string {
  return installation.kind || SUBTITLE_PROVIDER_KIND;
}

/** Whether a kind belongs on this page: every kind but the subtitle provider, and the filter's own. */
export function inFilter(kind: string, filter: TypeFilter): boolean {
  if (kind === SUBTITLE_PROVIDER_KIND) {
    return false;
  }
  return filter === "all" || kind === filter;
}

/**
 * Whether a plugin carries the Official mark. The id alone is not enough, since any source can list
 * an entry with that id: the plugin must come from the source Bazarr+ itself records as official, and
 * that source must not be a development source.
 */
export function isOfficial(
  pluginId: string,
  sourceId: string | null | undefined,
  sources: PluginCatalogSource[],
  development = false,
): boolean {
  if (pluginId !== OFFICIAL_PLUGIN_ID || development) {
    return false;
  }
  if (sourceId !== OFFICIAL_SOURCE_ID) {
    return false;
  }
  const source = sources.find((item) => item.id === sourceId);
  return source?.official === true;
}

/** What this host's sandbox probe says, as the status route reports it. */
export interface SandboxFacts {
  workers_sandboxed?: boolean;
  filesystem?: string;
  network?: string;
}

/**
 * Whether this host holds a plugin to a permission. The same rule the server applies to the
 * consent summary: only the network and the two folder permissions can be enforced, and only while
 * plugin workers actually run sandboxed. A probe that has not answered marks everything declared.
 */
export function permissionMark(
  name: string,
  sandbox: SandboxFacts | null | undefined,
): PluginPermissionMark {
  if (!sandbox?.workers_sandboxed) {
    return "declared";
  }
  if (name === "network") {
    return sandbox.network === "enforced" ? "enforced" : "declared";
  }
  if (name === "media_read" || name === "data_dir") {
    return sandbox.filesystem === "enforced" ? "enforced" : "declared";
  }
  return "declared";
}

const strings = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];

/** What a manifest says about what a plugin is, needs and gives, for a card. */
export interface ManifestFacts {
  description: string;
  permissions: string[];
  requires: string[];
  provides: string[];
  extendsId: string | null;
  extendsCapability: string | null;
  /** The fixed origins the plugin's code contacts, as the manifest writes them. */
  endpoints: string[];
  /** A program or container the plugin runs, from its runtime. */
  binaries: PluginBinary[];
  /** Whether a settings field lets the person point the plugin at an address of their own. */
  hasDestinationField: boolean;
}

/** The programs or containers a manifest's runtime starts: an archive's own program or a managed image. */
function binariesOf(manifest: Record<string, unknown>): PluginBinary[] {
  const runtime = manifest.runtime as
    | { artifact?: unknown; process?: unknown }
    | undefined;
  if (
    (runtime?.artifact === "archive" || runtime?.artifact === "oci") &&
    typeof runtime.process === "string"
  ) {
    return [{ artifact: runtime.artifact, process: runtime.process }];
  }
  return [];
}

function hasDestinationField(manifest: Record<string, unknown>): boolean {
  const settings = manifest.settings as
    | { fields?: unknown; backend_types?: unknown }
    | undefined;
  const lists: unknown[] = [settings?.fields];
  if (settings?.backend_types && typeof settings.backend_types === "object") {
    for (const definition of Object.values(settings.backend_types)) {
      lists.push((definition as { fields?: unknown } | null)?.fields);
    }
  }
  return lists.some(
    (fields) =>
      Array.isArray(fields) &&
      fields.some(
        (field) =>
          (field as { destination?: unknown } | null)?.destination === true,
      ),
  );
}

export function manifestFacts(
  source: PluginCatalogEntry | PluginInstallation,
): ManifestFacts {
  const manifest = parseManifest(source) ?? {};
  const extension = manifest.extends as
    | { id?: unknown; capability?: unknown }
    | undefined;
  const description =
    (typeof manifest.description === "string" && manifest.description) ||
    (typeof manifest.summary === "string" && manifest.summary) ||
    "";
  return {
    description,
    permissions: strings(manifest.permissions),
    requires: strings(manifest.requires),
    provides: strings(manifest.provides),
    extendsId: typeof extension?.id === "string" ? extension.id : null,
    extendsCapability:
      typeof extension?.capability === "string" ? extension.capability : null,
    endpoints: strings(manifest.endpoints),
    binaries: binariesOf(manifest),
    hasDestinationField: hasDestinationField(manifest),
  };
}

/**
 * What a card says about a host that does not hold a plugin to its permissions, in neutral words and never as
 * protection that was not earned. Null when the plugin asks for nothing, or while the status has not answered.
 */
export function sandboxNote(
  permissions: string[],
  sandbox: (SandboxFacts & { label?: string; pending?: boolean }) | undefined,
): string | null {
  if (permissions.length === 0 || !sandbox || sandbox.workers_sandboxed) {
    return null;
  }
  return (
    sandbox.label ??
    (sandbox.pending
      ? "Checking the worker sandbox"
      : "Not sandboxed on this host")
  );
}

/** One plugin as a card shows it, from the catalog, from what is installed, or from both. */
export interface PluginRow {
  key: string;
  id: string;
  name: string;
  version: string;
  kind: string;
  sourceId: string | null;
  /** From Bazarr+'s own record of the source, never a field of the manifest. */
  sourceName: string;
  trusted: boolean;
  official: boolean;
  facts: ManifestFacts;
  entry: PluginCatalogEntry | null;
  installation: PluginInstallation | null;
  /** The names of the installed plugins the server says depend on this one, which is what a removal refuses over. */
  dependents: string[];
  /** The same, counting only those that are switched on, which is what switching it off refuses over. */
  enabledDependents: string[];
  /** The version the bound source offers, when it is newer than the one installed. */
  update: string | null;
}

const UNKNOWN_SOURCE = "Unknown source";

/**
 * Whether an installation is one. A row that only records a failed first install has no active and
 * no staged version, and the backend does not count it as installed, so neither does this page: the
 * card offers the install again.
 */
export function isInstalled(
  installation: PluginInstallation | null | undefined,
): boolean {
  return Boolean(
    installation &&
    (installation.active_version || installation.staged_version),
  );
}

/**
 * The names of the plugins whose ids the server lists as dependents. The page decides nothing about who
 * depends on whom: the server's own check, the one that refuses a removal, is the only source.
 */
export function namesOf(
  ids: string[] | undefined,
  installations: PluginInstallation[],
): string[] {
  return (ids ?? [])
    .map((id) => {
      const other = installations.find((item) => item.provider_id === id);
      return other?.name ?? id;
    })
    .sort();
}

function rowOf(
  given: PluginCatalogEntry | null,
  installation: PluginInstallation | null,
  catalog: PluginCatalog | undefined,
  installations: PluginInstallation[],
): PluginRow {
  // A failed first install has nothing installed to describe, so it is shown as the catalog's entry
  // that can be installed again, when its source still lists one.
  const retry = !given && installation && !isInstalled(installation);
  const entry = retry
    ? (getLatestCatalogEntry(
        catalog,
        installation.provider_id,
        installation.source_id ?? null,
      ) as PluginCatalogEntry | null)
    : given;
  // What is installed is described by what was installed, and a catalog row by its entry.
  const facts = manifestFacts(
    (isInstalled(installation) ? installation : (entry ?? installation))!,
  );
  const id = installation?.provider_id ?? entry!.provider_id;
  const sourceId = entry
    ? (entry.source ?? null)
    : (installation!.source_id ?? null);
  const sources = catalog?.sources ?? [];
  const development =
    entry?.development_source === true ||
    installation?.development_source === true;
  const offered =
    installation &&
    installation.origin !== "local" &&
    isUpdateAvailable(installation, catalog)
      ? getLatestCatalogEntry(catalog, id, installation.source_id ?? null)
      : null;
  return {
    key: `${sourceId ?? ""}\u0000${id}`,
    id,
    name: entry?.name ?? installation?.name ?? id,
    version:
      entry?.version ??
      installation?.active_version ??
      installation?.staged_version ??
      "installed",
    kind: entry ? entryKind(entry) : installationKind(installation!),
    sourceId,
    sourceName: entry
      ? (entry.source_name ?? entry.source ?? UNKNOWN_SOURCE)
      : (installation!.source_name ?? UNKNOWN_SOURCE),
    trusted: entry ? Boolean(entry.trusted) : Boolean(installation!.trusted),
    official: isOfficial(id, sourceId, sources, development),
    facts,
    entry,
    installation,
    dependents: namesOf(installation?.dependents, installations),
    enabledDependents: namesOf(installation?.enabled_dependents, installations),
    update: offered?.version ?? null,
  };
}

/** The installation a catalog entry belongs to: the one bound to its source, or an unbound one by id. */
function installedFor(
  entry: PluginCatalogEntry,
  installations: PluginInstallation[],
): PluginInstallation | null {
  const installed = installations.find(
    (item) => item.provider_id === entry.provider_id,
  );
  if (!installed) {
    return null;
  }
  if (!installed.source_bound) {
    return installed;
  }
  return (installed.source_id ?? undefined) === entry.source ? installed : null;
}

/** One card per source and plugin in the catalog, at its newest version, for the kinds this page shows. */
export function marketplaceRows(
  catalog: PluginCatalog | undefined,
  installations: PluginInstallation[],
  filter: TypeFilter,
): PluginRow[] {
  const latest = new Map<string, PluginCatalogEntry>();
  for (const entry of catalog?.entries ?? []) {
    if (!inFilter(entryKind(entry), filter)) {
      continue;
    }
    const key = `${entry.source ?? ""}\u0000${entry.provider_id}`;
    const best = latest.get(key)
      ? getLatestCatalogEntry(
          { sources: [], entries: [latest.get(key)!, entry] },
          entry.provider_id,
        )
      : entry;
    if (best) {
      latest.set(key, best as PluginCatalogEntry);
    }
  }
  return [...latest.values()].map((entry) =>
    rowOf(entry, installedFor(entry, installations), catalog, installations),
  );
}

/** One card per installed plugin of the kinds this page shows. */
export function installedRows(
  catalog: PluginCatalog | undefined,
  installations: PluginInstallation[],
  filter: TypeFilter,
): PluginRow[] {
  return installations
    .filter(
      (item) =>
        item.state !== "removed" && inFilter(installationKind(item), filter),
    )
    .map((installation) => rowOf(null, installation, catalog, installations));
}

/** Whether a row matches what was typed in the search box. */
export function matchesQuery(row: PluginRow, query: string): boolean {
  const text = query.trim().toLowerCase();
  if (!text) {
    return true;
  }
  return [row.name, row.id, row.sourceName, row.facts.description]
    .join(" ")
    .toLowerCase()
    .includes(text);
}

/** An update that has been staged and waits for a restart to take over from the release that is running. */
export function isWaitingUpdate(
  installation: PluginInstallation | null,
): boolean {
  return Boolean(
    installation &&
    installation.needs_restart === true &&
    installation.active_version &&
    installation.staged_version &&
    installation.state !== "removed",
  );
}
