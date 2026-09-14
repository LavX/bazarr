/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
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
    http.get("/api/badges", () =>
      HttpResponse.json({
        episodes: 0,
        movies: 0,
        providers: 0,
        status: 0,
        sonarr_signalr: "LIVE",
        radarr_signalr: "LIVE",
        announcements: 0,
      }),
    ),
    http.get("/api/system/status", () =>
      HttpResponse.json({ data: { start_time: 1, database_engine: "SQLite" } }),
    ),
    http.get("/api/system/health", () => HttpResponse.json({ data: [] })),
    http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
    http.get("/api/system/tasks", () => HttpResponse.json({ data: [] })),
    http.get("/api/providers", () => HttpResponse.json({ data: [] })),
    http.get("/api/provider-hub/providers", () =>
      HttpResponse.json({ data: [] }),
    ),
    http.get("/api/provider-hub/jobs", () => HttpResponse.json({ data: [] })),
    http.get("/api/provider-hub/catalog", () =>
      HttpResponse.json({ sources: [], entries: [] }),
    ),
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

  it("opens on the overview tab", async () => {
    customRender(<StatisticsView />);

    expect(
      await screen.findByRole("tab", { name: /overview/i }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("shows a tab for each available section", async () => {
    customRender(<StatisticsView />);

    expect(
      await screen.findByRole("tab", { name: /overview/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /providers/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /tasks/i })).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: /translator/i }),
    ).toBeInTheDocument();
  });

  it("switches to the providers tab when selected", async () => {
    const user = userEvent.setup();
    customRender(<StatisticsView />);

    await user.click(await screen.findByRole("tab", { name: /providers/i }));

    expect(
      await screen.findByText(/no providers are throttled/i),
    ).toBeInTheDocument();
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
      await screen.findByRole("tab", { name: /overview/i }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByRole("tab", { name: /distribution/i }),
      ).not.toBeInTheDocument(),
    );
  });
});
