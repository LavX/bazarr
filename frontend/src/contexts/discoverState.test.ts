/* eslint-disable camelcase -- API fixture fields. */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { DiscoverDraft } from "@/contexts/discoverState";
import { episodeMatchesShow, searchSelection } from "@/contexts/discoverState";
import type { MetadataEpisode } from "@/types/discover";

const episode: MetadataEpisode = {
  source: "tmdb",
  source_id: "tmdb:show:100:episode:401",
  show_id: 100,
  season_id: 201,
  id: 401,
  season: 2,
  episode: 1,
  title: "Home",
  air_date: "2026-09-01",
  imdb_id: "tt7654321",
  tvdb_id: 501,
  show_imdb_id: "tt1234567",
  show_tvdb_id: 300,
  show_title: "Northern Light",
  show_year: 2020,
  target_season: 2,
  target_episode: 1,
  numbering: "tvdb_default",
  identity_status: "resolved",
  absolute_episode: null,
  tvdb_absolute_number: null,
  mapping_updated_at: "2026-09-02",
};

const show = {
  id: 100,
  imdb_id: "tt1234567",
  tvdb_id: 300,
  title: "Northern Light",
  year: 2020,
};

/**
 * A payload that leaves a nullable key out rather than sending null for it. The
 * interface spells these fields as always present, so the cases below have to
 * spell the omission past it: representing that omission is the whole point.
 */
const wire = (overrides: Record<string, unknown>) =>
  ({ ...episode, ...overrides }) as unknown as MetadataEpisode;

function draftWith(overrides: Partial<DiscoverDraft> = {}): DiscoverDraft {
  return {
    mode: "title",
    mediaType: "episode",
    imdbId: "tt1234567",
    language: "eng",
    season: "2",
    episode: "1",
    title: "Northern Light",
    year: 2020,
    showId: 100,
    showTvdbId: 300,
    episodeIdentity: episode,
    ...overrides,
  };
}

/**
 * One table, two spellings of the show: the item's, as the picker holds it, and
 * the draft's. Both go through the same predicate, and a case is a disagreement
 * or it is not, whichever side it is asked from.
 */
const cases = [
  {
    name: "all five agree",
    episode: {},
    show: {},
    draft: {},
    matches: true,
  },
  {
    name: "no IMDb id, null against absent",
    episode: { show_imdb_id: null },
    show: { imdb_id: undefined },
    draft: null,
    matches: true,
  },
  {
    name: "no TVDB id, null against absent",
    episode: { show_tvdb_id: null },
    show: { tvdb_id: undefined },
    draft: { showTvdbId: undefined },
    matches: true,
  },
  {
    name: "no TVDB id, absent against null",
    episode: { show_tvdb_id: undefined },
    show: { tvdb_id: null },
    draft: { showTvdbId: null },
    matches: true,
  },
  {
    name: "no year, null against absent",
    episode: { show_year: null },
    show: { year: undefined },
    draft: { year: undefined },
    matches: true,
  },
  {
    name: "no year, absent against null",
    episode: { show_year: undefined },
    show: { year: null },
    // The draft spells an absent year as undefined and nothing else, so this
    // half of the pair is the predicate's alone too.
    draft: null,
    matches: true,
  },
  {
    name: "a different show id",
    episode: { show_id: 101 },
    show: {},
    draft: {},
    matches: false,
  },
  {
    name: "a different IMDb id",
    episode: { show_imdb_id: "tt9999999" },
    show: {},
    draft: {},
    matches: false,
  },
  {
    name: "a different TVDB id",
    episode: { show_tvdb_id: 999 },
    show: {},
    draft: {},
    matches: false,
  },
  {
    name: "a TVDB id on the episode side only",
    episode: { show_tvdb_id: 300 },
    show: { tvdb_id: null },
    draft: { showTvdbId: null },
    matches: false,
  },
  {
    name: "a different title",
    episode: { show_title: "Other Show" },
    show: {},
    draft: {},
    matches: false,
  },
  {
    name: "a different year",
    episode: { show_year: 2021 },
    show: {},
    draft: {},
    matches: false,
  },
];

describe("episodeMatchesShow", () => {
  it.each(cases)("holds for $name", ({ episode: e, show: s, matches }) => {
    expect(episodeMatchesShow(wire(e), { ...show, ...s })).toBe(matches);
  });
});

describe("searchSelection", () => {
  // The draft reaches the comparison only through a valid IMDb id and a valid
  // season and episode, and spells an absent year as undefined rather than
  // null, so the cases that need those spellings are the predicate's alone.
  // Every other case is asked from both sides.
  const draftable = cases.filter((row) => row.draft !== null);

  it.each(draftable)(
    "follows the predicate for $name",
    ({ episode: e, draft, matches }) => {
      const unconfirmed = searchSelection(
        draftWith({ episodeIdentity: wire(e), ...draft }),
      );
      expect(unconfirmed === null).toBe(!matches);
    },
  );

  it.each(draftable)(
    "does not let $name stand in the way of a confirmed manual search",
    ({ episode: e, draft, matches }) => {
      const selection = searchSelection(
        draftWith({
          episodeIdentity: wire(e),
          manualEntry: true,
          manualConfirmed: true,
          ...draft,
        }),
      );
      expect(selection).toMatchObject({
        media_type: "episode",
        imdb_id: "tt1234567",
        season: 2,
        episode: 1,
        manual_confirmed: true,
      });
      // An identity that fits describes the numbering the reader is sending. One
      // that does not is what the confirmation overrode, and the server rejects
      // a manual search that carries a superseded episode, so it is left out.
      expect(selection !== null && "episode_identity" in selection).toBe(
        matches,
      );
    },
  );

  it("keeps a conflicting episode blocked even when the reader confirms", () => {
    expect(
      searchSelection(
        draftWith({
          episodeIdentity: wire({ identity_status: "conflict" }),
          manualEntry: true,
          manualConfirmed: true,
        }),
      ),
    ).toBeNull();
  });
});

/**
 * One comparison, one place. These five fields were hand-copied into
 * searchSelection once already, with its own coercions, and the copy and the
 * predicate answered differently: a fix to one of them left the other wrong.
 * Naming them here, outside the predicate, is the drift coming back.
 */
describe("the five-field comparison", () => {
  it("lives only in the predicate", () => {
    const source = readFileSync("src/contexts/discoverState.ts", "utf8");
    const start = source.indexOf("export function searchSelection");
    const end = source.indexOf("export function episodeIdentityKey");
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const selection = source.slice(start, end);
    expect(selection).toContain("episodeMatchesShow(");
    expect(selection).not.toMatch(/\bshow_(?:imdb_id|tvdb_id|year)\b/);
  });
});
