/* eslint-disable camelcase */

import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { HttpResponse } from "msw";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import WantedSeriesView from ".";

describe("Wanted Series", () => {
  it("should render with wanted series", async () => {
    const mockData = {
      data: [
        {
          id: 101,
          series_id: 201,
          sonarrSeriesId: 1,
          sonarrEpisodeId: 101,
          seriesTitle: "Breaking Bad",
          episode_number: "S01E01",
          episodeTitle: "Pilot",
          missing_subtitles: [
            {
              code2: "en",
              name: "English",
              hi: false,
              forced: false,
            },
          ],
        },
      ],
      total: 1,
      page: 1,
      per_page: 10,
    };

    server.use(
      http.get("/api/episodes/wanted", () => {
        return HttpResponse.json(mockData);
      }),
    );

    customRender(<WantedSeriesView />);

    await screen.findByText("Breaking Bad");
    expect(screen.getByText("Name")).toBeInTheDocument();
    expect(screen.getByText("Episode")).toBeInTheDocument();
    expect(screen.getByText("Missing")).toBeInTheDocument();
    expect(screen.getByText("Breaking Bad")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Breaking Bad" })).toHaveAttribute(
      "href",
      "/series/201",
    );
    expect(screen.getByText("S01E01")).toBeInTheDocument();
    expect(screen.getByText("Pilot")).toBeInTheDocument();
  });

  it("should flag an episode with a release type mismatch", async () => {
    server.use(
      http.get("/api/episodes/wanted", () => {
        return HttpResponse.json({
          data: [
            {
              id: 101,
              series_id: 201,
              sonarrSeriesId: 1,
              sonarrEpisodeId: 101,
              seriesTitle: "Breaking Bad",
              episode_number: "S01E01",
              episodeTitle: "Pilot",
              missing_subtitles: [],
              release_mismatch: true,
            },
            {
              id: 102,
              series_id: 201,
              sonarrSeriesId: 1,
              sonarrEpisodeId: 102,
              seriesTitle: "Better Call Saul",
              episode_number: "S01E01",
              episodeTitle: "Uno",
              missing_subtitles: [],
              release_mismatch: false,
            },
          ],
          total: 2,
          page: 1,
          per_page: 10,
        });
      }),
    );

    customRender(<WantedSeriesView />);

    await screen.findByText("Breaking Bad");
    expect(screen.getAllByText("Release mismatch")).toHaveLength(1);
  });

  it("clears the title search from the button inside the field", async () => {
    const episode = (id: number, seriesTitle: string) => ({
      id,
      series_id: id,
      sonarrSeriesId: id,
      sonarrEpisodeId: id,
      seriesTitle,
      episode_number: "S01E01",
      episodeTitle: "Pilot",
      missing_subtitles: [
        { code2: "en", name: "English", hi: false, forced: false },
      ],
    });
    server.use(
      http.get("/api/episodes/wanted", () =>
        HttpResponse.json({
          data: [episode(1, "Breaking Bad"), episode(2, "The Expanse")],
          total: 2,
        }),
      ),
    );
    const user = userEvent.setup();

    customRender(<WantedSeriesView />);
    await screen.findByText("The Expanse");

    const search = screen.getByPlaceholderText("Search by title...");
    await user.type(search, "Breaking");
    await waitFor(() =>
      expect(screen.queryByText("The Expanse")).not.toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: "Clear search" }));

    expect(search).toHaveValue("");
    expect(await screen.findByText("The Expanse")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Clear search" }),
    ).not.toBeInTheDocument();
  });

  it("should render empty state when no wanted series", async () => {
    server.use(
      http.get("/api/episodes/wanted", () => {
        return HttpResponse.json({
          data: [],
          total: 0,
          page: 1,
          per_page: 10,
        });
      }),
    );

    customRender(<WantedSeriesView />);

    await screen.findByText(/No missing Series subtitles/i);
  });
});
