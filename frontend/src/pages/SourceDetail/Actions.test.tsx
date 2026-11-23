/* eslint-disable camelcase */
import { ReactElement } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import SubtitleViewer from "@/pages/SubtitleEditor";
import type { Cue } from "@/pages/SubtitleEditor/types";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import * as files from "@/utilities/files";
import { sourceDetailKeys, SourceItem } from "./api";
import { SourceMovieDetailView, SourceSeriesDetailView } from ".";

// The viewer virtualizes these rows, and jsdom has no viewport to lay out.
vi.mock("@/pages/SubtitleEditor/CueTable", () => ({
  default: ({ cues }: { cues: Cue[] }) =>
    cues.map((cue) => <p key={cue.id}>{cue.text}</p>),
}));

type Kind = "movie" | "episode";
type User = ReturnType<typeof userEvent.setup>;

const arrRequests: string[] = [];
const writes: { method: string; path: string; body: unknown }[] = [];

function recordRequest({ request }: { request: Request }) {
  const path = new URL(request.url).pathname;
  if (/^\/api\/(?:movies|episodes|series)(?:\/|$)/.test(path)) {
    arrRequests.push(`${request.method} ${path}`);
  }
}

function fixture(kind: Kind): SourceItem {
  const name = kind === "movie" ? "movie" : "episode";
  return {
    id: kind === "movie" ? 7 : 71,
    source_id: 3,
    library_id: 11,
    kind,
    group_id: kind === "movie" ? null : 7,
    title: `Folder ${name}`,
    parsed_title: `Folder ${name}`,
    year: "2026",
    poster: null,
    imdb_id: null,
    profile_id: 1,
    monitored: true,
    identity_state: "identified",
    identity_candidates: [],
    path: `/media/${name}.mkv`,
    scene_name: null,
    season: kind === "episode" ? 2 : null,
    episode: kind === "episode" ? 3 : null,
    arr_shadow: false,
    audio_language: ["English"],
    subtitles: [
      {
        code2: "en",
        name: "English",
        hi: true,
        forced: false,
        path: `/media/${name}.en.hi.srt`,
      },
      {
        code2: "fr",
        name: "French",
        hi: false,
        forced: false,
        path: `/media/${name}.fr.srt`,
      },
      {
        code2: "de",
        name: "German",
        hi: false,
        forced: false,
        path: null,
        codec: "pgs",
      },
    ],
    missing_subtitles: [
      { code2: "hu", name: "Hungarian", hi: false, forced: true },
    ],
  };
}

function openPage(
  kind: Kind,
  changes: Partial<SourceItem> = {},
  activeDocument?: ReactElement,
) {
  const item = { ...fixture(kind), ...changes };
  const state: { current: SourceItem | null; reads: (string | null)[] } = {
    current: item,
    reads: [],
  };
  server.use(
    http.get(`/api/sources/items/${item.id}`, ({ request }) => {
      state.reads.push(new URL(request.url).searchParams.get("kind"));
      return state.current
        ? HttpResponse.json(state.current)
        : HttpResponse.json(
            { message: "Source item no longer exists." },
            { status: 404 },
          );
    }),
    http.get("/api/sources/groups/7", () =>
      HttpResponse.json({ ...item, id: 7, kind: "show", title: "Folder show" }),
    ),
    http.get("/api/sources/items", () =>
      HttpResponse.json({ data: [item], total: 1 }),
    ),
  );
  queryClient.setQueryData([QueryKeys.Movies, item.id], {
    title: "Colliding Arr movie",
    path: "/arr/wrong.mkv",
  });
  queryClient.setQueryData([QueryKeys.Episodes, item.id], {
    title: "Colliding Arr episode",
    path: "/arr/wrong.mkv",
  });
  queryClient.setQueryData(
    sourceDetailKeys.item(item.id, kind === "movie" ? "episode" : "movie"),
    {
      ...item,
      title: "Wrong source kind",
      kind: kind === "movie" ? "episode" : "movie",
    },
  );
  const router = createMemoryRouter(
    [
      { path: "/movies/source/:id", element: <SourceMovieDetailView /> },
      { path: "/series/source/:id", element: <SourceSeriesDetailView /> },
      {
        path: "/subtitles/:mode/source/:id/:language",
        element: <p>Source subtitle destination</p>,
      },
    ],
    {
      initialEntries: [
        kind === "movie" ? "/movies/source/7" : "/series/source/7",
      ],
    },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
      {activeDocument}
    </AllProviders>,
  );
  return { item, state, router };
}

async function openItem(user: User, item: SourceItem) {
  if (item.kind === "episode") {
    await user.click(await screen.findByRole("button", { name: item.title }));
    await screen.findByRole("dialog", { name: item.title });
  } else {
    await screen.findByRole("heading", { name: /Folder movie/ });
  }
  expect(
    screen.queryByText(/Colliding Arr|Wrong source kind/),
  ).not.toBeInTheDocument();
}

async function openMenu(user: User, item: SourceItem) {
  expect(
    within(await screen.findByRole("row", { name: /pgs/ })).queryByLabelText(
      "Actions",
    ),
  ).not.toBeInTheDocument();
  const row = await screen.findByRole("row", {
    name: new RegExp(`${item.kind}\\.en\\.hi\\.srt`),
  });
  const action = within(row).getByLabelText("Actions");
  await waitFor(() => expect(action).toBeEnabled());
  await user.click(action);
}

beforeEach(() => {
  arrRequests.length = 0;
  writes.length = 0;
  server.events.on("request:start", recordRequest);
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: {}, media_sources: { visible: true } }),
    ),
    http.get("/api/system/media-sources", () =>
      HttpResponse.json({
        data: [
          {
            id: 3,
            name: "Folder owner",
            kind: "folder",
            enabled: true,
            libraries: [{ id: 11, enabled: true, content_kind: "mixed" }],
          },
        ],
      }),
    ),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/sources/items/:id/history", () =>
      HttpResponse.json({ data: [], total: 0 }),
    ),
    http.get("/api/subtitles", () =>
      HttpResponse.json({
        data: {
          audio_tracks: [],
          embedded_subtitles_tracks: [],
          external_subtitles_tracks: [],
        },
      }),
    ),
    http.patch("/api/subtitles", async ({ request }) => {
      writes.push({
        method: "PATCH",
        path: `${new URL(request.url).pathname}${new URL(request.url).search}`,
        body: Object.fromEntries(await request.formData()),
      });
      return HttpResponse.json({ job_id: null });
    }),
    http.delete("/api/sources/items/:id/subtitles", async ({ request }) => {
      writes.push({
        method: "DELETE",
        path: new URL(request.url).pathname,
        body: Object.fromEntries(await request.formData()),
      });
      return new HttpResponse(null, { status: 204 });
    }),
    http.all(/\/api\/(?:movies|episodes|series)(?:\/|$)/, () =>
      HttpResponse.json({ message: "Unexpected Arr route" }, { status: 500 }),
    ),
  );
});

afterEach(() => {
  server.events.removeListener("request:start", recordRequest);
  vi.restoreAllMocks();
  expect(arrRequests).toEqual([]);
});

describe.each<Kind>(["movie", "episode"])(
  "source %s subtitle controls",
  (kind) => {
    it.each(["View", "Edit"] as const)(
      "opens %s with its source identity and language variant",
      async (action) => {
        const user = userEvent.setup();
        const { item, router } = openPage(kind);
        const cachedDocument = [
          QueryKeys.Subtitles,
          "content",
          "source",
          item.id,
          "en:hi",
          undefined,
          null,
          null,
        ];
        queryClient.setQueryData(cachedDocument, "previous document");
        await openItem(user, item);
        await openMenu(user, item);
        await user.click(await screen.findByRole("menuitem", { name: action }));
        await waitFor(() =>
          expect(router.state.location.pathname).toBe(
            `/subtitles/${action === "View" ? "preview" : "edit"}/source/${item.id}/en%3Ahi`,
          ),
        );
        expect(
          await screen.findByText("Source subtitle destination"),
        ).toBeVisible();
        expect(queryClient.getQueryData(cachedDocument)).toBeUndefined();
        await waitFor(() =>
          expect(
            screen.queryByRole("dialog", { name: item.title }),
          ).not.toBeInTheDocument(),
        );
        expect(writes).toEqual([]);
      },
    );

    it("downloads the selected source sidecar content with its indexed filename", async () => {
      const user = userEvent.setup();
      const save = vi
        .spyOn(files, "saveBlobAs")
        .mockImplementation(() => undefined);
      const { item } = openPage(kind);
      const downloads: string[] = [];
      server.use(
        http.get(
          "/api/sources/items/:id/subtitles/:language/content",
          ({ params }) => {
            downloads.push(`${params.id}/${params.language}`);
            return HttpResponse.json({
              content: "1\n00:00:01,000 --> 00:00:02,000\nSource subtitle\n",
              encoding: "utf-8",
              format: "srt",
            });
          },
        ),
      );
      await openItem(user, item);
      await openMenu(user, item);
      await user.click(
        await screen.findByRole("menuitem", { name: "Download" }),
      );
      await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
      expect(downloads).toEqual([`${item.id}/en:hi`]);
      expect(save).toHaveBeenCalledWith(expect.any(Blob), `${kind}.en.hi.srt`);
      expect(writes).toEqual([]);
    });

    it("uses source references, applies tools, and confirms deletion of only that file", async () => {
      const user = userEvent.setup();
      const { item, state } = openPage(kind);
      const references: Record<string, string>[] = [];
      server.use(
        http.get("/api/subtitles", ({ request }) => {
          references.push(
            Object.fromEntries(new URL(request.url).searchParams),
          );
          return HttpResponse.json({
            data: {
              audio_tracks: [
                { stream: "a:0", name: "Source audio", language: "en" },
              ],
              embedded_subtitles_tracks: [],
              external_subtitles_tracks: [],
            },
          });
        }),
      );
      await openItem(user, item);
      await openMenu(user, item);
      expect(writes).toEqual([]);
      await user.click(
        await screen.findByRole("menuitem", { name: "Common Fixes" }),
      );
      await waitFor(() => expect(writes).toHaveLength(1));
      expect(writes[0]).toMatchObject({
        method: "PATCH",
        path: "/api/subtitles?action=common",
        body: {
          id: String(item.id),
          type: "source",
          language: "en",
          path: `/media/${kind}.en.hi.srt`,
          hi: "True",
          forced: "False",
        },
      });
      expect(writes[0].body).not.toHaveProperty("arr_instance_id");
      await openMenu(user, item);
      await user.click(
        await screen.findByRole("menuitem", { name: "Sync..." }),
      );
      const sync = await screen.findByRole("dialog", {
        name: "Sync Subtitle Options",
      });
      await waitFor(() =>
        expect(references).toEqual([
          {
            sourceItemId: String(item.id),
            subtitlesPath: `/media/${kind}.en.hi.srt`,
          },
        ]),
      );
      await user.click(within(sync).getByRole("button", { name: "Sync" }));
      await waitFor(() => expect(writes).toHaveLength(2));
      expect(writes[1]).toMatchObject({
        method: "PATCH",
        path: "/api/subtitles?action=sync",
        body: {
          id: String(item.id),
          type: "source",
          path: `/media/${kind}.en.hi.srt`,
        },
      });
      await waitFor(() =>
        expect(
          screen.queryByRole("dialog", { name: "Sync Subtitle Options" }),
        ).not.toBeInTheDocument(),
      );
      await openMenu(user, item);
      await user.click(
        await screen.findByRole("menuitem", { name: "Delete..." }),
      );
      const confirm = await screen.findByRole("dialog", {
        name: /deleted|Delete Subtitle/,
      });
      await waitFor(() =>
        expect(
          within(confirm).getByText(new RegExp(`${kind}\\.en\\.hi\\.srt`)),
        ).toBeVisible(),
      );
      expect(writes).toHaveLength(2);
      await user.click(within(confirm).getByRole("button", { name: "Delete" }));
      await waitFor(() => expect(writes).toHaveLength(3));
      expect(writes[2]).toEqual({
        method: "DELETE",
        path: `/api/sources/items/${item.id}/subtitles`,
        body: {
          subtitles_path: `/media/${kind}.en.hi.srt`,
          language: "en",
          hi: "true",
          forced: "false",
        },
      });
      expect(state.reads.length).toBeGreaterThanOrEqual(4);
      expect(state.reads.every((value) => value === kind)).toBe(true);
    });

    it.each(["missing item", "moved item", "missing sidecar"] as const)(
      "refuses Common Fixes after a %s response",
      async (condition) => {
        const user = userEvent.setup();
        const { item, state } = openPage(kind);
        await openItem(user, item);
        await openMenu(user, item);
        state.current =
          condition === "missing item"
            ? null
            : condition === "moved item"
              ? { ...item, path: "/media/replaced.mkv" }
              : { ...item, subtitles: [] };
        await user.click(
          await screen.findByRole("menuitem", { name: "Common Fixes" }),
        );
        expect(
          await screen.findByText(
            /no longer exists|moved or is no longer available|no longer listed/,
          ),
        ).toBeVisible();
        expect(state.reads).toEqual([kind, kind]);
        expect(writes).toEqual([]);
      },
    );

    it("checks again when a previously opened delete confirmation is submitted", async () => {
      const user = userEvent.setup();
      const { item, state } = openPage(kind);
      await openItem(user, item);
      await openMenu(user, item);
      await user.click(
        await screen.findByRole("menuitem", { name: "Delete..." }),
      );
      const confirm = await screen.findByRole("dialog", {
        name: /deleted|Delete Subtitle/,
      });
      await waitFor(() =>
        expect(
          within(confirm).getByRole("button", { name: "Delete" }),
        ).toBeVisible(),
      );
      state.current = { ...item, subtitles: [] };
      await user.click(within(confirm).getByRole("button", { name: "Delete" }));
      await waitFor(() => expect(state.reads).toEqual([kind, kind]));
      expect(await screen.findByText(/no longer listed/)).toBeVisible();
      expect(writes).toEqual([]);
    });

    it("checks the sidecar again when an open Sync form is submitted", async () => {
      const user = userEvent.setup();
      const { item, state } = openPage(kind);
      await openItem(user, item);
      await openMenu(user, item);
      await user.click(
        await screen.findByRole("menuitem", { name: "Sync..." }),
      );
      const sync = await screen.findByRole("dialog", {
        name: "Sync Subtitle Options",
      });
      state.current = { ...item, subtitles: [] };
      await user.click(within(sync).getByRole("button", { name: "Sync" }));
      expect(await screen.findByText(/no longer listed/)).toBeVisible();
      expect(sync).toBeVisible();
      expect(writes).toEqual([]);
    });

    it.each(["missing file", "Arr shadow"] as const)(
      "offers no file actions for a %s",
      async (condition) => {
        const user = userEvent.setup();
        const { item } = openPage(
          kind,
          condition === "missing file"
            ? { server_missing: true }
            : { arr_shadow: true },
        );
        await openItem(user, item);
        await screen.findByText(`${kind}.en.hi.srt`);
        expect(screen.queryByLabelText("Actions")).not.toBeInTheDocument();
        expect(writes).toEqual([]);
      },
    );
  },
);

it.each([false, true])(
  "pins selected indexed variants in Combine (selected input removed: %s)",
  async (removed) => {
    const user = userEvent.setup();
    const { subtitles } = fixture("movie");
    const { item, state } = openPage("movie", {
      subtitles: [
        ...subtitles,
        { ...subtitles[0], hi: false, path: "/media/movie.en.srt" },
      ],
    });
    const combined: unknown[] = [];
    server.use(
      http.post(
        "/api/sources/items/7/subtitles/combine",
        async ({ request }) => {
          combined.push(await request.json());
          return HttpResponse.json({
            status: "built",
            path: "/media/movie.en.combined-fr.srt",
          });
        },
      ),
    );
    await openItem(user, item);
    const button = await screen.findByRole("button", {
      name: "Combine Subtitles",
    });
    await waitFor(() => expect(button).toBeEnabled());
    await user.click(button);
    const dialog = await screen.findByRole("dialog", {
      name: "Combine subtitles",
    });
    for (const language of ["FR", "EN (movie.en.hi.srt)"]) {
      const input = within(dialog).getByPlaceholderText("Add language");
      await user.click(input);
      const listbox = await waitFor(() => {
        const id = input.getAttribute("aria-controls");
        const value = screen
          .getAllByRole("listbox", { hidden: true })
          .find((element) => element.id === id);
        if (!value) throw new Error("The language selector did not open");
        return value;
      });
      await user.click(
        within(listbox).getByRole("option", { name: language, hidden: true }),
      );
    }
    if (removed) {
      state.current = { ...item, subtitles: item.subtitles.slice(1) };
    }
    await user.click(within(dialog).getByRole("button", { name: "Generate" }));
    if (removed) {
      expect(await screen.findByText(/no longer listed/)).toBeVisible();
      expect(dialog).toBeVisible();
      expect(combined).toEqual([]);
    } else {
      expect(
        await screen.findByText("Combined subtitle generated"),
      ).toBeVisible();
      expect(combined).toEqual([
        {
          languages: ["fr", "en"],
          format: "srt",
          inputs: [
            { path: "/media/movie.fr.srt", language: "fr" },
            { path: "/media/movie.en.hi.srt", language: "en:hi" },
          ],
        },
      ]);
    }
    expect(state.reads.length).toBeGreaterThanOrEqual(2);
    expect(writes).toEqual([]);
  },
);

it("rebuilds the selected source combined output with its format, order and indexed inputs", async () => {
  const user = userEvent.setup();
  const { subtitles } = fixture("movie");
  const { item } = openPage("movie", {
    subtitles: [
      ...subtitles,
      {
        ...subtitles[1],
        language: "fr:combined-en",
        path: "/media/movie.fr.combined-en.ass",
      },
    ],
  });
  const combined: unknown[] = [];
  server.use(
    http.post("/api/sources/items/7/subtitles/combine", async ({ request }) => {
      combined.push(await request.json());
      return HttpResponse.json({
        status: "built",
        path: "/media/movie.fr.combined-en.ass",
      });
    }),
  );
  await openItem(user, item);
  const row = await screen.findByRole("row", {
    name: /movie.fr.combined-en.ass/,
  });
  await user.click(within(row).getByLabelText("Actions"));
  await user.click(await screen.findByRole("menuitem", { name: "Rebuild" }));
  expect(await screen.findByText("Combined subtitle generated")).toBeVisible();
  expect(combined).toEqual([
    {
      languages: ["fr", "en"],
      format: "ass",
      inputs: [
        { path: "/media/movie.fr.srt", language: "fr" },
        { path: "/media/movie.en.hi.srt", language: "en:hi" },
      ],
    },
  ]);
});

it.each(["hi", "forced", "alternate"] as const)(
  "asks which English input to rebuild when the %s input competes with plain",
  async (variant) => {
    const user = userEvent.setup();
    const { subtitles } = fixture("movie");
    const selectedPath = `/media/movie.en.${variant}.srt`;
    const { item } = openPage("movie", {
      subtitles: [
        { ...subtitles[0], hi: false, path: "/media/movie.en.srt" },
        {
          ...subtitles[0],
          hi: variant === "hi",
          forced: variant === "forced",
          path: selectedPath,
        },
        subtitles[1],
        {
          ...subtitles[1],
          language: "fr:combined-en",
          path: "/media/movie.fr.combined-en.ass",
        },
      ],
    });
    const combined: unknown[] = [];
    server.use(
      http.post(
        "/api/sources/items/7/subtitles/combine",
        async ({ request }) => {
          combined.push(await request.json());
          return HttpResponse.json({
            status: "built",
            path: "/media/movie.fr.combined-en.ass",
          });
        },
      ),
    );
    await openItem(user, item);
    const row = await screen.findByRole("row", {
      name: /movie.fr.combined-en.ass/,
    });
    await user.click(within(row).getByLabelText("Actions"));
    await user.click(await screen.findByRole("menuitem", { name: "Rebuild" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Combine subtitles",
    });
    expect(combined).toEqual([]);
    expect(
      within(dialog).getByRole("radio", { name: "ASS positioned" }),
    ).toBeChecked();
    expect(within(dialog).getAllByRole("combobox")[0]).toHaveValue("FR");
    const generate = within(dialog).getByRole("button", { name: "Generate" });
    expect(generate).toBeDisabled();
    const input = within(dialog).getByPlaceholderText("Choose EN input");
    await user.click(input);
    const listbox = await waitFor(() => {
      const value = screen
        .getAllByRole("listbox", { hidden: true })
        .find((element) => element.id === input.getAttribute("aria-controls"));
      if (!value) throw new Error("The input selector did not open");
      return value;
    });
    await user.click(
      within(listbox).getByRole("option", {
        name: `EN (movie.en.${variant}.srt)`,
        hidden: true,
      }),
    );
    await user.click(generate);
    await waitFor(() =>
      expect(combined).toEqual([
        {
          languages: ["fr", "en"],
          format: "ass",
          inputs: [
            { path: "/media/movie.fr.srt", language: "fr" },
            {
              path: selectedPath,
              language: variant === "alternate" ? "en" : `en:${variant}`,
            },
          ],
        },
      ]),
    );
  },
);

it("requires a choice when a known primary HI input is gone and plain remains", async () => {
  const user = userEvent.setup();
  const { subtitles } = fixture("movie");
  const { item } = openPage("movie", {
    subtitles: [
      { ...subtitles[0], hi: false, path: "/media/movie.en.srt" },
      subtitles[1],
      {
        ...subtitles[0],
        language: "en:hi:combined-fr",
        path: "/media/movie.en.combined-fr.ass",
      },
    ],
  });
  const combined: unknown[] = [];
  server.use(
    http.post("/api/sources/items/7/subtitles/combine", async ({ request }) => {
      combined.push(await request.json());
      return HttpResponse.json({
        status: "built",
        path: "/media/movie.en.combined-fr.ass",
      });
    }),
  );
  await openItem(user, item);
  const row = await screen.findByRole("row", {
    name: /movie.en.combined-fr.ass/,
  });
  await user.click(within(row).getByLabelText("Actions"));
  await user.click(await screen.findByRole("menuitem", { name: "Rebuild" }));
  const dialog = await screen.findByRole("dialog", {
    name: "Combine subtitles",
  });
  expect(combined).toEqual([]);
  expect(
    within(dialog).getByRole("button", { name: "Generate" }),
  ).toBeDisabled();
  expect(within(dialog).getByPlaceholderText("Choose EN input")).toHaveValue(
    "",
  );
  expect(within(dialog).getAllByRole("combobox")[1]).toHaveValue("FR");
  expect(
    within(dialog).getByRole("radio", { name: "ASS positioned" }),
  ).toBeChecked();
  await user.click(within(dialog).getByPlaceholderText("Choose EN input"));
  await user.click(
    await screen.findByRole("option", { name: "EN", hidden: true }),
  );
  await user.click(within(dialog).getByRole("button", { name: "Generate" }));
  await waitFor(() =>
    expect(combined).toEqual([
      {
        languages: ["en", "fr"],
        format: "ass",
        inputs: [
          { path: "/media/movie.en.srt", language: "en" },
          { path: "/media/movie.fr.srt", language: "fr" },
        ],
      },
    ]),
  );
});

it.each([
  "competing input appeared",
  "previous input disappeared",
  "one of several inputs disappeared",
] as const)(
  "asks for current inputs when the %s after the detail loaded",
  async (condition) => {
    const user = userEvent.setup();
    const { subtitles } = fixture("movie");
    const alternate = {
      ...subtitles[0],
      hi: false,
      path: "/media/movie.en.srt",
    };
    const { item, state } = openPage("movie", {
      subtitles: [
        ...subtitles,
        ...(condition === "one of several inputs disappeared"
          ? [alternate]
          : []),
        {
          ...subtitles[1],
          language: "fr:combined-en",
          path: "/media/movie.fr.combined-en.ass",
        },
      ],
    });
    const combined: unknown[] = [];
    server.use(
      http.post(
        "/api/sources/items/7/subtitles/combine",
        async ({ request }) => {
          combined.push(await request.json());
          return HttpResponse.json({
            status: "built",
            path: "/media/movie.fr.combined-en.ass",
          });
        },
      ),
    );
    await openItem(user, item);
    state.current = {
      ...item,
      subtitles: [
        ...item.subtitles.filter(
          (subtitle) =>
            condition === "competing input appeared" ||
            (subtitle.path !== subtitles[0].path &&
              subtitle.path !== alternate.path),
        ),
        alternate,
      ],
    };
    const row = await screen.findByRole("row", {
      name: /movie.fr.combined-en.ass/,
    });
    await user.click(within(row).getByLabelText("Actions"));
    await user.click(await screen.findByRole("menuitem", { name: "Rebuild" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Combine subtitles",
    });
    expect(combined).toEqual([]);
    expect(within(dialog).getByPlaceholderText("Choose EN input")).toHaveValue(
      "",
    );
    expect(
      within(dialog).getByRole("button", { name: "Generate" }),
    ).toBeDisabled();
    await user.click(within(dialog).getByPlaceholderText("Choose EN input"));
    const input = within(dialog).getByPlaceholderText("Choose EN input");
    const listbox = await waitFor(() => {
      const value = screen
        .getAllByRole("listbox", { hidden: true })
        .find((element) => element.id === input.getAttribute("aria-controls"));
      if (!value) throw new Error("The input selector did not open");
      return value;
    });
    await user.click(
      within(listbox).getByRole("option", {
        name:
          condition === "competing input appeared" ? "EN (movie.en.srt)" : "EN",
        hidden: true,
      }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Generate" }));
    await waitFor(() =>
      expect(combined).toEqual([
        {
          languages: ["fr", "en"],
          format: "ass",
          inputs: [
            { path: "/media/movie.fr.srt", language: "fr" },
            { path: "/media/movie.en.srt", language: "en" },
          ],
        },
      ]),
    );
  },
);

it("excludes a current provider release from its physical source episode row", async () => {
  const user = userEvent.setup();
  const { item } = openPage("episode");
  const excluded: unknown[] = [];
  const release = {
    origin: "source",
    source_key: "source:3",
    source_id: 3,
    item_id: 71,
    item_kind: "episode",
    history_id: 81,
    action: 1,
    language: { code2: "en", hi: true, forced: false },
    provider: "fixture-provider",
    subs_id: "source-release-81",
    subtitles_path: "/media/episode.en.hi.srt",
    video_path: "/media/episode.mkv",
    blacklisted: false,
    upgradable: false,
  };
  server.use(
    http.get("/api/sources/items/71/history", () =>
      HttpResponse.json({ data: [release], total: 1 }),
    ),
    http.post("/api/sources/items/71/blacklist", async ({ request }) => {
      excluded.push(Object.fromEntries(await request.formData()));
      return new HttpResponse(null, { status: 204 });
    }),
  );
  await openItem(user, item);
  await openMenu(user, item);
  await user.click(
    await screen.findByRole("menuitem", { name: "Exclude from search..." }),
  );
  const confirm = await screen.findByRole("dialog", {
    name: "Exclude from search?",
  });
  expect(within(confirm).getByText(/episode\.en\.hi\.srt/)).toBeVisible();
  expect(excluded).toEqual([]);
  await user.click(
    within(confirm).getByRole("button", { name: "Exclude from search" }),
  );
  await waitFor(() =>
    expect(excluded).toEqual([
      {
        history_id: "81",
        provider: "fixture-provider",
        subs_id: "source-release-81",
        subtitles_path: "/media/episode.en.hi.srt",
        language: "en",
        hi: "true",
        forced: "false",
      },
    ]),
  );
  expect(writes).toEqual([]);
});

it("refreshes an open source viewer after a same-language upload and removes the document on deletion", async () => {
  const user = userEvent.setup();
  const activeDocument = createMemoryRouter(
    [
      {
        path: "/subtitles/preview/:mediaType/:mediaId/:language",
        element: <SubtitleViewer />,
      },
    ],
    { initialEntries: ["/subtitles/preview/source/7/en%3Ahi"] },
  );
  const { item, state } = openPage(
    "movie",
    {},
    <section aria-label="Open source viewer">
      <RouterProvider router={activeDocument} />
    </section>,
  );
  const viewer = within(
    screen.getByRole("region", { name: "Open source viewer" }),
  );
  let cue: string | null = "Original source cue";
  const posted = vi.fn();
  const untouchedKey = [
    QueryKeys.Subtitles,
    "content",
    "source",
    item.id,
    "en",
  ];
  queryClient.setQueryData(untouchedKey, "plain variant");
  server.use(
    http.get("/api/system/languages", () =>
      HttpResponse.json([{ code2: "en", name: "English", enabled: true }]),
    ),
    http.get(
      "/api/sources/items/7/subtitles/:language/content",
      ({ params }) => {
        if (!cue)
          return HttpResponse.json(
            { message: "Subtitle file no longer exists" },
            { status: 404 },
          );
        return HttpResponse.json(
          {
            content: `1\n00:00:01,000 --> 00:00:02,000\n${cue}\n`,
            encoding: "utf-8",
            format: "srt",
            language: params.language,
            size: 80,
            mediaId: 7,
          },
          { headers: { ETag: `"${cue}"` } },
        );
      },
    ),
    http.post("/api/sources/items/7/subtitles", ({ request }) => {
      posted(request.headers.get("Content-Type"));
      return HttpResponse.json(
        { job_id: 71, session: "upload-run" },
        { status: 202 },
      );
    }),
    http.delete("/api/sources/items/7/subtitles", () => {
      cue = null;
      state.current = { ...item, subtitles: item.subtitles.slice(1) };
      return new HttpResponse(null, { status: 204 });
    }),
  );
  await openItem(user, item);
  expect(await screen.findByText("Original source cue")).toBeVisible();
  const open = await screen.findByRole("button", { name: "Upload" });
  await waitFor(() => expect(open).toBeEnabled());
  await user.click(open);
  const upload = await screen.findByRole("dialog", {
    name: "Upload Subtitles",
  });
  await user.upload(
    within(upload).getByLabelText("Subtitle file"),
    new File(["replacement"], "movie.en.hi.srt"),
  );
  await user.click(within(upload).getByRole("combobox", { name: "Language" }));
  await user.click(await screen.findByRole("option", { name: "English" }));
  await user.click(
    within(upload).getByRole("checkbox", { name: "Hearing impaired" }),
  );
  await user.click(within(upload).getByRole("button", { name: "Upload" }));
  await waitFor(() =>
    expect(posted).toHaveBeenCalledWith(
      expect.stringContaining("multipart/form-data"),
    ),
  );
  expect(screen.getByText("Original source cue")).toBeVisible();
  cue = "Replacement source cue";
  act(() => {
    queryClient.setQueryData(
      [QueryKeys.System, QueryKeys.Jobs],
      [{ job_id: 71, session: "upload-run", status: "completed" }],
    );
  });
  expect(await screen.findByText("Replacement source cue")).toBeVisible();
  expect(screen.queryByText("Original source cue")).not.toBeInTheDocument();

  await openMenu(user, item);
  await user.click(await screen.findByRole("menuitem", { name: "Delete..." }));
  const confirm = await screen.findByRole("dialog", {
    name: /deleted|Delete Subtitle/,
  });
  await user.click(within(confirm).getByRole("button", { name: "Delete" }));
  await waitFor(() => {
    expect(
      screen.queryByText("Replacement source cue"),
    ).not.toBeInTheDocument();
    expect(
      viewer.getByText(/status code 404|Subtitle file no longer exists/),
    ).toBeVisible();
  });
  expect(queryClient.getQueryData(untouchedKey)).toBe("plain variant");
});

it("keeps file actions available for a forced source subtitle without offering automatic Sync", async () => {
  const user = userEvent.setup();
  const { subtitles } = fixture("movie");
  const { item } = openPage("movie", {
    subtitles: [
      {
        ...subtitles[0],
        hi: false,
        forced: true,
        path: "/media/movie.en.forced.srt",
      },
      ...subtitles.slice(1),
    ],
  });
  await openItem(user, item);
  const forced = await screen.findByRole("row", {
    name: /movie.en.forced.srt/,
  });
  await user.click(within(forced).getByLabelText("Actions"));
  const edit = await screen.findByRole("menuitem", { name: "Edit" });
  await waitFor(() => expect(edit).toBeVisible());
  expect(
    within(screen.getByRole("menu")).queryByRole("menuitem", {
      name: "Sync...",
    }),
  ).not.toBeInTheDocument();
});
