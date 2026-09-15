import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { defaultStatisticsFilters } from "@/pages/Statistics/filters";
import ProvidersPanel from "@/pages/Statistics/ProvidersPanel";
import { customRender, screen } from "@/tests";
import server from "@/tests/mocks/node";

const emptyMetrics: History.Metrics = {
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
};

const mock = (over: Partial<History.Metrics> = {}) => {
  server.use(
    http.get("/api/history/metrics", () =>
      HttpResponse.json({ ...emptyMetrics, ...over }),
    ),
  );
};

const render = () =>
  customRender(<ProvidersPanel filters={defaultStatisticsFilters} />);

describe("Statistics > ProvidersPanel", () => {
  beforeEach(() => mock());

  it("lists providers with their download counts", async () => {
    mock({
      byProvider: [
        { provider: "opensubtitles", count: 412, avgScorePct: 88.5 },
        { provider: "podnapisi", count: 97, avgScorePct: 74.2 },
      ],
    });
    render();

    expect(await screen.findByText("opensubtitles")).toBeInTheDocument();
    expect(screen.getByText("podnapisi")).toBeInTheDocument();
  });

  it("shows mean match quality per provider", async () => {
    mock({
      byProvider: [{ provider: "alpha", count: 10, avgScorePct: 88.5 }],
    });
    render();

    expect(await screen.findByText(/88.5%/)).toBeInTheDocument();
  });

  it("shows a dash when a provider has no scored download", async () => {
    mock({ byProvider: [{ provider: "alpha", count: 3, avgScorePct: null }] });
    render();

    expect(await screen.findByText("alpha")).toBeInTheDocument();
    expect(screen.getByText("-")).toBeInTheDocument();
  });

  it("reports the blacklist rate that tells you to drop a provider", async () => {
    mock({
      providerReliability: [
        { provider: "flaky", downloads: 412, blacklisted: 38, ratePct: 9.2 },
      ],
    });
    render();

    expect(await screen.findByText(/9.2%/)).toBeInTheDocument();
    expect(screen.getByText(/38 of 412/)).toBeInTheDocument();
  });

  it("explains what the blacklist rate measures", async () => {
    mock({
      providerReliability: [
        { provider: "flaky", downloads: 10, blacklisted: 1, ratePct: 10 },
      ],
    });
    render();

    expect(
      await screen.findByText(/share of this provider's downloads/i),
    ).toBeInTheDocument();
  });

  it("says so when nothing was downloaded in the window", async () => {
    render();

    expect(
      await screen.findByText(/no downloads in this period/i),
    ).toBeInTheDocument();
  });
});
