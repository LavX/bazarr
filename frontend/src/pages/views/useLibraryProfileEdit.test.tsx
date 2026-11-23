/* eslint-disable camelcase */
import { PropsWithChildren } from "react";
import {
  onlineManager,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryKeys } from "@/apis/queries/keys";
import server from "@/tests/mocks/node";
import { useLibraryProfileEdit } from "./useLibraryProfileEdit";

vi.mock("@/modules/modals", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/modules/modals")>()),
  useModals: () => ({ openContextModal: vi.fn() }),
}));

interface ProfileRow {
  id: number;
  profileId: number | null;
  origin?: "arr" | "source";
}

let client: QueryClient;

function wrapper({ children }: PropsWithChildren) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  onlineManager.setOnline(true);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});

describe("mixed library profile edits", () => {
  it.each([
    ["series", "groups", QueryKeys.Series],
    ["movies", "items", QueryKeys.Movies],
  ] as const)(
    "saves overlapping %s IDs through each owner's endpoint and refreshes the source caches",
    async (kind, endpoint, queryKey) => {
      const requests: unknown[] = [];
      server.use(
        http.patch(`/api/sources/${endpoint}/7`, async ({ request }) => {
          requests.push(await request.json());
          return HttpResponse.json({ id: 7, profile_id: null });
        }),
      );
      const mutateArr = vi.fn().mockResolvedValue(undefined);
      const rows: ProfileRow[] = [
        { id: 7, profileId: 1 },
        { id: 7, profileId: 2, origin: "source" },
      ];
      client.setQueryData([queryKey, "library"], { data: rows });
      client.setQueryData(["source-detail", endpoint, 7], { id: 7 });
      client.setQueryData(["source-detail", "groups", 7, "items"], []);
      client.setQueryData([QueryKeys.System, QueryKeys.Badges, true], {});
      const { result } = renderHook(
        () => useLibraryProfileEdit(kind, rows, mutateArr),
        { wrapper },
      );

      act(() => result.current.setProfiles(null));
      expect(result.current.dirties).toHaveLength(2);

      await act(() => result.current.save());

      expect(mutateArr).toHaveBeenCalledExactlyOnceWith({
        id: [7],
        profileid: [null],
      });
      expect(requests).toEqual([{ profile_id: null }]);
      expect(result.current.dirties).toEqual([]);
      expect(client.getQueryState([queryKey, "library"])?.isInvalidated).toBe(
        true,
      );
      expect(
        client.getQueryState(["source-detail", endpoint, 7])?.isInvalidated,
      ).toBe(true);
      expect(
        client.getQueryState([QueryKeys.System, QueryKeys.Badges, true])
          ?.isInvalidated,
      ).toBe(true);
      if (kind === "series") {
        expect(
          client.getQueryState(["source-detail", "groups", 7, "items"])
            ?.isInvalidated,
        ).toBe(true);
      }
    },
  );

  it("replaces a pending edit without dropping another origin with the same ID", () => {
    const mutateArr = vi.fn();
    const rows: ProfileRow[] = [
      { id: 7, profileId: 1 },
      { id: 7, profileId: 2, origin: "source" },
    ];
    const { result, rerender } = renderHook(
      ({ selections }) =>
        useLibraryProfileEdit("series", selections, mutateArr),
      { wrapper, initialProps: { selections: rows } },
    );
    act(() => result.current.setProfiles(3));
    rerender({ selections: [rows[1]] });
    act(() => result.current.setProfiles(4));

    expect(result.current.dirties).toEqual(
      expect.arrayContaining([
        { id: 7, profileId: 3 },
        { id: 7, profileId: 4, origin: "source" },
      ]),
    );
    expect(result.current.dirties).toHaveLength(2);
    act(() => result.current.cancel());
    expect(result.current.dirties).toEqual([]);
    expect(mutateArr).not.toHaveBeenCalled();
  });

  it("retains only unsaved edits after a source request fails", async () => {
    const requests: number[] = [];
    let rejectLast = true;
    server.use(
      http.patch("/api/sources/items/:id", ({ params }) => {
        const id = Number(params.id);
        requests.push(id);
        if (id === 8 && rejectLast) {
          return HttpResponse.json({ message: "Unavailable" }, { status: 503 });
        }
        return HttpResponse.json({ id, profile_id: 3 });
      }),
    );
    const mutateArr = vi.fn().mockResolvedValue(undefined);
    const rows: ProfileRow[] = [
      { id: 7, profileId: 1 },
      { id: 7, profileId: 1, origin: "source" },
      { id: 8, profileId: 1, origin: "source" },
    ];
    const { result } = renderHook(
      () => useLibraryProfileEdit("movies", rows, mutateArr),
      { wrapper },
    );
    act(() => result.current.setProfiles(3));

    await act(async () => {
      await expect(result.current.save()).rejects.toThrow();
    });

    expect(result.current.dirties).toEqual([
      { id: 8, profileId: 3, origin: "source" },
    ]);
    expect(result.current.isSaving).toBe(false);
    rejectLast = false;
    await act(() => result.current.save());

    expect(requests).toEqual([7, 8, 8]);
    expect(mutateArr).toHaveBeenCalledOnce();
    expect(result.current.dirties).toEqual([]);
  });

  it("preserves a newer edit queued while an earlier profile save is running", async () => {
    let complete: () => void = () => undefined;
    const request = new Promise<void>((resolve) => {
      complete = resolve;
    });
    const mutateArr = vi.fn().mockReturnValue(request);
    const rows: ProfileRow[] = [{ id: 7, profileId: 1 }];
    const { result } = renderHook(
      () => useLibraryProfileEdit("movies", rows, mutateArr),
      { wrapper },
    );
    act(() => result.current.setProfiles(3));
    let saving = Promise.resolve();
    act(() => {
      saving = result.current.save();
    });
    expect(result.current.isSaving).toBe(true);
    act(() => result.current.setProfiles(4));

    await act(async () => {
      complete();
      await saving;
    });

    expect(result.current.dirties).toEqual([{ id: 7, profileId: 4 }]);
    expect(result.current.isSaving).toBe(false);
    expect(mutateArr).toHaveBeenCalledExactlyOnceWith({
      id: [7],
      profileid: [3],
    });
  });

  it("keeps existing Arr batches bounded to 1000 rows", async () => {
    const mutateArr = vi.fn().mockResolvedValue(undefined);
    const rows = Array.from({ length: 1001 }, (_, index) => ({
      id: index + 1,
      profileId: null,
    }));
    const { result } = renderHook(
      () => useLibraryProfileEdit("movies", rows, mutateArr),
      { wrapper },
    );
    act(() => result.current.setProfiles(3));
    await act(() => result.current.save());

    expect(mutateArr).toHaveBeenCalledTimes(2);
    expect(mutateArr.mock.calls[0][0].id).toHaveLength(1000);
    expect(mutateArr.mock.calls[1][0]).toEqual({
      id: [1001],
      profileid: [3],
    });
  });
});
