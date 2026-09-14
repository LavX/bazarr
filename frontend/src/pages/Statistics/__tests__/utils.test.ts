/* eslint-disable camelcase */
import { describe, expect, it } from "vitest";
import type { TranslatorJob } from "@/apis/hooks/translator";
import type { ProviderHubJob } from "@/apis/raw/providerHub";
import {
  formatDurationMs,
  percentile,
  summarizeHubJobs,
  summarizeJobQueue,
  summarizeProviderHealth,
  summarizeTranslatorJobs,
  translatorLinesPerDay,
} from "@/pages/Statistics/utils";

const provider = (
  name: string,
  status: string,
  retry = "-",
): System.Provider => ({ name, status, retry });

describe("percentile", () => {
  it("returns null for an empty sample", () => {
    expect(percentile([], 50)).toBeNull();
  });

  it("returns the single value for a one-element sample", () => {
    expect(percentile([42], 95)).toBe(42);
  });

  it("returns the median of an odd-length sample regardless of input order", () => {
    expect(percentile([30, 10, 20], 50)).toBe(20);
  });

  it("interpolates between neighbours for a fractional rank", () => {
    // p50 of [10,20,30,40] sits at rank 1.5, halfway between 20 and 30.
    expect(percentile([10, 20, 30, 40], 50)).toBe(25);
  });

  it("returns the maximum at p100", () => {
    expect(percentile([5, 1, 9], 100)).toBe(9);
  });

  it("ignores non-finite samples rather than poisoning the result", () => {
    expect(percentile([10, Number.NaN, 20], 50)).toBe(15);
  });
});

describe("summarizeProviderHealth", () => {
  it("reports zeroes for an empty provider list", () => {
    expect(summarizeProviderHealth([])).toEqual({
      total: 0,
      good: 0,
      throttled: 0,
      reasons: [],
    });
  });

  it("counts a provider whose status is Good as healthy", () => {
    const summary = summarizeProviderHealth([
      provider("opensubtitles", "Good"),
      provider("podnapisi", "Good"),
    ]);
    expect(summary).toMatchObject({ total: 2, good: 2, throttled: 0 });
  });

  it("counts any status other than Good as throttled", () => {
    const summary = summarizeProviderHealth([
      provider("a", "Good"),
      provider("b", "DownloadLimitExceeded", "in 4 hours"),
    ]);
    expect(summary).toMatchObject({ total: 2, good: 1, throttled: 1 });
  });

  it("groups throttle reasons and orders them by descending count", () => {
    const summary = summarizeProviderHealth([
      provider("a", "TooManyRequests"),
      provider("b", "DownloadLimitExceeded"),
      provider("c", "DownloadLimitExceeded"),
    ]);
    expect(summary.reasons).toEqual([
      { reason: "DownloadLimitExceeded", count: 2 },
      { reason: "TooManyRequests", count: 1 },
    ]);
  });

  it("breaks count ties by reason name so the chart order is stable", () => {
    const summary = summarizeProviderHealth([
      provider("a", "ZebraError"),
      provider("b", "AlphaError"),
    ]);
    expect(summary.reasons.map((r) => r.reason)).toEqual([
      "AlphaError",
      "ZebraError",
    ]);
  });

  it("does not treat the history placeholder status as a throttle reason", () => {
    // useSystemProviders(true) returns status "History" for every row. That
    // payload carries no health signal, so it must not be charted as an outage.
    const summary = summarizeProviderHealth([
      provider("a", "History"),
      provider("b", "History"),
    ]);
    expect(summary).toEqual({ total: 2, good: 0, throttled: 0, reasons: [] });
  });
});

const job = (status: string, name = "sync"): System.Jobs =>
  ({ job_name: name, status }) as System.Jobs;

describe("summarizeJobQueue", () => {
  it("reports zeroes for an empty queue", () => {
    expect(summarizeJobQueue([])).toMatchObject({
      pending: 0,
      running: 0,
      completed: 0,
      failed: 0,
    });
  });

  it("counts jobs into their status buckets", () => {
    const summary = summarizeJobQueue([
      job("pending"),
      job("pending"),
      job("running"),
      job("completed"),
      job("failed"),
    ]);
    expect(summary).toMatchObject({
      pending: 2,
      running: 1,
      completed: 1,
      failed: 1,
    });
  });

  it("ignores a status the backend does not document", () => {
    const summary = summarizeJobQueue([job("reserved"), job("pending")]);
    expect(summary).toMatchObject({ pending: 1, running: 0 });
  });

  it("flags completed as saturated once it reaches the queue cap", () => {
    // jobs_completed_queue is a deque(maxlen=10), so 10 means "at least 10".
    const summary = summarizeJobQueue(
      Array.from({ length: 10 }, () => job("completed")),
    );
    expect(summary.completed).toBe(10);
    expect(summary.completedSaturated).toBe(true);
  });

  it("flags failed as saturated once it reaches the queue cap", () => {
    const summary = summarizeJobQueue(
      Array.from({ length: 10 }, () => job("failed")),
    );
    expect(summary.failedSaturated).toBe(true);
  });

  it("does not flag saturation below the cap", () => {
    const summary = summarizeJobQueue([job("completed"), job("failed")]);
    expect(summary.completedSaturated).toBe(false);
    expect(summary.failedSaturated).toBe(false);
  });

  it("never flags the unbounded pending and running queues as saturated", () => {
    const summary = summarizeJobQueue(
      Array.from({ length: 40 }, () => job("pending")),
    );
    expect(summary.pending).toBe(40);
    expect(summary).not.toHaveProperty("pendingSaturated");
  });
});

describe("summarizeHubJobs", () => {
  it("reports an empty summary with null percentiles for no jobs", () => {
    expect(summarizeHubJobs([])).toEqual({
      total: 0,
      saturated: false,
      byState: [],
      byAction: [],
      durationP50Ms: null,
      durationP95Ms: null,
    });
  });

  it("counts jobs by state, most frequent first", () => {
    const summary = summarizeHubJobs([
      { state: "completed" },
      { state: "failed" },
      { state: "completed" },
    ]);
    expect(summary.byState).toEqual([
      { state: "completed", count: 2 },
      { state: "failed", count: 1 },
    ]);
  });

  it("counts jobs by action, most frequent first", () => {
    const summary = summarizeHubJobs([
      { action: "install" },
      { action: "update" },
      { action: "update" },
    ]);
    expect(summary.byAction).toEqual([
      { action: "update", count: 2 },
      { action: "install", count: 1 },
    ]);
  });

  it("labels a job with no recorded action or state as unknown", () => {
    const summary = summarizeHubJobs([{ id: "j1" }]);
    expect(summary.byAction).toEqual([{ action: "unknown", count: 1 }]);
    expect(summary.byState).toEqual([{ state: "unknown", count: 1 }]);
  });

  it("computes duration percentiles from recorded durations", () => {
    const summary = summarizeHubJobs([
      { duration_ms: 100 },
      { duration_ms: 200 },
      { duration_ms: 300 },
    ]);
    expect(summary.durationP50Ms).toBe(200);
    expect(summary.durationP95Ms).toBe(290);
  });

  it("excludes jobs with no recorded duration from the percentiles", () => {
    // duration_ms is nullable in the job log; a null must not read as 0ms.
    const summary = summarizeHubJobs([
      { duration_ms: null },
      { duration_ms: 500 },
    ]);
    expect(summary.durationP50Ms).toBe(500);
  });

  it("flags the job log as saturated at its 200-entry cap", () => {
    const jobs: ProviderHubJob[] = Array.from({ length: 200 }, () => ({
      state: "completed",
    }));
    expect(summarizeHubJobs(jobs).saturated).toBe(true);
  });
});

const tJob = (over: Partial<TranslatorJob> = {}): TranslatorJob =>
  ({
    jobId: "j",
    status: "completed",
    progress: 100,
    createdAt: "2026-09-14T10:00:00",
    ...over,
  }) as TranslatorJob;

describe("summarizeTranslatorJobs", () => {
  it("reports zeroes when the sidecar has no jobs", () => {
    expect(summarizeTranslatorJobs([])).toMatchObject({
      jobs: 0,
      completed: 0,
      failed: 0,
      partial: 0,
      active: 0,
      costUsd: 0,
      tokens: 0,
      linesTranslated: 0,
      byModel: [],
    });
  });

  it("sums cost and tokens across jobs", () => {
    const summary = summarizeTranslatorJobs([
      tJob({ totalCost: 0.25, tokensUsed: 1000 }),
      tJob({ totalCost: 0.5, tokensUsed: 2000 }),
    ]);
    expect(summary.costUsd).toBeCloseTo(0.75);
    expect(summary.tokens).toBe(3000);
  });

  it("treats a job with no recorded cost or tokens as zero rather than NaN", () => {
    const summary = summarizeTranslatorJobs([tJob(), tJob({ totalCost: 1 })]);
    expect(summary.costUsd).toBe(1);
    expect(summary.tokens).toBe(0);
  });

  it("counts partial as its own outcome, not as completed or failed", () => {
    const summary = summarizeTranslatorJobs([
      tJob({ status: "completed" }),
      tJob({ status: "partial" }),
      tJob({ status: "failed" }),
    ]);
    expect(summary).toMatchObject({ completed: 1, partial: 1, failed: 1 });
  });

  it("counts queued and processing jobs as active", () => {
    const summary = summarizeTranslatorJobs([
      tJob({ status: "queued" }),
      tJob({ status: "processing" }),
      tJob({ status: "completed" }),
    ]);
    expect(summary.active).toBe(2);
  });

  it("sums completed lines rather than requested lines", () => {
    // A partial job delivered fewer lines than it asked for; throughput must
    // reflect what actually landed.
    const summary = summarizeTranslatorJobs([
      tJob({ status: "partial", totalLines: 900, completedLines: 120 }),
    ]);
    expect(summary.linesTranslated).toBe(120);
  });

  it("groups cost by model, most expensive first", () => {
    const summary = summarizeTranslatorJobs([
      tJob({ model: "cheap", totalCost: 1 }),
      tJob({ model: "pricey", totalCost: 9 }),
      tJob({ model: "cheap", totalCost: 2 }),
    ]);
    expect(summary.byModel).toEqual([
      { model: "pricey", costUsd: 9, jobs: 1 },
      { model: "cheap", costUsd: 3, jobs: 2 },
    ]);
  });

  it("labels jobs with no model so their cost is still attributed", () => {
    const summary = summarizeTranslatorJobs([tJob({ totalCost: 5 })]);
    expect(summary.byModel).toEqual([
      { model: "unknown", costUsd: 5, jobs: 1 },
    ]);
  });
});

describe("translatorLinesPerDay", () => {
  it("returns nothing for no jobs", () => {
    expect(translatorLinesPerDay([])).toEqual([]);
  });

  it("merges jobs completed on the same day", () => {
    const series = translatorLinesPerDay([
      tJob({ completedAt: "2026-09-14T08:00:00", completedLines: 100 }),
      tJob({ completedAt: "2026-09-14T20:00:00", completedLines: 50 }),
    ]);
    expect(series).toEqual([{ date: "2026-09-14", lines: 150 }]);
  });

  it("orders days oldest first so the chart reads left to right", () => {
    const series = translatorLinesPerDay([
      tJob({ completedAt: "2026-09-14T08:00:00", completedLines: 1 }),
      tJob({ completedAt: "2026-09-12T08:00:00", completedLines: 2 }),
    ]);
    expect(series.map((p) => p.date)).toEqual(["2026-09-12", "2026-09-14"]);
  });

  it("skips a job that has not completed yet", () => {
    const series = translatorLinesPerDay([
      tJob({ status: "processing", completedLines: 10 }),
    ]);
    expect(series).toEqual([]);
  });

  it("accepts a UTC instant and yields a calendar date", () => {
    const series = translatorLinesPerDay([
      tJob({ completedAt: "2026-09-14T12:00:00Z", completedLines: 7 }),
    ]);
    expect(series).toHaveLength(1);
    expect(series[0].date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(series[0].lines).toBe(7);
  });

  it("skips a job whose completion timestamp is unparseable", () => {
    const series = translatorLinesPerDay([
      tJob({ completedAt: "not a date", completedLines: 9 }),
    ]);
    expect(series).toEqual([]);
  });
});

describe("formatDurationMs", () => {
  it("renders a dash when no duration was recorded", () => {
    expect(formatDurationMs(null)).toBe("-");
  });

  it("renders sub-second durations in milliseconds", () => {
    expect(formatDurationMs(450)).toBe("450ms");
  });

  it("renders seconds with one decimal", () => {
    expect(formatDurationMs(2000)).toBe("2.0s");
  });

  it("renders durations over a minute as minutes and seconds", () => {
    expect(formatDurationMs(95_000)).toBe("1m 35s");
  });

  it("treats zero as a real measurement, not a missing one", () => {
    expect(formatDurationMs(0)).toBe("0ms");
  });
});
