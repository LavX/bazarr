import { MantineProvider } from "@mantine/core";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { customRender, screen } from "@/tests";
import {
  buildEpisodeSubtitleToolSelections,
  Subtitle,
  SubtitleScoreInfo,
} from "./components";

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

describe("Subtitle badge score", () => {
  const renderBadge = (
    subtitle: Subtitle,
    scoreInfo?: SubtitleScoreInfo,
    missing = false,
  ) =>
    customRender(
      <MantineProvider env="test">
        <Subtitle
          seriesId={1}
          episodeId={2}
          arrInstanceId={1}
          missing={missing}
          subtitle={subtitle}
          availableSubtitles={[]}
          scoreInfo={scoreInfo}
        />
      </MantineProvider>,
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

  // The language code and the score sit in sibling nodes inside the badge, so
  // match the badge label by its combined, whitespace-normalized text.
  const findScoredBadge = (text: RegExp) =>
    screen.findByText((_content, element) => {
      if (!element?.classList.contains("mantine-Badge-label")) return false;
      return text.test((element.textContent ?? "").replace(/\s+/g, " "));
    });

  it("shows the score inside the badge when a history record matches", async () => {
    renderBadge(sub("/tv/Show/Season 1/Show S01E08.en.srt"), {
      score: "98.0%",
      provider: "opensubtitles",
      matches: ["hash"],
      notMatches: ["resolution"],
    });
    // Parsed percentage is appended to the language code: "en 98%".
    expect(await findScoredBadge(/^en 98%$/i)).toBeInTheDocument();
  });

  it.each([
    ["89.6%", "yellow", /^en 89\.6%$/i],
    ["69.6%", "red", /^en 69\.6%$/i],
  ])(
    "shows the %s score unrounded and keeps it in its %s band",
    async (score, colour, text) => {
      renderBadge(sub("/tv/Show/Season 1/Show S01E08.en.srt"), { score });
      expect(await findScoredBadge(text)).toBeInTheDocument();
      const badge = await screen.findByText(
        (_content, element) =>
          element?.classList.contains("mantine-Badge-root") ?? false,
      );
      expect(badge).toHaveStyle({
        color: `var(--mantine-color-${colour}-light-color)`,
      });
    },
  );

  it("leaves the badge unchanged when there is no score", async () => {
    renderBadge(sub("/tv/Show/Season 1/Show S01E08.en.srt"));
    const badge = await screen.findByTitle("File: Show S01E08.en.srt");
    expect(badge).toHaveTextContent(/^en$/i);
    expect(badge).not.toHaveTextContent("%");
  });

  it.each([
    ["98.0%", "green", "file"],
    ["72.0%", "yellow", "file"],
    ["50.0%", "red", "file"],
    ["100.0%", "green", "embedded"],
  ])(
    "highlights an open menu over the %s score colour",
    async (score, colour, source) => {
      const user = userEvent.setup();
      renderBadge(
        sub(
          source === "embedded" ? null : "/tv/Show/Season 1/Show S01E08.en.srt",
        ),
        { score },
      );
      const label = await findScoredBadge(/^en \d+%$/i);
      const badge = await screen.findByText(
        (_content, element) =>
          element?.classList.contains("mantine-Badge-root") ?? false,
      );
      expect(badge).toHaveStyle({
        color: `var(--mantine-color-${colour}-light-color)`,
      });

      await user.click(label);
      expect(await screen.findByRole("menu")).toBeInTheDocument();
      expect(badge).toHaveAttribute("data-variant", "highlight");
      expect(badge.style.color).toBe("");
      expect(badge.style.backgroundColor).toBe("");
      expect(badge.style.border).toBe("");

      await user.keyboard("{Escape}");
      expect(badge).toHaveAttribute("data-variant", "light");
      expect(badge).toHaveStyle({
        color: `var(--mantine-color-${colour}-light-color)`,
      });
    },
  );

  it("scores an embedded track badge", async () => {
    renderBadge(sub(null), {
      score: "100.0%",
      provider: "embedded",
      matches: [],
      notMatches: [],
    });
    expect(await findScoredBadge(/^en 100%$/i)).toBeInTheDocument();
  });

  it("never scores a missing badge", async () => {
    renderBadge(
      sub(null),
      {
        score: "95.0%",
        provider: "opensubtitles",
        matches: [],
        notMatches: [],
      },
      true,
    );
    const badge = await screen.findByTitle("Missing");
    expect(badge).toHaveTextContent(/^en$/i);
    expect(badge).not.toHaveTextContent("%");
  });

  it("reveals provider and match details on hover", async () => {
    renderBadge(sub("/tv/Show/Season 1/Show S01E08.en.srt"), {
      score: "72.0%",
      provider: "opensubtitles",
      matches: ["series", "year"],
      notMatches: ["resolution"],
    });
    // The tooltip content mounts only once the badge is hovered.
    expect(screen.queryByText("Provider: opensubtitles")).toBeNull();
    await userEvent.hover(await findScoredBadge(/^en 72%$/i));
    expect(
      await screen.findByText("Provider: opensubtitles"),
    ).toBeInTheDocument();
    expect(await screen.findByText("resolution")).toBeInTheDocument();
  });
});
