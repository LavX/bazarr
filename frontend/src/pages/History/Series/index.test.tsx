/* eslint-disable camelcase */

import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { HttpResponse } from "msw";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import SeriesHistoryView from ".";

describe("History Series", () => {
  it("should render with series", async () => {
    server.use(
      http.get("/api/episodes/history", () => {
        return HttpResponse.json({
          data: [
            {
              id: 4560,
              series_id: 789,
              seriesTitle: "Breaking Bad",
              episode_number: "S05E07",
              episodeTitle: "Pilot",
              language: { code2: "en", name: "English" },
              action: 1,
              provider: "subdl",
              ai_translated: true,
              timestamp: "2023-05-10",
              parsed_timestamp: "May 10, 2023",
              sonarrSeriesId: 123,
              sonarrEpisodeId: 456,
              description: "Test description",
              score: 100,
              matches: [],
              dont_matches: [],
              upgradable: false,
              blacklisted: false,
            },
          ],
          page: 1,
          totalPages: 1,
          totalItems: 1,
        });
      }),
    );

    customRender(<SeriesHistoryView />);

    await waitFor(() => {
      expect(screen.getByText("Breaking Bad")).toBeInTheDocument();
    });

    expect(screen.getByText("S05E07")).toBeInTheDocument();
    expect(screen.getByLabelText("AI-translated")).toHaveTextContent("AI");
    expect(screen.getByRole("link", { name: "Breaking Bad" })).toHaveAttribute(
      "href",
      "/series/789",
    );
  });

  it("offers each row on the media servers by its SxxEyy name, asking about the local episode id", async () => {
    let asked = "";
    server.use(
      http.get("/api/episodes/history", () =>
        HttpResponse.json({
          data: [
            {
              id: 4560,
              series_id: 789,
              seriesTitle: "Breaking Bad",
              // The history API sends season x episode.
              episode_number: "5x7",
              episodeTitle: "Pilot",
              language: { code2: "en", name: "English" },
              action: 1,
              provider: "subdl",
              ai_translated: false,
              timestamp: "2023-05-10",
              parsed_timestamp: "May 10, 2023",
              sonarrSeriesId: 123,
              sonarrEpisodeId: 456,
              description: "Test description",
              score: 100,
              matches: [],
              dont_matches: [],
              upgradable: false,
              blacklisted: false,
            },
          ],
          page: 1,
          totalPages: 1,
          totalItems: 1,
        }),
      ),
      http.get("/api/system/media-server-links/destinations", () =>
        HttpResponse.json({
          data: [
            { instance_id: "e1", kind: "emby", display_name: "Emby" },
            { instance_id: "j1", kind: "jellyfin", display_name: "Jellyfin" },
          ],
        }),
      ),
      http.get("/api/system/media-server-links/candidates", ({ request }) => {
        asked = new URL(request.url).search;
        return HttpResponse.json({ data: [] });
      }),
    );
    const user = userEvent.setup();
    customRender(<SeriesHistoryView />);
    // Named the way the episode rows name it, with the series in front.
    const trigger = await screen.findByRole("button", {
      name: "Play Breaking Bad S05E07 on a media server",
    });
    trigger.focus();
    // The history row's id is the episode's local id, not its Sonarr ids.
    await waitFor(() => expect(asked).toBe("?media_type=episode&id=4560"));
    await user.hover(trigger);
    expect(
      await screen.findByRole("tooltip", { hidden: true }, { timeout: 3000 }),
    ).toHaveTextContent("Play Breaking Bad S05E07 on a media server");
  });
});
