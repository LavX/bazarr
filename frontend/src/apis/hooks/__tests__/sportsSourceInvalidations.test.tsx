/* eslint-disable camelcase */
import { PropsWithChildren } from "react";
import { MemoryRouter } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, expect, it, vi } from "vitest";
import {
  mediaSourcesKey,
  useRemoveMediaSource,
  useWriteMediaSource,
} from "@/apis/hooks/mediaSources";
import { useSportsWantedPagination } from "@/apis/hooks/sports";
import { useBadges } from "@/apis/hooks/system";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import type { MediaSource } from "@/apis/raw/mediaSources";
import type { SportsFilters } from "@/apis/raw/sports";
import { sportarr } from "@/pages/Settings/Connections/__tests__/fixtures";
import {
  sourceDetailApi,
  sourceDetailKeys,
  useSourceLeagueEvents,
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
  await waitFor(() => expect(result.current.events.isError).toBe(true));
  expect(result.current.events.data).toBeUndefined();
  expect(fetchEvents.mock.calls[0][1]?.aborted).toBe(true);
  expect(reads).toBe(2);
  await act(async () => {
    releaseFirst();
    await Promise.resolve();
  });
  expect(result.current.events.data).toBeUndefined();
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
