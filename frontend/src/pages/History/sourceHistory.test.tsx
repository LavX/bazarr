/* eslint-disable camelcase -- API field names. */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { vi } from "vitest";
import { useWriteMediaSource } from "@/apis/hooks/mediaSources";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { createDefaultReducer } from "@/modules/socketio/reducer";
import { act, customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import MoviesHistoryView from "./Movies";
import SeriesHistoryView from "./Series";

const absent = { browsable: false, searchable: false, retained: false };

const historyRow = {
  action: 3,
  provider: "subdl",
  language: { code2: "en", name: "English" },
  subs_id: "release",
  matches: [],
  dont_matches: [],
  timestamp: "Today",
  parsed_timestamp: "November 23",
  upgradable: false,
  blacklisted: false,
};

const cases = [
  { kind: "series", arr: "sonarr", api: "episodes", View: SeriesHistoryView },
  { kind: "movie", arr: "radarr", api: "movies", View: MoviesHistoryView },
] as const;

function RemoveSource() {
  const mutation = useWriteMediaSource();
  return (
    <button
      onClick={() => mutation.mutate(() => api.mediaSources.remove(8))}
      disabled={mutation.isPending}
    >
      {mutation.isSuccess ? "Source removed" : "Remove source"}
    </button>
  );
}

afterEach(() => window.history.replaceState(null, "", "/"));

describe.each(cases)(
  "$api media-source History",
  ({ kind, arr, api, View }) => {
    const native = {
      ...historyRow,
      origin: "arr",
      source_key: "arr:5",
      arr_instance_id: 5,
      id: 31,
      history_id: 12,
      series_id: 33,
      seriesTitle: "Native title",
      title: "Native title",
      episodeTitle: "Native episode",
      episode_number: "1x4",
      sonarrSeriesId: 33,
      sonarrEpisodeId: 31,
      radarrId: 31,
      upgradedFromId: 7,
    };
    const live = {
      ...historyRow,
      origin: "source",
      source_key: "source:8",
      source_id: 8,
      source_name: "Archive",
      source_kind: "folder",
      id: 31,
      item_id: 31,
      history_id: 12,
      series_id: 33,
      group_title: "Archive series",
      item_title: "Archive item",
      video_path: "/archive/live.mkv",
      upgradedFromId: 7,
    };
    const secondOwner = {
      ...native,
      source_key: "arr:6",
      arr_instance_id: 6,
      id: 32,
      series_id: 34,
      seriesTitle: "Second native title",
      title: "Second native title",
      history_id: 14,
    };
    const removed = {
      ...live,
      source_key: "source:removed",
      source_id: null,
      source_name: "Old archive",
      source_kind: null,
      id: null,
      item_id: null,
      history_id: 13,
      series_id: null,
      group_title: "Retained series",
      item_title: "Retained item",
      video_path: "/archive/removed.mkv",
      upgradedFromId: null,
    };

    function configure({
      visible = true,
      nativeEnabled = true,
      retainedOnly = false,
      noSources = false,
      emptyFolder = false,
    } = {}) {
      server.use(
        http.get("/api/system/settings", () =>
          HttpResponse.json({
            general: { [`use_${arr}`]: nativeEnabled, page_size: 25 },
            media_sources: { visible },
          }),
        ),
        http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
        http.get("/api/system/arr-instances", () =>
          HttpResponse.json(
            nativeEnabled
              ? [
                  {
                    id: 5,
                    kind: arr,
                    name: arr,
                    enabled: true,
                    is_default: true,
                  },
                  {
                    id: 6,
                    kind: arr,
                    name: `${arr} secondary`,
                    enabled: true,
                    is_default: false,
                  },
                ]
              : [],
          ),
        ),
        http.get("/api/system/media-sources", () =>
          HttpResponse.json({
            data:
              retainedOnly || noSources
                ? []
                : [
                    {
                      id: 8,
                      name: "Archive",
                      kind: "folder",
                      enabled: true,
                      libraries: [{ content_kind: kind, enabled: true }],
                    },
                  ],
            availability: {
              series: absent,
              movie: absent,
              sports: absent,
              [kind]: noSources
                ? absent
                : {
                    browsable: !retainedOnly,
                    searchable: !retainedOnly,
                    retained: !emptyFolder,
                  },
            },
          }),
        ),
      );
    }

    it("keeps filters server-paged and renders retained snapshots alongside native links", async () => {
      const user = userEvent.setup();
      const requests = vi.fn();
      configure();
      window.history.replaceState(null, "", "/?page=2");
      server.use(
        http.get(`/api/${api}/history`, ({ request }) => {
          const params = new URL(request.url).searchParams;
          const keys = params.getAll("source_key[]");
          requests(
            params.get("sources"),
            keys,
            params.get("start"),
            params.get("length"),
          );
          const rows = keys.includes("source:removed")
            ? [removed]
            : keys.includes("source:8")
              ? [live]
              : keys.includes("arr:6") || params.get("start") === "50"
                ? [secondOwner]
                : [native, live, removed];
          return HttpResponse.json({
            data: rows,
            total: keys.length ? 1 : 75,
            source_keys: ["source:8", "source:removed"],
          });
        }),
      );

      customRender(<View />);
      const nativeLink = await screen.findByRole("link", {
        name: "Native title",
      });
      expect(nativeLink).toHaveAttribute(
        "href",
        kind === "series" ? "/series/33" : "/movies/31",
      );
      expect(requests).toHaveBeenCalledWith("1", [], "25", "25");
      const liveRow = screen.getByRole("row", { name: /Archive item/ });
      const retainedRow = screen.getByRole("row", { name: /Retained item/ });
      expect(within(liveRow).getByText("Archive")).toBeInTheDocument();
      expect(
        within(retainedRow).getByText("Old archive (removed)"),
      ).toBeInTheDocument();
      expect(
        within(retainedRow).getByText("Retained item"),
      ).toBeInTheDocument();
      expect(within(retainedRow).queryByRole("link")).not.toBeInTheDocument();
      expect(
        within(retainedRow).queryByRole("button", { name: "Exclude" }),
      ).not.toBeInTheDocument();
      await user.hover(
        within(retainedRow).getByRole("button", { name: "History details" }),
      );
      expect(
        await screen.findByText("/archive/removed.mkv"),
      ).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "3" }));
      expect(
        await screen.findByRole("link", { name: "Second native title" }),
      ).toHaveAttribute(
        "href",
        kind === "series" ? "/series/34" : "/movies/32",
      );
      expect(requests).toHaveBeenCalledWith("1", [], "50", "25");
      expect(screen.getByText("Show 51 to 75 of 75 entries")).toBeVisible();

      await user.click(screen.getByRole("combobox", { name: "Source" }));
      await user.click(
        await screen.findByRole("option", { name: "Removed sources" }),
      );
      await waitFor(() =>
        expect(requests).toHaveBeenCalledWith(
          "1",
          ["source:removed"],
          "0",
          "25",
        ),
      );
      expect(
        await screen.findByText("Show 1 to 1 of 1 entries"),
      ).toBeInTheDocument();
      expect(requests).not.toHaveBeenCalledWith(
        "1",
        ["source:removed"],
        "25",
        "25",
      );
      expect(screen.queryByText("Native title")).not.toBeInTheDocument();

      await user.click(screen.getByRole("combobox", { name: "Source" }));
      await user.click(await screen.findByRole("option", { name: "Archive" }));
      expect(await screen.findByText("Archive item")).toBeVisible();
      expect(requests).toHaveBeenCalledWith("1", ["source:8"], "0", "25");
      expect(screen.queryByText("Retained item")).not.toBeInTheDocument();
      expect(screen.queryByText("Native title")).not.toBeInTheDocument();

      await user.click(screen.getByRole("combobox", { name: "Source" }));
      await user.click(
        await screen.findByRole("option", { name: `${arr} secondary` }),
      );
      expect(
        await screen.findByRole("link", { name: "Second native title" }),
      ).toBeVisible();
      expect(requests).toHaveBeenCalledWith("1", ["arr:6"], "0", "25");
      expect(screen.queryByText("Archive item")).not.toBeInTheDocument();
      expect(screen.queryByText("Retained item")).not.toBeInTheDocument();

      await user.click(screen.getByRole("combobox", { name: "Source" }));
      await user.click(
        await screen.findByRole("option", { name: `${arr} secondary` }),
      );
      expect(
        await screen.findByRole("link", { name: "Native title" }),
      ).toBeVisible();
      expect(screen.getByText("Archive item")).toBeVisible();
      expect(screen.getByText("Retained item")).toBeVisible();
      expect(requests).toHaveBeenCalledWith("1", [], "0", "25");
      expect(screen.getByText("Show 1 to 25 of 75 entries")).toBeVisible();
      expect(new URLSearchParams(window.location.search).has("source")).toBe(
        false,
      );
    });

    it.each([false, true])(
      "refetches the selected History page when source-item precedes the commit: %s",
      async (earlyEvent) => {
        const user = userEvent.setup();
        const requests = vi.fn();
        let committed = false;
        configure();
        window.history.replaceState(null, "", "/?page=2&source=source%3A8");
        server.use(
          http.get(`/api/${api}/history`, ({ request }) => {
            const params = new URL(request.url).searchParams;
            requests(
              params.get("sources"),
              params.getAll("source_key[]"),
              params.get("start"),
              params.get("length"),
              params.get("include_embedded"),
            );
            return HttpResponse.json({
              data: [
                committed
                  ? {
                      ...live,
                      item_title: "New source history event",
                      history_id: 15,
                    }
                  : live,
              ],
              total: 75,
              source_keys: ["source:8"],
            });
          }),
          http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
        );
        const nativeKey = [
          ...(kind === "series"
            ? [QueryKeys.Series, QueryKeys.Episodes, QueryKeys.History]
            : [QueryKeys.Movies, QueryKeys.History]),
          false,
          { includeSources: false },
          QueryKeys.Range,
          { start: 25, size: 25 },
        ];
        queryClient.setQueryData(nativeKey, { data: [native], total: 75 });
        customRender(<View />);
        expect(await screen.findByText("Archive item")).toBeVisible();
        await user.click(
          screen.getByRole("switch", { name: "Show Embedded Source records" }),
        );
        await waitFor(() =>
          expect(requests).toHaveBeenLastCalledWith(
            "1",
            ["source:8"],
            "25",
            "25",
            "true",
          ),
        );
        const beforeEvent = requests.mock.calls.length;

        committed = !earlyEvent;
        await act(async () => {
          const reducer = createDefaultReducer().find(
            ({ key }) => key === "source-item",
          );
          expect(reducer).toBeDefined();
          reducer?.any?.();
        });

        if (earlyEvent) {
          // The source-item notification may reach the UI before the history write.
          // Let that first refresh finish against the old data, then commit it.
          await waitFor(() =>
            expect(requests).toHaveBeenCalledTimes(beforeEvent + 1),
          );
          await waitFor(() => expect(queryClient.isFetching()).toBe(0));
          expect(screen.getByText("Archive item")).toBeVisible();
          committed = true;
          await act(async () => {
            const reducer = createDefaultReducer().find(
              ({ key }) => key === "jobs",
            );
            if (reducer?.key !== "jobs")
              throw new Error("jobs reducer is missing");
            reducer.update?.([
              {
                job_id: 71,
                status: "completed",
                progress_value: 1,
                progress_max: 1,
                progress_message: "Finished source search",
              },
            ]);
          });
        }

        expect(
          await screen.findByText("New source history event"),
        ).toBeVisible();
        expect(requests).toHaveBeenCalledTimes(
          beforeEvent + (earlyEvent ? 2 : 1),
        );
        expect(requests).toHaveBeenLastCalledWith(
          "1",
          ["source:8"],
          "25",
          "25",
          "true",
        );
        expect(screen.getByText("Show 26 to 50 of 75 entries")).toBeVisible();
        expect(queryClient.getQueryState(nativeKey)?.isInvalidated).toBe(false);
      },
    );

    it("refreshes a recently visited History page after removing its source", async () => {
      const user = userEvent.setup();
      const requests = vi.fn();
      let deleted = false;
      configure();
      window.history.replaceState(null, "", "/?page=2");
      server.use(
        http.get(`/api/${api}/history`, ({ request }) => {
          const params = new URL(request.url).searchParams;
          requests(params.get("start"), params.get("length"));
          return HttpResponse.json({
            data: [native, deleted ? removed : live],
            total: 75,
            source_keys: [deleted ? "source:removed" : "source:8"],
          });
        }),
        http.delete("/api/system/media-sources/8", () => {
          deleted = true;
          return new HttpResponse(null, { status: 204 });
        }),
        http.get("/api/system/media-sources", () =>
          HttpResponse.json({
            data: deleted
              ? []
              : [
                  {
                    id: 8,
                    name: "Archive",
                    kind: "folder",
                    enabled: true,
                    libraries: [{ content_kind: kind, enabled: true }],
                  },
                ],
            availability: {
              series: absent,
              movie: absent,
              sports: absent,
              [kind]: {
                browsable: !deleted,
                searchable: !deleted,
                retained: true,
              },
            },
          }),
        ),
      );
      const { unmount: unmountHistory } = customRender(<View />);
      expect(await screen.findByText("Archive item")).toBeVisible();
      expect(requests).toHaveBeenCalledTimes(1);
      unmountHistory();

      const { unmount: unmountSettings } = customRender(<RemoveSource />);
      await user.click(screen.getByRole("button", { name: "Remove source" }));
      expect(
        await screen.findByRole("button", { name: "Source removed" }),
      ).toBeVisible();
      unmountSettings();

      customRender(<View />);
      expect(await screen.findByText("Retained item")).toBeVisible();
      expect(screen.getByText("Old archive (removed)")).toBeVisible();
      expect(screen.queryByText("Archive item")).not.toBeInTheDocument();
      expect(requests).toHaveBeenCalledTimes(2);
      expect(requests).toHaveBeenLastCalledWith("25", "25");
      expect(screen.getByText("Show 26 to 50 of 75 entries")).toBeVisible();
      await user.click(screen.getByRole("combobox", { name: "Source" }));
      expect(
        await screen.findByRole("option", { name: "Removed sources" }),
      ).toBeVisible();
      expect(
        screen.queryByRole("option", { name: "Archive" }),
      ).not.toBeInTheDocument();
    });

    it("shows verified originals on this page and in earlier pages", async () => {
      const user = userEvent.setup();
      configure();
      const original = {
        history_id: 7,
        timestamp_iso: "2026-11-20T12:00:00",
        action: 1,
        provider: "subdl",
        description: "Original download",
      };
      const arrOriginal = { ...original, origin: "arr", source_key: "arr:5" };
      const sourceOriginal = {
        ...original,
        origin: "source",
        source_key: "source:8",
      };
      server.use(
        http.get(`/api/${api}/history`, () =>
          HttpResponse.json({
            data: [
              { ...native, original: arrOriginal },
              { ...live, original: sourceOriginal },
              {
                ...live,
                item_title: "Source upgrade",
                history_id: 20,
                upgradedFromId: 13,
                original: {
                  ...sourceOriginal,
                  history_id: 13,
                  description: "Source download on an earlier page",
                },
              },
              {
                ...native,
                title: "Arr upgrade",
                seriesTitle: "Arr upgrade",
                history_id: 21,
                upgradedFromId: 22,
                original: {
                  ...arrOriginal,
                  history_id: 22,
                  description: "Arr download on an earlier page",
                },
              },
              {
                ...live,
                item_title: "Unverified upgrade",
                history_id: 23,
                upgradedFromId: 7,
                original: null,
              },
              {
                ...live,
                item_title: "Different owner",
                history_id: 24,
                original: { ...sourceOriginal, source_key: "source:9" },
              },
              {
                ...live,
                item_title: "Different origin",
                history_id: 25,
                original: arrOriginal,
              },
              {
                ...removed,
                item_title: "Removed owner",
                history_id: 26,
                original: { ...sourceOriginal, source_key: "source:removed" },
              },
              {
                ...native,
                title: "Native original",
                seriesTitle: "Native original",
                action: 1,
                history_id: 7,
                upgradedFromId: null,
              },
              {
                ...live,
                item_title: "Archive original",
                action: 1,
                history_id: 7,
                upgradedFromId: null,
              },
              {
                ...native,
                title: "Unrelated native event",
                seriesTitle: "Unrelated native event",
                action: 1,
                history_id: 13,
                upgradedFromId: null,
              },
            ],
            total: 75,
            source_keys: ["source:8"],
          }),
        ),
      );
      customRender(<View />);

      const arr = await screen.findByRole("row", { name: /Native title/ });
      const source = screen.getByRole("row", { name: /Archive item/ });
      expect(
        within(arr).getByRole("link", { name: "Original entry" }),
      ).toHaveAttribute("href", "#history-arr-7");
      expect(
        within(source).getByRole("link", { name: "Original entry" }),
      ).toHaveAttribute("href", "#history-source-7");

      for (const origin of ["Source", "Arr"]) {
        const row = screen.getByRole("row", {
          name: new RegExp(`${origin} upgrade`),
        });
        expect(
          within(row).queryByRole("link", { name: "Original entry" }),
        ).not.toBeInTheDocument();
        await user.click(
          within(row).getByRole("button", { name: "Original entry" }),
        );
        await waitFor(() =>
          expect(
            screen.getByText(`${origin} download on an earlier page`),
          ).toBeVisible(),
        );
        await user.keyboard("{Escape}");
      }

      for (const title of [
        "Unverified upgrade",
        "Different owner",
        "Different origin",
        "Removed owner",
      ]) {
        const row = screen.getByRole("row", { name: new RegExp(title) });
        expect(
          within(row).queryByText("Original entry"),
        ).not.toBeInTheDocument();
      }
    });

    it("refreshes only mixed History of the removed source's kind from a socket event", async () => {
      const requests = vi.fn();
      let deleted = false;
      configure();
      window.history.replaceState(null, "", "/?page=2");
      server.use(
        http.get(`/api/${api}/history`, ({ request }) => {
          const params = new URL(request.url).searchParams;
          requests(
            params.get("sources"),
            params.get("start"),
            params.get("length"),
          );
          return HttpResponse.json({
            data: [native, deleted ? removed : live],
            total: 75,
            source_keys: [deleted ? "source:removed" : "source:8"],
          });
        }),
      );
      const sameKind =
        kind === "series"
          ? [QueryKeys.Series, QueryKeys.Episodes, QueryKeys.History]
          : [QueryKeys.Movies, QueryKeys.History];
      const otherKind =
        kind === "series"
          ? [QueryKeys.Movies, QueryKeys.History]
          : [QueryKeys.Series, QueryKeys.Episodes, QueryKeys.History];
      const nativeKey = [
        ...sameKind,
        false,
        { includeSources: false },
        QueryKeys.Range,
        { start: 25, size: 25 },
      ];
      const otherMixedKey = [
        ...otherKind,
        false,
        { includeSources: true },
        QueryKeys.Range,
        { start: 25, size: 25 },
      ];
      queryClient.setQueryData(nativeKey, { data: [native], total: 75 });
      queryClient.setQueryData(otherMixedKey, { data: [live], total: 75 });
      customRender(<View />);
      expect(await screen.findByText("Archive item")).toBeVisible();

      deleted = true;
      await act(async () => {
        const reducer = createDefaultReducer().find(
          ({ key }) => key === "source-removed",
        );
        if (reducer?.key !== "source-removed")
          throw new Error("source-removed reducer is missing");
        reducer.update?.([
          { kinds: [kind === "series" ? "episode" : "movie"] },
        ]);
      });

      expect(await screen.findByText("Retained item")).toBeVisible();
      expect(screen.queryByText("Archive item")).not.toBeInTheDocument();
      expect(requests).toHaveBeenLastCalledWith("1", "25", "25");
      expect(queryClient.getQueryState(nativeKey)?.isInvalidated).toBe(false);
      expect(queryClient.getQueryState(otherMixedKey)?.isInvalidated).toBe(
        false,
      );
    });

    it("includes removed sources on a retained-only page and preserves the embedded toggle", async () => {
      const user = userEvent.setup();
      const requests = vi.fn();
      configure({ nativeEnabled: false, retainedOnly: true });
      server.use(
        http.get(`/api/${api}/history`, ({ request }) => {
          const params = new URL(request.url).searchParams;
          requests(
            params.get("sources"),
            params.getAll("source_key[]"),
            params.get("include_embedded"),
          );
          const scoped = params
            .getAll("source_key[]")
            .includes("source:removed");
          return HttpResponse.json({
            data: scoped ? [removed] : [native, removed],
            total: scoped ? 1 : 2,
            source_keys: ["source:removed"],
          });
        }),
      );
      customRender(<View />);

      expect(await screen.findByText("Retained item")).toBeInTheDocument();
      expect(requests).toHaveBeenCalledWith("1", ["source:removed"], "false");
      expect(screen.queryByText("Native title")).not.toBeInTheDocument();
      await user.click(
        screen.getByRole("switch", { name: "Show Embedded Source records" }),
      );
      await waitFor(() =>
        expect(requests).toHaveBeenCalledWith("1", ["source:removed"], "true"),
      );
    });

    it("keeps a hidden-source installation on the native request and table", async () => {
      const requests = vi.fn();
      configure({ visible: false });
      window.history.replaceState(null, "", "/?source=source%3Aremoved");
      server.use(
        http.get(`/api/${api}/history`, ({ request }) => {
          const params = new URL(request.url).searchParams;
          requests(params.get("sources"), params.getAll("source_key[]"));
          return HttpResponse.json({ data: [native], total: 1 });
        }),
      );
      customRender(<View />);

      expect(
        await screen.findByRole("link", { name: "Native title" }),
      ).toBeInTheDocument();
      expect(requests).toHaveBeenCalledWith(null, []);
      expect(screen.getByRole("button", { name: "Exclude" })).toBeEnabled();
      expect(
        screen.queryByRole("combobox", { name: "Source" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("columnheader", { name: "Source" }),
      ).not.toBeInTheDocument();
    });

    it.each(["source:8", "source:removed"])(
      "clears a saved %s filter when this kind has no source history",
      async (source) => {
        const requests = vi.fn();
        configure({ emptyFolder: true });
        window.history.replaceState(
          null,
          "",
          `/?page=2&source=${encodeURIComponent(source)}`,
        );
        server.use(
          http.get(`/api/${api}/history`, ({ request }) => {
            const params = new URL(request.url).searchParams;
            const keys = params.getAll("source_key[]");
            requests(
              params.get("sources"),
              keys,
              params.get("start"),
              params.get("length"),
            );
            return HttpResponse.json({
              data: keys.length ? [] : [native],
              total: keys.length ? 0 : 1,
            });
          }),
        );
        customRender(<View />);

        expect(
          await screen.findByRole("link", { name: "Native title" }),
        ).toBeVisible();
        await waitFor(() =>
          expect(
            new URLSearchParams(window.location.search).has("source"),
          ).toBe(false),
        );
        expect(new URLSearchParams(window.location.search).has("page")).toBe(
          false,
        );
        expect(requests).toHaveBeenLastCalledWith("1", [], "0", "25");
        expect(
          screen.queryByRole("combobox", { name: "Source" }),
        ).not.toBeInTheDocument();
        expect(
          screen.queryByRole("columnheader", { name: "Source" }),
        ).not.toBeInTheDocument();
        expect(screen.getByRole("button", { name: "Exclude" })).toBeEnabled();
      },
    );

    it("clears the mounted selection after the final source event disappears", async () => {
      const user = userEvent.setup();
      const requests = vi.fn();
      let sourceHistoryRemoved = false;
      configure();
      server.use(
        http.get(`/api/${api}/history`, ({ request }) => {
          const params = new URL(request.url).searchParams;
          const keys = params.getAll("source_key[]");
          requests(params.get("sources"), keys);
          if (sourceHistoryRemoved) {
            return HttpResponse.json({
              data: keys.length ? [] : [native],
              total: keys.length ? 0 : 1,
            });
          }
          return HttpResponse.json({
            data: keys.length ? [live] : [native, live],
            total: keys.length ? 1 : 2,
            source_keys: ["source:8"],
          });
        }),
      );
      customRender(<View />);
      expect(await screen.findByText("Archive item")).toBeVisible();
      await user.click(screen.getByRole("combobox", { name: "Source" }));
      await user.click(await screen.findByRole("option", { name: "Archive" }));
      await waitFor(() =>
        expect(requests).toHaveBeenLastCalledWith("1", ["source:8"]),
      );
      expect(new URLSearchParams(window.location.search).get("source")).toBe(
        "source:8",
      );
      expect(screen.queryByText("Native title")).not.toBeInTheDocument();

      sourceHistoryRemoved = true;
      await act(async () => {
        const reducer = createDefaultReducer().find(
          ({ key }) => key === "source-item",
        );
        if (reducer?.key !== "source-item")
          throw new Error("source-item reducer is missing");
        reducer.any?.();
      });

      await waitFor(() => {
        expect(requests).toHaveBeenLastCalledWith("1", []);
        expect(queryClient.isFetching()).toBe(0);
        expect(
          screen.getByRole("link", { name: "Native title" }),
        ).toBeVisible();
        expect(screen.queryByText("Archive item")).not.toBeInTheDocument();
      });
      await waitFor(() =>
        expect(new URLSearchParams(window.location.search).has("source")).toBe(
          false,
        ),
      );
      expect(requests).toHaveBeenLastCalledWith("1", []);
      expect(screen.queryByText("Archive item")).not.toBeInTheDocument();
      expect(
        screen.queryByRole("combobox", { name: "Source" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("columnheader", { name: "Source" }),
      ).not.toBeInTheDocument();

      sourceHistoryRemoved = false;
      await act(async () => {
        const reducer = createDefaultReducer().find(
          ({ key }) => key === "source-item",
        );
        if (reducer?.key !== "source-item")
          throw new Error("source-item reducer is missing");
        reducer.any?.();
      });

      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
        expect(screen.getByText("Archive item")).toBeVisible();
        expect(
          screen.getByRole("link", { name: "Native title" }),
        ).toBeVisible();
        expect(
          screen.getByRole("columnheader", { name: "Source" }),
        ).toBeVisible();
      });
      await user.click(screen.getByRole("combobox", { name: "Source" }));
      await user.click(await screen.findByRole("option", { name: "Archive" }));
      await waitFor(() =>
        expect(requests).toHaveBeenLastCalledWith("1", ["source:8"]),
      );
      expect(new URLSearchParams(window.location.search).get("source")).toBe(
        "source:8",
      );
      expect(screen.queryByText("Native title")).not.toBeInTheDocument();
    });

    it("drops automatic source-only keys when no source history remains", async () => {
      const requests = vi.fn();
      configure({ nativeEnabled: false, emptyFolder: true });
      server.use(
        http.get(`/api/${api}/history`, ({ request }) => {
          const params = new URL(request.url).searchParams;
          const keys = params.getAll("source_key[]");
          requests(params.get("sources"), keys);
          return HttpResponse.json({
            data: keys.length ? [] : [native],
            total: keys.length ? 0 : 1,
          });
        }),
      );
      customRender(<View />);

      expect(
        await screen.findByRole("link", { name: "Native title" }),
      ).toBeVisible();
      expect(requests).toHaveBeenCalledWith("1", [
        "source:removed",
        "source:8",
      ]);
      expect(requests).toHaveBeenLastCalledWith("1", []);
      expect(
        screen.queryByRole("combobox", { name: "Source" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("columnheader", { name: "Source" }),
      ).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Exclude" })).toBeEnabled();
    });

    it("preserves a selected source on an empty page when kind-wide metadata still has source history", async () => {
      const requests = vi.fn();
      configure();
      window.history.replaceState(null, "", "/?source=source%3A8");
      server.use(
        http.get(`/api/${api}/history`, ({ request }) => {
          const params = new URL(request.url).searchParams;
          requests(params.get("sources"), params.getAll("source_key[]"));
          return HttpResponse.json({
            data: [],
            total: 0,
            source_keys: ["source:9"],
          });
        }),
      );
      customRender(<View />);

      expect(
        await screen.findByText(
          `Nothing Found in ${kind === "series" ? "Series" : "Movies"} History`,
        ),
      ).toBeVisible();
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));
      expect(new URLSearchParams(window.location.search).get("source")).toBe(
        "source:8",
      );
      await waitFor(() =>
        expect(screen.getByRole("combobox", { name: "Source" })).toHaveValue(
          "Archive",
        ),
      );
      expect(
        screen.getByRole("columnheader", { name: "Source" }),
      ).toBeVisible();
      expect(requests).toHaveBeenLastCalledWith("1", ["source:8"]);
      expect(screen.queryByText("Native title")).not.toBeInTheDocument();
    });

    it("keeps the selected source while a stale native payload is being refreshed", async () => {
      const requests = vi.fn();
      let finishRefresh: (() => void) | undefined;
      const refreshing = new Promise<void>((resolve) => {
        finishRefresh = resolve;
      });
      configure();
      window.history.replaceState(null, "", "/?source=source%3A8");
      const scopedKey = [
        ...(kind === "series"
          ? [QueryKeys.Series, QueryKeys.Episodes, QueryKeys.History]
          : [QueryKeys.Movies, QueryKeys.History]),
        false,
        { includeSources: true, sourceKeys: ["source:8"] },
        QueryKeys.Range,
        { start: 0, size: 25 },
      ];
      queryClient.setQueryData(
        scopedKey,
        { data: [native], total: 1 },
        { updatedAt: Date.now() - 120_000 },
      );
      server.use(
        http.get(`/api/${api}/history`, async ({ request }) => {
          const params = new URL(request.url).searchParams;
          requests(params.get("sources"), params.getAll("source_key[]"));
          await refreshing;
          return HttpResponse.json({
            data: [],
            total: 0,
            source_keys: ["source:9"],
          });
        }),
      );
      customRender(<View />);

      try {
        await waitFor(() => expect(requests).toHaveBeenCalledTimes(1));
        expect(queryClient.getQueryState(scopedKey)?.fetchStatus).toBe(
          "fetching",
        );
        expect(requests).toHaveBeenLastCalledWith("1", ["source:8"]);
        expect(new URLSearchParams(window.location.search).get("source")).toBe(
          "source:8",
        );
      } finally {
        finishRefresh?.();
      }

      await waitFor(() => expect(queryClient.isFetching()).toBe(0));
      await waitFor(() =>
        expect(screen.getByRole("combobox", { name: "Source" })).toHaveValue(
          "Archive",
        ),
      );
      expect(new URLSearchParams(window.location.search).get("source")).toBe(
        "source:8",
      );
      expect(requests).toHaveBeenCalledTimes(1);
      expect(requests).toHaveBeenLastCalledWith("1", ["source:8"]);
    });

    it("shows the recorded owner name on a non-linkable handover event", async () => {
      configure();
      server.use(
        http.get(`/api/${api}/history`, () =>
          HttpResponse.json({
            data: [
              {
                ...live,
                source_name: "Recorded folder",
                id: null,
                item_id: null,
                series_id: null,
              },
            ],
            total: 1,
            source_keys: ["source:8"],
          }),
        ),
      );
      customRender(<View />);

      const row = await screen.findByRole("row", { name: /Archive item/ });
      expect(within(row).getByText("Recorded folder")).toBeVisible();
      expect(within(row).queryByText("Archive")).not.toBeInTheDocument();
      expect(within(row).queryByRole("link")).not.toBeInTheDocument();
      expect(
        within(row).queryByRole("button", { name: "Exclude" }),
      ).not.toBeInTheDocument();
    });

    it.each([false, true])(
      "keeps the native table for a legacy payload with an empty folder: %s",
      async (emptyFolder) => {
        configure({ noSources: !emptyFolder, emptyFolder });
        server.use(
          http.get(`/api/${api}/history`, ({ request }) => {
            expect(new URL(request.url).searchParams.get("sources")).toBe("1");
            return HttpResponse.json({
              data: [
                { ...native, origin: undefined, upgradedFromId: undefined },
              ],
              total: 1,
            });
          }),
        );
        customRender(<View />);
        expect(
          await screen.findByRole("link", { name: "Native title" }),
        ).toBeVisible();
        expect(screen.getByRole("button", { name: "Exclude" })).toBeEnabled();
        expect(
          screen.queryByRole("columnheader", { name: "Source" }),
        ).not.toBeInTheDocument();
        expect(
          screen.queryByRole("combobox", { name: "Source" }),
        ).not.toBeInTheDocument();
      },
    );
  },
);
