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
import { Table as TableInstance } from "@tanstack/react-table";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import Table from "@/pages/Episodes/table";
import { customRender, screen } from "@/tests";
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
});
