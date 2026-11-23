/* eslint-disable camelcase -- API field names. */
import { useState } from "react";
import { act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { useArrInstances } from "@/apis/hooks/arrInstances";
import { mediaSourcesKey } from "@/apis/hooks/mediaSources";
import { useSportsAvailability } from "@/apis/hooks/sports";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import type { SourceAvailability } from "@/apis/raw/mediaSources";
import { makeInstance } from "@/pages/Settings/Connections/__tests__/fixtures";
import InstanceCard from "@/pages/Settings/Connections/InstanceCard";
import { useRoutes } from "@/Router";
import type { CustomRouteObject } from "@/Router/type";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

function Navigation() {
  // eslint-disable-next-line testing-library/no-node-access -- route metadata, not a DOM node.
  const routes: CustomRouteObject[] = useRoutes()[0].children ?? [];
  const sports = useSportsAvailability();
  return (
    <>
      <nav>
        {routes
          .filter((route) => route.name && !route.hidden)
          .map((route) => (
            <div key={route.path}>
              <span>{route.name}</span>
              {((route.children ?? []) as CustomRouteObject[])
                .filter((child) => child.name && !child.hidden)
                .map((child) => (
                  <span
                    key={child.path}
                  >{`${route.name} / ${child.name}`}</span>
                ))}
            </div>
          ))}
      </nav>
      <output>
        {`Sports ${sports.enabled ? "browsable" : "hidden"}, ${sports.searchable ? "searchable" : "search disabled"}`}
      </output>
    </>
  );
}

const ABSENT = { browsable: false, searchable: false, retained: false };
const ENABLED = { ...ABSENT, browsable: true, searchable: true };
const DISABLED = { ...ABSENT, browsable: true };
const RETAINED = { ...ABSENT, retained: true };

it.each([
  ["series", "sonarr", "My series", "Episodes"],
  ["movie", "radarr", "My movies", "Movies"],
  ["sports", "sportarr", "Sports", "Sports"],
] as const)(
  "shows %s library and activity independently of %s",
  async (kind, arr, title, activity) => {
    let visible = true;
    let arrOn = false;
    const availability: SourceAvailability = {
      series: ABSENT,
      movie: ABSENT,
      sports: ABSENT,
    };
    const list = vi.fn();
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { [`use_${arr}`]: arrOn },
          media_sources: { visible },
        }),
      ),
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([makeInstance({ kind: arr, enabled: true })]),
      ),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/system/media-sources", () => {
        list();
        return HttpResponse.json({ data: [], availability });
      }),
      http.get("/api/badges", () => HttpResponse.json({})),
    );
    customRender(<Navigation />);
    const refresh = async (
      update: SourceAvailability[typeof kind],
      useArr = arrOn,
      show = visible,
    ) => {
      availability[kind] = update;
      arrOn = useArr;
      visible = show;
      await act(async () => {
        await queryClient.invalidateQueries({
          queryKey: [QueryKeys.System, QueryKeys.Settings],
        });
        await queryClient.invalidateQueries({
          queryKey: mediaSourcesKey,
          exact: true,
        });
      });
    };
    await waitFor(() => expect(list).toHaveBeenCalled());
    expect(screen.queryByText(title, { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByText(`Missing / ${activity}`)).not.toBeInTheDocument();
    expect(
      screen.queryByText(`Excluded / ${activity}`),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(`History / ${activity}`)).not.toBeInTheDocument();

    await refresh(ABSENT, true);
    expect(await screen.findByText(title, { exact: true })).toBeInTheDocument();
    expect(screen.getByText(`Missing / ${activity}`)).toBeInTheDocument();
    expect(screen.getByText(`Excluded / ${activity}`)).toBeInTheDocument();
    expect(screen.getByText(`History / ${activity}`)).toBeInTheDocument();

    await refresh(ENABLED, false);
    if (kind === "sports") {
      expect(
        await screen.findByText("Sports browsable, searchable"),
      ).toBeInTheDocument();
      expect(screen.getByText(`History / ${activity}`)).toBeInTheDocument();
    } else {
      await waitFor(() =>
        expect(
          screen.queryByText(`History / ${activity}`),
        ).not.toBeInTheDocument(),
      );
    }
    expect(await screen.findByText(title, { exact: true })).toBeInTheDocument();
    expect(screen.getByText(`Missing / ${activity}`)).toBeInTheDocument();
    expect(screen.getByText(`Excluded / ${activity}`)).toBeInTheDocument();

    await refresh(DISABLED);
    expect(screen.getByText(title, { exact: true })).toBeInTheDocument();
    expect(screen.getByText(`Missing / ${activity}`)).toBeInTheDocument();
    if (kind === "sports")
      expect(
        await screen.findByText("Sports browsable, search disabled"),
      ).toBeInTheDocument();

    await refresh(RETAINED);
    await waitFor(() =>
      expect(
        screen.queryByText(title, { exact: true }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByText(`Missing / ${activity}`)).toBeInTheDocument();
    expect(screen.getByText(`Excluded / ${activity}`)).toBeInTheDocument();
    if (kind === "sports")
      expect(screen.getByText(`History / ${activity}`)).toBeInTheDocument();

    await refresh(ENABLED, false, false);
    await waitFor(() =>
      expect(
        screen.queryByText(`Missing / ${activity}`),
      ).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(title, { exact: true })).not.toBeInTheDocument();
    expect(
      screen.queryByText(`Excluded / ${activity}`),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(`History / ${activity}`)).not.toBeInTheDocument();

    await refresh(RETAINED, true, false);
    expect(await screen.findByText(title, { exact: true })).toBeInTheDocument();
    expect(screen.getByText(`Missing / ${activity}`)).toBeInTheDocument();
    expect(screen.getByText(`Excluded / ${activity}`)).toBeInTheDocument();
    expect(screen.getByText(`History / ${activity}`)).toBeInTheDocument();
  },
);

function MediaPage({ group, path }: { group: string; path?: string }) {
  // eslint-disable-next-line testing-library/no-node-access -- route metadata, not a DOM node.
  const routes: CustomRouteObject[] = useRoutes()[0].children ?? [];
  const top = routes.find((route) => route.path === group);
  // eslint-disable-next-line testing-library/no-node-access -- nested route metadata.
  const children: CustomRouteObject[] = top?.children ?? [];
  const page = children.find((route) =>
    path ? route.path === path : route.index,
  );
  return page?.element;
}

function ConnectionsToExcluded() {
  const { data: instances } = useArrInstances();
  const [excluded, setExcluded] = useState(false);
  if (excluded) return <MediaPage group="blacklist" path="series" />;
  const instance = instances?.[0];
  return (
    instance && (
      <>
        <InstanceCard
          instance={instance}
          onEdit={() => undefined}
          onDelete={() => undefined}
        />
        <button type="button" onClick={() => setExcluded(true)}>
          Open Excluded
        </button>
      </>
    )
  );
}

it.each(["succeeds", "fails"] as const)(
  "uses the confirmed disable while the instance list refresh %s",
  async (outcome) => {
    const user = userEvent.setup();
    const instance = makeInstance({ enabled: true });
    const updated = { ...instance, enabled: false, api_key_set: false };
    const { promise: refresh, resolve: releaseRefresh } =
      Promise.withResolvers<void>();
    let patched = false;
    const refreshStarted = vi.fn();
    const excluded = vi.fn();
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { use_sonarr: true },
          media_sources: { visible: true },
        }),
      ),
      http.get("/api/system/arr-instances", async () => {
        if (!patched) return HttpResponse.json([instance]);
        refreshStarted();
        await refresh;
        return outcome === "fails"
          ? HttpResponse.json(
              { message: "Instance list unavailable" },
              { status: 503 },
            )
          : HttpResponse.json([updated]);
      }),
      http.patch("/api/system/arr-instances/42", async ({ request }) => {
        expect(await request.json()).toEqual({ enabled: false });
        patched = true;
        return HttpResponse.json(updated);
      }),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/badges", () => HttpResponse.json({})),
      http.get("/api/system/media-sources", () =>
        HttpResponse.json({
          data: [],
          availability: { series: RETAINED, movie: ABSENT, sports: ABSENT },
        }),
      ),
      http.all("/api/episodes/blacklist", () => {
        excluded();
        return HttpResponse.json({ data: [] });
      }),
    );
    customRender(<ConnectionsToExcluded />);
    const listKey = [QueryKeys.ArrInstances];
    try {
      await user.click(
        await screen.findByRole("switch", { name: "Disable instance" }),
      );
      await waitFor(() => expect(refreshStarted).toHaveBeenCalled());
      expect(
        await screen.findByRole("switch", { name: "Enable instance" }),
      ).not.toBeChecked();
      expect(queryClient.getQueryData(listKey)).toEqual([
        { ...instance, enabled: false },
      ]);

      await user.click(screen.getByRole("button", { name: "Open Excluded" }));
      expect(
        await screen.findByRole("link", { name: "Connections" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Remove All" }),
      ).not.toBeInTheDocument();

      releaseRefresh();
      await waitFor(() =>
        expect(queryClient.getQueryState(listKey)?.fetchStatus).toBe("idle"),
      );
      expect(queryClient.getQueryState(listKey)?.status).toBe(
        outcome === "fails" ? "error" : "success",
      );
      expect(
        screen.getByRole("link", { name: "Connections" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Remove All" }),
      ).not.toBeInTheDocument();
      expect(excluded).not.toHaveBeenCalled();
    } finally {
      releaseRefresh();
    }
  },
);

it.each([
  ["series", undefined, "sonarr", "/api/series"],
  ["movies", undefined, "radarr", "/api/movies"],
  ["sports", undefined, "sportarr", "/api/sports/leagues"],
  ["wanted", "series", "sonarr", "/api/episodes/wanted"],
  ["wanted", "movies", "radarr", "/api/movies/wanted"],
  ["wanted", "sports", "sportarr", "/api/sports/wanted"],
  ["blacklist", "series", "sonarr", "/api/episodes/blacklist"],
  ["blacklist", "movies", "radarr", "/api/movies/blacklist"],
  ["blacklist", "sports", "sportarr", "/api/sports/blacklist"],
  ["history", "sports", "sportarr", "/api/sports/history"],
] as const)(
  "opens %s %s for folders without calling inactive %s",
  async (group, path, arr, endpoint) => {
    const requests = vi.fn();
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { [`use_${arr}`]: false },
          media_sources: { visible: true },
        }),
      ),
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([makeInstance({ kind: arr, enabled: true })]),
      ),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/system/media-sources", () => {
        const state = path ? RETAINED : ENABLED;
        return HttpResponse.json({
          data: [],
          availability: { series: state, movie: state, sports: state },
        });
      }),
      http.get("/api/badges", () =>
        HttpResponse.json({ episodes: 9, movies: 9, sports: 9 }),
      ),
      http.get("/api/subtitles/upgradable", () => {
        requests("upgradable");
        return HttpResponse.json({ movies: [], series: [], sports: [] });
      }),
      http.get("/api/system/languages/audio", () => {
        requests("audio catalogue");
        return HttpResponse.json([]);
      }),
      http.all(endpoint, ({ request }) => {
        requests(request.method);
        return HttpResponse.json({
          data: [{ id: 72, title: "Inactive Arr title" }],
          total: 1,
        });
      }),
    );
    customRender(<MediaPage group={group} path={path} />);
    expect(
      await screen.findByRole("link", { name: "Connections" }),
    ).toHaveAttribute("href", "/settings/connections#folders");
    expect(screen.queryByText("Inactive Arr title")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: /remove all|search all|sync leagues/i,
      }),
    ).not.toBeInTheDocument();
    expect(requests).not.toHaveBeenCalled();
  },
);

describe.each([
  ["sonarr", "series", "/api/episodes/blacklist"],
  ["radarr", "movies", "/api/movies/blacklist"],
] as const)(
  "%s exclusions with the master toggle on",
  (kind, path, endpoint) => {
    it.each([
      {
        mode: "disabled instance and an old scalar",
        record: false,
        legacy: true,
        visible: true,
        accessible: false,
      },
      {
        mode: "no instance or legacy connection",
        record: null,
        legacy: false,
        visible: true,
        accessible: false,
      },
      {
        mode: "active instance",
        record: true,
        legacy: false,
        visible: true,
        accessible: true,
      },
      {
        mode: "configured pre-instance legacy connection",
        record: null,
        legacy: true,
        visible: true,
        accessible: true,
      },
      {
        mode: "hidden folder feature",
        record: null,
        legacy: false,
        visible: false,
        accessible: true,
      },
    ])(
      "handles $mode without granting folder sources Arr privileges",
      async ({ record, legacy, visible, accessible }) => {
        const user = userEvent.setup();
        const requestArr = vi.fn();
        const requestExcluded = vi.fn();
        const removeAll = vi.fn();
        server.use(
          http.get("/api/system/settings", () =>
            HttpResponse.json({
              general: { [`use_${kind}`]: true },
              media_sources: { visible },
              [kind]: {
                ip: "127.0.0.1",
                apikey: legacy ? "synthetic-configured" : "",
              },
            }),
          ),
          http.get("/api/system/arr-instances", () => {
            requestArr();
            return HttpResponse.json([
              makeInstance({
                kind: kind === "sonarr" ? "radarr" : "sonarr",
                id: 5,
                enabled: true,
              }),
              ...(record === null
                ? []
                : [makeInstance({ kind, enabled: record })]),
            ]);
          }),
          http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
          http.get("/api/system/media-sources", () =>
            HttpResponse.json({
              data: [],
              availability: {
                series: RETAINED,
                movie: RETAINED,
                sports: ABSENT,
              },
            }),
          ),
          http.get("/api/badges", () => HttpResponse.json({})),
          http.all(endpoint, ({ request }) => {
            requestExcluded(request.method);
            if (request.method === "DELETE") {
              removeAll(new URL(request.url).searchParams.get("all"));
              return new HttpResponse(null, { status: 204 });
            }
            return HttpResponse.json({
              data: [
                {
                  id: 72,
                  arr_instance_id: 42,
                  title: "Fixture movie",
                  radarrId: 72,
                  seriesTitle: "Fixture series",
                  episodeTitle: "Fixture episode",
                  episode_number: "1x1",
                  sonarrSeriesId: 72,
                  provider: "fixture-provider",
                  subs_id: "fixture-release",
                  language: {
                    name: "English",
                    code2: "en",
                    code3: "eng",
                    forced: false,
                    hi: false,
                  },
                  timestamp: "now",
                  parsed_timestamp: "01/24/25 01:38:03",
                },
              ],
            });
          }),
        );
        customRender(<MediaPage group="blacklist" path={path} />);
        if (accessible) {
          expect(
            await screen.findByText("fixture-provider"),
          ).toBeInTheDocument();
          const action = screen.getByRole("button", { name: "Remove All" });
          expect(action).toBeEnabled();
          await user.click(action);
          await waitFor(() => expect(removeAll).toHaveBeenCalledWith("true"));
        } else {
          expect(
            await screen.findByRole("link", { name: "Connections" }),
          ).toBeInTheDocument();
          expect(
            screen.queryByRole("button", { name: "Remove All" }),
          ).not.toBeInTheDocument();
          expect(requestExcluded).not.toHaveBeenCalled();
        }
        if (!visible) expect(requestArr).not.toHaveBeenCalled();
      },
    );
  },
);

it.each([
  ["series", "sonarr", "/api/episodes/wanted", "seriesTitle"],
  ["movies", "radarr", "/api/movies/wanted", "title"],
] as const)(
  "shows folder-only Wanted %s with a badge count and no Arr actions",
  async (kind, arr, path, titleKey) => {
    const requests = vi.fn();
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { [`use_${arr}`]: false },
          media_sources: { visible: true },
        }),
      ),
      http.get("/api/system/arr-instances", () => HttpResponse.json([])),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/system/languages/audio", () => HttpResponse.json([])),
      http.get("/api/system/media-sources", () =>
        HttpResponse.json({
          data: [
            {
              id: 8,
              enabled: true,
              name: "Archive",
              kind: "folder",
              libraries: [
                {
                  enabled: true,
                  content_kind: kind === "series" ? "series" : "movie",
                },
              ],
            },
          ],
          availability: {
            series: kind === "series" ? ENABLED : ABSENT,
            movie: kind === "movies" ? ENABLED : ABSENT,
            sports: ABSENT,
          },
        }),
      ),
      http.get("/api/badges", () =>
        HttpResponse.json({ episodes: 2, movies: 2 }),
      ),
      http.get(path, ({ request }) => {
        const params = new URL(request.url).searchParams;
        requests(params.get("sources"), params.get("source_key[]"));
        return HttpResponse.json({
          data: [
            {
              id: 9,
              [titleKey]: "Archive title",
              origin: "source",
              source_key: "source:8",
              missing_subtitles: [],
            },
          ],
          total: 1,
        });
      }),
    );

    customRender(<MediaPage group="wanted" path={kind} />);
    expect(await screen.findByText("Archive title")).toBeInTheDocument();
    expect(requests).toHaveBeenCalledWith("1", "source:8");
    expect(screen.getByRole("button", { name: "Search All" })).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "Scan All" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "Source" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("columnheader", { name: "Source" }),
    ).not.toBeInTheDocument();
  },
);
