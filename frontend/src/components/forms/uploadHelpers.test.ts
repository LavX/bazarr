import { describe, expect, it } from "vitest";
import {
  assignEpisodes,
  matchEpisode,
  shouldAutoCloseUpload,
} from "./uploadHelpers";

const ep = (season: number, episode: number, id: number) =>
  ({ season, episode, sonarrEpisodeId: id }) as unknown as Item.Episode;
const info = (filename: string, season: number, episode: number) =>
  ({ filename, season, episode }) as SubtitleInfo;

describe("matchEpisode", () => {
  const episodes = [ep(1, 1, 11), ep(1, 2, 12)];

  it("matches an episode by the file's info season/episode", () => {
    const infos = [info("show.s01e02.srt", 1, 2)];
    expect(matchEpisode("show.s01e02.srt", infos, episodes)).toBe(episodes[1]);
  });

  it("returns null when no info matches the filename", () => {
    expect(matchEpisode("unknown.srt", [], episodes)).toBeNull();
  });

  it("returns null when the info has no matching episode", () => {
    const infos = [info("x.srt", 9, 9)];
    expect(matchEpisode("x.srt", infos, episodes)).toBeNull();
  });
});

describe("assignEpisodes", () => {
  const episodes = [ep(1, 1, 11), ep(1, 2, 12)];
  const infos = [info("a.srt", 1, 1), info("b.srt", 1, 2)];

  it("fills the episode for rows that don't have one", () => {
    const rows = [{ file: { name: "a.srt" }, episode: null }];
    expect(assignEpisodes(rows, infos, episodes)[0].episode).toBe(episodes[0]);
  });

  it("preserves an episode the user already chose (no clobber on re-run)", () => {
    const manual = ep(1, 2, 12);
    const rows = [{ file: { name: "a.srt" }, episode: manual }];
    // a.srt's info points at episode 1, but the row already has episode 2 set.
    expect(assignEpisodes(rows, infos, episodes)[0].episode).toBe(manual);
  });

  it("leaves the episode null when nothing matches", () => {
    const rows = [{ file: { name: "z.srt" }, episode: null }];
    expect(assignEpisodes(rows, infos, episodes)[0].episode).toBeNull();
  });
});

describe("shouldAutoCloseUpload", () => {
  it("closes only when ready, not processing, and empty", () => {
    expect(shouldAutoCloseUpload(true, false, 0)).toBe(true);
  });

  it("does not close while extraction is in progress", () => {
    expect(shouldAutoCloseUpload(true, true, 0)).toBe(false);
  });

  it("does not close before the initial load is ready", () => {
    expect(shouldAutoCloseUpload(false, false, 0)).toBe(false);
  });

  it("does not close while rows remain", () => {
    expect(shouldAutoCloseUpload(true, false, 2)).toBe(false);
  });
});
