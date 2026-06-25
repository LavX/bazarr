import { describe, expect, it } from "vitest";
import { buildEpisodeSubtitleToolSelections } from "./components";

describe("buildEpisodeSubtitleToolSelections", () => {
  it("carries arr_instance_id for existing subtitle tool actions", () => {
    const selections = buildEpisodeSubtitleToolSelections({
      episodeId: 80,
      arrInstanceId: 3,
      missing: false,
      subtitle: {
        code2: "en",
        path: "/series/show.en.srt",
        forced: false,
        hi: false,
      } as Subtitle,
    });

    expect(selections).toEqual([
      {
        id: 80,
        type: "episode",
        path: "/series/show.en.srt",
        language: "en",
        forced: "False",
        hi: "False",
        from_language: undefined,
        arr_instance_id: 3,
      },
    ]);
  });

  it("carries arr_instance_id for embedded episode track translation", () => {
    const selections = buildEpisodeSubtitleToolSelections({
      episodeId: 80,
      arrInstanceId: 3,
      missing: false,
      subtitle: {
        code2: "ja",
        path: null,
        forced: false,
        hi: true,
      } as Subtitle,
    });

    expect(selections[0]).toMatchObject({
      id: 80,
      type: "episode",
      path: "",
      language: "ja",
      from_language: "ja",
      arr_instance_id: 3,
    });
  });
});
