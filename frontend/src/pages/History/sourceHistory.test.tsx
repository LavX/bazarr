/* eslint-disable camelcase -- API field names. */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { vi } from "vitest";
import { customRender, screen, waitFor, within } from "@/tests";
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
                    retained: true,
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
          return HttpResponse.json({
            data: keys.length ? [removed] : [native, live, removed],
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
    });

    it("links upgrades only to an original from the same origin on the page", async () => {
      configure();
      server.use(
        http.get(`/api/${api}/history`, () =>
          HttpResponse.json({
            data: [
              native,
              live,
              {
                ...live,
                item_title: "Outside page",
                history_id: 20,
                upgradedFromId: 13,
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
            total: 7,
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
      expect(
        within(screen.getByRole("row", { name: /Outside page/ })).queryByRole(
          "link",
          { name: "Original entry" },
        ),
      ).not.toBeInTheDocument();
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

    it("keeps the native table when opting in returns the legacy payload", async () => {
      configure({ noSources: true });
      server.use(
        http.get(`/api/${api}/history`, ({ request }) => {
          expect(new URL(request.url).searchParams.get("sources")).toBe("1");
          return HttpResponse.json({
            data: [{ ...native, origin: undefined, upgradedFromId: undefined }],
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
    });
  },
);
