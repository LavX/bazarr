import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { defaultStatisticsFilters } from "@/pages/Statistics/filters";
import QualityPanel from "@/pages/Statistics/QualityPanel";
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
  customRender(<QualityPanel filters={defaultStatisticsFilters} />);

describe("Statistics > QualityPanel", () => {
  beforeEach(() => mock());

  it("labels the overflow bucket as a hash match rather than clipping it", async () => {
    // A hash match scores 359 on top of other matches against a 360
    // denominator, so over 100% is legitimate and needs its own label.
    mock({
      scoreHistogram: [
        { bucket: 9, count: 4 },
        { bucket: 10, count: 7 },
      ],
    });
    render();

    expect(await screen.findByText(/100%\+/)).toBeInTheDocument();
    expect(screen.getByText(/hash match/i)).toBeInTheDocument();
  });

  it("names each language by its download count", async () => {
    mock({
      byLanguage: [
        { language: "en", count: 120 },
        { language: "hu", count: 34 },
      ],
    });
    render();

    expect(await screen.findByText("en")).toBeInTheDocument();
    expect(screen.getByText("hu")).toBeInTheDocument();
  });

  it("separates a hearing-impaired variant from the plain language", async () => {
    mock({
      byLanguage: [
        { language: "en", count: 10 },
        { language: "en:hi", count: 3 },
      ],
    });
    render();

    expect(await screen.findByText("en:hi")).toBeInTheDocument();
  });

  it("translates action ids into words", async () => {
    mock({
      byAction: [
        { action: 1, count: 80 },
        { action: 2, count: 12 },
        { action: 3, count: 8 },
      ],
    });
    render();

    expect(await screen.findByText("Automatic")).toBeInTheDocument();
    expect(screen.getByText("Manual")).toBeInTheDocument();
    expect(screen.getByText("Upgraded")).toBeInTheDocument();
  });

  it("says the quality figures exclude synthetic scores", async () => {
    // Uploads, embedded scans and translations carry a score that is either
    // hardcoded perfect or derived from a setting.
    mock({ scoreHistogram: [{ bucket: 5, count: 1 }] });
    render();

    expect(
      await screen.findByText(/uploads, embedded tracks and translations/i),
    ).toBeInTheDocument();
  });

  it("says so when nothing matched the filters", async () => {
    render();

    expect(await screen.findByText(/nothing scored/i)).toBeInTheDocument();
  });
});
