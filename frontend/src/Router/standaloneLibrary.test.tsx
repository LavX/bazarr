/* eslint-disable camelcase -- API field names. */
import { MemoryRouter, useRoutes as useRouterRoutes } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { makeInstance } from "@/pages/Settings/Connections/__tests__/fixtures";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { openSelect } from "@/tests/select";
import { useRoutes } from ".";

function LibraryRoutes() {
  return useRouterRoutes(useRoutes()[0].children ?? []);
}

function renderLibrary(path: string) {
  return rawRender(
    <AllProviders>
      <MemoryRouter initialEntries={[path]}>
        <LibraryRoutes />
      </MemoryRouter>
    </AllProviders>,
  );
}

const unavailable = { browsable: false, searchable: false, retained: false };
const id = 42;
const sourceId = 8;
const libraryId = 11;

describe.each([
  ["series", "sonarr", "series", "groups", QueryKeys.Series],
  ["movies", "radarr", "movie", "items", QueryKeys.Movies],
] as const)(
  "mixed %s library",
  (kind, arrKind, contentKind, detailKind, key) => {
    function setup({
      arr = true,
      attention = true,
      visible = true,
      enabled = true,
    } = {}) {
      const folder = {
        ...unavailable,
        browsable: visible,
        searchable: enabled,
      };
      const arrRow = {
        id,
        arr_instance_id: 5,
        ...(kind === "series" ? { sonarrSeriesId: 777 } : { radarrId: 777 }),
        title: "Arr title",
        profileId: 7,
        audio_language: [],
        missing_subtitles: [],
        episodeFileCount: 2,
        episodeMissingCount: 1,
      };
      const sourceRow = {
        ...arrRow,
        ...(kind === "series" ? { sonarrSeriesId: null } : { radarrId: null }),
        origin: "source",
        source_key: `source:${sourceId}`,
        arr_instance_id: null,
        source_id: sourceId,
        library_id: libraryId,
        source_name: "Archive",
        identity_state: attention ? "unmatched" : "identified",
        shadowed_by: null,
        needs_attention: attention,
        title: "Archive title",
      };
      const list = vi.fn();
      const saveArr = vi.fn();
      const saveSource = vi.fn();
      const searchSource = vi.fn();
      const scanSource = vi.fn();
      server.use(
        http.get("/api/system/settings", () =>
          HttpResponse.json({
            general: { [`use_${arrKind}`]: arr },
            media_sources: { visible },
          }),
        ),
        http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
        http.get("/api/badges", () => HttpResponse.json({})),
        http.get("/api/system/languages/audio", () => HttpResponse.json([])),
        http.get("/api/system/languages/profiles", () => HttpResponse.json([])),
        http.get("/api/subtitles/upgradable", () =>
          HttpResponse.json({ series: [], movies: [], sports: [] }),
        ),
        http.get("/api/system/arr-instances", () =>
          HttpResponse.json(
            arr ? [makeInstance({ kind: arrKind, id: 5, name: "Main" })] : [],
          ),
        ),
        http.get("/api/system/media-sources", () =>
          HttpResponse.json({
            data: visible
              ? [
                  {
                    id: sourceId,
                    name: "Archive",
                    kind: "folder",
                    enabled,
                    libraries: [
                      { id: libraryId, enabled, content_kind: contentKind },
                    ],
                  },
                ]
              : [],
            availability: {
              ...{
                series: unavailable,
                movie: unavailable,
                sports: unavailable,
              },
              [contentKind]: folder,
            },
          }),
        ),
        http.get(`/api/${kind}`, ({ request }) => {
          const params = new URL(request.url).searchParams;
          list(
            params.get("sources"),
            params.getAll("source_key[]"),
            params.get("needs_attention"),
          );
          const keys = params.getAll("source_key[]");
          const onlyAttention = params.get("needs_attention") === "1";
          const data = [
            ...(!onlyAttention && (!keys.length || keys.includes("arr:5"))
              ? [arrRow]
              : []),
            ...(visible &&
            params.get("sources") === "1" &&
            (!onlyAttention || attention) &&
            (!keys.length || keys.includes(`source:${sourceId}`))
              ? [sourceRow]
              : []),
          ];
          return HttpResponse.json({ data, total: data.length });
        }),
        http.post(`/api/${kind}`, async ({ request }) => {
          const form = await request.formData();
          saveArr(form.getAll("id"), form.getAll("profileid"));
          return new HttpResponse(null, { status: 204 });
        }),
        http.patch(`/api/sources/${detailKind}/${id}`, async ({ request }) => {
          saveSource(await request.json());
          return HttpResponse.json({ id });
        }),
        http.post(`/api/sources/${detailKind}/${id}/search`, () => {
          searchSource();
          return HttpResponse.json({ job_id: null }, { status: 202 });
        }),
        http.post(
          `/api/system/media-sources/${sourceId}/libraries/${libraryId}/scan`,
          () => {
            scanSource();
            return HttpResponse.json({ job_id: null }, { status: 202 });
          },
        ),
      );
      return { list, saveArr, saveSource, searchSource, scanSource };
    }

    it("shows distinct links, leaves the numeric detail cache Arr-owned, and scopes mass profile writes", async () => {
      const { list, saveArr, saveSource } = setup();
      const user = userEvent.setup();
      renderLibrary(`/${kind}`);
      expect(
        await screen.findByRole("link", { name: "Archive title" }),
      ).toHaveAttribute("href", `/${kind}/source/${id}`);
      expect(screen.getByRole("link", { name: "Arr title" })).toHaveAttribute(
        "href",
        `/${kind}/${id}`,
      );
      expect(
        screen.getByRole("columnheader", { name: "Source" }),
      ).toBeInTheDocument();
      expect(screen.getByText("Archive", { exact: true })).toBeInTheDocument();
      expect(list).toHaveBeenCalledWith("1", [], null);
      expect(queryClient.getQueryData([key, id])).toEqual(
        expect.objectContaining({ title: "Arr title" }),
      );
      expect(
        queryClient.getQueryData([key, "library-row", `source:${id}`]),
      ).toEqual(expect.objectContaining({ title: "Archive title" }));
      await user.click(
        screen.getByRole("checkbox", { name: "Select Archive title" }),
      );
      expect(
        screen.getByRole("checkbox", { name: "Select Arr title" }),
      ).not.toBeChecked();
      expect(
        screen.queryByRole("button", { name: "Sync Subtitles" }),
      ).not.toBeInTheDocument();
      await user.click(
        screen.getByRole("checkbox", { name: "Select Arr title" }),
      );
      expect(
        screen.getByRole("checkbox", { name: "Select Archive title" }),
      ).toBeChecked();
      await user.click(screen.getByRole("button", { name: "Change Profile" }));
      await user.click(
        await screen.findByRole("button", { name: "Clear Profile" }),
      );
      await user.click(await screen.findByRole("button", { name: "Save" }));
      await waitFor(() =>
        expect(saveArr).toHaveBeenCalledWith([String(id)], ["null"]),
      );
      await waitFor(() =>
        expect(saveSource).toHaveBeenCalledWith({ profile_id: null }),
      );
    });

    it("passes the source and attention filters, and clears both together", async () => {
      const { list } = setup();
      const user = userEvent.setup();
      renderLibrary(`/${kind}`);
      expect(
        await screen.findByRole("link", { name: "Archive title" }),
      ).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Toggle filters" }));
      const owners = screen.getByPlaceholderText("Filter by source...");
      await user.click(owners);
      await user.type(owners, "Archive");
      await user.keyboard("{ArrowDown}{Enter}");
      await waitFor(() =>
        expect(list).toHaveBeenLastCalledWith(
          "1",
          [`source:${sourceId}`],
          null,
        ),
      );
      expect(
        await screen.findByRole("link", { name: "Archive title" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("link", { name: "Arr title" }),
      ).not.toBeInTheDocument();
      await user.click(
        screen.getByRole("checkbox", { name: "Needs attention" }),
      );
      await waitFor(() =>
        expect(list).toHaveBeenLastCalledWith("1", [`source:${sourceId}`], "1"),
      );
      await user.click(screen.getByText("Clear all"));
      expect(
        await screen.findByRole("link", { name: "Arr title" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("checkbox", { name: "Needs attention" }),
      ).not.toBeChecked();
    });

    it("opens a source-only library and targets source-owned search and scan", async () => {
      const { list, searchSource, scanSource } = setup({
        arr: false,
        attention: false,
      });
      const user = userEvent.setup();
      renderLibrary(`/${kind}`);
      await waitFor(() => {
        expect(list).toHaveBeenLastCalledWith(
          "1",
          [`source:${sourceId}`],
          null,
        );
        expect(
          screen.getByRole("link", { name: "Archive title" }),
        ).toBeInTheDocument();
      });
      expect(
        screen.queryByRole("link", { name: "Arr title" }),
      ).not.toBeInTheDocument();
      expect(queryClient.getQueryData([key, id])).toBeUndefined();
      const row = screen.getByRole("row", { name: /Archive title/ });
      await user.click(within(row).getByRole("button", { name: "Actions" }));
      expect(
        screen.queryByRole("menuitem", { name: /extract|ocr|hide embedded/i }),
      ).not.toBeInTheDocument();
      await user.click(
        await screen.findByRole("menuitem", { name: "Search Missing" }),
      );
      await waitFor(() => expect(searchSource).toHaveBeenCalledOnce());
      await user.click(
        within(
          await screen.findByRole("row", { name: /Archive title/ }),
        ).getByRole("button", { name: "Actions" }),
      );
      await user.click(
        await screen.findByRole("menuitem", { name: "Scan Disk" }),
      );
      await waitFor(() => expect(scanSource).toHaveBeenCalledOnce());
    });

    it("preserves the Arr-only list and keeps source filters out with no sources", async () => {
      const { list } = setup({ visible: false });
      const user = userEvent.setup();
      renderLibrary(`/${kind}`);
      expect(
        await screen.findByRole("link", { name: "Arr title" }),
      ).toHaveAttribute("href", `/${kind}/${id}`);
      expect(
        screen.queryByRole("link", { name: "Archive title" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("columnheader", { name: "Source" }),
      ).not.toBeInTheDocument();
      expect(list).toHaveBeenCalledWith("1", [], null);
      await user.click(screen.getByRole("button", { name: "Toggle filters" }));
      expect(
        screen.queryByRole("checkbox", { name: "Needs attention" }),
      ).not.toBeInTheDocument();
    });
  },
);
