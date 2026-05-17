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

function compareSemverParts(a: string, b: string): number {
  const aParts = a.split(/[.\-+]/).map((p) => Number(p) || 0);
  const bParts = b.split(/[.\-+]/).map((p) => Number(p) || 0);
  const len = Math.max(aParts.length, bParts.length);
  for (let i = 0; i < len; i++) {
    const av = aParts[i] ?? 0;
    const bv = bParts[i] ?? 0;
    if (av !== bv) return av - bv;
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
    available: list.filter((p) => isUpdateAvailable(p, catalog)),
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
