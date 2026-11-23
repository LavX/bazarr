/* eslint-disable camelcase */
import { PropsWithChildren } from "react";
import { MemoryRouter } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, expect, it, vi } from "vitest";
import { useDeleteMediaServerInstance } from "@/apis/hooks/mediaServers";
import {
  mediaSourcesKey,
  useRemoveMediaSource,
  useScanMediaSource,
  useUpdateServerSourceRoots,
  useWriteMediaSource,
} from "@/apis/hooks/mediaSources";
import {
  useSportsHistoryPagination,
  useSportsWantedPagination,
} from "@/apis/hooks/sports";
import { useBadges } from "@/apis/hooks/system";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import type { MediaSource } from "@/apis/raw/mediaSources";
import type { SportsFilters } from "@/apis/raw/sports";
import { sportarr } from "@/pages/Settings/Connections/__tests__/fixtures";
import {
  sourceDetailApi,
  sourceDetailKeys,
  useSourceItem,
  useSourceLeagueEvents,
  useSourceScan,
} from "@/pages/SourceDetail/api";
import server from "@/tests/mocks/node";

const clients: QueryClient[] = [];
afterEach(() => clients.splice(0).forEach((client) => client.clear()));

const mixedBadgesKey = [QueryKeys.System, QueryKeys.Badges, true];
const nativeBadgesKey = [QueryKeys.System, QueryKeys.Badges, false];
const sourceScope: SportsFilters = {
  includeSources: true,
  sourceKeys: ["source:8"],
};
const nativeScope: SportsFilters = { owner: sportarr.id };
const explicitNativeScope: SportsFilters = {
  owner: sportarr.id,
  includeSources: true,
};
const arrOnlyScope: SportsFilters = {
  includeSources: true,
  sourceKeys: [`arr:${sportarr.id}`],
};
const replacementRoots = [
  { server_path: "/server/remaining", local_path: "/local/remaining" },
];

function setup() {
  const source: MediaSource = {
    id: 8,
    name: "Sports archive",
    kind: "folder",
    enabled: true,
    scan_frequency_hours: 12,
    libraries: [
      { id: 9, locator: "/sports", content_kind: "sports" },
      { id: 10, locator: "/other-sports", content_kind: "sports" },
      { id: 11, locator: "/movies", content_kind: "movie" },
    ].map((library) => ({
      ...library,
      content_kind: library.content_kind === "movie" ? "movie" : "sports",
      enabled: true,
      profile_id: null,
      last_scan_status: null,
      last_scan_message: null,
    })),
  };
  const state = {
    source: structuredClone(source),
    removed: false,
    sourceReads: 0,
    nativeReads: 0,
    badgeReads: 0,
  };
  const availability = () => ({
    data: state.removed ? [] : [state.source],
    availability: {
      sports: {
        browsable: !state.removed,
        searchable:
          !state.removed &&
          state.source.enabled &&
          state.source.libraries.some(
            (library) => library.content_kind === "sports" && library.enabled,
          ),
        retained: true,
        retained_source_ids: state.removed ? [] : [source.id],
        removed: state.removed,
      },
    },
  });
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: Infinity,
        networkMode: "offlineFirst",
      },
      mutations: { retry: false, networkMode: "offlineFirst" },
    },
  });
  clients.push(client);
  client.setQueryData(mediaSourcesKey, availability());
  client.setQueryData([QueryKeys.ArrInstances], [sportarr]);
  client.setQueryData([QueryKeys.System, QueryKeys.Settings], {
    general: { use_sportarr: true, page_size: 25 },
  });
  client.setQueryData(nativeBadgesKey, { sports: 1 });
  server.use(
    http.get("/api/system/media-sources", () =>
      HttpResponse.json(availability()),
    ),
    http.patch("/api/system/media-sources/8", async ({ request }) => {
      const input = (await request.json()) as Record<string, unknown>;
      if (
        Object.keys(input).some(
          (field) =>
            !["name", "enabled", "scan_frequency_hours"].includes(field),
        )
      ) {
        return HttpResponse.json({ error: "invalid_request" }, { status: 400 });
      }
      Object.assign(state.source, input);
      return HttpResponse.json(state.source);
    }),
    http.patch(
      "/api/system/media-sources/8/libraries/:id",
      async ({ request, params }) => {
        const library = state.source.libraries.find(
          (value) => value.id === Number(params.id),
        );
        Object.assign(library!, await request.json());
        return HttpResponse.json(library);
      },
    ),
    http.delete("/api/system/media-sources/8", () => {
      state.removed = true;
      return new HttpResponse(null, { status: 204 });
    }),
    http.delete("/api/system/media-sources/8/libraries/9", () => {
      state.source.libraries = state.source.libraries.filter(
        (library) => library.id !== 9,
      );
      return new HttpResponse(null, { status: 204 });
    }),
    http.get("/api/sports/wanted", ({ request }) => {
      const standalone =
        new URL(request.url).searchParams.get("sources") === "1";
      if (standalone) state.sourceReads++;
      else state.nativeReads++;
      const visible =
        !standalone ||
        (!state.removed &&
          state.source.enabled &&
          state.source.libraries.some(
            (library) => library.id === 9 && library.enabled,
          ));
      return HttpResponse.json({
        data: visible
          ? [
              {
                id: 70,
                title: standalone ? "Archive final" : "Native final",
                origin: standalone ? "source" : "arr",
                arr_instance_id: standalone ? null : sportarr.id,
                league_id: 17,
                source_id: standalone ? source.id : null,
                source_key: standalone ? "source:8" : undefined,
                source_kind: standalone ? "folder" : undefined,
                source_group_id: standalone ? 17 : null,
                audio_language: [],
                missing_subtitles: ["en"],
              },
            ]
          : [],
        total: visible ? 1 : 0,
      });
    }),
    http.get("/api/badges", () => {
      state.badgeReads++;
      return HttpResponse.json({
        sports:
          state.source.enabled &&
          state.source.libraries.some(
            (library) => library.id === 9 && library.enabled,
          )
            ? 2
            : 1,
      });
    }),
  );
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  return { client, wrapper, source, state };
}

function primeMembers(client: QueryClient) {
  const group = { source_id: 8, library_id: 9, id: 17, kind: "league" };
  const event = { ...group, id: 70, group_id: 17, kind: "sports_event" };
  const affected = [
    sourceDetailKeys.group(17, "league"),
    [...sourceDetailKeys.group(17, "league"), "items"],
    sourceDetailKeys.item(70, "sports_event"),
    // A mounted event list can outlive the parent league's cache entry.
    [...sourceDetailKeys.group(19, "league"), "items"],
    [...sourceDetailKeys.group(20, "league"), "items"],
  ];
  [group, [event], event, [{ ...event, group_id: 19 }], []].forEach(
    (data, index) => client.setQueryData(affected[index], data),
  );
  const sibling = sourceDetailKeys.group(18, "league");
  client.setQueryData(sibling, { ...group, id: 18, library_id: 10 });
  const untouched = [
    sourceDetailKeys.group(17, "show"),
    sourceDetailKeys.item(70, "movie"),
    sourceDetailKeys.item(70, "episode"),
    sourceDetailKeys.group(90, "league"),
    [QueryKeys.Sports, "league", 17, sportarr.id, [sportarr.id]],
    [QueryKeys.Sports, "events", 17, sportarr.id, 1, [sportarr.id]],
    [QueryKeys.Movies, "library", "unrelated"],
    nativeBadgesKey,
  ];
  untouched.forEach((key) =>
    client.setQueryData(key, { ...group, source_id: key[3] === 90 ? 80 : 8 }),
  );
  return { affected, sibling, untouched };
}

function primeSportsLists(client: QueryClient) {
  const scopes = [sourceScope, { includeSources: true, sourceKeys: [] }];
  const affected = scopes.flatMap((filters) =>
    ["leagues", "wanted", "history", "blacklist"].map((kind) => [
      QueryKeys.Sports,
      kind,
      filters,
      ...(kind === "wanted" ? [[]] : []),
      [sportarr.id],
      ...(kind === "leagues" ? [[]] : []),
      QueryKeys.Range,
      { start: 0, size: 25 },
    ]),
  );
  const untouched = [
    ...[explicitNativeScope, arrOnlyScope].map((filters) => [
      QueryKeys.Sports,
      "history",
      filters,
    ]),
    [
      QueryKeys.Sports,
      "wanted",
      nativeScope,
      [],
      [sportarr.id],
      QueryKeys.Range,
      { start: 0, size: 25 },
    ],
    [
      QueryKeys.Sports,
      "history",
      { includeSources: true, sourceKeys: ["source:80"] },
    ],
  ];
  [...affected, ...untouched].forEach((key) =>
    client.setQueryData(key, { data: [{ title: "Cached final" }], total: 1 }),
  );
  return { affected, untouched };
}

it.each(["source", "library"] as const)(
  "removing a sports %s invalidates its cached leagues, events, activity and mixed badge",
  async (target) => {
    const { client, wrapper, source } = setup();
    const members = primeMembers(client);
    const lists = primeSportsLists(client);
    client.setQueryData(mixedBadgesKey, { sports: 2 });
    const { result, unmount } = renderHook(() => useRemoveMediaSource(), {
      wrapper,
    });
    await act(async () => {
      await result.current.mutateAsync({
        source,
        ...(target === "library" ? { library: source.libraries[0] } : {}),
      });
    });
    for (const key of [
      ...members.affected,
      ...lists.affected,
      mixedBadgesKey,
    ]) {
      expect(
        client.getQueryState(key)?.isInvalidated,
        JSON.stringify(key),
      ).toBe(true);
    }
    expect(client.getQueryState(members.sibling)?.isInvalidated).toBe(
      target === "source",
    );
    for (const key of [...members.untouched, ...lists.untouched]) {
      expect(
        client.getQueryState(key)?.isInvalidated,
        JSON.stringify(key),
      ).toBe(false);
    }
    unmount();
  },
);

it.each(["source", "library"] as const)(
  "disabling a sports %s refreshes Missing Sports and badges while alternating cached native and standalone scopes",
  async (target) => {
    const { client, wrapper, source, state } = setup();
    const members = primeMembers(client);
    const lists = primeSportsLists(client);
    // Populate both live scopes through their actual hooks before the mutation.
    client.removeQueries({ queryKey: [QueryKeys.Sports, "wanted"] });
    const { result, rerender, unmount } = renderHook(
      ({ filters }: { filters: SportsFilters }) => ({
        wanted: useSportsWantedPagination(filters),
        badges: useBadges(),
        save: useWriteMediaSource({
          source,
          ...(target === "library" ? { library: source.libraries[0] } : {}),
        }),
      }),
      { wrapper, initialProps: { filters: sourceScope } },
    );
    await waitFor(() =>
      expect(result.current.wanted.data?.data[0]?.title).toBe("Archive final"),
    );
    await waitFor(() => expect(result.current.badges.data?.sports).toBe(2));
    const standaloneKey = client.getQueryCache().findAll({
      queryKey: [QueryKeys.Sports, "wanted"],
    })[0].queryKey;
    rerender({ filters: nativeScope });
    await waitFor(() =>
      expect(result.current.wanted.data?.data[0]?.title).toBe("Native final"),
    );
    const nativeReads = state.nativeReads;
    await act(async () => {
      await result.current.save.mutateAsync(() =>
        target === "library"
          ? api.mediaSources.updateLibrary(source.id, source.libraries[0].id, {
              enabled: false,
              profile_id: null,
            })
          : api.mediaSources.update(source.id, {
              name: source.name,
              enabled: false,
              scan_frequency_hours: source.scan_frequency_hours,
            }),
      );
    });
    expect(result.current.wanted.data?.data[0]?.title).toBe("Native final");
    expect(state.nativeReads).toBe(nativeReads);
    await waitFor(() => expect(result.current.badges.data?.sports).toBe(1));
    expect(state.badgeReads).toBe(2);
    expect(client.getQueryState(standaloneKey)?.isInvalidated).toBe(true);
    for (const key of [
      ...members.affected,
      ...lists.affected.filter((key) => key[1] !== "wanted"),
    ]) {
      expect(
        client.getQueryState(key)?.isInvalidated,
        JSON.stringify(key),
      ).toBe(true);
    }
    expect(client.getQueryState(members.sibling)?.isInvalidated).toBe(
      target === "source",
    );
    for (const key of members.untouched) {
      expect(
        client.getQueryState(key)?.isInvalidated,
        JSON.stringify(key),
      ).toBe(false);
    }
    rerender({ filters: sourceScope });
    await waitFor(() => expect(result.current.wanted.data?.total).toBe(0));
    expect(result.current.wanted.data?.data).toEqual([]);
    expect(state.sourceReads).toBe(2);
    rerender({ filters: nativeScope });
    await waitFor(() =>
      expect(result.current.wanted.data?.data[0]?.title).toBe("Native final"),
    );
    expect(state.nativeReads).toBe(nativeReads);
    unmount();
  },
);

it("saving a movie library leaves sports members, activity and badges fresh", async () => {
  const { client, wrapper, source } = setup();
  const members = primeMembers(client);
  const lists = primeSportsLists(client);
  client.setQueryData(mixedBadgesKey, { sports: 2 });
  const library = source.libraries[2];
  const { result, unmount } = renderHook(
    () => useWriteMediaSource({ source, library }),
    { wrapper },
  );
  await act(async () => {
    await result.current.mutateAsync(() =>
      api.mediaSources.updateLibrary(source.id, library.id, {
        enabled: false,
        profile_id: null,
      }),
    );
  });
  for (const key of [
    ...members.affected,
    members.sibling,
    ...members.untouched,
    ...lists.affected,
    ...lists.untouched,
    mixedBadgesKey,
  ]) {
    expect(client.getQueryState(key)?.isInvalidated, JSON.stringify(key)).toBe(
      false,
    );
  }
  unmount();
});

it("removing a movie-only source with only removed-source Sports history leaves sports caches fresh", async () => {
  const { client, wrapper, source } = setup();
  const movieSource = {
    ...source,
    libraries: source.libraries.filter(
      (library) => library.content_kind === "movie",
    ),
  };
  client.setQueryData(mediaSourcesKey, {
    data: [movieSource],
    availability: {
      sports: {
        browsable: false,
        searchable: false,
        retained: true,
        removed: true,
      },
    },
  });
  const members = primeMembers(client);
  const lists = primeSportsLists(client);
  client.setQueryData(mixedBadgesKey, { sports: 2 });
  const { result, unmount } = renderHook(() => useRemoveMediaSource(), {
    wrapper,
  });
  await act(async () => {
    await result.current.mutateAsync({ source: movieSource });
  });
  for (const key of [
    ...members.affected,
    members.sibling,
    ...members.untouched,
    ...lists.affected,
    ...lists.untouched,
    mixedBadgesKey,
  ]) {
    expect(client.getQueryState(key)?.isInvalidated, JSON.stringify(key)).toBe(
      false,
    );
  }
  unmount();
});

it("cancels an unknown-owner event list's first fetch before refreshing after removal", async () => {
  const { client, wrapper, source } = setup();
  let reads = 0;
  const fetchEvents = vi.spyOn(sourceDetailApi, "leagueEvents");
  let releaseFirst = () => {};
  server.use(
    http.get("/api/sources/groups/19/items", async () => {
      reads++;
      if (reads === 1) {
        await new Promise<void>((resolve) => {
          releaseFirst = resolve;
        });
        return HttpResponse.json({
          data: [{ id: 70, source_id: 8, library_id: 9, title: "Stale event" }],
          total: 1,
        });
      }
      return HttpResponse.json({ error: "not_found" }, { status: 404 });
    }),
  );
  const { result, unmount } = renderHook(
    () => ({
      events: useSourceLeagueEvents(19),
      remove: useRemoveMediaSource(),
    }),
    { wrapper },
  );
  await waitFor(() => expect(reads).toBe(1));
  expect(
    client.getQueryData(sourceDetailKeys.group(19, "league")),
  ).toBeUndefined();
  await act(async () => {
    await result.current.remove.mutateAsync({
      source,
      library: source.libraries[0],
    });
  });
  await waitFor(() => expect(result.current.events.data).toBeNull());
  expect(fetchEvents.mock.calls[0][1]?.aborted).toBe(true);
  expect(reads).toBe(2);
  await act(async () => {
    releaseFirst();
    await Promise.resolve();
  });
  expect(result.current.events.data).toBeNull();
  fetchEvents.mockRestore();
  unmount();
});

it.each(["refresh decision", "library owner"] as const)(
  "uses the submitted sports scope when the %s changes during a pending save",
  async (changedOption) => {
    const { client, wrapper, source } = setup();
    const members = primeMembers(client);
    const lists = primeSportsLists(client);
    client.setQueryData(mixedBadgesKey, { sports: 2 });
    let patchStarted = false;
    let finishPatch = () => {};
    const pendingPatch = new Promise<void>((resolve) => {
      finishPatch = resolve;
    });
    server.use(
      http.patch("/api/system/media-sources/8/libraries/9", async () => {
        patchStarted = true;
        await pendingPatch;
        return HttpResponse.json({ ...source.libraries[0], enabled: false });
      }),
    );
    const { result, rerender, unmount } = renderHook(
      ({ library, refreshSports }) =>
        useWriteMediaSource({ source, library, refreshSports }),
      {
        wrapper,
        initialProps: { library: source.libraries[0], refreshSports: true },
      },
    );
    let saved: Promise<unknown> | undefined;
    act(() => {
      const write = () =>
        api.mediaSources.updateLibrary(source.id, source.libraries[0].id, {
          enabled: false,
          profile_id: null,
        });
      if (changedOption === "refresh decision") {
        result.current.mutate(write);
      } else {
        saved = result.current.mutateAsync(write);
      }
    });
    await waitFor(() => expect(patchStarted).toBe(true));
    rerender({
      library:
        changedOption === "library owner"
          ? source.libraries[1]
          : source.libraries[0],
      refreshSports: changedOption !== "refresh decision",
    });
    await act(async () => {
      finishPatch();
      await saved;
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    for (const key of [
      ...members.affected,
      ...lists.affected,
      mixedBadgesKey,
    ]) {
      expect(
        client.getQueryState(key)?.isInvalidated,
        JSON.stringify(key),
      ).toBe(true);
    }
    expect(client.getQueryState(members.sibling)?.isInvalidated).toBe(false);
    for (const key of [...members.untouched, ...lists.untouched]) {
      expect(
        client.getQueryState(key)?.isInvalidated,
        JSON.stringify(key),
      ).toBe(false);
    }
    unmount();
  },
);

it.each(["scan", "event-modal scan", "roots"] as const)(
  "keeps mounted Sports current when an existing queued vendor %s job completes",
  async (operation) => {
    const { client, wrapper, source } = setup();
    const rootEdit = operation === "roots";
    const vendor = {
      ...source,
      kind: "jellyfin" as const,
      media_server_instance_id: "saved",
    };
    const { sibling, untouched } = primeMembers(client);
    const eventKey = sourceDetailKeys.item(70, "sports_event");
    client.removeQueries({ queryKey: eventKey, exact: true });
    let unavailable = false;
    let removed = false;
    let reads = 0;
    const jobs = [QueryKeys.System, QueryKeys.Jobs];
    client.setQueryData(jobs, [{ job_id: 31, status: "pending" }]);
    server.use(
      http.get("/api/sources/items/70", ({ request }) => {
        expect(new URL(request.url).searchParams.get("kind")).toBe(
          "sports_event",
        );
        reads++;
        if (removed)
          return HttpResponse.json({ error: "not_found" }, { status: 404 });
        return HttpResponse.json({
          id: 70,
          kind: "sports_event",
          source_id: vendor.id,
          library_id: 9,
          title: "Archive final",
          server_missing: unavailable,
        });
      }),
      http.get("/api/sports/history", () =>
        HttpResponse.json({
          data: [
            {
              id: 7,
              title: "Archive final",
              origin: "source",
              source_id: 8,
              source_key: "source:8",
              source_item_id: removed ? null : 70,
            },
          ],
          total: 1,
        }),
      ),
      http.post("/api/system/media-sources/8/scan", () =>
        HttpResponse.json(
          { job_id: 31, session: "existing-scan" },
          { status: 202 },
        ),
      ),
      http.post("/api/system/media-sources/8/libraries/9/scan", () =>
        HttpResponse.json({ job_id: 31 }, { status: 202 }),
      ),
      http.patch(
        "/api/system/media-sources/8/libraries/9",
        async ({ request }) => {
          const payload = await request.json();
          expect(payload).toEqual({
            roots: [
              { server_path: "/server/events", local_path: "/local/events" },
            ],
            confirmed_removed: 1,
          });
          removed = true;
          return HttpResponse.json({
            ...vendor.libraries[0],
            source_id: 8,
            removed_items: 1,
            job_id: 31,
          });
        },
      ),
    );
    const { result } = renderHook(
      () => ({
        event: useSourceItem(70, "sports_event"),
        history: useSportsHistoryPagination(sourceScope),
        all: useSportsHistoryPagination({ includeSources: true }),
        scan: useScanMediaSource(),
        eventScan: useSourceScan(),
        roots: useUpdateServerSourceRoots(),
      }),
      { wrapper },
    );
    await waitFor(() =>
      expect(result.current.event.data).toMatchObject({
        server_missing: false,
      }),
    );
    await waitFor(() =>
      expect(result.current.history.data?.data[0]?.source_item_id).toBe(70),
    );
    await waitFor(() =>
      expect(result.current.all.data?.data[0]?.source_item_id).toBe(70),
    );
    await act(async () => {
      if (operation === "scan") {
        await result.current.scan.mutateAsync(vendor);
      } else if (operation === "event-modal scan") {
        await result.current.eventScan.mutateAsync({
          source_id: vendor.id,
          library_id: 9,
          kind: "sports_event",
        });
      } else {
        await result.current.roots.mutateAsync({
          sourceId: vendor.id,
          libraryId: 9,
          confirmedRemoved: 1,
          roots: [
            { server_path: "/server/events", local_path: "/local/events" },
          ],
        });
      }
    });
    expect(reads).toBe(rootEdit ? 2 : 1);
    await waitFor(() =>
      expect(result.current.event.data).toEqual(
        rootEdit ? null : expect.objectContaining({ server_missing: false }),
      ),
    );
    if (rootEdit) {
      await waitFor(() =>
        expect(result.current.history.data?.data[0]).toMatchObject({
          source_key: "source:8",
          source_item_id: null,
        }),
      );
      expect(result.current.all.data?.data[0]?.source_item_id).toBeNull();
    }
    unavailable = true;
    await act(async () => {
      client.setQueryData(jobs, [{ job_id: 31, status: "completed" }]);
    });
    await waitFor(() =>
      expect(result.current.event.data).toEqual(
        rootEdit ? null : expect.objectContaining({ server_missing: true }),
      ),
    );
    await waitFor(() => expect(reads).toBe(rootEdit ? 3 : 2));
    expect(client.getQueryState(sibling)?.isInvalidated).toBe(true);
    for (const key of untouched)
      expect(client.getQueryState(key)?.isInvalidated).toBe(
        key[0] === "source-detail" && key[2] === "league",
      );
  },
);

it.each(["jellyfin", "emby"] as const)(
  "refreshes retained Sports history after deleting its %s connection",
  async (kind) => {
    const { client, wrapper, source } = setup();
    const linked = {
      ...source,
      kind,
      media_server_instance_id: "saved",
      libraries: source.libraries.filter(
        (library) => library.content_kind === "movie",
      ),
    };
    const { affected, untouched } = primeMembers(client);
    const { untouched: untouchedLists } = primeSportsLists(client);
    const removedHistory = [
      QueryKeys.Sports,
      "history",
      {
        includeSources: true,
        sourceKeys: ["source:removed"],
      },
    ];
    client.setQueryData(removedHistory, { data: [], total: 0 });
    let removed = false;
    let reads = 0;
    server.use(
      http.get("/api/system/media-sources", () =>
        HttpResponse.json({
          data: removed ? [] : [linked],
          availability: {
            sports: {
              browsable: false,
              searchable: false,
              retained: true,
              removed,
              retained_source_ids: removed ? [] : [linked.id],
            },
          },
        }),
      ),
      http.delete("/api/system/media-server-instances/saved", () => {
        removed = true;
        return new HttpResponse(null, { status: 204 });
      }),
      http.get("/api/sports/history", () => {
        reads++;
        return HttpResponse.json({
          data: [
            {
              id: 7,
              origin: "source",
              title: "Archive final",
              action: 1,
              source_id: removed ? null : linked.id,
              source_key: removed ? "source:removed" : "source:8",
            },
          ],
          total: 1,
        });
      }),
    );
    const { result } = renderHook(
      () => ({
        history: useSportsHistoryPagination({ includeSources: true }),
        remove: useDeleteMediaServerInstance(kind, "saved", {
          inlineErrors: true,
        }),
      }),
      { wrapper },
    );
    await waitFor(() =>
      expect(result.current.history.data?.data[0]).toMatchObject({
        source_id: linked.id,
        source_key: "source:8",
      }),
    );
    await act(async () => {
      await result.current.remove.mutateAsync();
    });
    await waitFor(() =>
      expect(result.current.history.data?.data[0]).toMatchObject({
        title: "Archive final",
        source_id: null,
        source_key: "source:removed",
      }),
    );
    expect(reads).toBe(2);
    for (const key of [...affected, removedHistory]) {
      expect(client.getQueryState(key)?.isInvalidated).toBe(true);
    }
    for (const key of [...untouched, ...untouchedLists]) {
      expect(client.getQueryState(key)?.isInvalidated).toBe(false);
    }
  },
);

it.each([
  ["jellyfin", "missing cache"],
  ["emby", "failed list with cached owner"],
  ["jellyfin", "partial list"],
] as const)(
  "deleting %s with a %s refreshes mounted former, removed and combined Sports history",
  async (kind, visibility) => {
    const { client, wrapper, source } = setup();
    const emptyList = {
      data: [],
      availability: {
        sports: {
          browsable: false,
          searchable: false,
          retained: true,
          removed: true,
          retained_source_ids: [],
        },
      },
    };
    if (visibility === "missing cache")
      client.removeQueries({ queryKey: mediaSourcesKey });
    else
      client.setQueryData(
        mediaSourcesKey,
        visibility === "failed list with cached owner"
          ? {
              ...emptyList,
              data: [{ ...source, kind, media_server_instance_id: "saved" }],
            }
          : emptyList,
      );
    let removed = false;
    const reads = { former: 0, removed: 0, all: 0, other: 0, native: 0 };
    server.use(
      http.get("/api/system/media-sources", () =>
        visibility === "partial list"
          ? HttpResponse.json(emptyList)
          : HttpResponse.json({ error: "unavailable" }, { status: 503 }),
      ),
      http.delete("/api/system/media-server-instances/saved", () => {
        removed = true;
        return new HttpResponse(null, { status: 204 });
      }),
      http.get("/api/sports/history", ({ request }) => {
        const params = new URL(request.url).searchParams;
        const key =
          params.has("arr_instance_id") || params.get("sources") !== "1"
            ? "native"
            : params.getAll("source_key[]").includes("source:80")
              ? "other"
              : params.getAll("source_key[]").includes("source:8")
                ? "former"
                : params.getAll("source_key[]").includes("source:removed")
                  ? "removed"
                  : "all";
        reads[key]++;
        const visible =
          key === "native" ||
          key === "other" ||
          key === "all" ||
          (key === "former" ? !removed : removed);
        return HttpResponse.json({
          data: visible
            ? [
                {
                  id: 7,
                  title: key === "native" ? "Native final" : "Archive final",
                  origin: key === "native" ? "arr" : "source",
                  arr_instance_id: key === "native" ? sportarr.id : null,
                  source_id:
                    key === "native"
                      ? null
                      : key === "other"
                        ? 80
                        : removed
                          ? null
                          : 8,
                  source_key:
                    key === "native"
                      ? undefined
                      : key === "other"
                        ? "source:80"
                        : removed
                          ? "source:removed"
                          : "source:8",
                },
              ]
            : [],
          total: visible ? 1 : 0,
        });
      }),
    );
    const { result, unmount } = renderHook(
      () => ({
        former: useSportsHistoryPagination(sourceScope),
        removed: useSportsHistoryPagination({
          includeSources: true,
          sourceKeys: ["source:removed"],
        }),
        all: useSportsHistoryPagination({ includeSources: true }),
        native: useSportsHistoryPagination(explicitNativeScope),
        other: useSportsHistoryPagination({
          includeSources: true,
          sourceKeys: ["source:80"],
        }),
        remove: useDeleteMediaServerInstance(kind, "saved", {
          inlineErrors: true,
        }),
      }),
      { wrapper },
    );
    await waitFor(() =>
      expect(reads).toEqual({
        former: 1,
        removed: 1,
        all: 1,
        other: 1,
        native: 1,
      }),
    );
    await waitFor(() => expect(result.current.former.data?.total).toBe(1));
    await act(async () => {
      await result.current.remove.mutateAsync();
    });
    expect(removed).toBe(true);
    await waitFor(() => expect(result.current.former.data?.total).toBe(0));
    expect(result.current.removed.data?.data[0]?.source_key).toBe(
      "source:removed",
    );
    expect(result.current.all.data?.data[0]?.source_key).toBe("source:removed");
    expect(result.current.native.data?.data[0]?.title).toBe("Native final");
    expect(reads).toEqual({
      former: 2,
      removed: 2,
      all: 2,
      other: 2,
      native: 1,
    });
    unmount();
  },
);

it.each([
  ["scan", 80, 89],
  ["event-modal scan", 80, 89],
  ["roots", 80, 89],
] as const)(
  "vendor %s completion refreshes a recording moved from source %s library %s",
  async (operation, oldSourceId, oldLibraryId) => {
    const { client, wrapper, source } = setup();
    const vendor = { ...source, kind: "jellyfin" as const };
    let moved = false;
    let eventReads = 0;
    let memberReads = 0;
    let nativeReads = 0;
    server.use(
      http.get("/api/sources/items/71", () => {
        eventReads++;
        return HttpResponse.json({
          id: 71,
          kind: "sports_event",
          title: "Moved final",
          source_id: moved ? 8 : oldSourceId,
          library_id: moved ? 9 : oldLibraryId,
          group_id: moved ? 17 : 27,
        });
      }),
      http.get("/api/sources/groups/27/items", () => {
        memberReads++;
        return HttpResponse.json({
          data: moved
            ? []
            : [
                {
                  id: 71,
                  kind: "sports_event",
                  title: "Moved final",
                  source_id: oldSourceId,
                  library_id: oldLibraryId,
                },
              ],
          total: moved ? 0 : 1,
        });
      }),
      http.get("/api/sports/wanted", ({ request }) => {
        const params = new URL(request.url).searchParams;
        if (params.has("arr_instance_id") || params.get("sources") !== "1") {
          nativeReads++;
          return HttpResponse.json({
            data: [
              {
                id: 7,
                title: "Native final",
                origin: "arr",
                arr_instance_id: sportarr.id,
              },
            ],
            total: 1,
          });
        }
        const keys = params.getAll("source_key[]");
        const owner = moved ? 8 : oldSourceId;
        const visible = !keys.length || keys.includes(`source:${owner}`);
        return HttpResponse.json({
          data: visible
            ? [
                {
                  id: 71,
                  title: "Moved final",
                  origin: "source",
                  source_id: owner,
                  source_key: `source:${owner}`,
                  source_kind: "jellyfin",
                  source_item_id: 71,
                  source_group_id: moved ? 17 : 27,
                  league_id: null,
                  arr_instance_id: null,
                  missing_subtitles: ["en"],
                  audio_language: [],
                },
              ]
            : [],
          total: visible ? 1 : 0,
        });
      }),
      http.post("/api/system/media-sources/8/scan", () =>
        HttpResponse.json(
          { job_id: 31, session: "existing-scan" },
          { status: 202 },
        ),
      ),
      http.post("/api/system/media-sources/8/libraries/9/scan", () =>
        HttpResponse.json({ job_id: 31 }, { status: 202 }),
      ),
      http.patch("/api/system/media-sources/8/libraries/9", () =>
        HttpResponse.json({
          ...source.libraries[0],
          source_id: 8,
          removed_items: 0,
          job_id: 31,
        }),
      ),
    );
    const { result, unmount } = renderHook(
      () => ({
        event: useSourceItem(71, "sports_event"),
        members: useSourceLeagueEvents(27),
        old: useSportsWantedPagination({
          includeSources: true,
          sourceKeys: [`source:${oldSourceId}`],
        }),
        destination: useSportsWantedPagination(sourceScope),
        all: useSportsWantedPagination({ includeSources: true }),
        native: useSportsWantedPagination(explicitNativeScope),
        scan: useScanMediaSource(),
        eventScan: useSourceScan(),
        roots: useUpdateServerSourceRoots(),
      }),
      { wrapper },
    );
    await waitFor(() =>
      expect(result.current.event.data).toMatchObject({
        source_id: oldSourceId,
        library_id: oldLibraryId,
      }),
    );
    await waitFor(() => expect(result.current.members.data).toHaveLength(1));
    await waitFor(() => expect(result.current.native.data?.total).toBe(1));
    await waitFor(() => expect(result.current.old.data?.total).toBe(1));
    await act(async () => {
      if (operation === "scan") await result.current.scan.mutateAsync(vendor);
      else if (operation === "event-modal scan") {
        await result.current.eventScan.mutateAsync({
          source_id: 8,
          library_id: 9,
          kind: "sports_event",
        });
      } else {
        const updated = await result.current.roots.mutateAsync({
          sourceId: 8,
          libraryId: 9,
          roots: replacementRoots,
          confirmedRemoved: 0,
        });
        expect(updated).toMatchObject({
          id: 9,
          source_id: 8,
          removed_items: 0,
          job_id: 31,
        });
      }
    });
    // An immediate root edit knows only the destination, so it must leave the previous owner alone.
    expect(eventReads).toBe(1);
    expect(memberReads).toBe(1);
    moved = true;
    await act(async () => {
      client.setQueryData(
        [QueryKeys.System, QueryKeys.Jobs],
        [{ job_id: 31, status: "completed" }],
      );
    });
    await waitFor(() =>
      expect(result.current.event.data).toMatchObject({
        source_id: 8,
        library_id: 9,
      }),
    );
    await waitFor(() => expect(result.current.members.data).toEqual([]));
    await waitFor(() => expect(result.current.old.data?.total).toBe(0));
    expect(result.current.destination.data?.data[0]?.source_id).toBe(8);
    expect(result.current.all.data?.data[0]?.source_id).toBe(8);
    expect(eventReads).toBe(2);
    expect(memberReads).toBe(2);
    expect(nativeReads).toBe(1);
    unmount();
  },
);
