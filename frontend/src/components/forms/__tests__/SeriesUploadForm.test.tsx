/* eslint-disable camelcase */
/**
 * Tests for the SeriesUploadForm component.
 *
 * Focus: which series' episodes the upload modal offers.
 *
 * A series row carries two identifiers: the canonical local `id` and the
 * upstream `sonarrSeriesId`, which is no longer globally unique (see
 * SeriesIdType in types/api.d.ts). The episodes endpoint used by this form is
 * keyed on the LOCAL id. Passing the upstream id resolves either to nothing or,
 * worse, to a different series' episodes, which the filename auto-matcher will
 * then happily select from.
 *
 * Strategy: vi.mock replaces useEpisodesBySeriesId with a fake backed by a map
 * keyed on the local series id, so the component's own argument decides which
 * episodes come back. Assertions are on the episodes the user is offered, not on
 * how the hook was called, so a refactor that keeps the behaviour correct stays
 * green.
 *
 * Fixture values are taken from a real installation where the two identifiers
 * had drifted: "Room 104" holds local id 559, while "Love me now" holds local id
 * 560 and upstream id 559. Reading "Love me now" by its upstream id therefore
 * lands on "Room 104".
 */

import { describe, expect, it, vi } from "vitest";
import SeriesUploadForm from "@/components/forms/SeriesUploadForm";
import { customRender, screen, waitFor } from "@/tests";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeEpisode(
  localId: number,
  seriesLocalId: number,
  sonarrEpisodeId: number,
  season: number,
  episode: number,
  title: string,
): Item.Episode {
  return {
    id: localId,
    series_id: seriesLocalId,
    sonarrSeriesId: 0,
    sonarrEpisodeId,
    season,
    episode,
    title,
    path: `/tv/${title}.mkv`,
    monitored: true,
    subtitles: [],
    missing_subtitles: [],
    sceneName: null,
    audio_language: [],
  } as unknown as Item.Episode;
}

/** "Room 104" episodes. Local series id 559. */
const ROOM_104_EPISODES = [
  makeEpisode(9001, 559, 79001, 1, 1, "Ralphie"),
  makeEpisode(9002, 559, 79002, 1, 2, "Pizza Boy"),
];

/** "Love me now" episodes. Local series id 560, upstream id 559. */
const LOVE_ME_NOW_EPISODES = [
  makeEpisode(9101, 560, 79101, 1, 1, "First Sight"),
  makeEpisode(9102, 560, 79102, 1, 2, "Second Chance"),
];

const EPISODES_BY_LOCAL_SERIES_ID: Record<number, Item.Episode[]> = {
  559: ROOM_104_EPISODES,
  560: LOVE_ME_NOW_EPISODES,
};

/** "Love me now": local id and upstream id deliberately differ. */
function makeSeries(): Item.Series {
  return {
    id: 560,
    sonarrSeriesId: 559,
    arr_instance_id: 1,
    title: "Love me now",
    path: "/tv/Love me now",
    profileId: 1,
    fanart: "",
    overview: "",
    imdbId: "tt0000002",
    alternativeTitles: [],
    poster: "",
    year: "2023",
    episodeFileCount: 2,
    episodeMissingCount: 0,
    ended: false,
    lastAired: "2023-01-01",
    seriesType: "standard",
    tvdbId: 1234,
    monitored: true,
    tags: [],
    audio_language: [],
  } as unknown as Item.Series;
}

function makeFile() {
  return new File(["1\n00:00:01,000 --> 00:00:02,000\nhi\n"], "S01E01.srt", {
    type: "application/x-subrip",
  });
}

// ---------------------------------------------------------------------------
// Module mocks
// ---------------------------------------------------------------------------

vi.mock("@/apis/hooks", async (importActual) => {
  const actual = await importActual<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useEpisodesBySeriesId: vi.fn(),
    useEpisodeSubtitleModification: vi.fn(),
    useSubtitleInfos: vi.fn(),
  };
});

vi.mock("@/utilities/languages", async (importActual) => {
  const actual = await importActual<typeof import("@/utilities/languages")>();
  return {
    ...actual,
    useLanguageProfileBy: vi.fn(),
    useProfileItemsToLanguages: vi.fn(),
  };
});

async function setupHooks() {
  const {
    useEpisodesBySeriesId,
    useEpisodeSubtitleModification,
    useSubtitleInfos,
  } = await import("@/apis/hooks");
  const { useLanguageProfileBy, useProfileItemsToLanguages } =
    await import("@/utilities/languages");

  // The fake resolves episodes by LOCAL series id. Whatever the component
  // passes is what it gets back, so a wrong identifier surfaces as wrong
  // episodes rather than as a failed assertion on a call argument.
  vi.mocked(useEpisodesBySeriesId).mockImplementation(
    (id: number) =>
      ({
        data: EPISODES_BY_LOCAL_SERIES_ID[id] ?? [],
      }) as unknown as ReturnType<typeof useEpisodesBySeriesId>,
  );

  vi.mocked(useEpisodeSubtitleModification).mockReturnValue({
    upload: { mutateAsync: vi.fn(), isPending: false },
  } as unknown as ReturnType<typeof useEpisodeSubtitleModification>);

  // The uploaded file is recognised as season 1, episode 1. This is what drives
  // the auto-matcher, and the auto-matcher is the dangerous path: it selects an
  // episode from whatever list the form fetched, and submission only rejects an
  // unset episode, never one belonging to another series.
  vi.mocked(useSubtitleInfos).mockReturnValue({
    data: [{ filename: "S01E01.srt", season: 1, episode: 1 }],
  } as unknown as ReturnType<typeof useSubtitleInfos>);

  vi.mocked(useLanguageProfileBy).mockReturnValue({
    profileId: 1,
    name: "English",
    items: [],
    mustContain: [],
    mustNotContain: [],
    originalFormat: false,
    cutoff: null,
    tag: undefined,
  } as unknown as Language.Profile);

  vi.mocked(useProfileItemsToLanguages).mockReturnValue([
    { code2: "en", name: "English", forced: false, hi: false },
  ] as Language.Info[]);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("SeriesUploadForm episode resolution", () => {
  it("offers episodes of the opened series when its local and upstream ids differ", async () => {
    await setupHooks();

    customRender(
      <SeriesUploadForm series={makeSeries()} files={[makeFile()]} />,
    );

    // The auto-matcher resolves S01E01 against the fetched episode list, so the
    // selected episode is the observable proof of which series was fetched.
    await waitFor(() => {
      expect(screen.getByDisplayValue("(1x1) First Sight")).toBeInTheDocument();
    });
  });

  it("does not offer another series' episodes", async () => {
    await setupHooks();

    customRender(
      <SeriesUploadForm series={makeSeries()} files={[makeFile()]} />,
    );

    await waitFor(() => {
      expect(screen.getByDisplayValue("(1x1) First Sight")).toBeInTheDocument();
    });

    // "Room 104" owns local id 559, which is "Love me now"'s upstream id. Its
    // episodes must never be offered here, and must never be auto-selected:
    // submission only rejects an unset episode, so a wrong one uploads silently.
    expect(screen.queryByDisplayValue(/Ralphie/)).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue(/Pizza Boy/)).not.toBeInTheDocument();
  });
});
