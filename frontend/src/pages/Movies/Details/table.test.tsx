import { describe, expect, it } from "vitest";
import { buildMovieSubtitleToolSelections } from "./table";

describe("buildMovieSubtitleToolSelections", () => {
  it("carries arr_instance_id for existing subtitle tool actions", () => {
    const selections = buildMovieSubtitleToolSelections(
      {
        radarrId: 50,
        arr_instance_id: 7,
      } as Item.Movie,
      {
        code2: "en",
        path: "/movies/movie.en.srt",
        forced: false,
        hi: false,
      } as Subtitle,
    );

    expect(selections).toEqual([
      {
        id: 50,
        type: "movie",
        path: "/movies/movie.en.srt",
        language: "en",
        forced: "False",
        hi: "False",
        from_language: undefined,
        arr_instance_id: 7,
      },
    ]);
  });

  it("carries arr_instance_id for embedded movie track translation", () => {
    const selections = buildMovieSubtitleToolSelections(
      {
        radarrId: 50,
        arr_instance_id: 7,
      } as Item.Movie,
      {
        code2: "ja",
        path: null,
        forced: false,
        hi: true,
      } as Subtitle,
    );

    expect(selections[0]).toMatchObject({
      id: 50,
      type: "movie",
      path: "",
      language: "ja",
      from_language: "ja",
      arr_instance_id: 7,
    });
  });
});
