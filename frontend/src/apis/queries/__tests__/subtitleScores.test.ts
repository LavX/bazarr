import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryKeys } from "@/apis/queries/keys";
import { scheduleSubtitleScoresRefresh } from "@/apis/queries/subtitleScores";

describe("subtitle score refresh", () => {
  let client: QueryClient;

  beforeEach(() => {
    vi.useFakeTimers();
    client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    });
  });

  afterEach(() => {
    client.clear();
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it.each([QueryKeys.Movies, QueryKeys.Series] as const)(
    "marks only %s lists with scores stale, including inactive pages",
    async (media) => {
      const scored = [
        [media, QueryKeys.Range, { all: true, scores: true }],
        [media, QueryKeys.Range, { start: 0, size: 25, scores: true }],
      ];
      const unrelated = [
        [media, QueryKeys.Range, { all: true }],
        [media, QueryKeys.Range, { start: 0, size: 25 }],
        [media, QueryKeys.All],
        [media, 501],
        [media, QueryKeys.History],
        [
          media === QueryKeys.Movies ? QueryKeys.Series : QueryKeys.Movies,
          QueryKeys.Range,
          { all: true, scores: true },
        ],
      ];
      [...scored, ...unrelated].forEach((key) => client.setQueryData(key, []));

      scheduleSubtitleScoresRefresh(client, media);
      await vi.advanceTimersByTimeAsync(1_000);

      scored.forEach((key) =>
        expect(client.getQueryState(key)?.isInvalidated).toBe(true),
      );
      unrelated.forEach((key) =>
        expect(client.getQueryState(key)?.isInvalidated).toBe(false),
      );
    },
  );

  it("coalesces a burst of history writes into one list request", async () => {
    const queryFn = vi.fn().mockResolvedValue(80);
    const observer = new QueryObserver(client, {
      queryKey: [
        QueryKeys.Series,
        QueryKeys.Range,
        { all: true, scores: true },
      ],
      queryFn,
    });
    const unsubscribe = observer.subscribe(() => undefined);
    await vi.advanceTimersByTimeAsync(0);
    expect(queryFn).toHaveBeenCalledTimes(1);

    scheduleSubtitleScoresRefresh(client, QueryKeys.Series);
    await vi.advanceTimersByTimeAsync(600);
    scheduleSubtitleScoresRefresh(client, QueryKeys.Series);
    await vi.advanceTimersByTimeAsync(600);
    scheduleSubtitleScoresRefresh(client, QueryKeys.Series);
    expect(queryFn).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(queryFn).toHaveBeenCalledTimes(2);
    unsubscribe();
  });

  it("refreshes within five seconds while history writes continue", async () => {
    const queryFn = vi.fn().mockResolvedValue(80);
    const observer = new QueryObserver(client, {
      queryKey: [
        QueryKeys.Movies,
        QueryKeys.Range,
        { all: true, scores: true },
      ],
      queryFn,
    });
    const unsubscribe = observer.subscribe(() => undefined);
    await vi.advanceTimersByTimeAsync(0);
    scheduleSubtitleScoresRefresh(client, QueryKeys.Movies);
    for (let elapsed = 0; elapsed < 4_800; elapsed += 600) {
      await vi.advanceTimersByTimeAsync(600);
      scheduleSubtitleScoresRefresh(client, QueryKeys.Movies);
    }
    expect(queryFn).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(200);
    expect(queryFn).toHaveBeenCalledTimes(2);
    unsubscribe();
  });

  it("replaces an initial request that read history before the event", async () => {
    let finishOldRequest!: (score: number) => void;
    const oldRequest = new Promise<number>((resolve) => {
      finishOldRequest = resolve;
    });
    const queryKey = [
      QueryKeys.Movies,
      QueryKeys.Range,
      { all: true, scores: true },
    ];
    const queryFn = vi
      .fn()
      .mockReturnValueOnce(oldRequest)
      .mockResolvedValue(100);
    const observer = new QueryObserver(client, { queryKey, queryFn });
    const unsubscribe = observer.subscribe(() => undefined);
    expect(queryFn).toHaveBeenCalledTimes(1);

    scheduleSubtitleScoresRefresh(client, QueryKeys.Movies);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(queryFn).toHaveBeenCalledTimes(2);
    expect(client.getQueryData(queryKey)).toBe(100);

    finishOldRequest(80);
    await vi.advanceTimersByTimeAsync(0);
    expect(client.getQueryData(queryKey)).toBe(100);
    unsubscribe();
  });
});
