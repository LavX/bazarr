/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { createMemoryRouter, Link, RouterProvider } from "react-router";
import { Button } from "@mantine/core";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { DiscoverSetupReturn, useDiscover } from "@/contexts/Discover";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import * as files from "@/utilities/files";
import Discover from ".";

const query = "Example.Movie.2024.1080p.WEB-DL";
const srt = "1\n00:00:03,000 --> 00:00:04,000\nRaw translation\n\n";
let searches: Record<string, unknown>[];
let downloads: URLSearchParams[];
let save: ReturnType<typeof vi.spyOn>;

function snapshot(context: Record<string, unknown>, id: string) {
  return {
    search_id: `containing-${id}`,
    context: {
      ...context,
      matching_mode: context.mode === "release" ? "release" : "title",
    },
    status: "partial",
    checked_at: new Date().toISOString(),
    attempted_at: new Date().toISOString(),
    cache_status: "fresh",
    coverage: {
      complete: false,
      configured_count: 2,
      completed_count: 1,
      providers: [
        {
          provider: "catalog-example",
          status: "success",
          reason: null,
          result_count: 1,
          elapsed_ms: 1,
          retry_at: null,
        },
        {
          provider: "catalog-account",
          status: "authentication_required",
          reason: "authentication_required",
          result_count: 0,
          elapsed_ms: 1,
          retry_at: null,
        },
      ],
    },
    results: [
      {
        id: `${id}-forced`,
        search_id: id,
        provider: "catalog-example",
        release: `${context.query ?? "Identified"}.forced`,
        language: "en",
        language_variant: null,
        scope: "forced",
        hearing_impaired: false,
        uploader: null,
        matches: null,
        compatibility_score: null,
        compatibility_score_max: null,
        rating: null,
        checked_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 3600000).toISOString(),
        stale: false,
      },
    ],
  };
}
function ReconcileMetadata() {
  const { updateDraft } = useDiscover();
  return (
    <Button
      onClick={() =>
        updateDraft({
          mediaType: "movie",
          imdbId: "tt0133093",
          title: "The Matrix",
          year: 1999,
        })
      }
    >
      Accept fresh metadata
    </Button>
  );
}
function renderDiscover() {
  const router = createMemoryRouter(
    [
      {
        path: "/discover",
        element: (
          <>
            <ReconcileMetadata />
            <Discover />
          </>
        ),
      },
      {
        path: "/settings/discover",
        element: (
          <DiscoverSetupReturn>
            <Link to="/discover">Back</Link>
          </DiscoverSetupReturn>
        ),
      },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return { user: userEvent.setup() };
}
async function releaseMode(user: ReturnType<typeof userEvent.setup>) {
  await user.click(
    screen.getByRole("button", { name: "Search providers by release name" }),
  );
  await user.type(screen.getByLabelText("Release name"), query);
  await user.selectOptions(screen.getByLabelText("Subtitle language"), "eng");
}
async function rawSearch(user: ReturnType<typeof userEvent.setup>) {
  await releaseMode(user);
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await screen.findByRole("heading", { name: `${query}.forced` });
}

beforeEach(() => {
  localStorage.clear();
  searches = [];
  downloads = [];
  save = vi.spyOn(files, "saveBlobAs").mockImplementation(() => undefined);
  save.mockClear();
  server.use(
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { code3: "eng", name: "English" },
        { code3: "hun", name: "Hungarian" },
      ]),
    ),
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({
        data: {
          source: "tmdb",
          status: "unconfigured",
          configured: false,
          revision: "no-key",
          locale: "en-US",
          message: "Set up TMDB",
          checked_at: null,
          fetched_at: null,
        },
      }),
    ),
    http.post("/api/discover/search", async ({ request }) => {
      const selection = (await request.json()) as Record<string, unknown>;
      searches.push(selection);
      const context = { ...selection };
      delete context.refresh;
      return HttpResponse.json(snapshot(context, `raw-${searches.length}`));
    }),
    http.get("/api/discover/download", ({ request }) => {
      downloads.push(new URL(request.url).searchParams);
      return new HttpResponse(srt, {
        headers: {
          "Content-Type": "application/x-subrip",
          "Content-Disposition": `attachment; filename="${query}.en.forced.srt"`,
        },
      });
    }),
  );
});

it("keeps metadata setup visible and requires explicit valid release query and language", async () => {
  const { user } = renderDiscover();
  await screen.findByText(/Set up TMDB/);
  await user.click(
    screen.getByRole("button", { name: "Search providers by release name" }),
  );
  expect(screen.getByText(/Advanced release-name search/)).toBeVisible();
  expect(screen.getByText(/Set up TMDB/)).toBeVisible();
  expect(
    screen.getByText(/Title and episode identity.*unverified/),
  ).toBeVisible();
  const find = screen.getByRole("button", { name: "Find subtitles" });
  await user.type(screen.getByLabelText("Release name"), "...-_/!?");
  await user.selectOptions(screen.getByLabelText("Subtitle language"), "eng");
  expect(find).toBeDisabled();
  await user.clear(screen.getByLabelText("Release name"));
  await user.type(screen.getByLabelText("Release name"), query);
  await user.selectOptions(screen.getByLabelText("Subtitle language"), "");
  expect(find).toBeDisabled();
  expect(searches).toEqual([]);
  await user.selectOptions(screen.getByLabelText("Subtitle language"), "eng");
  await user.click(find);
  await screen.findByRole("heading", { name: `${query}.forced` });
  expect(searches).toEqual([
    { mode: "release", query, language: "eng", refresh: false },
  ]);
  expect(screen.getByText(/Sign in to this provider/)).toBeVisible();
});

it("downloads the exact raw row and recovers 410 with the original query and language", async () => {
  const { user } = renderDiscover();
  await rawSearch(user);
  await user.click(screen.getByRole("button", { name: "Download SRT" }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  expect(save.mock.calls[0][0].size).toBe(srt.length);
  expect(save.mock.calls[0][1]).toBe(`${query}.en.forced.srt`);
  expect(Object.fromEntries(downloads[0])).toEqual({
    result_id: "raw-1-forced",
    search_id: "raw-1",
  });
  expect(await screen.findByText(/Download started for/)).toHaveTextContent(
    `${query} (unverified release query) · eng`,
  );
  server.use(
    http.get("/api/discover/download", () =>
      HttpResponse.json({}, { status: 410 }),
    ),
  );
  await user.click(screen.getByRole("button", { name: "Download SRT" }));
  await screen.findByText(/This result has expired/);
  expect(screen.getByRole("button", { name: "Download SRT" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Search again" }));
  await waitFor(() => expect(searches).toHaveLength(2));
  expect(searches[1]).toEqual({
    mode: "release",
    query,
    language: "eng",
    refresh: true,
  });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Download SRT" })).toBeEnabled(),
  );
  expect(screen.queryByText(/This result has expired/)).not.toBeInTheDocument();
});

it("preserves both mode inputs and retires incompatible results and feedback", async () => {
  const { user } = renderDiscover();
  await user.type(screen.getByLabelText("IMDb ID"), "tt0903747");
  await rawSearch(user);
  await user.click(screen.getByRole("button", { name: "Download SRT" }));
  await screen.findByText(/Download started for/);
  await user.click(
    screen.getByRole("button", { name: "Return to identified title" }),
  );
  expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0903747");
  expect(
    screen.queryByRole("button", { name: "Download SRT" }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText(/Download started for/)).not.toBeInTheDocument();
  await user.click(
    screen.getByRole("button", { name: "Search providers by release name" }),
  );
  expect(screen.getByLabelText("Release name")).toHaveValue(query);
  expect(screen.getByLabelText("Subtitle language")).toHaveValue("eng");
  expect(searches).toHaveLength(1);
});

it("keeps the active release draft and handles when accepted metadata reconciles", async () => {
  const { user } = renderDiscover();
  await rawSearch(user);
  await user.click(
    screen.getByRole("button", { name: "Accept fresh metadata" }),
  );
  expect(screen.getByLabelText("Release name")).toHaveValue(query);
  expect(screen.getByRole("button", { name: "Download SRT" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "Download SRT" }));
  await screen.findByText(/Download started for/);
  expect(searches).toHaveLength(1);
  await user.click(
    screen.getByRole("button", { name: "Return to identified title" }),
  );
  expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093");
});

it.each(["mode", "query"])(
  "rejects pending search responses after a %s change",
  async (change) => {
    let finish: (() => void) | undefined;
    server.use(
      http.post("/api/discover/search", async ({ request }) => {
        const context = (await request.json()) as Record<string, unknown>;
        searches.push(context);
        await new Promise<void>((resolve) => {
          finish = resolve;
        });
        return HttpResponse.json(snapshot(context, "late"));
      }),
    );
    const { user } = renderDiscover();
    if (change === "mode") {
      await user.type(screen.getByLabelText("IMDb ID"), "tt0133093");
      await user.selectOptions(
        screen.getByLabelText("Subtitle language"),
        "eng",
      );
    } else await releaseMode(user);
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    await waitFor(() => expect(finish).toBeDefined());
    if (change === "mode") await releaseMode(user);
    else await user.type(screen.getByLabelText("Release name"), ".Other");
    finish?.();
    await waitFor(() =>
      expect(
        queryClient.isMutating({ mutationKey: [QueryKeys.Discover] }),
      ).toBe(0),
    );
    expect(
      screen.queryByRole("button", { name: "Download SRT" }),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Release name")).toHaveValue(
      change === "mode" ? query : `${query}.Other`,
    );
    expect(searches).toHaveLength(1);
  },
);

it("rejects a late raw download after switching mode away and back", async () => {
  let finish: (() => void) | undefined;
  server.use(
    http.get("/api/discover/download", async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return new HttpResponse(srt, {
        headers: { "Content-Type": "application/x-subrip" },
      });
    }),
  );
  const { user } = renderDiscover();
  await rawSearch(user);
  await user.click(screen.getByRole("button", { name: "Download SRT" }));
  await waitFor(() => expect(finish).toBeDefined());
  await user.click(
    screen.getByRole("button", { name: "Return to identified title" }),
  );
  await user.click(
    screen.getByRole("button", { name: "Search providers by release name" }),
  );
  finish?.();
  await waitFor(() =>
    expect(
      queryClient.isMutating({ mutationKey: [QueryKeys.Discover, "download"] }),
    ).toBe(0),
  );
  expect(save).not.toHaveBeenCalled();
  expect(screen.queryByText(/Preparing download for/)).not.toBeInTheDocument();
});

it("returns from settings to the preserved raw query without searching", async () => {
  const { user } = renderDiscover();
  await releaseMode(user);
  await user.click(screen.getByRole("link", { name: "Discover settings" }));
  await user.click(screen.getByRole("link", { name: "Return to Discover" }));
  expect(screen.getByLabelText("Release name")).toHaveValue(query);
  expect(searches).toEqual([]);
});

it("keeps ambiguous release input and shows the server recovery message", async () => {
  server.use(
    http.post("/api/discover/search", () =>
      HttpResponse.json(
        {
          message:
            "Use one explicitly numbered episode, for example Show.S02E03.",
        },
        { status: 400 },
      ),
    ),
  );
  const { user } = renderDiscover();
  await releaseMode(user);
  await user.clear(screen.getByLabelText("Release name"));
  await user.type(screen.getByLabelText("Release name"), "Example.Show.E03");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  expect(
    await within(screen.getByRole("region", { name: "Discover" })).findByText(
      /Use one explicitly numbered episode/,
    ),
  ).toBeVisible();
  expect(screen.getByLabelText("Release name")).toHaveValue("Example.Show.E03");
});

it("does not label empty unverified provider output as no matches", async () => {
  server.use(
    http.post("/api/discover/search", () =>
      HttpResponse.json({
        ...snapshot({ mode: "release", query, language: "eng" }, "unverified"),
        status: "failed",
        results: [],
        coverage: {
          complete: false,
          configured_count: 1,
          completed_count: 0,
          providers: [
            {
              provider: "catalog-unknown",
              status: "unverified",
              reason: "query_support_unverified",
              result_count: 0,
              elapsed_ms: 1,
              retry_at: null,
            },
          ],
        },
      }),
    ),
  );
  const { user } = renderDiscover();
  await releaseMode(user);
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  expect(
    await screen.findByText(/No results returned for this unverified query/),
  ).toBeVisible();
  expect(
    screen.queryByText(/No matches|No subtitles matched/),
  ).not.toBeInTheDocument();
});
