/* eslint-disable camelcase -- API fixture and transport field names. */
import { describe, expect, it } from "vitest";
import type { SeerrMediaState, SeerrRequestStatus } from "@/types/seerr";
import { seerrClause, seerrIdentity } from "./seerrTitle";

const base: SeerrMediaState = {
  configured: true,
  known: true,
  status: "unknown",
  status_4k: "unknown",
  request: null,
  seasons: [],
  requestable: false,
  requestable_4k: false,
  partial_requests: true,
  special_episodes: false,
  link: null,
};

const request = (status: SeerrRequestStatus) => ({
  id: 1,
  status,
  is4k: false,
  seasons: [],
});

describe("seerrClause", () => {
  it.each([
    ["pending", null, "Awaiting approval in Seerr"],
    ["processing", null, "Processing in Seerr"],
    ["partially_available", null, "Some seasons available in Seerr"],
    ["blocklisted", null, "Blocklisted in Seerr"],
    ["unknown", null, null],
    ["deleted", null, null],
    ["unknown", "pending", "Awaiting approval in Seerr"],
    ["unknown", "declined", "Declined in Seerr"],
    ["unknown", "failed", "Failed in Seerr"],
    ["unknown", "approved", "Processing in Seerr"],
  ] as const)(
    "reads status %s with request %s as %s",
    (status, req, clause) => {
      const state = {
        ...base,
        status,
        request: req ? request(req) : null,
      } as SeerrMediaState;
      expect(seerrClause(state, false)).toBe(clause);
    },
  );

  it("lets the media row outrank a request Seerr left approved", () => {
    // Seerr does not tidy a finished request away, so an available title
    // routinely still carries an APPROVED row. Reading the request first
    // reported "processing" over a title that is ready, and hid "some seasons
    // available" (with its seasons flow) behind the same stale row.
    const approved = request("approved");
    expect(
      seerrClause({ ...base, status: "available", request: approved }, false),
    ).toBe("Ready in Seerr");
    expect(
      seerrClause(
        { ...base, status: "partially_available", request: approved },
        true,
      ),
    ).toBe("Some seasons available in Seerr");
  });

  it("keeps a live request's answer over a fully available series", () => {
    // Seerr does not recompute a series' status when a new season is
    // requested, so "available" plus a pending request row is the normal
    // shape of "the reader just asked for season 9". This clause is the only
    // place that answer appears.
    expect(
      seerrClause(
        { ...base, status: "available", request: request("pending") },
        true,
      ),
    ).toBe("Awaiting approval in Seerr");
    expect(
      seerrClause(
        { ...base, status: "available", request: request("declined") },
        true,
      ),
    ).toBe("Declined in Seerr");
  });

  it("drops the ready clause for a title the library already holds", () => {
    // "In your library, ready in Seerr" is one fact stated twice. Said of a
    // title the library does not hold, the two halves genuinely disagree and
    // both are worth reading.
    expect(seerrClause({ ...base, status: "available" }, true)).toBeNull();
    expect(seerrClause({ ...base, status: "available" }, false)).toBe(
      "Ready in Seerr",
    );
  });
});

describe("seerrIdentity", () => {
  it.each([
    [null, null],
    [{ source: "omdb", media_type: "movie", id: "tt0137523" }, null],
    [
      { source: "tmdb", media_type: "movie", id: 550 },
      { kind: "movie", tmdbId: 550 },
    ],
    [
      { source: "tmdb", media_type: "show", id: 1399 },
      { kind: "tv", tmdbId: 1399 },
    ],
    [
      { source: "local", media_type: "show", id: 7, tvdb_id: 121361 },
      { kind: "tv", tvdbId: 121361 },
    ],
    [
      { source: "local", media_type: "movie", id: 42, tmdb_id: 550 },
      { kind: "movie", tmdbId: 550 },
    ],
    // A local movie with no usable TMDB id: Seerr is TMDB-keyed throughout,
    // so there is nothing to ask it about.
    [{ source: "local", media_type: "movie", id: 42, tmdb_id: 0 }, null],
  ])("maps %o to %o", (title, identity) => {
    expect(seerrIdentity(title as never)).toEqual(identity);
  });
});
