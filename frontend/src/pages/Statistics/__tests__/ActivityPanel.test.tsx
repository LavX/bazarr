import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ActivityPanel from "@/pages/Statistics/ActivityPanel";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const onStats = vi.fn();

const mock = (
  opts: {
    series?: { date: string; count: number }[];
    movies?: { date: string; count: number }[];
    providers?: System.Provider[];
    languages?: Language.Server[];
  } = {},
) => {
  server.use(
    http.get("/api/history/stats", ({ request }) => {
      const url = new URL(request.url);
      onStats(Object.fromEntries(url.searchParams));
      return HttpResponse.json({
        series: opts.series ?? [],
        movies: opts.movies ?? [],
      });
    }),
    http.get("/api/providers", () =>
      HttpResponse.json({ data: opts.providers ?? [] }),
    ),
    http.get("/api/system/languages", () =>
      HttpResponse.json(opts.languages ?? []),
    ),
  );
};

describe("Statistics > ActivityPanel", () => {
  beforeEach(() => {
    onStats.mockClear();
    mock();
  });

  it("offers the timeframe, action, provider and language filters", async () => {
    customRender(<ActivityPanel />);

    expect(await screen.findByPlaceholderText(/time/i)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/action/i)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/provider/i)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/language/i)).toBeInTheDocument();
  });

  it("requests the last month by default", async () => {
    customRender(<ActivityPanel />);

    await waitFor(() => expect(onStats).toHaveBeenCalled());
    expect(onStats.mock.calls[0][0]).toMatchObject({ timeFrame: "month" });
  });

  it("re-requests when the timeframe changes", async () => {
    const user = userEvent.setup();
    customRender(<ActivityPanel />);

    await waitFor(() => expect(onStats).toHaveBeenCalled());
    await user.click(screen.getByPlaceholderText(/time/i));
    await user.click(await screen.findByText("Last Week"));

    await waitFor(() =>
      expect(onStats.mock.calls.some((c) => c[0].timeFrame === "week")).toBe(
        true,
      ),
    );
  });

  it("filters by the chosen provider", async () => {
    const user = userEvent.setup();
    mock({ providers: [{ name: "podnapisi", status: "History", retry: "-" }] });
    customRender(<ActivityPanel />);

    await waitFor(() => expect(onStats).toHaveBeenCalled());
    await user.click(screen.getByPlaceholderText(/provider/i));
    await user.click(await screen.findByText("podnapisi"));

    await waitFor(() =>
      expect(
        onStats.mock.calls.some((c) => c[0].provider === "podnapisi"),
      ).toBe(true),
    );
  });

  it("warns that removing media retroactively deletes its history", async () => {
    // The history FKs are ON DELETE CASCADE, so older bars genuinely shrink
    // when media leaves Sonarr or Radarr. The chart has to say so.
    // (Chart internals are not asserted: recharts renders nothing in jsdom
    // because ResponsiveContainer measures a zero-size container.)
    mock({
      series: [{ date: "2026-09-14", count: 4 }],
      movies: [{ date: "2026-09-14", count: 2 }],
    });
    customRender(<ActivityPanel />);

    expect(
      await screen.findByText(/subtitles downloaded per day/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/deletes its history/i)).toBeInTheDocument();
  });

  it("renders without stats", async () => {
    customRender(<ActivityPanel />);

    expect(await screen.findByPlaceholderText(/time/i)).toBeInTheDocument();
  });
});
