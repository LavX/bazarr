import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import type { TranslatorJob } from "@/apis/hooks/translator";
import TranslatorPanel from "@/pages/Statistics/TranslatorPanel";
import { customRender, screen } from "@/tests";
import server from "@/tests/mocks/node";

const status = {
  service: "ai-subtitle-translator",
  version: "1.3.4",
  healthy: true,
  config: { model: "gpt-x", apiKeyConfigured: true },
  queue: {
    maxConcurrent: 2,
    processing: 1,
    queued: 4,
    completed: 20,
    failed: 2,
    total: 27,
  },
};

const job = (over: Partial<TranslatorJob> = {}): TranslatorJob =>
  ({
    jobId: "j1",
    status: "completed",
    progress: 100,
    createdAt: "2026-09-14T09:00:00",
    completedAt: "2026-09-14T10:00:00",
    ...over,
  }) as TranslatorJob;

const mock = (jobs: TranslatorJob[] = []) => {
  server.use(
    http.get("/api/translator/status", () => HttpResponse.json(status)),
    http.get("/api/translator/jobs", () =>
      HttpResponse.json({
        jobs,
        total: jobs.length,
        processing: 0,
        queued: 0,
      }),
    ),
  );
};

describe("Statistics > TranslatorPanel", () => {
  beforeEach(() => mock());

  it("shows the sidecar queue depth", async () => {
    customRender(<TranslatorPanel />);

    expect(await screen.findByText("4")).toBeInTheDocument();
    expect(screen.getByText(/queued/i)).toBeInTheDocument();
  });

  it("sums cost across the jobs the sidecar still holds", async () => {
    mock([
      job({ totalCost: 0.5, tokensUsed: 1000 }),
      job({ jobId: "j2", totalCost: 1.25, tokensUsed: 2000 }),
    ]);
    customRender(<TranslatorPanel />);

    // Appears twice by design: the summary tile and the per-model row.
    expect((await screen.findAllByText("$1.7500")).length).toBeGreaterThan(0);
  });

  it("never labels the cost as a total, because the job list is a window", async () => {
    // The sidecar /jobs route returns a bounded recent window, so an all-time
    // figure is not available and must not be implied.
    mock([job({ totalCost: 1 })]);
    customRender(<TranslatorPanel />);

    expect(await screen.findByText(/recent jobs only/i)).toBeInTheDocument();
    expect(screen.queryByText(/total cost/i)).not.toBeInTheDocument();
  });

  it("attributes cost per model, most expensive first", async () => {
    mock([
      job({ model: "cheap-model", totalCost: 1 }),
      job({ jobId: "j2", model: "pricey-model", totalCost: 8 }),
    ]);
    customRender(<TranslatorPanel />);

    expect(await screen.findByText("pricey-model")).toBeInTheDocument();
    expect(screen.getByText("cheap-model")).toBeInTheDocument();
  });

  it("counts a partial job separately from a completed one", async () => {
    mock([
      job({ status: "completed", completedLines: 100, totalLines: 100 }),
      job({
        jobId: "j2",
        status: "partial",
        completedLines: 40,
        totalLines: 100,
      }),
    ]);
    customRender(<TranslatorPanel />);

    // Throughput counts delivered lines (100 + 40), not requested lines.
    expect(await screen.findByText("140")).toBeInTheDocument();
    expect(screen.getByText(/partial/i)).toBeInTheDocument();
  });

  it("reports the sidecar as unavailable instead of erroring", async () => {
    server.use(
      http.get("/api/translator/status", () => HttpResponse.error()),
      http.get("/api/translator/jobs", () => HttpResponse.error()),
    );
    customRender(<TranslatorPanel />);

    expect(
      await screen.findByText(/translator is not reachable/i),
    ).toBeInTheDocument();
  });

  it("keeps the queue but does not report zero cost when only the job list fails", async () => {
    server.use(
      http.get("/api/translator/status", () => HttpResponse.json(status)),
      http.get("/api/translator/jobs", () => HttpResponse.error()),
    );
    customRender(<TranslatorPanel />);

    expect(
      await screen.findByText(/recent jobs could not be loaded/i),
    ).toBeInTheDocument();
    expect(await screen.findByText("4")).toBeInTheDocument();
    expect(screen.queryByText("$0.0000")).not.toBeInTheDocument();
    expect(
      screen.queryByText(/no translation jobs recorded/i),
    ).not.toBeInTheDocument();
  });

  it("keeps the job figures but does not report an empty queue when only the status fails", async () => {
    server.use(
      http.get("/api/translator/status", () => HttpResponse.error()),
      http.get("/api/translator/jobs", () =>
        HttpResponse.json({
          jobs: [job({ totalCost: 1 })],
          total: 1,
          processing: 0,
          queued: 0,
        }),
      ),
    );
    customRender(<TranslatorPanel />);

    expect(
      await screen.findByText(/queue status could not be loaded/i),
    ).toBeInTheDocument();
    expect((await screen.findAllByText("$1.0000")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Queued")).not.toBeInTheDocument();
    expect(screen.queryByText("Processing")).not.toBeInTheDocument();
  });

  it("says so when the sidecar is reachable but has run nothing", async () => {
    customRender(<TranslatorPanel />);

    expect(await screen.findByText(/no translation jobs/i)).toBeInTheDocument();
  });
});
