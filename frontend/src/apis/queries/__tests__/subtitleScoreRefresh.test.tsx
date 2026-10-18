/* eslint-disable camelcase -- API fixtures keep their transport names. */
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { useMoviesPagination, useSeriesPagination } from "@/apis/hooks";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { createDefaultReducer } from "@/modules/socketio/reducer";
import { act, customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

function MovieScore() {
  const query = useMoviesPagination(true, true);
  return (
    <output data-testid="score">
      {query.data?.data[0]?.lowest_subtitle_score}
    </output>
  );
}

function SeriesScore() {
  const query = useSeriesPagination(true, true);
  return (
    <output data-testid="score">
      {query.data?.data[0]?.lowest_subtitle_score}
    </output>
  );
}

function reducer<K extends SocketIO.EventType>(key: K) {
  const result = createDefaultReducer().find(
    (candidate): candidate is Extract<SocketIO.Reducer, { key: K }> =>
      candidate.key === key,
  );
  if (!result) throw new Error(`Missing reducer ${key}`);
  return result;
}

describe("Score-list cache after background subtitle changes", () => {
  it.each([
    ["movies", "history"],
    ["series", "history"],
    ["movies", "metadata-control"],
    ["series", "metadata-control"],
    ["series", "episode-update"],
    ["series", "episode-delete"],
  ] as const)("refreshes %s scores on %s events", async (media, event) => {
    let latestScore = 80;
    let scoreRequests = 0;
    server.use(
      http.get(`/api/${media}`, ({ request }) => {
        const withScores =
          new URL(request.url).searchParams.get("scores") === "1";
        if (withScores) scoreRequests++;
        return HttpResponse.json({
          data: [
            {
              id: 501,
              arr_instance_id: 1,
              ...(media === "series"
                ? { sonarrSeriesId: 42 }
                : { radarrId: 42 }),
              ...(withScores ? { lowest_subtitle_score: latestScore } : {}),
            },
          ],
          total: 1,
        });
      }),
    );
    // Visiting a show's detail primes this exact individual-episode cache.
    queryClient.setQueryData([QueryKeys.Episodes, 601], {
      id: 601,
      series_id: 501,
      sonarrEpisodeId: 7,
      sonarrSeriesId: 42,
      arr_instance_id: 1,
    });
    customRender(media === "series" ? <SeriesScore /> : <MovieScore />);
    await waitFor(() =>
      expect(screen.getByTestId("score")).toHaveTextContent(/^80$/),
    );
    expect(scoreRequests).toBe(1);

    latestScore = 100;
    act(() => {
      if (event === "metadata-control") {
        reducer(media === "series" ? "series" : "movie").update?.([42]);
      } else if (event === "episode-update" || event === "episode-delete") {
        // A scan can remove a subtitle, or Sonarr can delete an episode,
        // without writing subtitle history. The cached owner must not keep
        // the list's previous minimum after either event.
        reducer("episode")[event === "episode-update" ? "update" : "delete"]?.([
          601,
        ]);
      } else if (media === "movies") {
        reducer("movie-history").any?.();
      } else {
        reducer("episode").update?.([601]);
        reducer("episode-history").update?.([501]);
        reducer("episode-history").any?.();
      }
    });

    // A scored read that bypassed the scheduler would request right here,
    // before the debounce; the event may only hand it to the scheduler.
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(scoreRequests).toBe(1);

    // Includes the existing 1-second history-event debounce.
    await waitFor(
      () => expect(screen.getByTestId("score")).toHaveTextContent(/^100$/),
      { timeout: 5000 },
    );
    expect(scoreRequests).toBe(2);
  });
});
