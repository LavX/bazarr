import { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import {
  faCheckCircle,
  faCircleDot,
  faCircleNotch,
  faClock,
  faPause,
  faXmarkCircle,
} from "@fortawesome/free-solid-svg-icons";
import type {
  ProviderHubCatalog,
  ProviderHubCatalogEntry,
  ProviderHubInstallation,
  ProviderHubManifest,
} from "@/apis/raw/providerHub";

export type ProviderState =
  | "active"
  | "staged"
  | "failed"
  | "removed"
  | "inactive"
  | string
  | undefined;

export type JobState =
  | "completed"
  | "pending"
  | "running"
  | "failed"
  | string
  | undefined;

export type Tone = "success" | "warning" | "danger" | "info" | "neutral";

export interface StatusMeta {
  tone: Tone;
  icon: IconDefinition;
  label: string;
}

const PROVIDER_STATE_META: Record<string, StatusMeta> = {
  active: { tone: "success", icon: faCheckCircle, label: "Active" },
  staged: { tone: "warning", icon: faClock, label: "Staged" },
  failed: { tone: "danger", icon: faXmarkCircle, label: "Failed" },
  removed: { tone: "neutral", icon: faCircleDot, label: "Removed" },
  inactive: { tone: "neutral", icon: faPause, label: "Inactive" },
};

const JOB_STATE_META: Record<string, StatusMeta> = {
  completed: { tone: "success", icon: faCheckCircle, label: "Completed" },
  pending: { tone: "info", icon: faClock, label: "Pending" },
  running: { tone: "warning", icon: faCircleNotch, label: "Running" },
  failed: { tone: "danger", icon: faXmarkCircle, label: "Failed" },
};

export function getProviderStateMeta(state: ProviderState): StatusMeta {
  if (state && PROVIDER_STATE_META[state]) {
    return PROVIDER_STATE_META[state];
  }
  return { tone: "info", icon: faCircleDot, label: state ?? "Unknown" };
}

export function getJobStateMeta(state: JobState): StatusMeta {
  if (state && JOB_STATE_META[state]) {
    return JOB_STATE_META[state];
  }
  return { tone: "info", icon: faCircleDot, label: state ?? "Unknown" };
}

export function parseManifest(
  entry: ProviderHubCatalogEntry | LooseObject | null | undefined,
): ProviderHubManifest | null {
  if (!entry) return null;
  const candidate =
    (entry as LooseObject).manifest ?? (entry as LooseObject).manifest_json;
  if (candidate == null) return null;
  if (typeof candidate === "string") {
    try {
      return JSON.parse(candidate) as ProviderHubManifest;
    } catch {
      return null;
    }
  }
  if (typeof candidate === "object") {
    return candidate as ProviderHubManifest;
  }
  return null;
}

const ANTI_CAPTCHA_RE = /captcha|flaresolverr/i;

/**
 * Best-effort detection of whether a catalog provider needs an Anti-Captcha
 * service or FlareSolverr to solve captchas. Catalog manifests do not yet carry
 * a structured flag, so an explicit declaration is honoured when present and we
 * otherwise scan the human-facing manifest text and config schema for the known
 * keywords. See https://github.com/LavX/bazarr/issues/215
 */
export function requiresAntiCaptcha(
  entry: ProviderHubCatalogEntry | LooseObject | null | undefined,
): boolean {
  const manifest = parseManifest(entry) as LooseObject | null;
  if (!manifest) return false;

  if (typeof manifest.requires_anti_captcha === "boolean") {
    return manifest.requires_anti_captcha;
  }
  if (
    Array.isArray(manifest.requires) &&
    manifest.requires.some(
      (r) => typeof r === "string" && ANTI_CAPTCHA_RE.test(r),
    )
  ) {
    return true;
  }

  const haystacks: string[] = [];
  if (typeof manifest.description === "string") {
    haystacks.push(manifest.description);
  }
  if (typeof manifest.long_description === "string") {
    haystacks.push(manifest.long_description);
  }
  if (manifest.config_schema) {
    try {
      haystacks.push(JSON.stringify(manifest.config_schema));
    } catch {
      // ignore a non-serializable schema
    }
  }
  return haystacks.some((text) => ANTI_CAPTCHA_RE.test(text));
}

interface ParsedSemver {
  core: number[];
  prerelease: string[];
}

function splitVersionTokens(value: string): string[] {
  return value.match(/\d+|[A-Za-z]+/g) ?? [];
}

function parseSemver(version: string): ParsedSemver {
  const withoutBuild = version.split("+", 1)[0] ?? "";
  const prereleaseIndex = withoutBuild.indexOf("-");
  const coreText =
    prereleaseIndex === -1
      ? withoutBuild
      : withoutBuild.slice(0, prereleaseIndex);
  const prereleaseText =
    prereleaseIndex === -1 ? "" : withoutBuild.slice(prereleaseIndex + 1);
  const prerelease: string[] = [];
  const core = coreText.split(".").map((part) => {
    const match = part.match(/^(\d+)(.*)$/);
    if (!match) {
      prerelease.push(...splitVersionTokens(part));
      return 0;
    }
    const suffix = match[2] ?? "";
    if (suffix) {
      prerelease.push(...splitVersionTokens(suffix));
    }
    return Number(match[1]);
  });
  if (prereleaseText) {
    prerelease.push(...splitVersionTokens(prereleaseText));
  }

  while (core.length < 3) core.push(0);
  return { core, prerelease };
}

function comparePrereleaseIdentifier(a: string, b: string): number {
  const aNumeric = /^\d+$/.test(a);
  const bNumeric = /^\d+$/.test(b);
  if (aNumeric && bNumeric) {
    return Number(a) - Number(b);
  }
  if (aNumeric !== bNumeric) {
    return aNumeric ? -1 : 1;
  }
  return a.localeCompare(b);
}

function compareSemverParts(a: string, b: string): number {
  const aVersion = parseSemver(a);
  const bVersion = parseSemver(b);
  const len = Math.max(aVersion.core.length, bVersion.core.length);
  for (let i = 0; i < len; i++) {
    const av = aVersion.core[i] ?? 0;
    const bv = bVersion.core[i] ?? 0;
    if (av !== bv) return av - bv;
  }
  if (aVersion.prerelease.length === 0 && bVersion.prerelease.length === 0) {
    return 0;
  }
  if (aVersion.prerelease.length === 0) return 1;
  if (bVersion.prerelease.length === 0) return -1;

  const prereleaseLen = Math.max(
    aVersion.prerelease.length,
    bVersion.prerelease.length,
  );
  for (let i = 0; i < prereleaseLen; i++) {
    const av = aVersion.prerelease[i];
    const bv = bVersion.prerelease[i];
    if (av == null) return -1;
    if (bv == null) return 1;
    const comparison = comparePrereleaseIdentifier(av, bv);
    if (comparison !== 0) return comparison;
  }
  return 0;
}

export function getLatestCatalogEntry(
  catalog: ProviderHubCatalog | undefined | null,
  providerId: string,
): ProviderHubCatalogEntry | null {
  const entries = catalog?.entries ?? [];
  const matching = entries.filter((e) => e.provider_id === providerId);
  if (matching.length === 0) return null;
  return matching.reduce((best, current) =>
    compareSemverParts(current.version ?? "0", best.version ?? "0") > 0
      ? current
      : best,
  );
}

export function isUpdateAvailable(
  provider: ProviderHubInstallation,
  catalog: ProviderHubCatalog | undefined | null,
): boolean {
  if (!provider.active_version) return false;
  if (provider.pending_restart) return false;
  const latest = getLatestCatalogEntry(catalog, provider.provider_id);
  if (!latest) return false;
  return compareSemverParts(latest.version, provider.active_version) > 0;
}

export interface UpdateSummary {
  available: ProviderHubInstallation[];
  pendingRestart: ProviderHubInstallation[];
}

export function summarizeUpdates(
  providers: ProviderHubInstallation[] | undefined,
  catalog: ProviderHubCatalog | undefined | null,
): UpdateSummary {
  const list = providers ?? [];
  return {
    // Local packages are owned by their uploaded file, so a catalog update is
    // never offered for them (the backend also refuses apply_update for local).
    available: list.filter(
      (p) => p.origin !== "local" && isUpdateAvailable(p, catalog),
    ),
    pendingRestart: list.filter((p) => p.pending_restart === true),
  };
}

export interface ParsedGitHubUrl {
  owner: string;
  repo: string;
  suggestedName: string;
}

export function parseGitHubUrl(url: string): ParsedGitHubUrl | null {
  const trimmed = url.trim();
  if (!trimmed) return null;
  let host: string;
  let pathname: string;
  try {
    const parsed = new URL(trimmed);
    host = parsed.host.toLowerCase();
    pathname = parsed.pathname;
  } catch {
    return null;
  }
  if (host !== "github.com" && host !== "raw.githubusercontent.com") {
    return null;
  }
  const parts = pathname.split("/").filter(Boolean);
  if (parts.length < 2) return null;
  const owner = parts[0];
  const repo = parts[1].replace(/\.git$/, "");
  if (!owner || !repo) return null;
  return { owner, repo, suggestedName: `${owner}/${repo}` };
}

export function parseGitHubRef(url: string): string | null {
  const trimmed = url.trim();
  if (!trimmed) return null;
  let pathname: string;
  try {
    pathname = new URL(trimmed).pathname;
  } catch {
    return null;
  }
  const parts = pathname.split("/").filter(Boolean);
  // /<owner>/<repo>/blob/<ref>/<path...>
  if (parts.length < 5) return null;
  if (parts[2] !== "blob" && parts[2] !== "raw") return null;
  return parts[3] || null;
}

export function formatRelativeTime(
  iso: string | null | undefined,
  now: Date = new Date(),
): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const diffMs = now.getTime() - d.getTime();
  const absSec = Math.abs(diffMs) / 1000;
  if (absSec < 5) return "just now";
  if (absSec < 60) return `${Math.round(absSec)}s ago`;
  const minutes = absSec / 60;
  if (minutes < 60) return `${Math.round(minutes)}m ago`;
  const hours = minutes / 60;
  if (hours < 24) return `${Math.round(hours)}h ago`;
  const days = hours / 24;
  if (days < 30) return `${Math.round(days)}d ago`;
  const months = days / 30;
  if (months < 12) return `${Math.round(months)}mo ago`;
  return `${Math.round(months / 12)}y ago`;
}

export function formatAbsoluteTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d
    .toISOString()
    .replace("T", " ")
    .replace(/\.\d+Z$/, " UTC");
}

export function formatDuration(durationMs: number | null | undefined): string {
  if (durationMs == null || Number.isNaN(durationMs)) return "";
  if (durationMs < 1) return "<1ms";
  if (durationMs < 1000) return `${Math.round(durationMs)}ms`;
  const seconds = durationMs / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds - minutes * 60);
  return remainder > 0 ? `${minutes}m ${remainder}s` : `${minutes}m`;
}

const ACTION_LABELS: Record<string, string> = {
  install: "Install plugin",
  stage_update: "Stage update",
  activate: "Activate plugin",
  uninstall: "Uninstall plugin",
  test_connection: "Test connection",
  refresh_catalog: "Refresh catalog",
  check_updates: "Check updates",
  add_source: "Add source",
  update_source: "Update source",
  remove_source: "Remove source",
};

export function getActionLabel(action: string | null | undefined): string {
  if (!action) return "Unknown action";
  return (
    ACTION_LABELS[action] ??
    action.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
  );
}
