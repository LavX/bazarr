import type { TranslatorJob } from "@/apis/hooks/translator";

/**
 * Linear-interpolation percentile over an unsorted sample.
 *
 * Returns null for an empty sample so callers render a dash rather than a
 * misleading zero. Non-finite entries are dropped: the upstream durations come
 * from a JSON job log where a missing field reads back as null/NaN, and a
 * single one of those would otherwise poison every percentile.
 */
export function percentile(values: number[], p: number): number | null {
  const sorted = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (sorted.length === 0) return null;

  const rank = (p / 100) * (sorted.length - 1);
  const lower = Math.floor(rank);
  const upper = Math.ceil(rank);
  if (lower === upper) return sorted[lower];

  return sorted[lower] + (sorted[upper] - sorted[lower]) * (rank - lower);
}

/** Label used when a record omits the field being grouped on. */
const UNKNOWN_LABEL = "unknown";

/** Statuses the sidecar reports for a job that is still in flight. */
const TRANSLATOR_ACTIVE_STATUSES = new Set(["queued", "processing"]);

export interface TranslatorModelCost {
  model: string;
  costUsd: number;
  jobs: number;
}

export interface TranslatorUsageSummary {
  jobs: number;
  completed: number;
  partial: number;
  failed: number;
  active: number;
  costUsd: number;
  tokens: number;
  linesTranslated: number;
  byModel: TranslatorModelCost[];
}

/**
 * Aggregate the sidecar's job list.
 *
 * This describes only the jobs the sidecar still holds (its `/jobs` route
 * returns a bounded recent window), so callers must label these figures as
 * "recent jobs" and never as totals or all-time cost.
 */
export function summarizeTranslatorJobs(
  jobs: TranslatorJob[],
): TranslatorUsageSummary {
  const perModel = new Map<string, { costUsd: number; jobs: number }>();
  const summary: TranslatorUsageSummary = {
    jobs: jobs.length,
    completed: 0,
    partial: 0,
    failed: 0,
    active: 0,
    costUsd: 0,
    tokens: 0,
    linesTranslated: 0,
    byModel: [],
  };

  for (const job of jobs) {
    const cost = job.totalCost ?? 0;

    if (job.status === "completed") summary.completed += 1;
    else if (job.status === "partial") summary.partial += 1;
    else if (job.status === "failed") summary.failed += 1;
    if (TRANSLATOR_ACTIVE_STATUSES.has(job.status)) summary.active += 1;

    summary.costUsd += cost;
    summary.tokens += job.tokensUsed ?? 0;
    // completedLines, not totalLines: a partial job asked for more than it
    // delivered, and throughput should reflect what actually landed.
    summary.linesTranslated += job.completedLines ?? 0;

    const model = job.model ?? UNKNOWN_LABEL;
    const bucket = perModel.get(model) ?? { costUsd: 0, jobs: 0 };
    bucket.costUsd += cost;
    bucket.jobs += 1;
    perModel.set(model, bucket);
  }

  summary.byModel = [...perModel.entries()]
    .map(([model, v]) => ({ model, ...v }))
    .sort((a, b) => b.costUsd - a.costUsd || a.model.localeCompare(b.model));

  return summary;
}

/**
 * Local calendar date (YYYY-MM-DD) for a timestamp, or null if unparseable.
 *
 * Sidecar timestamps may be UTC instants ("...Z") or naive local strings.
 * Date handles both, and formatting from the local parts keeps a day bucket
 * aligned with the viewer's calendar instead of UTC's.
 */
function localDateKey(timestamp: string | undefined): string | null {
  if (!timestamp) return null;
  const at = new Date(timestamp);
  if (Number.isNaN(at.getTime())) return null;

  const month = `${at.getMonth() + 1}`.padStart(2, "0");
  const day = `${at.getDate()}`.padStart(2, "0");
  return `${at.getFullYear()}-${month}-${day}`;
}

export interface TranslatorThroughputPoint {
  date: string;
  lines: number;
}

/** Lines delivered per calendar day, oldest first. Jobs still running are skipped. */
export function translatorLinesPerDay(
  jobs: TranslatorJob[],
): TranslatorThroughputPoint[] {
  const perDay = new Map<string, number>();

  for (const job of jobs) {
    const date = localDateKey(job.completedAt);
    if (!date) continue;
    perDay.set(date, (perDay.get(date) ?? 0) + (job.completedLines ?? 0));
  }

  return [...perDay.entries()]
    .map(([date, lines]) => ({ date, lines }))
    .sort((a, b) => a.date.localeCompare(b.date));
}

/**
 * Human-readable duration.
 *
 * null means "not recorded" and renders as a dash; 0 is a real measurement and
 * renders as 0ms, so a missing value is never mistaken for an instant one.
 */
export function formatDurationMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "-";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;

  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
}
