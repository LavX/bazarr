/* eslint-disable camelcase -- Backend form fields. */
import { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { useSubtitleAction, useSubtitleContent } from "@/apis/hooks/subtitles";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import server from "@/tests/mocks/node";

function setup() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, networkMode: "offlineFirst" },
      mutations: { retry: false, networkMode: "offlineFirst" },
    },
  });
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

const sourceForm: FormType.ModifySubtitle = {
  type: "source",
  id: 7,
  language: "en",
  path: "/library/movie.en.srt",
  sourceLanguage: "en:hi",
};

describe("source subtitle tools", () => {
  it("waits for the source guard and serializes only backend fields", async () => {
    let release!: () => void;
    const guard = new Promise<void>((resolve) => {
      release = resolve;
    });
    const validateSource = vi.fn(() => guard);
    const requests: Record<string, FormDataEntryValue>[] = [];
    server.use(
      http.patch("/api/subtitles", async ({ request }) => {
        requests.push(Object.fromEntries((await request.formData()).entries()));
        return HttpResponse.json({ job_id: 44 }, { status: 202 });
      }),
    );

    const write = api.subtitles.modify("remove_HI", {
      ...sourceForm,
      validateSource,
    });
    expect(validateSource).toHaveBeenCalledOnce();
    expect(validateSource).toHaveBeenCalledWith(false);
    expect(requests).toHaveLength(0);
    release();
    await write;

    expect(requests).toEqual([
      {
        type: "source",
        id: "7",
        language: "en",
        path: sourceForm.path,
      },
    ]);
  });

  it("refuses a stale source without sending a tool request", async () => {
    const patch = vi.fn(() => HttpResponse.json({ job_id: 44 }));
    server.use(http.patch("/api/subtitles", patch));
    await expect(
      api.subtitles.modify("sync", {
        ...sourceForm,
        validateSource: () =>
          Promise.reject(new Error("This source is disabled")),
      }),
    ).rejects.toThrow("This source is disabled");
    expect(patch).not.toHaveBeenCalled();
  });

  it("checks language ambiguity again before a two-point write", async () => {
    const validateSource = vi.fn(() => Promise.resolve());
    server.use(
      http.patch("/api/subtitles", () => HttpResponse.json({ job_id: 44 })),
    );
    await api.subtitles.modify(
      "two_point_fit(rh=0,rm=0,rs=1,rms=0,oh=0,om=0,os=2,oms=0,from=1,to=1)",
      {
        ...sourceForm,
        validateSource,
      },
    );
    expect(validateSource).toHaveBeenCalledWith(true);
  });

  it("does not read source content when its language is ambiguous", async () => {
    const read = vi.fn(() => HttpResponse.json({ content: "wrong file" }));
    server.use(http.get("/api/sources/items/7/subtitles/en:hi/content", read));
    const { client, wrapper } = setup();
    const guard = vi.fn(() =>
      Promise.reject(new Error("More than one subtitle file is listed")),
    );
    const { result, unmount } = renderHook(
      () =>
        useSubtitleContent(
          "source",
          7,
          "en:hi",
          undefined,
          undefined,
          undefined,
          guard,
        ),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(guard).toHaveBeenCalledOnce();
    expect(read).not.toHaveBeenCalled();
    expect(result.current.error?.message).toBe(
      "More than one subtitle file is listed",
    );
    unmount();
    client.clear();
  });

  it.each(["completed", "failed", "cancelled"])(
    "refreshes source data after a %s job without touching Arr details",
    async (status) => {
      server.use(
        http.patch("/api/subtitles", () =>
          HttpResponse.json({ job_id: 44 }, { status: 202 }),
        ),
      );
      const { client, wrapper } = setup();
      const sourceKey = ["source-detail", "items", "movie", 7];
      const contentKey = [QueryKeys.Subtitles, "content", "source", 7, "en:hi"];
      const referencesKey = [
        QueryKeys.Subtitles,
        "source",
        7,
        "references",
        sourceForm.path,
      ];
      const sourceLibraryKey = [
        QueryKeys.Movies,
        "library",
        { includeSources: true },
      ];
      const arrKey = [QueryKeys.Movies, 7];
      const arrLibraryKey = [
        QueryKeys.Movies,
        "library",
        { includeSources: false },
      ];
      for (const key of [
        sourceKey,
        contentKey,
        referencesKey,
        sourceLibraryKey,
        arrKey,
        arrLibraryKey,
      ]) {
        client.setQueryData(key, {});
      }
      const { result, unmount } = renderHook(() => useSubtitleAction(), {
        wrapper,
      });
      await act(async () => {
        await result.current.mutateAsync({ action: "sync", form: sourceForm });
      });
      for (const key of [
        sourceKey,
        contentKey,
        referencesKey,
        sourceLibraryKey,
      ]) {
        expect(client.getQueryState(key)?.isInvalidated).toBe(false);
      }

      act(() => {
        client.setQueryData(
          [QueryKeys.System, QueryKeys.Jobs],
          [{ job_id: 44, status }],
        );
      });
      await waitFor(() => {
        for (const key of [
          sourceKey,
          contentKey,
          referencesKey,
          sourceLibraryKey,
        ]) {
          expect(client.getQueryState(key)?.isInvalidated).toBe(true);
        }
      });
      expect(client.getQueryState(arrKey)?.isInvalidated).toBe(false);
      expect(client.getQueryState(arrLibraryKey)?.isInvalidated).toBe(false);
      unmount();
      client.clear();
    },
  );

  it("follows the translation session rather than an older job with the same id", async () => {
    server.use(
      http.patch("/api/subtitles", () =>
        HttpResponse.json(
          { job_id: 44, session: "current-run" },
          { status: 202 },
        ),
      ),
    );
    const { client, wrapper } = setup();
    const key = ["source-detail", "items", "movie", 7];
    client.setQueryData(key, {});
    client.setQueryData(
      [QueryKeys.System, QueryKeys.Jobs],
      [{ job_id: 44, session: "older-run", status: "completed" }],
    );
    const { result, unmount } = renderHook(() => useSubtitleAction(), {
      wrapper,
    });
    await act(async () => {
      await result.current.mutateAsync({
        action: "translate",
        form: sourceForm,
      });
    });
    expect(client.getQueryState(key)?.isInvalidated).toBe(false);
    act(() => {
      client.setQueryData(
        [QueryKeys.System, QueryKeys.Jobs],
        [{ job_id: 44, session: "current-run", status: "completed" }],
      );
    });
    await waitFor(() =>
      expect(client.getQueryState(key)?.isInvalidated).toBe(true),
    );
    unmount();
    client.clear();
  });
});
