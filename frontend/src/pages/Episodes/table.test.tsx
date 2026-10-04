/* eslint-disable camelcase */

/**
 * Tests for the Episodes Detail subtitle Table component.
 *
 * Why: Verifies that the whole-series episode history is mapped to per-subtitle
 * match scores shown inside the badges (mirrors the Movies Detail table): a
 * downloaded file is keyed by (episode, path), an embedded track by
 * (episode, language) from action=7 records, missing subtitles are never
 * scored, and a subtitle with no history record keeps its plain badge.
 *
 * What: Renders Table with controlled episodes/history props (a ref wrapper is
 * needed because GroupTable only draws child rows once the season group is
 * expanded) and asserts the rendered badge text.
 */

import { useRef } from "react";
import { MantineProvider } from "@mantine/core";
import { Table as TableInstance } from "@tanstack/react-table";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import Table from "@/pages/Episodes/table";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeEpisode(
  subtitles: Subtitle[],
  missing_subtitles: Subtitle[] = [],
): Item.Episode {
  return {
    id: 10,
    series_id: 1,
    sonarrSeriesId: 1,
    sonarrEpisodeId: 100,
    arr_instance_id: 1,
    title: "Pilot",
    path: "/tv/Show/Season 1/Show S01E01.mkv",
    monitored: true,
    season: 1,
    episode: 1,
    subtitles,
    missing_subtitles,
    audio_language: [],
  } as unknown as Item.Episode;
}

function makeHistoryEntry(
  overrides: Partial<History.Episode>,
): History.Episode {
  return {
    id: 10,
    history_id: 1,
    series_id: 1,
    sonarrSeriesId: 1,
    sonarrEpisodeId: 100,
    arr_instance_id: 1,
    seriesTitle: "Show",
    episodeTitle: "Pilot",
    episode_number: "1x01",
    action: 1,
    blacklisted: false,
    parsed_timestamp: "2024-01-01T00:00:00",
    timestamp: "2024-01-01T00:00:00",
    timestamp_iso: "2024-01-01T00:00:00",
    description: "Downloaded",
    upgradable: false,
    matches: [],
    dont_matches: [],
    tags: [],
    monitored: true,
    subtitles_path: "",
    ...overrides,
  } as History.Episode;
}

/** GroupTable only shows child rows once the season group is expanded, which
 *  the Table's effect does through the instance ref. */
function TableHarness({
  episodes,
  history,
}: {
  episodes: Item.Episode[];
  history?: History.Episode[];
}) {
  const ref = useRef<TableInstance<Item.Episode> | null>(null);
  return (
    <Table
      ref={ref}
      episodes={episodes}
      history={history}
      profile={undefined}
      disabled={false}
      onAllRowsExpandedChanged={() => undefined}
    />
  );
}

// The language code and the score sit in sibling nodes inside the badge, so
// match the badge label by its combined, whitespace-normalized text.
function findScoredBadge(text: RegExp) {
  return screen.findByText((_content, element) => {
    if (!element?.classList.contains("mantine-Badge-label")) return false;
    return text.test((element.textContent ?? "").replace(/\s+/g, " "));
  });
}

function setupApiMocks() {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { embedded_subs_show_desired: false } }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json([])),
  );
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe("Episodes Detail Table, subtitle scores", () => {
  const file: Subtitle = {
    code2: "en",
    name: "English",
    hi: false,
    forced: false,
    path: "/tv/Show/Season 1/Show S01E01.en.srt",
  };

  const fileHistory = (overrides: Partial<History.Episode>) =>
    makeHistoryEntry({
      subtitles_path: file.path!,
      score: "98.0%",
      ...overrides,
    });

  it.each([0, 4, 6])(
    "clears an older download score after a newer action %s",
    async (action) => {
      setupApiMocks();
      customRender(
        <TableHarness
          episodes={[makeEpisode([file])]}
          history={[
            fileHistory({ history_id: 1 }),
            // Even a scored upload/translation must clear the download score.
            fileHistory({ action, history_id: 2 }),
          ]}
        />,
      );
      const badge = await screen.findByTitle("File: Show S01E01.en.srt");
      expect(badge).toHaveTextContent(/^en$/i);
    },
  );

  it.each([1, 2, 3])(
    "shows the newest action %s score after an older replacement",
    async (action) => {
      setupApiMocks();
      customRender(
        <TableHarness
          episodes={[makeEpisode([file])]}
          history={[
            fileHistory({ action: 4, history_id: 1 }),
            fileHistory({ action, history_id: 2, score: "72.0%" }),
          ]}
        />,
      );
      expect(await findScoredBadge(/^en 72%$/i)).toBeInTheDocument();
    },
  );

  it("orders unsorted history by precise timestamp before event id", async () => {
    setupApiMocks();
    const history = [
      fileHistory({
        history_id: 99,
        timestamp_iso: "2024-01-01T00:00:00.123456",
      }),
      fileHistory({
        history_id: 1,
        timestamp_iso: "2024-01-01T00:00:00.123457",
        score: "72.0%",
      }),
    ];
    const original = [...history];
    customRender(
      <TableHarness episodes={[makeEpisode([file])]} history={history} />,
    );
    expect(await findScoredBadge(/^en 72%$/i)).toBeInTheDocument();
    expect(history).toEqual(original);
  });

  it("ignores synchronization and embedded records when scoring a file", async () => {
    setupApiMocks();
    customRender(
      <TableHarness
        episodes={[makeEpisode([file])]}
        history={[
          fileHistory({ action: 5, history_id: 3, score: "100.0%" }),
          fileHistory({
            action: 7,
            history_id: 2,
            score: "100.0%",
            language: file,
          }),
          fileHistory({ history_id: 1, score: "72.0%" }),
        ]}
      />,
    );
    expect(await findScoredBadge(/^en 72%$/i)).toBeInTheDocument();
  });

  it("does not resurrect a score when the newest download is unscored", async () => {
    setupApiMocks();
    customRender(
      <TableHarness
        episodes={[makeEpisode([file])]}
        history={[
          fileHistory({ history_id: 1 }),
          fileHistory({ history_id: 2, score: undefined }),
        ]}
      />,
    );
    expect(
      await screen.findByTitle("File: Show S01E01.en.srt"),
    ).toHaveTextContent(/^en$/i);
  });

  it("keeps scores separate for episodes sharing a subtitle path", async () => {
    setupApiMocks();
    customRender(
      <TableHarness
        episodes={[makeEpisode([file])]}
        history={[fileHistory({ id: 20 })]}
      />,
    );
    expect(
      await screen.findByTitle("File: Show S01E01.en.srt"),
    ).toHaveTextContent(/^en$/i);
  });

  it("takes the newest embedded score without mixing language variants", async () => {
    setupApiMocks();
    const embedded = { ...file, path: null, hi: true, forced: true };
    customRender(
      <TableHarness
        episodes={[makeEpisode([embedded])]}
        history={[
          makeHistoryEntry({
            action: 7,
            history_id: 1,
            language: { ...file, hi: true },
            score: "72.0%",
          }),
          makeHistoryEntry({
            action: 7,
            history_id: 3,
            language: { ...file, forced: true },
            score: "50.0%",
          }),
          makeHistoryEntry({
            action: 7,
            history_id: 2,
            language: { ...file, hi: true },
            score: "100.0%",
          }),
        ]}
      />,
    );
    expect(await findScoredBadge(/100%$/i)).toBeInTheDocument();
  });

  it("scores a downloaded file badge from a matching download record", async () => {
    setupApiMocks();

    const file: Subtitle = {
      code2: "en",
      name: "English",
      hi: false,
      forced: false,
      path: "/tv/Show/Season 1/Show S01E01.en.srt",
    };

    const history = [
      makeHistoryEntry({
        action: 1,
        score: "98.0%",
        provider: "opensubtitles",
        subtitles_path: "/tv/Show/Season 1/Show S01E01.en.srt",
        language: { code2: "en", name: "English", hi: false, forced: false },
      }),
    ];

    customRender(
      <TableHarness episodes={[makeEpisode([file])]} history={history} />,
    );

    expect(await findScoredBadge(/^en 98%$/i)).toBeInTheDocument();
  });

  it("scores an embedded track badge from an action=7 record", async () => {
    setupApiMocks();

    const embedded: Subtitle = {
      code2: "fr",
      name: "French",
      hi: false,
      forced: false,
      path: null,
    };

    const history = [
      makeHistoryEntry({
        action: 7,
        score: "100.0%",
        provider: "embedded",
        subtitles_path: "",
        language: { code2: "fr", name: "French", hi: false, forced: false },
      }),
    ];

    customRender(
      <TableHarness episodes={[makeEpisode([embedded])]} history={history} />,
    );

    expect(await findScoredBadge(/^fr 100%$/i)).toBeInTheDocument();
  });

  it("leaves a badge unscored when no history record matches", async () => {
    setupApiMocks();

    const file: Subtitle = {
      code2: "en",
      name: "English",
      hi: false,
      forced: false,
      path: "/tv/Show/Season 1/Show S01E01.en.srt",
    };

    customRender(
      <TableHarness episodes={[makeEpisode([file])]} history={[]} />,
    );

    const badge = await screen.findByTitle("File: Show S01E01.en.srt");
    expect(badge).toHaveTextContent(/^en$/i);
    expect(badge).not.toHaveTextContent("%");
  });

  it("never scores a missing subtitle badge", async () => {
    setupApiMocks();

    const missing: Subtitle = {
      code2: "de",
      name: "German",
      hi: false,
      forced: false,
      path: null,
    };

    // A download record for the same episode/language must not leak onto the
    // missing badge.
    const history = [
      makeHistoryEntry({
        action: 1,
        score: "88.0%",
        provider: "opensubtitles",
        subtitles_path: "/tv/Show/Season 1/Show S01E01.de.srt",
        language: { code2: "de", name: "German", hi: false, forced: false },
      }),
    ];

    customRender(
      <TableHarness
        episodes={[makeEpisode([], [missing])]}
        history={history}
      />,
    );

    const badge = await screen.findByTitle("Missing");
    expect(badge).toHaveTextContent(/^de$/i);
    expect(badge).not.toHaveTextContent("%");
  });
  it("keeps the subtitle menu open when only scores refresh", async () => {
    setupApiMocks();
    const episodes = [makeEpisode([file])];
    const { rerender } = customRender(
      <MantineProvider env="test">
        <TableHarness
          episodes={episodes}
          history={[fileHistory({ score: "98.0%" })]}
        />
      </MantineProvider>,
    );
    await findScoredBadge(/^en 98%$/i);
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    await userEvent.click(await findScoredBadge(/^en 98%$/i));
    const menu = await screen.findByRole("menu");
    rerender(
      <MantineProvider env="test">
        <TableHarness
          episodes={episodes}
          history={[fileHistory({ score: "72.0%" })]}
        />
      </MantineProvider>,
    );
    expect(await findScoredBadge(/^en 72%$/i)).toBeInTheDocument();
    expect(screen.getByRole("menu")).toBe(menu);
  });

  it("closes a removed episode's menu instead of transferring it to the next row", async () => {
    setupApiMocks();
    const first = makeEpisode([file]);
    const nextFile = { ...file, path: "/tv/Show/Season 1/Show S01E02.en.srt" };
    const second = {
      ...makeEpisode([nextFile]),
      id: 20,
      sonarrEpisodeId: 200,
      episode: 2,
    };
    const view = (episodes: Item.Episode[]) => (
      <MantineProvider env="test">
        <TableHarness episodes={episodes} />
      </MantineProvider>
    );
    const { rerender } = customRender(view([first, second]));
    const badge = await screen.findByTitle("File: Show S01E01.en.srt");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    await userEvent.click(badge);
    await screen.findByRole("menu");
    rerender(view([second]));
    expect(
      await screen.findByTitle("File: Show S01E02.en.srt"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("keeps a surviving episode's menu when another row is removed", async () => {
    setupApiMocks();
    const first = makeEpisode([file]);
    const nextFile = { ...file, path: "/tv/Show/Season 1/Show S01E02.en.srt" };
    const second = {
      ...makeEpisode([nextFile]),
      id: 20,
      sonarrEpisodeId: 200,
      episode: 2,
    };
    const view = (episodes: Item.Episode[]) => (
      <MantineProvider env="test">
        <TableHarness episodes={episodes} />
      </MantineProvider>
    );
    const { rerender } = customRender(view([first, second]));
    const badge = await screen.findByTitle("File: Show S01E02.en.srt");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    await userEvent.click(badge);
    const menu = await screen.findByRole("menu");
    rerender(view([second]));
    expect(screen.getByRole("menu")).toBe(menu);
    expect(screen.getByTitle("File: Show S01E02.en.srt")).toHaveAttribute(
      "data-variant",
      "highlight",
    );
  });

  it("keeps the menu on the same file when subtitles are reordered", async () => {
    setupApiMocks();
    const otherFile = {
      ...file,
      path: "/tv/Show/Season 1/Show S01E01.other.en.srt",
    };
    const view = (subtitles: Subtitle[]) => (
      <MantineProvider env="test">
        <TableHarness episodes={[makeEpisode(subtitles)]} />
      </MantineProvider>
    );
    const { rerender } = customRender(view([file, otherFile]));
    const badge = await screen.findByTitle("File: Show S01E01.en.srt");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    await userEvent.click(badge);
    const menu = await screen.findByRole("menu");
    rerender(view([otherFile, file]));
    expect(screen.getByRole("menu")).toBe(menu);
    expect(screen.getByTitle("File: Show S01E01.en.srt")).toHaveAttribute(
      "data-variant",
      "highlight",
    );
    expect(
      screen.getByTitle("File: Show S01E01.other.en.srt"),
    ).not.toHaveAttribute("data-variant", "highlight");
  });

  it("removes repeated embedded language tracks without leaving stale badges", async () => {
    setupApiMocks();
    const english = { ...file, path: null };
    const german = { ...english, code2: "de", name: "German" };
    const view = (subtitles: Subtitle[]) => (
      <MantineProvider env="test">
        <TableHarness episodes={[makeEpisode(subtitles)]} />
      </MantineProvider>
    );
    const { rerender } = customRender(view([english, { ...english }, german]));
    await screen.findAllByTitle("Embedded in the video file");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(screen.getAllByTitle("Embedded in the video file")).toHaveLength(3);

    rerender(view([german]));
    expect(
      screen
        .getAllByTitle("Embedded in the video file")
        .map((badge) => badge.textContent),
    ).toEqual(["de"]);

    rerender(view([german, english, { ...english }]));
    expect(
      screen
        .getAllByTitle("Embedded in the video file")
        .map((badge) => badge.textContent),
    ).toEqual(["de", "en", "en"]);
    rerender(view([english, german]));
    expect(
      screen
        .getAllByTitle("Embedded in the video file")
        .map((badge) => badge.textContent),
    ).toEqual(["en", "de"]);
  });

  it("closes a menu when its file is replaced by a different path", async () => {
    setupApiMocks();
    const replacement = {
      ...file,
      path: "/tv/Show/Season 1/Show S01E01.other.en.srt",
    };
    const view = (subtitle: Subtitle) => (
      <MantineProvider env="test">
        <TableHarness episodes={[makeEpisode([subtitle])]} />
      </MantineProvider>
    );
    const { rerender } = customRender(view(file));
    const badge = await screen.findByTitle("File: Show S01E01.en.srt");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    await userEvent.click(badge);
    await screen.findByRole("menu");
    rerender(view(replacement));
    expect(
      await screen.findByTitle("File: Show S01E01.other.en.srt"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });
});
