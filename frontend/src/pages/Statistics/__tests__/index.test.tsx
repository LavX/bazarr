/* eslint-disable camelcase */
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import StatisticsView from "@/pages/Statistics";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const distSettings = (enabled: boolean) => ({
  enabled,
  consent: true,
  search_timeout_seconds: 20,
  search_rate_limit_enabled: true,
  usage_retention_days: 30,
  default_tier: "free",
  downloads_per_window: 0,
  downloads_window_seconds: 0,
  serve_local_subs: true,
  has_token: true,
});

const mock = (opts: { distEnabled?: boolean } = {}) => {
  server.use(
    http.get("/api/history/stats", () =>
      HttpResponse.json({ series: [], movies: [], sports: [] }),
    ),
    http.get("/api/providers", () => HttpResponse.json({ data: [] })),
    http.get("/api/history/metrics", () =>
      HttpResponse.json({
        totals: {
          downloads: 0,
          series: 0,
          movies: 0,
          sports: 0,
          dailyAverage: 0,
          peakDate: null,
          peakCount: 0,
          automaticPct: 0,
        },
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
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/distribution-hub/settings", () =>
      HttpResponse.json(distSettings(opts.distEnabled ?? true)),
    ),
    http.get("/api/distribution-hub/stats/overview", () =>
      HttpResponse.json({
        totals: {
          today: { search: 0, download: 0 },
          d7: { search: 0, download: 0 },
          d30: { search: 0, download: 0 },
        },
        blocked_30d: 0,
        key_count: 0,
        enabled_count: 0,
        active_count: 0,
        top_keys: [],
      }),
    ),
    http.get("/api/distribution-hub/stats/timeseries", () =>
      HttpResponse.json({ range_days: 30, series: [] }),
    ),
    http.get("/api/translator/status", () => HttpResponse.error()),
    http.get("/api/translator/jobs", () => HttpResponse.error()),
  );
};

describe("Statistics page", () => {
  beforeEach(() => mock());

  it("opens on the activity tab", async () => {
    customRender(<StatisticsView />);

    expect(
      await screen.findByRole("tab", { name: /activity/i }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("offers only chart-bearing tabs", async () => {
    customRender(<StatisticsView />);

    // Await the gated tab: Distribution appears only once dist settings load.
    await screen.findByRole("tab", { name: /distribution/i });
    const tabs = screen.getAllByRole("tab").map((t) => t.textContent?.trim());
    expect(tabs).toEqual([
      "Activity",
      "Providers",
      "Quality",
      "Distribution",
      "Translator",
    ]);
  });

  it("does not re-implement the System status pages", async () => {
    // Uptime, health and the scheduler board each already have a dedicated
    // System page. Duplicating them here is what made this read as a status
    // page rather than a statistics one, so assert on that content, not on
    // tab names: there is a legitimate Providers tab now, about download
    // counts and quality rather than throttle state.
    customRender(<StatisticsView />);

    await screen.findByRole("tab", { name: /activity/i });
    expect(screen.queryByText(/uptime/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/health issues/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/next run/i)).not.toBeInTheDocument();
  });

  it("keeps one filter row shared across the history-driven tabs", async () => {
    customRender(<StatisticsView />);

    // Exactly one of each, rendered by the page rather than per panel.
    expect(await screen.findByPlaceholderText(/time/i)).toBeInTheDocument();
    expect(screen.getAllByPlaceholderText(/provider/i)).toHaveLength(1);
  });

  it("offers the distribution tab while the endpoint is enabled", async () => {
    customRender(<StatisticsView />);

    expect(
      await screen.findByRole("tab", { name: /distribution/i }),
    ).toBeInTheDocument();
  });

  it("hides the distribution tab when the endpoint is disabled", async () => {
    // compat_usage stays empty while the endpoint is off, so the chart would
    // be a flat zero line that reads as "usage stopped".
    mock({ distEnabled: false });
    customRender(<StatisticsView />);

    expect(
      await screen.findByRole("tab", { name: /activity/i }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByRole("tab", { name: /distribution/i }),
      ).not.toBeInTheDocument(),
    );
  });
});
