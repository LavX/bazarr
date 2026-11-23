/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { vi } from "vitest";
import {
  useEpisodeBlacklist,
  useEpisodeDeleteBlacklist,
} from "@/apis/hooks/episodes";
import {
  useMovieBlacklist,
  useMovieDeleteBlacklist,
} from "@/apis/hooks/movies";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { openSelect } from "@/tests/select";
import Movies from "./Movies";
import Series from "./Series";

describe.each(["movie", "series"] as const)("%s source exclusions", (kind) => {
  const isMovie = kind === "movie";
  const endpoint = isMovie ? "movies" : "episodes";
  const arrKind = isMovie ? "radarr" : "sonarr";
  const titleKey = isMovie ? "title" : "seriesTitle";
  const detail = isMovie ? "movies" : "series";
  const View = isMovie ? Movies : Series;
  const useBlacklist = isMovie ? useMovieBlacklist : useEpisodeBlacklist;
  const useRemove = isMovie
    ? useMovieDeleteBlacklist
    : useEpisodeDeleteBlacklist;
  beforeEach(() => window.history.replaceState(null, "", "/"));
  afterEach(() => window.history.replaceState(null, "", "/"));
  const empty = `No blacklisted ${detail} subtitles`;
  const common = {
    provider: "opensubtitlescom",
    subs_id: "subtitle-123",
    language: {
      name: "English",
      code2: "en",
      code3: "eng",
      forced: false,
      hi: false,
    },
    timestamp: "now",
    parsed_timestamp: "11/23/26 12:00:00",
    ...(kind === "series"
      ? { episode_number: "1x02", episodeTitle: "The second episode" }
      : {}),
  };
  const arr = {
    ...common,
    id: 81,
    origin: "arr",
    source_key: "arr:2",
    arr_instance_id: 2,
    [titleKey]: "Arr title",
    ...(kind === "movie" ? { radarrId: 141 } : { sonarrSeriesId: 141 }),
  };
  const source = {
    ...common,
    id: 81,
    origin: "source",
    removal_key: "original-exclusion-key",
    source_key: "source:7",
    source_id: 7,
    source_item_id: 501,
    source_name: "Media Folder",
    source_kind: "folder",
    [titleKey]: "Folder title",
    video_path: "/media/folder-video.mkv",
  };
  const removed = {
    ...source,
    id: 82,
    source_key: "source:removed",
    source_id: null,
    source_item_id: null,
    source_name: "Old Folder",
    source_kind: null,
    [titleKey]: "Recorded removed title",
    video_path: "/removed/recorded-video.mkv",
  };
  const retained = {
    ...source,
    id: 83,
    source_key: "source:8",
    source_id: 8,
    source_name: "Retained Folder",
    [titleKey]: "Retained folder title",
  };

  function mockExclusions(conflict = false) {
    const gets = vi.fn();
    const deletes = vi.fn();
    let rows = [arr, source, removed, retained];
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { [`use_${arrKind}`]: true },
          media_sources: { visible: true },
        }),
      ),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([
          {
            id: 2,
            kind: arrKind,
            enabled: true,
            is_default: true,
            name: "Home Arr",
          },
        ]),
      ),
      http.get("/api/system/media-sources", () =>
        HttpResponse.json({
          data: [
            {
              id: 7,
              kind: "folder",
              name: "Media Folder",
              enabled: true,
              libraries: [{ content_kind: kind, enabled: true }],
            },
            {
              id: 8,
              kind: "folder",
              name: "Retained Folder",
              enabled: true,
              libraries: [],
            },
          ],
        }),
      ),
      http.get(`/api/${endpoint}/blacklist`, ({ request }) => {
        const params = new URL(request.url).searchParams;
        const keys = params.getAll("source_key[]");
        const sources = params.get("sources");
        gets({ sources, keys });
        const filtered = rows.filter(
          (row) =>
            (sources === "1" || row.origin === "arr") &&
            (!keys.length || keys.includes(row.source_key)),
        );
        const length = Number(params.get("length"));
        const data = length > 0 ? filtered.slice(0, length) : filtered;
        const sourceFilters = rows
          .filter((row) => "source_name" in row)
          .map((row) => ({
            key: row.source_key,
            name:
              row.source_key === "source:removed"
                ? "Removed sources"
                : row.source_name,
          }));
        return HttpResponse.json(
          sources === "1"
            ? { data, total: filtered.length, source_filters: sourceFilters }
            : { data },
        );
      }),
      http.delete(`/api/${endpoint}/blacklist`, async ({ request }) => {
        const params = new URL(request.url).searchParams;
        const text = await request.clone().text();
        const form = text ? Object.fromEntries(await request.formData()) : {};
        const keys = params.getAll("source_key[]");
        deletes({ params: Object.fromEntries(params), keys, form });
        if (params.get("all") === "true") {
          rows = keys.length
            ? rows.filter((row) => !keys.includes(row.source_key))
            : [];
        } else if (params.has("source_blacklist_id")) {
          if (conflict) {
            conflict = false;
            rows = rows.map((row) =>
              row === source
                ? {
                    ...row,
                    [titleKey]: "Updated exclusion",
                    removal_key: "updated-key",
                  }
                : row,
            );
            return HttpResponse.json(
              { message: "The exclusion has changed." },
              { status: 409 },
            );
          }
          rows = rows.filter(
            (row) =>
              row.origin !== "source" ||
              row.id !== Number(params.get("source_blacklist_id")),
          );
        } else {
          rows = rows.filter((row) => row.origin !== "arr");
        }
        return new HttpResponse(null, { status: 204 });
      }),
    );
    function expectDelete(
      params: Record<string, string>,
      keys: string[] = [],
      form: Record<string, string> = {},
    ) {
      return waitFor(() =>
        expect(deletes).toHaveBeenCalledWith({ params, keys, form }),
      );
    }
    return { gets, expectDelete };
  }

  it("renders active and removed sources as recorded titles without detail links", async () => {
    const { gets } = mockExclusions();
    customRender(<View />);
    expect(
      await screen.findByRole("link", { name: "Arr title" }),
    ).toHaveAttribute("href", `/${detail}/81`);
    for (const [title, badge] of [
      ["Folder title", "Media Folder"],
      ["Recorded removed title", "Old Folder (removed)"],
    ]) {
      const row = await screen.findByRole("row", { name: new RegExp(title) });
      expect(within(row).queryByRole("link")).not.toBeInTheDocument();
      expect(within(row).getByText(badge)).toBeInTheDocument();
    }
    expect(screen.getByText("/removed/recorded-video.mkv")).toBeInTheDocument();
    expect(gets).toHaveBeenCalledWith({ sources: "1", keys: [] });
  });

  it("selects a configured source in the URL and removes only its exclusions", async () => {
    const user = userEvent.setup();
    const { gets, expectDelete } = mockExclusions();
    customRender(<View />);
    await screen.findByText("Folder title");
    expect(
      screen.getByRole("combobox", { name: "Source" }),
    ).toBeInTheDocument();
    const listbox = await openSelect(user, "All sources");
    await user.click(
      within(listbox).getByRole("option", {
        name: "Media Folder",
        hidden: true,
      }),
    );
    await waitFor(() =>
      expect(gets).toHaveBeenCalledWith({ sources: "1", keys: ["source:7"] }),
    );
    expect(
      new URLSearchParams(window.location.search).getAll("source"),
    ).toEqual(["source:7"]);
    await waitFor(() =>
      expect(screen.queryByText("Arr title")).not.toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: "Remove All" }));
    await expectDelete(
      { all: "true", sources: "1", "source_key[]": "source:7" },
      ["source:7"],
    );
    expect(await screen.findByText(empty)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove All" })).toBeDisabled();
  });

  it("sends repeated source keys through the real query and removal hooks", async () => {
    const sourceKeys: MediaSourceKey[] = ["source:7", "source:removed"];
    function ScopeProbe() {
      const { data } = useBlacklist({ includeSources: true, sourceKeys });
      const { mutate } = useRemove();
      return (
        <button
          disabled={!data?.length}
          onClick={() =>
            mutate({ all: true, includeSources: true, sourceKeys })
          }
        >
          Remove
        </button>
      );
    }
    const user = userEvent.setup();
    const { gets, expectDelete } = mockExclusions();
    customRender(<ScopeProbe />);
    await waitFor(() =>
      expect(gets).toHaveBeenCalledWith({ sources: "1", keys: sourceKeys }),
    );
    const remove = screen.getByRole("button", {
      name: "Remove",
    });
    await waitFor(() => expect(remove).toBeEnabled());
    await user.click(remove);
    await expectDelete(
      { all: "true", sources: "1", "source_key[]": "source:removed" },
      sourceKeys,
    );
    await waitFor(() => expect(remove).toBeDisabled());
  });

  it.each([
    ["source:removed", "Removed sources", "Recorded removed title"],
    ["source:8", "Retained Folder", "Retained folder title"],
  ])(
    "selects retained exclusions for %s in the dropdown",
    async (key, label, title) => {
      const user = userEvent.setup();
      const { gets, expectDelete } = mockExclusions();
      customRender(<View />);
      await screen.findByText(title);
      const listbox = await openSelect(user, "All sources");
      await user.click(
        within(listbox).getByRole("option", { name: label, hidden: true }),
      );
      await waitFor(() =>
        expect(gets).toHaveBeenCalledWith({ sources: "1", keys: [key] }),
      );
      expect(
        new URLSearchParams(window.location.search).getAll("source"),
      ).toEqual([key]);
      await waitFor(() =>
        expect(screen.queryByText("Arr title")).not.toBeInTheDocument(),
      );
      const retainedListbox = await openSelect(user, "All sources");
      expect(
        within(retainedListbox).getByRole("option", {
          name:
            key === "source:removed" ? "Retained Folder" : "Removed sources",
          hidden: true,
        }),
      ).toBeInTheDocument();
      await user.keyboard("{Escape}");
      await user.click(screen.getByRole("button", { name: "Remove All" }));
      await expectDelete({ all: "true", sources: "1", "source_key[]": key }, [
        key,
      ]);
      expect(await screen.findByText(empty)).toBeInTheDocument();
      const remainingListbox = await openSelect(user, "All sources");
      expect(
        within(remainingListbox).getByRole("option", {
          name:
            key === "source:removed" ? "Retained Folder" : "Removed sources",
          hidden: true,
        }),
      ).toBeInTheDocument();
    },
  );

  it("removes all Arr and source exclusions without a source filter", async () => {
    const user = userEvent.setup();
    const { expectDelete } = mockExclusions();
    customRender(<View />);
    await screen.findByText("Folder title");
    await user.click(screen.getByRole("button", { name: "Remove All" }));
    await expectDelete({ all: "true", sources: "1" });
    expect(await screen.findByText(empty)).toBeInTheDocument();
  });

  it("refreshes a stale exclusion and uses the new removal key", async () => {
    const user = userEvent.setup();
    const { expectDelete } = mockExclusions(true);
    customRender(<View />);
    const stale = await screen.findByRole("row", { name: /Folder title/ });
    await user.click(
      within(stale).getByRole("button", { name: "Remove from Excluded" }),
    );
    expect(await screen.findByText("Exclusion changed")).toBeInTheDocument();
    const current = await screen.findByRole("row", {
      name: /Updated exclusion/,
    });
    expect(screen.queryByText("Folder title")).not.toBeInTheDocument();
    await user.click(
      within(current).getByRole("button", { name: "Remove from Excluded" }),
    );
    await expectDelete({
      sources: "1",
      source_blacklist_id: "81",
      source_blacklist_key: "updated-key",
    });
    await waitFor(() =>
      expect(screen.queryByText("Updated exclusion")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Arr title")).toBeInTheDocument();
  });

  it("deletes by source blacklist id and preserves the Arr deletion form", async () => {
    const user = userEvent.setup();
    const { gets, expectDelete } = mockExclusions();
    customRender(<View />);
    const sourceRow = await screen.findByRole("row", {
      name: /Folder title/,
    });
    await user.click(
      within(sourceRow).getByRole("button", { name: "Remove from Excluded" }),
    );
    await expectDelete({
      sources: "1",
      source_blacklist_id: "81",
      source_blacklist_key: "original-exclusion-key",
    });
    await waitFor(() =>
      expect(screen.queryByText("Folder title")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Recorded removed title")).toBeInTheDocument();
    const arrRow = screen.getByRole("row", { name: /Arr title/ });
    await user.click(
      within(arrRow).getByRole("button", { name: "Remove from Excluded" }),
    );
    await expectDelete({ all: "false" }, [], {
      provider: "opensubtitlescom",
      subs_id: "subtitle-123",
      arr_instance_id: "2",
    });
    await waitFor(() =>
      expect(screen.queryByText("Arr title")).not.toBeInTheDocument(),
    );
    expect(gets.mock.calls.length).toBeGreaterThanOrEqual(3);
  });
});
