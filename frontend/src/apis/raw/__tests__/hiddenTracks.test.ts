/* eslint-disable camelcase */

/**
 * The hidden-tracks listing of a whole series or league page. Each id adds
 * about 21 characters to the URL, so a long series in one request would pass
 * the 8 KB request line of a stock nginx reverse proxy and fail with a 414.
 * The client asks in batches instead and merges the answers.
 */

import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import api from "@/apis/raw";
import { HIDDEN_TRACKS_BATCH } from "@/apis/raw/subtitles";
import server from "@/tests/mocks/node";

describe("SubtitlesApi.hiddenTracks", () => {
  it("asks in batches and merges the answers", async () => {
    const asked: number[][] = [];
    server.use(
      http.get("/api/subtitles/embedded-hidden", ({ request }) => {
        const url = new URL(request.url);
        const ids = url.searchParams.getAll("media_id[]").map(Number);
        asked.push(ids);
        expect(url.searchParams.get("media_type")).toBe("episode");
        expect(request.url.length).toBeLessThan(4096);
        return HttpResponse.json({
          data: ids
            .filter((id) => id % 100 === 1)
            .map((id) => ({ media_id: id, language: "fr" })),
        });
      }),
    );

    const ids = Array.from({ length: 250 }, (_, index) => 10001 + index);
    const tracks = await api.subtitles.hiddenTracks("episode", ids);

    expect(asked.map((batch) => batch.length)).toEqual([
      HIDDEN_TRACKS_BATCH,
      HIDDEN_TRACKS_BATCH,
      50,
    ]);
    expect(asked.flat()).toEqual(ids);
    expect(tracks.map((track) => track.media_id)).toEqual([
      10001, 10101, 10201,
    ]);
  });
});
