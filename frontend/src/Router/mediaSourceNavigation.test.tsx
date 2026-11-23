/* eslint-disable camelcase -- API field names. */
import { useState } from "react";
import { act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { useArrInstances } from "@/apis/hooks/arrInstances";
import { mediaSourcesKey } from "@/apis/hooks/mediaSources";
import { useSportsAvailability } from "@/apis/hooks/sports";
import { useBadges } from "@/apis/hooks/system";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import type { SourceAvailability } from "@/apis/raw/mediaSources";
import { latestWhatsNewVersion } from "@/data/whatsNew";
import { makeInstance } from "@/pages/Settings/Connections/__tests__/fixtures";
import InstanceCard from "@/pages/Settings/Connections/InstanceCard";
import { AllProviders } from "@/providers";
import { Router, useRoutes } from "@/Router";
import type { CustomRouteObject } from "@/Router/type";
import { customRender, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { setAuthenticated } from "@/utilities/event";
import { WHATS_NEW_SEEN_KEY } from "@/utilities/whatsNew";

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
              <span data-badge={route.badge}>{route.name}</span>
              {((route.children ?? []) as CustomRouteObject[])
                .filter((child) => child.name && !child.hidden)
                .map((child) => (
                  <span
                    key={child.path}
                    data-badge={child.badge}
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
  { visible: true, status: 200, sibling: false, folder: "disabled" },
  { visible: true, status: 200, sibling: false, folder: "populated" },
  { visible: true, status: 200, sibling: false, folder: "empty" },
  { visible: true, status: 503, sibling: false, folder: "disabled" },
  { visible: false, status: 200, sibling: false, folder: "disabled" },
  { visible: false, status: 503, sibling: false, folder: "disabled" },
  { visible: false, status: 200, sibling: true, folder: "disabled" },
])(
  "updates Radarr badges on disable (folders visible: $visible, PATCH $status, enabled sibling: $sibling, folder: $folder)",
  async ({ visible, status, sibling, folder }) => {
    const user = userEvent.setup();
    const movie = makeInstance({ kind: "radarr", name: "Radarr" });
    const series = makeInstance({ id: 1 });
    const sports = makeInstance({ id: 2, kind: "sportarr" });
    const instances = [movie, series, sports];
    if (sibling) {
      instances.push(makeInstance({ id: 43, kind: "radarr", name: "Sibling" }));
    }
    const patch = Promise.withResolvers<void>();
    const refresh = Promise.withResolvers<void>();
    const badgeRefresh = Promise.withResolvers<void>();
    const patchStarted = vi.fn();
    const badgeRefreshStarted = vi.fn();
    const list = vi.fn();
    const sources = vi.fn();
    const excluded = vi.fn();
    let confirmed = false;

    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: {
            theme: "light",
            setup_complete: true,
            use_radarr: true,
            use_sonarr: true,
            use_sportarr: true,
          },
          media_sources: { visible },
          auth: { type: "form" },
        }),
      ),
      http.get("/api/system/arr-instances", async () => {
        list();
        if (!confirmed) return HttpResponse.json(instances);
        await refresh.promise;
        return new HttpResponse(null, { status: 503 });
      }),
      http.patch("/api/system/arr-instances/42", async ({ request }) => {
        patchStarted(await request.json());
        await patch.promise;
        if (status === 503) return new HttpResponse(null, { status });
        confirmed = true;
        return HttpResponse.json({ ...movie, enabled: false }, { status });
      }),
      http.get("/api/system/media-sources", () => {
        sources();
        return HttpResponse.json({
          data: [],
          availability: {
            movie: {
              ...(folder === "disabled" ? DISABLED : ENABLED),
              retained: true,
            },
            series: ABSENT,
            sports: ABSENT,
          },
        });
      }),
      http.get("/api/badges", async ({ request }) => {
        const mixed = new URL(request.url).searchParams.has("sources");
        if (confirmed && mixed) {
          badgeRefreshStarted();
          await badgeRefresh.promise;
        }
        const sourceCount = folder === "populated" && mixed;
        return HttpResponse.json({
          movies: sourceCount && confirmed ? 3 : 9,
          ...(sourceCount ? { source_movies: confirmed ? 3 : 1 } : {}),
          episodes: 4,
          sports: 2,
          radarr_signalr: "DOWN",
          sonarr_signalr: "LIVE",
          sportarr_sse: "LIVE",
        });
      }),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
      http.get("/api/system/searches", () => HttpResponse.json([])),
      http.get("/api/discover/metadata/status", () =>
        HttpResponse.json({ data: { configured: false } }),
      ),
      http.all("/api/movies/blacklist", ({ request }) => {
        excluded(request.method);
        return HttpResponse.json({ data: [] });
      }),
    );

    // Mantine's flyout hides its content when the trigger has no layout bounds.
    const bounds = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockReturnValue(new DOMRect(100, 100, 100, 40));
    window.history.replaceState(null, "", "/settings/connections#radarr");
    setAuthenticated(true);
    localStorage.setItem(WHATS_NEW_SEEN_KEY, latestWhatsNewVersion);
    const { unmount } = rawRender(
      <AllProviders>
        <Router />
      </AllProviders>,
    );

    const moviesLink = () => screen.getByRole("link", { name: "Movies" });
    const wanted = () => screen.getByRole("button", { name: "Wanted" });
    const toggle = () =>
      screen.getAllByRole("switch", { name: "Disable instance" })[0];
    const initialCounts = "Sports: 2, Episodes: 4, Movies: 9";

    try {
      await screen.findAllByRole("switch", { name: "Disable instance" });
      await waitFor(() =>
        expect(moviesLink()).toHaveAttribute(
          "aria-description",
          "Connection down",
        ),
      );
      expect(wanted()).toHaveAttribute("aria-description", initialCounts);
      expect(list).toHaveBeenCalledOnce();
      if (!visible) expect(sources).not.toHaveBeenCalled();

      await user.click(toggle());
      await waitFor(() =>
        expect(patchStarted).toHaveBeenCalledWith({ enabled: false }),
      );
      expect(toggle()).toBeChecked();
      expect(moviesLink()).toHaveAttribute(
        "aria-description",
        "Connection down",
      );
      expect(wanted()).toHaveAttribute("aria-description", initialCounts);
      expect(list).toHaveBeenCalledOnce();

      patch.resolve();
      if (status === 503) {
        await waitFor(() => expect(toggle()).toBeEnabled());
        expect(toggle()).toBeChecked();
        expect(moviesLink()).toHaveAttribute(
          "aria-description",
          "Connection down",
        );
        expect(wanted()).toHaveAttribute("aria-description", initialCounts);
        expect(list).toHaveBeenCalledOnce();
        return;
      }

      await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
      expect(
        await screen.findByRole("switch", { name: "Enable instance" }),
      ).not.toBeChecked();
      if (visible) {
        await waitFor(() => expect(badgeRefreshStarted).toHaveBeenCalledOnce());
        expect(wanted()).toHaveAttribute(
          "aria-description",
          "Sports: 2, Episodes: 4",
        );
        badgeRefresh.resolve();
        await waitFor(() =>
          expect(
            queryClient.getQueryState([
              QueryKeys.System,
              QueryKeys.Badges,
              true,
            ])?.fetchStatus,
          ).toBe("idle"),
        );
      }

      const expectBadges = () => {
        expect(moviesLink()).toHaveAttribute("href", "/movies");
        if (sibling) {
          expect(moviesLink()).toHaveAttribute(
            "aria-description",
            "Connection down",
          );
        } else {
          expect(moviesLink()).not.toHaveAttribute("aria-description");
        }
        expect(wanted()).toHaveAttribute(
          "aria-description",
          sibling
            ? initialCounts
            : folder === "populated"
              ? "Sports: 2, Episodes: 4, Movies: 3"
              : "Sports: 2, Episodes: 4",
        );
        expect(screen.getByRole("link", { name: "Series" })).toHaveAttribute(
          "aria-description",
          "Connection live",
        );
        expect(screen.getByRole("link", { name: "Sports" })).toHaveAttribute(
          "aria-description",
          "Connection live",
        );
      };
      await waitFor(expectBadges);
      await user.click(screen.getByRole("button", { name: "Excluded" }));
      await user.click(await screen.findByRole("menuitem", { name: "Movies" }));
      if (visible) {
        expect(
          await screen.findByRole("link", { name: "Connections" }),
        ).toHaveAttribute("href", "/settings/connections#folders");
      } else {
        await waitFor(() => expect(excluded).toHaveBeenCalledWith("GET"));
      }

      const expectExcluded = () => {
        expect(window.location.pathname).toBe("/blacklist/movies");
        expect(
          screen.getByRole("button", { name: "Excluded" }),
        ).toHaveAttribute("aria-current", "page");
        if (visible) {
          expect(
            screen.queryByRole("button", { name: "Remove All" }),
          ).not.toBeInTheDocument();
          expect(excluded).not.toHaveBeenCalled();
        }
        expect(list).toHaveBeenCalledTimes(2);
      };
      expectExcluded();

      refresh.resolve();
      await waitFor(() =>
        expect(
          queryClient.getQueryState([QueryKeys.ArrInstances])?.status,
        ).toBe("error"),
      );
      expectBadges();
      expectExcluded();
      if (!visible) expect(sources).not.toHaveBeenCalled();
    } finally {
      patch.resolve();
      refresh.resolve();
      badgeRefresh.resolve();
      unmount();
      bounds.mockRestore();
      window.history.replaceState(null, "", "/");
      localStorage.removeItem(WHATS_NEW_SEEN_KEY);
      setAuthenticated(false);
    }
  },
);

it.each([true, false])(
  "handles a cold instance list and a missing kind (folders visible: %s)",
  async (visible) => {
    const list = Promise.withResolvers<void>();
    const listKey = [QueryKeys.ArrInstances];
    let recovered = false;
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { use_radarr: true },
          media_sources: { visible },
          ...(visible && { radarr: { ip: "127.0.0.1", apikey: "fixture" } }),
        }),
      ),
      http.get("/api/system/arr-instances", async () => {
        if (recovered) return HttpResponse.json([makeInstance()]);
        await list.promise;
        return new HttpResponse(null, { status: 503 });
      }),
      http.get("/api/system/media-sources", () =>
        HttpResponse.json({
          data: [],
          availability: { movie: ABSENT, series: ABSENT, sports: ABSENT },
        }),
      ),
      http.get("/api/badges", () =>
        HttpResponse.json({ movies: 9, radarr_signalr: "DOWN" }),
      ),
    );
    customRender(<Navigation />);

    const expectBadges = async () => {
      await waitFor(() =>
        expect(screen.getByText("My movies")).toHaveAttribute(
          "data-badge",
          "DOWN",
        ),
      );
      expect(screen.getByText("Missing / Movies")).toHaveAttribute(
        "data-badge",
        "9",
      );
    };
    const expectCold = async () => {
      if (visible) {
        expect(screen.queryByText("My movies")).not.toBeInTheDocument();
        expect(screen.queryByText("Missing / Movies")).not.toBeInTheDocument();
      } else {
        await expectBadges();
      }
    };
    try {
      await waitFor(() =>
        expect(queryClient.getQueryState(listKey)?.fetchStatus).toBe(
          "fetching",
        ),
      );
      await expectCold();
      list.resolve();
      await waitFor(() =>
        expect(queryClient.getQueryState(listKey)?.status).toBe("error"),
      );
      await expectCold();

      recovered = true;
      await act(async () => {
        await queryClient.invalidateQueries({ queryKey: listKey, exact: true });
      });
      await expectBadges();
    } finally {
      list.resolve();
    }
  },
);

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
        HttpResponse.json({
          episodes: 2,
          movies: 2,
          source_episodes: 2,
          source_movies: 2,
        }),
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

it.each([
  ["series", "sonarr", "/api/episodes/wanted", "/api/series", "seriesTitle"],
  ["movies", "radarr", "/api/movies/wanted", "/api/movies", "title"],
] as const)(
  "keeps hidden folder data out of the %s Wanted UI",
  async (kind, arr, wanted, action, titleKey) => {
    const user = userEvent.setup();
    const reads = vi.fn();
    const writes = vi.fn();
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { [`use_${arr}`]: true },
          media_sources: { visible: false },
        }),
      ),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/system/media-sources", () => {
        reads("folder API");
        return HttpResponse.json({ data: [] });
      }),
      http.get("/api/system/languages/audio", ({ request }) => {
        reads("audio", new URL(request.url).searchParams.get("sources"));
        return HttpResponse.json([]);
      }),
      http.get("/api/badges", ({ request }) => {
        reads("badges", new URL(request.url).searchParams.get("sources"));
        return HttpResponse.json({});
      }),
      http.get(wanted, ({ request }) => {
        const sources = new URL(request.url).searchParams.get("sources");
        reads("wanted", sources);
        return HttpResponse.json({
          data: [
            {
              id: 42,
              series_id: 42,
              [titleKey]: sources ? "Hidden folder title" : "Arr title",
              missing_subtitles: [],
            },
          ],
          total: 1,
        });
      }),
      http.patch(action, async ({ request }) => {
        const form = await request.formData();
        writes(form.get("action"), form.get("sources"));
        return new HttpResponse(null, { status: 204 });
      }),
    );
    customRender(<MediaPage group="wanted" path={kind} />);
    expect(await screen.findByText("Arr title")).toBeInTheDocument();
    expect(screen.queryByText("Hidden folder title")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "Source" }),
    ).not.toBeInTheDocument();
    expect(reads).toHaveBeenCalledWith("wanted", null);
    expect(reads).toHaveBeenCalledWith("badges", null);
    expect(reads).toHaveBeenCalledWith("audio", null);
    expect(reads).not.toHaveBeenCalledWith("folder API");
    await user.click(screen.getByRole("button", { name: "Search All" }));
    await waitFor(() =>
      expect(writes).toHaveBeenCalledWith("search-wanted", null),
    );
  },
);

it("drops mixed badge counts while the Arr-only badge is pending", async () => {
  let visible = true;
  const legacy = Promise.withResolvers<void>();
  function MovieBadge() {
    return <output>{useBadges().data?.movies ?? "No count"}</output>;
  }
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { use_radarr: true },
        media_sources: { visible },
      }),
    ),
    http.get("/api/badges", async ({ request }) => {
      const sources = new URL(request.url).searchParams.get("sources");
      if (sources === "1") return HttpResponse.json({ movies: 7 });
      await legacy.promise;
      return HttpResponse.json({ movies: 1 });
    }),
  );

  try {
    customRender(<MovieBadge />);
    expect(await screen.findByText("7")).toBeInTheDocument();
    visible = false;
    await act(async () => {
      await queryClient.invalidateQueries({
        queryKey: [QueryKeys.System, QueryKeys.Settings],
      });
    });
    expect(await screen.findByText("No count")).toBeInTheDocument();
    expect(screen.queryByText("7")).not.toBeInTheDocument();
  } finally {
    legacy.resolve();
  }
  expect(await screen.findByText("1")).toBeInTheDocument();
});
