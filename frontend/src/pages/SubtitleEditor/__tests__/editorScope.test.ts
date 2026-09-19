import { describe, expect, it } from "vitest";
import {
  appendArrInstanceParam,
  buildEditorAutosaveKey,
  buildEditorSubtitlesUrl,
  editorBreadcrumb,
} from "@/pages/SubtitleEditor/editorScope";

describe("appendArrInstanceParam", () => {
  it("leaves unscoped editor URLs unchanged", () => {
    expect(appendArrInstanceParam("/api/editor/info?mediaId=1")).toBe(
      "/api/editor/info?mediaId=1",
    );
  });

  it("adds arr_instance_id to editor URLs without existing query params", () => {
    expect(
      appendArrInstanceParam(
        "/api/editor/hls/movie/1/0/0.000/playlist.m3u8",
        3,
      ),
    ).toBe("/api/editor/hls/movie/1/0/0.000/playlist.m3u8?arr_instance_id=3");
  });

  it("adds arr_instance_id to editor URLs with existing query params", () => {
    expect(
      appendArrInstanceParam("/api/editor/info?mediaType=movie&mediaId=1", 3),
    ).toBe("/api/editor/info?mediaType=movie&mediaId=1&arr_instance_id=3");
  });
});

describe("buildEditorSubtitlesUrl", () => {
  it("includes arr_instance_id when fetching editor subtitle lists", () => {
    expect(buildEditorSubtitlesUrl("/bazarr", "movie", "50", "secret", 3)).toBe(
      "/bazarr/api/editor/subtitles?mediaType=movie&mediaId=50&apikey=secret&arr_instance_id=3",
    );
  });
});

describe("buildEditorAutosaveKey", () => {
  it("keeps drafts separate for duplicate upstream ids in different instances", () => {
    expect(buildEditorAutosaveKey("movie", "50", "en", 1)).toBe(
      "bazarr-editor-movie-50-1-en",
    );
    expect(buildEditorAutosaveKey("movie", "50", "en", 2)).toBe(
      "bazarr-editor-movie-50-2-en",
    );
  });

  it("preserves the legacy unscoped draft key", () => {
    expect(buildEditorAutosaveKey("movie", "50", "en")).toBe(
      "bazarr-editor-movie-50-en",
    );
  });
});

describe("editorBreadcrumb", () => {
  it("sends a sports event back to its league, not to a movie", () => {
    // The old "not episode means movie" branch linked /movies/<leagueId>,
    // which is either a 404 or an unrelated film.
    expect(editorBreadcrumb("sports", 51, 42)).toEqual({
      listPath: "/sports",
      listLabel: "Sports",
      detailPath: "/sports/51?instance=42",
    });
  });

  it("keeps the sports league link usable without an instance in the URL", () => {
    expect(editorBreadcrumb("sports", 51).detailPath).toBe("/sports/51");
  });

  it("still routes episodes and movies where they always went", () => {
    expect(editorBreadcrumb("episode", 7)).toEqual({
      listPath: "/series",
      listLabel: "Series",
      detailPath: "/series/7",
    });
    expect(editorBreadcrumb("movie", 9)).toEqual({
      listPath: "/movies",
      listLabel: "Movies",
      detailPath: "/movies/9",
    });
  });

  it("omits the detail link when the media id is not known yet", () => {
    expect(editorBreadcrumb("sports", undefined).detailPath).toBeUndefined();
  });
});
