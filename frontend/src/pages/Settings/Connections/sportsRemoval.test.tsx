/* eslint-disable camelcase */
import { createMemoryRouter, RouterProvider } from "react-router";
import { act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import type { MediaSource } from "@/apis/raw/mediaSources";
import BlacklistSportsView from "@/pages/Blacklist/Sports";
import SportsHistoryView from "@/pages/History/Sports";
import WantedSportsView from "@/pages/Wanted/Sports";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { makeInstance } from "./__tests__/fixtures";
import SettingsConnectionsView from "./index";

const unrelatedKey = [QueryKeys.Movies, "library", "unrelated"];
const unrelatedData = { data: [{ title: "Unrelated movie" }], total: 1 };

it.each([
  ["wanted", WantedSportsView],
  ["history", SportsHistoryView],
  ["blacklist", BlacklistSportsView],
] as const)(
  "refreshes cached sports %s after removing a library and its retained connection",
  async (route, Page) => {
    const user = userEvent.setup();
    const source: MediaSource = {
      id: 8,
      name: "Sports archive",
      kind: "folder",
      enabled: true,
      scan_frequency_hours: 12,
      libraries: [
        {
          id: 9,
          locator: "/media/sports",
          content_kind: "sports",
          enabled: true,
          profile_id: null,
          last_scan_status: null,
          last_scan_message: null,
        },
      ],
    };
    let removedLibrary = false;
    let removedSource = false;
    let reads = 0;
    const remainingLibraries =
      route === "wanted"
        ? [{ ...source.libraries[0], id: 10, locator: "/media/other-sports" }]
        : [];
    source.libraries.push(...remainingLibraries);
    const absent = { browsable: false, searchable: false, retained: false };
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { use_sportarr: false } }),
      ),
      http.get("/api/system/arr-instances", () => HttpResponse.json([])),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/system/media-server-instances", () =>
        HttpResponse.json({ data: [] }),
      ),
      http.get("/api/system/media-sources", () =>
        HttpResponse.json({
          data: removedSource
            ? []
            : [
                {
                  ...source,
                  libraries: removedLibrary
                    ? remainingLibraries
                    : source.libraries,
                },
              ],
          availability: {
            movie: absent,
            series: absent,
            sports: {
              browsable:
                !removedSource &&
                (!removedLibrary || remainingLibraries.length > 0),
              searchable:
                !removedSource &&
                (!removedLibrary || remainingLibraries.length > 0),
              retained: true,
              retained_source_ids: removedSource ? [] : [source.id],
              removed: removedSource,
            },
          },
        }),
      ),
      http.get(`/api/sports/${route}`, () => {
        reads++;
        if (route === "wanted" && removedLibrary) {
          return HttpResponse.json({ data: [], total: 0 });
        }
        return HttpResponse.json({
          data: [
            {
              id: 7,
              origin: "source",
              arr_instance_id: null,
              league_id: null,
              event_id: 70,
              source_id: removedSource ? null : source.id,
              source_item_id: removedLibrary ? null : 70,
              source_group_id: removedLibrary ? null : 17,
              source_name: removedSource ? null : source.name,
              source_key: removedSource ? "source:removed" : "source:8",
              source_kind: "folder",
              title: "Archive final",
              path: "/media/sports/final.mkv",
              identity_state: "parsed",
              missing_subtitles: ["en"],
              audio_language: [],
              language: "en",
              action: 1,
            },
          ],
          total: 1,
        });
      }),
      http.delete("/api/system/media-sources/8/libraries/9", () => {
        removedLibrary = true;
        return new HttpResponse(null, { status: 204 });
      }),
      http.delete("/api/system/media-sources/8", () => {
        removedSource = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    queryClient.setQueryData(unrelatedKey, unrelatedData);
    const leagueKey = [QueryKeys.Sports, "leagues", { includeSources: true }];
    queryClient.setQueryData(leagueKey, {
      data: [{ title: "Cached sports league" }],
      total: 1,
    });
    const router = createMemoryRouter(
      [
        { path: "/activity", element: <Page /> },
        { path: "/settings/connections", element: <SettingsConnectionsView /> },
      ],
      { initialEntries: ["/activity"] },
    );
    rawRender(
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>,
    );
    expect(
      await screen.findByRole("link", { name: "Archive final" }),
    ).toHaveAttribute("href", "/sports/source/17");
    const initialReads = reads;
    await act(() => router.navigate("/settings/connections#folders"));
    await user.click(
      await screen.findByRole("button", {
        name: "Actions for library /media/sports",
      }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "Remove library" }),
    );
    await user.click(
      within(
        await screen.findByRole("dialog", { name: "Remove library" }),
      ).getByRole("button", { name: "Remove library" }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    if (route === "wanted") {
      expect(queryClient.getQueryState(leagueKey)?.isInvalidated).toBe(true);
    }
    await act(() => router.navigate("/activity"));
    await waitFor(() => {
      if (route === "wanted") {
        expect(screen.queryByText("Archive final")).toBeNull();
      } else {
        expect(screen.getByText("Archive final")).toBeVisible();
      }
      expect(screen.queryByRole("link", { name: "Archive final" })).toBeNull();
    });
    expect(reads).toBeGreaterThan(initialReads);
    const retainedQueries = queryClient
      .getQueryCache()
      .findAll({ queryKey: [QueryKeys.Sports, route] });
    await act(() => router.navigate("/settings/connections#folders"));
    await user.click(
      await screen.findByRole("button", {
        name: "More actions for Sports archive",
      }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "Remove source" }),
    );
    await user.click(
      within(
        await screen.findByRole("dialog", { name: "Remove source" }),
      ).getByRole("button", { name: "Remove source" }),
    );
    await screen.findByText("No folder sources yet");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(retainedQueries.every((query) => query.state.isInvalidated)).toBe(
      true,
    );
    await act(() => router.navigate("/activity"));
    if (route !== "wanted") {
      expect(await screen.findByText("Archive final")).toBeVisible();
    }
    expect(screen.queryByRole("link", { name: "Archive final" })).toBeNull();
    expect(queryClient.getQueryData(unrelatedKey)).toEqual(unrelatedData);
    expect(queryClient.getQueryState(unrelatedKey)?.isInvalidated).toBe(false);
  },
);

it.each([
  ["sportarr", "stale"],
  ["sportarr", "absent"],
  ["radarr", "stale"],
] as const)(
  "invalidates sports activity only when deleting a %s connection with a %s instance cache",
  async (kind, cache) => {
    const user = userEvent.setup();
    const instance = makeInstance({ kind });
    let removed = false;
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { use_sportarr: true, use_radarr: true },
        }),
      ),
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json(removed ? [] : [instance]),
      ),
      http.get("/api/system/media-server-instances", () =>
        HttpResponse.json({ data: [] }),
      ),
      http.delete(`/api/system/arr-instances/${instance.id}`, () => {
        removed = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const historyKey = [
      QueryKeys.Sports,
      QueryKeys.History,
      { sourceKeys: [] },
    ];
    queryClient.setQueryData(historyKey, {
      data: [{ title: "Cached final" }],
      total: 1,
    });
    queryClient.setQueryData(unrelatedKey, unrelatedData);
    const router = createMemoryRouter(
      [{ path: "/settings/connections", element: <SettingsConnectionsView /> }],
      { initialEntries: [`/settings/connections#${kind}`] },
    );
    rawRender(
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>,
    );
    await waitFor(() =>
      expect(screen.getByRole("switch", { name: "Enabled" })).toBeChecked(),
    );
    await user.click(
      await screen.findByRole("button", {
        name: `More actions for ${instance.name}`,
      }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
    const dialog = within(
      await screen.findByRole("dialog", { name: "Delete instance" }),
    );
    await act(async () => {
      if (cache === "absent") {
        queryClient.removeQueries({ queryKey: [QueryKeys.ArrInstances] });
      } else {
        queryClient.setQueryData([QueryKeys.ArrInstances], []);
      }
    });
    await user.click(dialog.getByRole("button", { name: "Delete instance" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(removed).toBe(true);
    expect(queryClient.getQueryState(historyKey)?.isInvalidated).toBe(
      kind === "sportarr",
    );
    expect(queryClient.getQueryData(unrelatedKey)).toEqual(unrelatedData);
    expect(queryClient.getQueryState(unrelatedKey)?.isInvalidated).toBe(false);
  },
);
