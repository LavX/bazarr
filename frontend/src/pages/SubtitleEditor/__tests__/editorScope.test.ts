import { describe, expect, it } from "vitest";
import {
  appendArrInstanceParam,
  buildEditorAutosaveKey,
  buildEditorSubtitlesUrl,
  createFieldsForLanguageKey,
  editorBreadcrumb,
  editorSearchWithoutSource,
  readEditorCodec,
  readEditorSource,
  subtitleEditorUrl,
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
    expect(buildEditorSubtitlesUrl("/bazarr", "movie", "50", 3)).toBe(
      "/bazarr/api/editor/subtitles?mediaType=movie&mediaId=50&arr_instance_id=3",
    );
  });

  it("never puts the API key in the URL", () => {
    // The key travels in the X-API-KEY header. In a URL it lands in proxy
    // access logs and makes the server log a deprecation warning per call.
    const url = buildEditorSubtitlesUrl("", "episode", "7");
    expect(url).toBe("/api/editor/subtitles?mediaType=episode&mediaId=7");
    expect(new URL(url, "http://bazarr.test").searchParams.has("apikey")).toBe(
      false,
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

describe("the embedded source", () => {
  it("is read only from source=embedded", () => {
    expect(readEditorSource(new URLSearchParams("source=embedded"))).toBe(
      "embedded",
    );
    expect(readEditorSource(new URLSearchParams("source=file"))).toBe(
      undefined,
    );
    expect(readEditorSource(new URLSearchParams(""))).toBe(undefined);
  });

  it("carries the row's codec, and only beside the source", () => {
    expect(
      readEditorCodec(new URLSearchParams("source=embedded&codec=ass")),
    ).toBe("ass");
    expect(readEditorCodec(new URLSearchParams("source=embedded"))).toBe(
      undefined,
    );
    expect(readEditorCodec(new URLSearchParams("codec=ass"))).toBe(undefined);
  });

  it("is dropped from the editor's own navigation, and nothing else is", () => {
    expect(
      editorSearchWithoutSource(
        new URLSearchParams("arr_instance_id=2&source=embedded"),
      ),
    ).toBe("arr_instance_id=2");
    expect(
      editorSearchWithoutSource(
        new URLSearchParams("arr_instance_id=2&source=embedded&codec=ass"),
      ),
    ).toBe("arr_instance_id=2");
    expect(editorSearchWithoutSource(new URLSearchParams(""))).toBe("");
  });

  it("keeps a draft of the track apart from a draft of the file", () => {
    expect(buildEditorAutosaveKey("movie", "50", "en", 1, "embedded")).toBe(
      "bazarr-editor-movie-50-1-en-embedded",
    );
    expect(buildEditorAutosaveKey("movie", "50", "en", 1)).toBe(
      "bazarr-editor-movie-50-1-en",
    );
  });

  it("keeps two text tracks of one key apart, codec by codec", () => {
    expect(
      buildEditorAutosaveKey("movie", "50", "en", 1, "embedded", "ass"),
    ).toBe("bazarr-editor-movie-50-1-en-embedded-ass");
    expect(
      buildEditorAutosaveKey("movie", "50", "en", 1, "embedded", "subrip"),
    ).toBe("bazarr-editor-movie-50-1-en-embedded-subrip");
  });
});

describe("createFieldsForLanguageKey", () => {
  it("splits a key into what the create route accepts", () => {
    expect(createFieldsForLanguageKey("en")).toEqual({
      language: "en",
      hi: false,
      forced: false,
    });
    expect(createFieldsForLanguageKey("en:hi")).toEqual({
      language: "en",
      hi: true,
      forced: false,
    });
    expect(createFieldsForLanguageKey("pt:forced")).toEqual({
      language: "pt",
      hi: false,
      forced: true,
    });
    expect(createFieldsForLanguageKey("zh:HI")).toEqual({
      language: "zh",
      hi: true,
      forced: false,
    });
  });

  it("saves a key with both flags as forced, the name a download would get", () => {
    expect(createFieldsForLanguageKey("en:hi:forced")).toEqual({
      language: "en",
      hi: false,
      forced: true,
    });
  });
});

describe("subtitleEditorUrl", () => {
  it("opens a file row the way it always did", () => {
    expect(subtitleEditorUrl("edit", "movie", 30, "en:hi", {})).toBe(
      "/subtitles/edit/movie/30/en%3Ahi",
    );
    expect(
      subtitleEditorUrl("preview", "episode", 20, "en", { arrInstanceId: 2 }),
    ).toBe("/subtitles/preview/episode/20/en?arr_instance_id=2");
  });

  it("carries source=embedded and the row's codec for an embedded row", () => {
    expect(
      subtitleEditorUrl("edit", "sports", 61, "fr", {
        arrInstanceId: 1,
        source: "embedded",
        codec: "subrip",
      }),
    ).toBe(
      "/subtitles/edit/sports/61/fr?arr_instance_id=1&source=embedded&codec=subrip",
    );
  });

  it("never puts a codec on a URL that is not the embedded source", () => {
    expect(
      subtitleEditorUrl("preview", "movie", 30, "en", { codec: "ass" }),
    ).toBe("/subtitles/preview/movie/30/en");
  });
});
