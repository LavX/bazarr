import { describe, expect, it } from "vitest";
import {
  appendArrInstanceParam,
  buildEditorAutosaveKey,
  buildEditorSubtitlesUrl,
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
