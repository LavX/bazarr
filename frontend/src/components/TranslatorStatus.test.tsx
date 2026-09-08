/* eslint-disable camelcase */
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { customRender, screen, within } from "@/tests";
import server from "@/tests/mocks/node";
import TranslatorStatusPanel from "./TranslatorStatus";

function job(title: string, fields: Record<string, unknown> = {}) {
  return {
    jobId: title,
    title,
    status: "completed",
    progress: 100,
    createdAt: "2026-09-07T10:00:00Z",
    tokensUsed: 1200,
    elapsedSeconds: 60,
    ...fields,
  };
}

async function loadJobs(jobs: ReturnType<typeof job>[]) {
  server.use(
    http.get("/api/translator/status", () =>
      HttpResponse.json({
        healthy: true,
        queue: { processing: 0, completed: jobs.length, failed: 0 },
        bazarr_queue: { pending: 0 },
      }),
    ),
    http.get("/api/translator/jobs", () => HttpResponse.json({ jobs })),
  );
  customRender(<TranslatorStatusPanel />);
  return screen.findByRole("table", { name: "Translation Jobs" });
}

function cells(table: HTMLElement, title: string) {
  const row = within(table).getByRole("row", { name: new RegExp(title) });
  return within(row)
    .getAllByRole("cell")
    .map((cell) => cell.textContent);
}

describe("translation job reporting in the real table", () => {
  it("preserves reported cost precision on both sides of one cent and distinguishes zero from missing", async () => {
    const table = await loadJobs([
      job("below", { totalCost: 0.0098 }),
      job("above", { totalCost: 0.0102 }),
      job("zero", { totalCost: 0 }),
      job("tiny", { totalCost: 0.00001 }),
      job("missing"),
      job("negative", { totalCost: -0.01 }),
      job("invalid", { totalCost: "invalid" }),
    ]);
    expect(
      within(table).getByRole("columnheader", { name: "Reported cost" }),
    ).toBeVisible();
    expect(cells(table, "below")[5]).toBe("$0.0098");
    expect(cells(table, "above")[5]).toBe("$0.0102");
    expect(cells(table, "zero")[5]).toBe("$0.0000");
    expect(cells(table, "tiny")[5]).toBe("<$0.0001");
    for (const name of ["missing", "negative", "invalid"]) {
      expect(cells(table, name)[5]).toBe("-");
    }
    expect(
      screen.getByText(/Charges reported by the translation service/),
    ).toBeVisible();
  });

  it("shows total reported tokens per elapsed job second for live and restored jobs", async () => {
    const table = await loadJobs([
      job("live", {
        status: "processing",
        tokensUsed: 1200,
        elapsedSeconds: 60,
      }),
      job("restored", {
        tokensUsed: undefined,
        elapsedSeconds: undefined,
        result: { tokens_used: 1200 },
        startedAt: "2026-09-07T10:00:00Z",
        completedAt: "2026-09-07T10:01:00Z",
      }),
      job("zero tokens", { tokensUsed: 0, result: { tokens_used: 999 } }),
      job("zero duration", {
        elapsedSeconds: 0,
        startedAt: "2026-09-07T10:00:00Z",
        completedAt: "2026-09-07T10:01:00Z",
      }),
      job("unknown tokens", { tokensUsed: undefined }),
      job("bad tokens", { tokensUsed: -1 }),
      job("invalid tokens", { tokensUsed: "invalid" }),
      job("bad duration", { elapsedSeconds: -1 }),
      job("bad dates", {
        elapsedSeconds: undefined,
        startedAt: "invalid",
        completedAt: "invalid",
      }),
    ]);
    expect(
      within(table).getByRole("columnheader", { name: "Total tokens/s" }),
    ).toBeVisible();
    expect(
      screen.getByText(/Includes prompt tokens and reported retry usage/),
    ).toBeVisible();
    for (const name of ["live", "restored"]) {
      expect(cells(table, name)[7]).toBe("20");
    }
    expect(cells(table, "zero tokens")[4]).toBe("0");
    expect(cells(table, "zero tokens")[7]).toBe("0");
    expect(cells(table, "zero duration")[6]).toBe("0s");
    for (const name of [
      "zero duration",
      "unknown tokens",
      "bad tokens",
      "invalid tokens",
      "bad duration",
      "bad dates",
    ]) {
      expect(cells(table, name)[7]).toBe("-");
    }
    expect(cells(table, "bad dates")[6]).toBe("-");
  });
});
