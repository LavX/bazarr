/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { act, useEffect } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { Button, useMantineColorScheme } from "@mantine/core";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { useDiscover } from "@/contexts/Discover";
import type { DiscoverDraft } from "@/contexts/discoverState";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import type {
  DiscoverContext,
  DiscoverSearchSnapshot,
  MetadataEpisode,
} from "@/types/discover";
import { setAuthenticated } from "@/utilities/event";
import * as files from "@/utilities/files";
import SubtitleResults from "./SubtitleResults";

const episode: MetadataEpisode = {
  source: "tmdb",
  source_id: "tmdb:episode:401",
  show_id: 40,
  season_id: 400,
  id: 401,
  season: 2,
  episode: 1,
  title: "First light",
  air_date: "2026-09-01",
  imdb_id: "tt1000001",
  tvdb_id: 101,
  show_imdb_id: "tt0903747",
  show_tvdb_id: 100,
  show_title: "Northern Light",
  show_year: 2008,
  target_season: 2,
  target_episode: 1,
  numbering: "tvdb_default",
  identity_status: "resolved",
  absolute_episode: null,
  tvdb_absolute_number: 12,
  mapping_updated_at: "2026-09-01T00:00:00Z",
};
const draft: DiscoverDraft = {
  mediaType: "episode",
  imdbId: episode.show_imdb_id!,
  title: episode.show_title,
  year: 2008,
  showId: 40,
  showTvdbId: 100,
  language: "eng",
  season: "2",
  episode: "1",
  episodeIdentity: episode,
};
const context: DiscoverContext = {
  media_type: "episode",
  imdb_id: draft.imdbId,
  title: draft.title,
  year: draft.year,
  season: 2,
  episode: 1,
  language: "en",
  show_id: 40,
  episode_identity: episode,
  matching_mode: "title",
};
const snapshot: DiscoverSearchSnapshot = {
  search_id: "newer-search",
  context,
  status: "complete",
  checked_at: "2026-09-08T10:00:00Z",
  attempted_at: "2026-09-08T10:00:00Z",
  cache_status: "fresh",
  coverage: {
    complete: true,
    configured_count: 1,
    completed_count: 1,
    providers: [],
  },
  results: ["full", "forced"].map((scope) => ({
    id: `exact-${scope}`,
    search_id: "original-search",
    provider: "catalog-example",
    language: "en",
    language_variant: null,
    release: `Northern.Light.S02E01.${scope}`,
    scope: scope as "full" | "forced",
    hearing_impaired: false,
    uploader: null,
    matches: [],
    compatibility_score: null,
    compatibility_score_max: null,
    rating: null,
    checked_at: "2026-09-08T10:00:00Z",
    expires_at: new Date(Date.now() + 3600000).toISOString(),
    stale: true,
  })),
};
const cue = "Hello <script>alert(1)</script>";
let changeDraft: ReturnType<typeof useDiscover>["updateDraft"];
let changeTheme: () => void;
let findSubtitles: ReturnType<typeof useDiscover>["findSubtitles"];
function Page() {
  const { state, updateDraft, findSubtitles: find } = useDiscover();
  const { toggleColorScheme } = useMantineColorScheme();
  useEffect(() => {
    findSubtitles = find;
    changeDraft = updateDraft;
    changeTheme = toggleColorScheme;
  }, [find, updateDraft, toggleColorScheme]);
  return (
    <>
      <Button
        onClick={() => {
          updateDraft(draft);
          void find();
        }}
      >
        Search episode
      </Button>
      {state.snapshot && <SubtitleResults snapshot={state.snapshot} />}
    </>
  );
}
function renderPreview() {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <Page /> },
      { path: "/activity", element: <p>Local activity</p> },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return { router, user: userEvent.setup() };
}
function forcedRow(scope = "forced") {
  return within(
    screen
      .getByRole("heading", { name: `Northern.Light.S02E01.${scope}` })
      .closest("article")!,
  );
}
async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Search episode" }));
  await screen.findByRole("heading", { name: "Northern.Light.S02E01.forced" });
  const opener = forcedRow().getByRole("button", { name: "Preview" });
  await user.click(opener);
  return opener;
}
let previewRequests: {
  result: string | null;
  search: string | null;
  authenticated: boolean;
}[];
let save: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  localStorage.clear();
  previewRequests = [];
  save = vi.spyOn(files, "saveBlobAs").mockImplementation(() => undefined);
  save.mockClear();
  server.use(
    http.post("/api/discover/search", () => HttpResponse.json(snapshot)),
    http.get("/api/discover/preview", ({ request }) => {
      const url = new URL(request.url);
      previewRequests.push({
        result: url.searchParams.get("result_id"),
        search: url.searchParams.get("search_id"),
        authenticated: Boolean(request.headers.get("x-api-key")),
      });
      return HttpResponse.json({
        result_id: "exact-forced",
        search_id: "original-search",
        filename: "Northern_Light.S02E01.en.forced.srt",
        cues: [{ start_ms: 1000, end_ms: 2000, text: cue }],
        total_cues: 1,
        truncated: false,
      });
    }),
    http.get(
      "/api/discover/download",
      () =>
        new HttpResponse("1\n00:00:01,000 --> 00:00:02,000\n" + cue + "\n\n", {
          headers: { "Content-Type": "application/x-subrip" },
        }),
    ),
  );
});
it("uses the real modal, literal cues and retained forced-row identity for preview and download", async () => {
  const { user } = renderPreview();
  const opener = await open(user);
  const modal = within(
    await screen.findByRole("dialog", { name: "Subtitle preview" }),
  );
  expect(await modal.findByText(cue)).toBeInTheDocument();
  expect(modal.getByText(/Northern Light S02 E01/)).toBeInTheDocument();
  expect(modal.getByText("Northern.Light.S02E01.forced")).toBeInTheDocument();
  expect(modal.getByText(/does not verify timing/)).toBeInTheDocument();
  expect(modal.getByText(cue)).toContainHTML(
    "Hello &lt;script&gt;alert(1)&lt;/script&gt;",
  );
  expect(previewRequests).toEqual([
    { result: "exact-forced", search: "original-search", authenticated: true },
  ]);
  await user.click(modal.getByRole("button", { name: "Download SRT" }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  await user.keyboard("{Escape}");
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  await waitFor(() => expect(opener).toHaveFocus());
});
it("preserves ready preview across theme and local navigation and closes exactly once", async () => {
  const { user, router } = renderPreview();
  await open(user);
  await screen.findByText(cue);
  act(() => changeTheme());
  await act(() => router.navigate("/activity"));
  expect(screen.getByText(cue)).toBeInTheDocument();
  await act(() => router.navigate("/discover"));
  await user.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "Close preview",
    }),
  );
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(forcedRow().getByRole("button", { name: "Preview" })).toHaveFocus();
  expect(previewRequests).toHaveLength(1);
});
const changes: [string, Partial<DiscoverDraft>][] = [
  ["language", { language: "hun" }],
  ["episode", { episode: "2" }],
  ["title", { title: "Other" }],
  ["show identity", { showId: 41 }],
  ["parent TVDB", { showTvdbId: 102 }],
  ["source episode", { episodeIdentity: { ...episode, id: 402 } }],
  ["target mapping", { episodeIdentity: { ...episode, target_episode: 2 } }],
  [
    "mapping revision",
    {
      episodeIdentity: {
        ...episode,
        mapping_updated_at: "2026-09-02T00:00:00Z",
      },
    },
  ],
  [
    "known parent conflict",
    { episodeIdentity: { ...episode, identity_status: "conflict" } },
  ],
];
it.each(changes)(
  "retires real preview on %s changes and rejects held completion",
  async (_name, changes) => {
    let finish: (() => void) | undefined;
    server.use(
      http.get("/api/discover/preview", async () => {
        await new Promise<void>((resolve) => {
          finish = resolve;
        });
        return HttpResponse.json({
          result_id: "exact-forced",
          search_id: "original-search",
          filename: "forced.srt",
          cues: [{ start_ms: 1000, end_ms: 2000, text: cue }],
          total_cues: 1,
          truncated: false,
        });
      }),
    );
    const { user } = renderPreview();
    await open(user);
    await waitFor(() => expect(finish).toBeDefined());
    act(() => changeDraft(changes));
    finish?.();
    await waitFor(() =>
      expect(
        queryClient.isMutating({
          mutationKey: [QueryKeys.Discover, "preview"],
        }),
      ).toBe(0),
    );
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(cue)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Preview" }),
    ).not.toBeInTheDocument();
  },
);
it("clears preview on authentication loss", async () => {
  const { user } = renderPreview();
  await open(user);
  await screen.findByText(cue);
  act(() => setAuthenticated(false));
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(screen.queryByText(cue)).not.toBeInTheDocument();
});
it("shows parser recovery without invented cues and retires an expired row", async () => {
  server.use(
    http.get("/api/discover/preview", () =>
      HttpResponse.json({ reason: "preview_failed" }, { status: 502 }),
    ),
  );
  const { user } = renderPreview();
  await open(user);
  expect(
    await screen.findByText(/valid subtitle for preview/),
  ).toBeInTheDocument();
  expect(screen.queryByText(cue)).not.toBeInTheDocument();
  server.use(
    http.get("/api/discover/preview", () =>
      HttpResponse.json({ reason: "result_expired" }, { status: 410 }),
    ),
  );
  await user.click(screen.getByRole("button", { name: "Retry preview" }));
  expect(await screen.findByText(/result has expired/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Close preview" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(forcedRow().getByRole("button", { name: "Preview" })).toBeDisabled();
});

it("retires a pending download when preview discovers that the same handle expired", async () => {
  let finish: (() => void) | undefined;
  server.use(
    http.get("/api/discover/download", async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return new HttpResponse("old bytes", {
        headers: { "Content-Type": "application/x-subrip" },
      });
    }),
    http.get("/api/discover/preview", () =>
      HttpResponse.json({ reason: "result_expired" }, { status: 410 }),
    ),
  );
  const { user } = renderPreview();
  await user.click(screen.getByRole("button", { name: "Search episode" }));
  await screen.findByRole("heading", { name: "Northern.Light.S02E01.forced" });
  await user.click(forcedRow().getByRole("button", { name: "Download SRT" }));
  await waitFor(() => expect(finish).toBeDefined());
  await user.click(forcedRow().getByRole("button", { name: "Preview" }));
  await screen.findByRole("dialog");
  await screen.findByText(
    "This result has expired. Search again for the same selection.",
  );
  finish?.();
  await waitFor(() =>
    expect(
      queryClient.isMutating({ mutationKey: [QueryKeys.Discover, "preview"] }),
    ).toBe(0),
  );
  await user.click(screen.getByRole("button", { name: "Close preview" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(
    forcedRow("full").getByRole("button", { name: "Download SRT" }),
  ).toBeEnabled();
  expect(save).not.toHaveBeenCalled();
});

it.each(changes)(
  "clears already rendered cues on %s changes",
  async (_name, changes) => {
    const { user } = renderPreview();
    await open(user);
    await screen.findByText(cue);
    act(() => changeDraft(changes));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(cue)).not.toBeInTheDocument();
  },
);

it("never reopens a preview closed while its bytes were pending", async () => {
  let finish: (() => void) | undefined;
  server.use(
    http.get("/api/discover/preview", async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return HttpResponse.json({
        result_id: "exact-forced",
        search_id: "original-search",
        cues: [{ start_ms: 1000, end_ms: 2000, text: cue }],
        total_cues: 1,
        truncated: false,
        filename: "subtitle.srt",
      });
    }),
  );
  const { user } = renderPreview();
  const opener = await open(user);
  await waitFor(() => expect(finish).toBeDefined());
  await user.click(
    await screen.findByRole("button", { name: "Close preview" }),
  );
  finish?.();
  await waitFor(() =>
    expect(
      queryClient.isMutating({ mutationKey: [QueryKeys.Discover, "preview"] }),
    ).toBe(0),
  );
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(opener).toHaveFocus();
  expect(screen.queryByText(cue)).not.toBeInTheDocument();
});

it("retains raw preview during inactive episode updates and invalidates it on query changes", async () => {
  server.use(
    http.post("/api/discover/search", async ({ request }) => {
      const body = (await request.json()) as {
        query: string;
        language: string;
      };
      return HttpResponse.json({
        ...snapshot,
        context: {
          mode: "release",
          query: body.query,
          language: body.language,
          matching_mode: "release",
        },
      });
    }),
  );
  const { user } = renderPreview();
  act(() =>
    changeDraft({
      mode: "release",
      query: "Northern.Light.S02E01",
      language: "eng",
    }),
  );
  await act(() => findSubtitles());
  await screen.findByRole("heading", { name: "Northern.Light.S02E01.forced" });
  await user.click(forcedRow().getByRole("button", { name: "Preview" }));
  await screen.findByText(cue);
  expect(screen.getByText(/unverified release query/)).toBeInTheDocument();
  act(() =>
    changeDraft({ episodeIdentity: { ...episode, id: 402 }, showTvdbId: 102 }),
  );
  expect(screen.getByText(cue)).toBeInTheDocument();
  act(() => changeDraft({ query: "Northern.Light.S02E02" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
});

it("clears actual preview state after the authenticated client receives 401", async () => {
  server.use(
    http.get("/api/discover/preview", () =>
      HttpResponse.json({ message: "Unauthorized" }, { status: 401 }),
    ),
  );
  const { user } = renderPreview();
  await open(user);
  await waitFor(() =>
    expect(
      queryClient.isMutating({ mutationKey: [QueryKeys.Discover, "preview"] }),
    ).toBe(0),
  );
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(screen.queryByText(cue)).not.toBeInTheDocument();
});

it("rejects a preview response for a different exact result", async () => {
  server.use(
    http.get("/api/discover/preview", () =>
      HttpResponse.json({
        result_id: "exact-full",
        search_id: "original-search",
        cues: [{ start_ms: 1000, end_ms: 2000, text: cue }],
        total_cues: 1,
        truncated: false,
        filename: "full.srt",
      }),
    ),
  );
  const { user } = renderPreview();
  await open(user);
  await screen.findByText(/valid subtitle for preview/);
  expect(screen.queryByText(cue)).not.toBeInTheDocument();
});

it("retains the preview on failed refresh but discards it when refreshed results remove its row", async () => {
  const { user } = renderPreview();
  await open(user);
  await screen.findByText(cue);
  server.use(http.post("/api/discover/search", () => HttpResponse.error()));
  await act(() => findSubtitles(true));
  expect(screen.getByText(cue)).toBeInTheDocument();
  server.use(
    http.post("/api/discover/search", () =>
      HttpResponse.json({ ...snapshot, search_id: "replacement", results: [] }),
    ),
  );
  await act(() => findSubtitles(true));
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(screen.queryByText(cue)).not.toBeInTheDocument();
});
