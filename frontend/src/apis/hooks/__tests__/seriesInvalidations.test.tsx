/**
 * Tests for mutation hooks that must refresh the series views afterwards.
 *
 * Series queries are cached under the canonical LOCAL series id (see
 * SeriesIdType), while these mutations receive the upstream Sonarr id, because
 * the endpoints they call are keyed on it. Several hooks invalidated
 * [Series, <upstream id>], which therefore never matched: at best a no-op, at
 * worst an accidental refetch of whichever series happens to own that number as
 * its local id.
 *
 * These assert the observable contract of each hook: after it succeeds, the
 * series views are invalidated, and no key built from the upstream id is used.
 * Follows the pattern established in subtitleInvalidations.test.tsx.
 */

import { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useEpisodeAddBlacklist } from "@/apis/hooks/episodes";
import { useEpisodeSubtitleModification } from "@/apis/hooks/subtitles";
import { QueryKeys } from "@/apis/queries/keys";

vi.mock("@/apis/raw", () => ({
  default: {
    episodes: {
      addBlacklist: vi.fn().mockResolvedValue(undefined),
      downloadSubtitles: vi.fn().mockResolvedValue(undefined),
      deleteSubtitles: vi.fn().mockResolvedValue(undefined),
      uploadSubtitles: vi.fn().mockResolvedValue(undefined),
    },
  },
}));

/** The upstream id used by every fixture. If a key is built from it, that key
 * cannot match a series cache entry, which is keyed on the local id. */
const UPSTREAM_SERIES_ID = 559;

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

function capturedKeys(spy: ReturnType<typeof vi.spyOn>) {
  return spy.mock.calls.map((call: unknown[]) => {
    const arg = call[0] as { queryKey?: unknown[] } | undefined;
    return arg?.queryKey ?? [];
  });
}

describe("useEpisodeAddBlacklist", () => {
  let spy: ReturnType<typeof vi.spyOn>;
  let wrapper: ReturnType<typeof makeClientAndWrapper>["wrapper"];

  beforeEach(() => {
    ({ spy, wrapper } = makeClientAndWrapper());
  });

  it("refreshes the series views, so a blacklisted subtitle disappears without a manual reload", async () => {
    const { result } = renderHook(() => useEpisodeAddBlacklist(), { wrapper });

    result.current.mutate({
      seriesId: UPSTREAM_SERIES_ID,
      episodeId: 12,
      form: {
        provider: "x",
        subs_id: "y",
        language: "en",
        subtitles_path: "/p",
      },
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const keys = capturedKeys(spy);
    // The blacklist list itself.
    expect(keys).toContainEqual([
      QueryKeys.Series,
      QueryKeys.Episodes,
      QueryKeys.Blacklist,
    ]);
    // And the series views. Without this the detail page kept showing the
    // subtitle until something else happened to refresh it.
    expect(keys).toContainEqual([QueryKeys.Series]);
  });

  it("does not build a key from the upstream series id", async () => {
    const { result } = renderHook(() => useEpisodeAddBlacklist(), { wrapper });

    result.current.mutate({
      seriesId: UPSTREAM_SERIES_ID,
      episodeId: 12,
      form: {
        provider: "x",
        subs_id: "y",
        language: "en",
        subtitles_path: "/p",
      },
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(capturedKeys(spy)).not.toContainEqual([
      QueryKeys.Series,
      UPSTREAM_SERIES_ID,
    ]);
  });
});

describe("useEpisodeSubtitleModification", () => {
  let spy: ReturnType<typeof vi.spyOn>;
  let wrapper: ReturnType<typeof makeClientAndWrapper>["wrapper"];

  beforeEach(() => {
    ({ spy, wrapper } = makeClientAndWrapper());
  });

  it("download refreshes the series views without an upstream-keyed invalidation", async () => {
    const { result } = renderHook(() => useEpisodeSubtitleModification(), {
      wrapper,
    });

    result.current.download.mutate({
      seriesId: UPSTREAM_SERIES_ID,
      episodeId: 12,
      form: { language: "en", forced: false, hi: false },
    });

    await waitFor(() => expect(result.current.download.isSuccess).toBe(true));

    const keys = capturedKeys(spy);
    expect(keys).toContainEqual([QueryKeys.Series]);
    expect(keys).not.toContainEqual([QueryKeys.Series, UPSTREAM_SERIES_ID]);
  });

  it("remove refreshes the series views without an upstream-keyed invalidation", async () => {
    const { result } = renderHook(() => useEpisodeSubtitleModification(), {
      wrapper,
    });

    result.current.remove.mutate({
      seriesId: UPSTREAM_SERIES_ID,
      episodeId: 12,
      form: { language: "en", forced: false, hi: false, path: "/p" },
    });

    await waitFor(() => expect(result.current.remove.isSuccess).toBe(true));

    const keys = capturedKeys(spy);
    expect(keys).toContainEqual([QueryKeys.Series]);
    expect(keys).not.toContainEqual([QueryKeys.Series, UPSTREAM_SERIES_ID]);
  });
});
