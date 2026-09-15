import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ActivityPanel from "@/pages/Statistics/ActivityPanel";
import { defaultStatisticsFilters } from "@/pages/Statistics/filters";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const onStats = vi.fn();

const emptyTotals: History.MetricsTotals = {
  downloads: 0,
  series: 0,
  movies: 0,
  sports: 0,
  dailyAverage: 0,
  peakDate: null,
  peakCount: 0,
  automaticPct: 0,
};

const mock = (
  opts: {
    series?: History.StatItem[];
    movies?: History.StatItem[];
    sports?: History.StatItem[];
    totals?: Partial<History.MetricsTotals>;
  } = {},
) => {
  server.use(
    http.get("/api/history/stats", ({ request }) => {
      onStats(Object.fromEntries(new URL(request.url).searchParams));
      return HttpResponse.json({
        series: opts.series ?? [],
        movies: opts.movies ?? [],
        sports: opts.sports ?? [],
      });
    }),
    http.get("/api/history/metrics", () =>
      HttpResponse.json({
        totals: { ...emptyTotals, ...opts.totals },
        byProvider: [],
        providerReliability: [],
        byLanguage: [],
        byAction: [],
        scoreHistogram: [],
      }),
    ),
    http.get("/api/system/arr-instances", () => HttpResponse.json([])),
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { use_sportarr: false } }),
    ),
  );
};

const render = () =>
  customRender(<ActivityPanel filters={defaultStatisticsFilters} />);

describe("Statistics > ActivityPanel", () => {
  beforeEach(() => {
    onStats.mockClear();
    mock();
  });

  it("requests the timeframe it was given", async () => {
    render();

    await waitFor(() => expect(onStats).toHaveBeenCalled());
    expect(onStats.mock.calls[0][0]).toMatchObject({ timeFrame: "month" });
  });

  it("headlines the download total and its media split", async () => {
    mock({ totals: { downloads: 143, series: 120, movies: 23 } });
    render();

    expect(await screen.findByText("143")).toBeInTheDocument();
    expect(screen.getByText(/120 series \/ 23 movies/)).toBeInTheDocument();
  });

  it("reports the busiest day", async () => {
    mock({ totals: { peakCount: 31, peakDate: "2026-09-13" } });
    render();

    expect(await screen.findByText("31")).toBeInTheDocument();
    expect(screen.getByText("2026-09-13")).toBeInTheDocument();
  });

  it("reports the share that arrived without a manual search", async () => {
    mock({ totals: { downloads: 100, automaticPct: 94.5 } });
    render();

    expect(await screen.findByText("94.5%")).toBeInTheDocument();
    expect(screen.getByText(/no one had to search/i)).toBeInTheDocument();
  });

  it("says nothing yet when no day had a download", async () => {
    render();

    expect(await screen.findByText(/nothing yet/i)).toBeInTheDocument();
  });

  it("warns that removing media retroactively deletes its history", async () => {
    // The history FKs are ON DELETE CASCADE, so older bars genuinely shrink
    // when media leaves Sonarr or Radarr. The chart has to say so.
    mock({ series: [{ date: "2026-09-14", count: 4 }] });
    render();

    expect(
      await screen.findByText(/subtitles downloaded per day/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/deletes its history/i)).toBeInTheDocument();
  });
});
