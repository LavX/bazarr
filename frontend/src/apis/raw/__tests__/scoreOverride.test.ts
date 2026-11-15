/* eslint-disable camelcase */

/**
 * The manual score call names the movie or the episode by its own field, and
 * sends exactly one of them, as the endpoint requires.
 */

import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import api from "@/apis/raw";
import server from "@/tests/mocks/node";

async function capture(call: () => Promise<void>) {
  let body: FormData | undefined;
  server.use(
    http.post("/api/subtitles/score-override", async ({ request }) => {
      body = await request.formData();
      return new HttpResponse(null, { status: 204 });
    }),
  );
  await call();
  return body!;
}

describe("SubtitlesApi.setScoreOverride", () => {
  it("sends a movie by its radarr id", async () => {
    const body = await capture(() =>
      api.subtitles.setScoreOverride({
        type: "movie",
        id: 42,
        path: "/movies/a.en.srt",
        score: 85,
        arr_instance_id: 3,
      }),
    );

    expect(body.get("radarrMovieId")).toBe("42");
    expect(body.has("sonarrEpisodeId")).toBe(false);
    expect(body.get("subtitlesPath")).toBe("/movies/a.en.srt");
    expect(body.get("score")).toBe("85");
    expect(body.get("arr_instance_id")).toBe("3");
  });

  it("sends an episode by its sonarr episode id and keeps a zero score", async () => {
    const body = await capture(() =>
      api.subtitles.setScoreOverride({
        type: "episode",
        id: 7,
        path: "/tv/e.en.srt",
        score: 0,
      }),
    );

    expect(body.get("sonarrEpisodeId")).toBe("7");
    expect(body.has("radarrMovieId")).toBe(false);
    expect(body.get("score")).toBe("0");
    expect(body.has("arr_instance_id")).toBe(false);
  });
});
