/* eslint-disable camelcase -- kind ids are transport names. */
import type {
  PluginCatalog,
  PluginCatalogEntry,
  PluginCatalogSource,
  PluginInstallation,
  PluginPermissionMark,
} from "@/apis/raw/plugins";
import { SUBTITLE_PROVIDER_KIND } from "@/apis/raw/plugins";
import {
  getLatestCatalogEntry,
  isUpdateAvailable,
  parseManifest,
} from "@/components/hub/utils";

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
  };
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
  /** The names of the installed plugins that need this one. */
  dependents: string[];
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

/** An installed plugin that extends this one, or requires a capability it provides. */
export function dependentsOf(
  id: string,
  provides: string[],
  installations: PluginInstallation[],
): string[] {
  const names: string[] = [];
  for (const other of installations) {
    if (
      other.provider_id === id ||
      other.state === "removed" ||
      !isInstalled(other)
    ) {
      continue;
    }
    const facts = manifestFacts(other);
    const needs =
      facts.extendsId === id ||
      facts.requires.some((capability) => provides.includes(capability));
    if (needs) {
      names.push(other.name ?? other.provider_id);
    }
  }
  return names.sort();
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
    dependents: dependentsOf(id, facts.provides, installations),
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
