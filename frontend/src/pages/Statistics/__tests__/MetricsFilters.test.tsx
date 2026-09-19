import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MetricsFilters from "@/pages/Statistics/components/MetricsFilters";
import { defaultStatisticsFilters } from "@/pages/Statistics/filters";
import { customRender, screen } from "@/tests";
import server from "@/tests/mocks/node";

const mock = (
  opts: { providers?: System.Provider[]; languages?: Language.Server[] } = {},
) => {
  server.use(
    http.get("/api/providers", () =>
      HttpResponse.json({ data: opts.providers ?? [] }),
    ),
    http.get("/api/system/languages", () =>
      HttpResponse.json(opts.languages ?? []),
    ),
  );
};

describe("Statistics > MetricsFilters", () => {
  beforeEach(() => mock());

  it("offers the timeframe, action, provider and language filters", async () => {
    customRender(
      <MetricsFilters value={defaultStatisticsFilters} onChange={vi.fn()} />,
    );

    expect(await screen.findByPlaceholderText(/time/i)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/action/i)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/provider/i)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/language/i)).toBeInTheDocument();
  });

  it("reports a new timeframe without discarding the other filters", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const provider: System.Provider = {
      name: "podnapisi",
      status: "History",
      retry: "-",
    };
    customRender(
      <MetricsFilters
        value={{ ...defaultStatisticsFilters, provider }}
        onChange={onChange}
      />,
    );

    await user.click(await screen.findByPlaceholderText(/time/i));
    await user.click(await screen.findByText("Last Week"));

    expect(onChange).toHaveBeenCalledWith({
      ...defaultStatisticsFilters,
      provider,
      timeFrame: "week",
    });
  });

  it("reports the provider chosen from download history", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    mock({
      providers: [{ name: "podnapisi", status: "History", retry: "-" }],
    });
    customRender(
      <MetricsFilters value={defaultStatisticsFilters} onChange={onChange} />,
    );

    await user.click(await screen.findByPlaceholderText(/provider/i));
    await user.click(await screen.findByText("podnapisi"));

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: expect.objectContaining({ name: "podnapisi" }),
      }),
    );
  });
});
