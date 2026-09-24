import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { customRender, screen } from "@/tests";
import { buildEpisodeSubtitleToolSelections, Subtitle } from "./components";

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

describe("Subtitle badge source", () => {
  const renderBadge = (subtitle: Subtitle, missing = false) =>
    customRender(
      <Subtitle
        seriesId={1}
        episodeId={2}
        arrInstanceId={1}
        missing={missing}
        subtitle={subtitle}
        availableSubtitles={[]}
      />,
    );

  const sub = (path: string | null) =>
    ({
      name: "English",
      code2: "en",
      code3: "eng",
      path,
      forced: false,
      hi: false,
    }) as Subtitle;

  it("names the file for a subtitle on disk", async () => {
    renderBadge(sub("/tv/Show/Season 1/Show S01E08.en.srt"));
    const badge = await screen.findByTitle("File: Show S01E08.en.srt");
    await userEvent.hover(badge);
    expect(badge).toHaveTextContent(/^en$/i);
  });

  it("marks a track inside the video as embedded", async () => {
    renderBadge(sub(null));
    expect(
      await screen.findByTitle("Embedded in the video file"),
    ).toHaveTextContent(/^en$/i);
  });

  it("marks a wanted language as missing", async () => {
    renderBadge(sub(null), true);
    expect(await screen.findByTitle("Missing")).toHaveTextContent(/^en$/i);
  });
});
