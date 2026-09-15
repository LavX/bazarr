import type { TranslatorJob } from "@/apis/hooks/translator";
import type { ProviderHubJob } from "@/apis/raw/providerHub";

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

interface Tally {
  value: string;
  count: number;
}

/**
 * Frequency count, ordered by descending count then by name. The name
 * tie-break keeps chart and legend order stable across refetches instead of
 * letting equal-count entries shuffle on every poll.
 */
function tally(values: string[]): Tally[] {
  const counts = new Map<string, number>();
  for (const value of values) {
    counts.set(value, (counts.get(value) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([value, count]) => ({ value, count }))
    .sort((a, b) => b.count - a.count || a.value.localeCompare(b.value));
}

/** Status string the providers endpoint uses for a provider that is not throttled. */
const PROVIDER_STATUS_GOOD = "Good";

/**
 * Status stamped on every row when the providers endpoint is called with
 * `history=true`. That variant lists providers seen in download history and
 * carries no health signal at all, so these rows are counted but never
 * attributed to a throttle reason.
 */
const PROVIDER_STATUS_HISTORY = "History";

export interface ProviderThrottleReason {
  reason: string;
  count: number;
}

export interface ProviderHealthSummary {
  total: number;
  good: number;
  throttled: number;
  reasons: ProviderThrottleReason[];
}

export function summarizeProviderHealth(
  providers: System.Provider[],
): ProviderHealthSummary {
  let good = 0;
  const throttleStatuses: string[] = [];

  for (const { status } of providers) {
    if (status === PROVIDER_STATUS_GOOD) {
      good += 1;
    } else if (status !== PROVIDER_STATUS_HISTORY) {
      throttleStatuses.push(status);
    }
  }

  return {
    total: providers.length,
    good,
    throttled: throttleStatuses.length,
    reasons: tally(throttleStatuses).map(({ value, count }) => ({
      reason: value,
      count,
    })),
  };
}

/**
 * Cap on the failed and completed job deques in app/jobs_queue.py. Those two
 * are `deque(maxlen=10)`, so a count of 10 means "at least 10 since boot" and
 * must be rendered as such. The pending and running deques are unbounded.
 */
export const JOB_QUEUE_HISTORY_CAP = 10;

export interface JobQueueSummary {
  pending: number;
  running: number;
  completed: number;
  failed: number;
  completedSaturated: boolean;
  failedSaturated: boolean;
}

export function summarizeJobQueue(jobs: System.Jobs[]): JobQueueSummary {
  const counts = { pending: 0, running: 0, completed: 0, failed: 0 };

  for (const { status } of jobs) {
    if (status in counts) {
      counts[status as keyof typeof counts] += 1;
    }
  }

  return {
    ...counts,
    completedSaturated: counts.completed >= JOB_QUEUE_HISTORY_CAP,
    failedSaturated: counts.failed >= JOB_QUEUE_HISTORY_CAP,
  };
}

/**
 * Entries the Provider Hub keeps in its JSON job log (`_JOB_LOG_LIMIT` in
 * provider_hub/service.py). At the cap the oldest entries are discarded, so a
 * full log means the counts below describe a window, not all time.
 */
export const HUB_JOB_LOG_CAP = 200;

/** Label used when the job log omits an action or state. Both are optional. */
const UNKNOWN_LABEL = "unknown";

export interface HubJobsSummary {
  total: number;
  saturated: boolean;
  byState: { state: string; count: number }[];
  byAction: { action: string; count: number }[];
  durationP50Ms: number | null;
  durationP95Ms: number | null;
}

export function summarizeHubJobs(jobs: ProviderHubJob[]): HubJobsSummary {
  // duration_ms is nullable. Map to NaN rather than 0 so percentile() drops it
  // instead of reporting a job that never recorded a duration as instant.
  const durations = jobs.map((j) =>
    typeof j.duration_ms === "number" ? j.duration_ms : Number.NaN,
  );

  return {
    total: jobs.length,
    saturated: jobs.length >= HUB_JOB_LOG_CAP,
    byState: tally(jobs.map((j) => j.state ?? UNKNOWN_LABEL)).map(
      ({ value, count }) => ({ state: value, count }),
    ),
    byAction: tally(jobs.map((j) => j.action ?? UNKNOWN_LABEL)).map(
      ({ value, count }) => ({ action: value, count }),
    ),
    durationP50Ms: percentile(durations, 50),
    durationP95Ms: percentile(durations, 95),
  };
}

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
