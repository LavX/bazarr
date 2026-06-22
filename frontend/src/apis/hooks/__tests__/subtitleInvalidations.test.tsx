/**
 * Tests for subtitle mutation hooks: verifies that onSuccess callbacks
 * invalidate exactly the right query keys.
 *
 * Focused coverage:
 *   - useSubtitleAction: episode path -> [Episodes, id] + [Series]
 *   - useSubtitleAction: movie path -> [Movies, id] + [Movies, History]
 *   - useBatchAction: -> [Series] + [Movies] + [System, History] + [Translator]
 *     (NOT a bare [History] key)
 *   - usePromoteSyncSubtitle: episode -> [Series] + [System, History] + content key
 *   - usePromoteSyncSubtitle: movie  -> [Movies] + [System, History] + content key
 */

import { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  useBatchAction,
  usePromoteSyncSubtitle,
  useSubtitleAction,
} from "@/apis/hooks/subtitles";
import { QueryKeys } from "@/apis/queries/keys";

// ---------------------------------------------------------------------------
// Module mock: replace the raw API with lightweight stubs that resolve
// immediately. We only care about the onSuccess side-effects here, so the
// actual network call is irrelevant.
// ---------------------------------------------------------------------------

vi.mock("@/apis/raw", () => ({
  default: {
    subtitles: {
      modify: vi.fn().mockResolvedValue(undefined),
      batch: vi.fn().mockResolvedValue({ queued: 1, skipped: 0, errors: [] }),
      promoteSyncOutput: vi.fn().mockResolvedValue({
        sourceLanguage: "en",
        targetLanguage: "fr",
        targetPath: "/movies/1/subtitles/fr.srt",
      }),
    },
  },
}));

// ---------------------------------------------------------------------------
// Per-test QueryClient + wrapper so spy state never leaks between tests.
// ---------------------------------------------------------------------------

function makeClientAndWrapper() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { networkMode: "offlineFirst" },
    },
  });

  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );

  const spy = vi.spyOn(client, "invalidateQueries");

  return { client, wrapper, spy };
}

// ---------------------------------------------------------------------------
// Helper: extract the queryKey arrays from all recorded spy calls.
// ---------------------------------------------------------------------------

function capturedKeys(spy: ReturnType<typeof vi.spyOn>) {
  return spy.mock.calls.map((call: unknown[]) => {
    // invalidateQueries is called as invalidateQueries({ queryKey: [...] })
    const arg = call[0] as { queryKey?: unknown[] } | undefined;
    return arg?.queryKey ?? [];
  });
}

// ---------------------------------------------------------------------------
// useSubtitleAction
// ---------------------------------------------------------------------------

describe("useSubtitleAction – episode", () => {
  let spy: ReturnType<typeof vi.spyOn>;
  let wrapper: ReturnType<typeof makeClientAndWrapper>["wrapper"];

  beforeEach(() => {
    ({ spy, wrapper } = makeClientAndWrapper());
  });

  it("invalidates [Episodes, id] and [Series] but NOT [Movies]", async () => {
    const { result } = renderHook(() => useSubtitleAction(), { wrapper });

    result.current.mutate({
      action: "translate",
      form: {
        id: 42,
        type: "episode",
        language: "fr",
        path: "/series/1/ep01.en.srt",
      },
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const keys = capturedKeys(spy);

    // Must have invalidated the specific episode id
    expect(keys).toContainEqual([QueryKeys.Episodes, 42]);

    // Must have invalidated the Series root
    expect(keys).toContainEqual([QueryKeys.Series]);

    // Must NOT have touched Movies
    const touchedMovies = keys.some(
      (k: unknown[]) => Array.isArray(k) && k[0] === QueryKeys.Movies,
    );
    expect(touchedMovies).toBe(false);
  });
});

describe("useSubtitleAction – movie", () => {
  let spy: ReturnType<typeof vi.spyOn>;
  let wrapper: ReturnType<typeof makeClientAndWrapper>["wrapper"];

  beforeEach(() => {
    ({ spy, wrapper } = makeClientAndWrapper());
  });

  it("invalidates [Movies, id] and [Movies, History] but NOT [Episodes] or [Series]", async () => {
    const { result } = renderHook(() => useSubtitleAction(), { wrapper });

    result.current.mutate({
      action: "OCR_fixes",
      form: {
        id: 99,
        type: "movie",
        language: "en",
        path: "/movies/film.en.srt",
      },
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const keys = capturedKeys(spy);

    // Must have invalidated the specific movie id
    expect(keys).toContainEqual([QueryKeys.Movies, 99]);

    // Must have invalidated [Movies, History] to refresh movie history page
    expect(keys).toContainEqual([QueryKeys.Movies, QueryKeys.History]);

    // Must NOT have touched Episodes or Series
    const touchedEpisodes = keys.some(
      (k: unknown[]) => Array.isArray(k) && k[0] === QueryKeys.Episodes,
    );
    const touchedSeries = keys.some(
      (k: unknown[]) => Array.isArray(k) && k[0] === QueryKeys.Series,
    );
    expect(touchedEpisodes).toBe(false);
    expect(touchedSeries).toBe(false);
  });

  it("uses [Movies, History] NOT [History] as the history invalidation key", async () => {
    const { result } = renderHook(() => useSubtitleAction(), { wrapper });

    result.current.mutate({
      action: "common",
      form: {
        id: 7,
        type: "movie",
        language: "de",
        path: "/movies/film.de.srt",
      },
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const keys = capturedKeys(spy);

    // The bare [History] key is never invalidated by useSubtitleAction
    const usedBareHistory = keys.some(
      (k: unknown[]) =>
        Array.isArray(k) && k.length === 1 && k[0] === QueryKeys.History,
    );
    expect(usedBareHistory).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// useBatchAction
// ---------------------------------------------------------------------------

describe("useBatchAction", () => {
  let spy: ReturnType<typeof vi.spyOn>;
  let wrapper: ReturnType<typeof makeClientAndWrapper>["wrapper"];

  beforeEach(() => {
    ({ spy, wrapper } = makeClientAndWrapper());
  });

  it("invalidates [Series], [Movies], [System, History], and [Translator]", async () => {
    const { result } = renderHook(() => useBatchAction(), { wrapper });

    result.current.mutate({
      items: [
        { type: "episode", sonarrSeriesId: 1, sonarrEpisodeId: 10 },
        { type: "movie", radarrId: 5 },
      ],
      action: "sync",
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const keys = capturedKeys(spy);

    expect(keys).toContainEqual([QueryKeys.Series]);
    expect(keys).toContainEqual([QueryKeys.Movies]);
    expect(keys).toContainEqual([QueryKeys.System, QueryKeys.History]);
    expect(keys).toContainEqual([QueryKeys.Translator]);
  });

  it("uses [System, History] NOT the bare [History] key", async () => {
    const { result } = renderHook(() => useBatchAction(), { wrapper });

    result.current.mutate({
      items: [{ type: "movie", radarrId: 3 }],
      action: "translate",
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const keys = capturedKeys(spy);

    // [System, History] must be present
    expect(keys).toContainEqual([QueryKeys.System, QueryKeys.History]);

    // The bare [History] key must NOT appear
    const usedBareHistory = keys.some(
      (k: unknown[]) =>
        Array.isArray(k) && k.length === 1 && k[0] === QueryKeys.History,
    );
    expect(usedBareHistory).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// usePromoteSyncSubtitle
// ---------------------------------------------------------------------------

describe("usePromoteSyncSubtitle – episode", () => {
  let spy: ReturnType<typeof vi.spyOn>;
  let wrapper: ReturnType<typeof makeClientAndWrapper>["wrapper"];

  beforeEach(() => {
    ({ spy, wrapper } = makeClientAndWrapper());
  });

  it("invalidates [Series], [System, History], and the exact subtitle content key", async () => {
    const { result } = renderHook(() => usePromoteSyncSubtitle(), { wrapper });

    const params = {
      mediaType: "episode",
      mediaId: 55,
      targetLanguage: "fr",
      sourceLanguage: "en",
      arrInstanceId: undefined as number | undefined,
    };

    result.current.mutate(params);

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const keys = capturedKeys(spy);

    // Must invalidate the Series root (episode history lives here)
    expect(keys).toContainEqual([QueryKeys.Series]);

    // Must invalidate System history stats
    expect(keys).toContainEqual([QueryKeys.System, QueryKeys.History]);

    // Must invalidate the exact subtitle content cache entry
    expect(keys).toContainEqual([
      QueryKeys.Subtitles,
      "content",
      "episode",
      55,
      "fr",
      undefined,
    ]);

    // Must NOT touch Movies
    const touchedMovies = keys.some(
      (k: unknown[]) => Array.isArray(k) && k[0] === QueryKeys.Movies,
    );
    expect(touchedMovies).toBe(false);
  });
});

describe("usePromoteSyncSubtitle – movie", () => {
  let spy: ReturnType<typeof vi.spyOn>;
  let wrapper: ReturnType<typeof makeClientAndWrapper>["wrapper"];

  beforeEach(() => {
    ({ spy, wrapper } = makeClientAndWrapper());
  });

  it("invalidates [Movies], [System, History], and the exact subtitle content key", async () => {
    const { result } = renderHook(() => usePromoteSyncSubtitle(), { wrapper });

    const params = {
      mediaType: "movie",
      mediaId: 17,
      targetLanguage: "es",
      sourceLanguage: "en",
      arrInstanceId: 2,
    };

    result.current.mutate(params);

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const keys = capturedKeys(spy);

    // Must invalidate Movies root
    expect(keys).toContainEqual([QueryKeys.Movies]);

    // Must invalidate System history
    expect(keys).toContainEqual([QueryKeys.System, QueryKeys.History]);

    // Must invalidate the exact content key, including arrInstanceId
    expect(keys).toContainEqual([
      QueryKeys.Subtitles,
      "content",
      "movie",
      17,
      "es",
      2,
    ]);

    // Must NOT touch Series
    const touchedSeries = keys.some(
      (k: unknown[]) => Array.isArray(k) && k[0] === QueryKeys.Series,
    );
    expect(touchedSeries).toBe(false);
  });
});
