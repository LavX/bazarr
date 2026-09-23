/* eslint-disable camelcase -- API fixtures retain transport field names. */
import {
  createMemoryRouter,
  Link,
  RouterProvider,
  useNavigate,
} from "react-router";
import { Button, useMantineColorScheme } from "@mantine/core";
import { cleanNotifications } from "@mantine/notifications";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { useDiscover } from "@/contexts/Discover";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import type { DiscoverSearchSnapshot } from "@/types/discover";
import { setAuthenticated } from "@/utilities/event";
import * as files from "@/utilities/files";
import {
  downloadJobHandlers,
  DownloadRequest,
  emitJob,
  findJobToast,
  JobOutcome,
  jobs,
  setJob,
} from "./downloadJobHarness";
import { pickOption } from "./selectTestHelpers";
import Discover from "./testHarness";

const fullSrt = "1\n00:00:01,000 --> 00:00:02,000\nFull dialogue\n\n";
const forcedSrt = "1\n00:00:03,000 --> 00:00:04,000\nForced translation\n\n";
const target = {
  media_type: "episode" as const,
  imdb_id: "tt0903747",
  title: "Breaking Bad",
  year: 2008,
  season: 2,
  episode: 1,
  language: "eng",
  matching_mode: "title" as const,
  manual_confirmed: true,
};

function snapshot(searchId = "search-1"): DiscoverSearchSnapshot {
  return {
    search_id: searchId,
    context: target,
    status: "complete",
    checked_at: "2026-09-08T10:00:00Z",
    attempted_at: "2026-09-08T10:00:00Z",
    cache_status: "fresh",
    coverage: {
      complete: true,
      configured_count: 1,
      completed_count: 1,
      providers: [
        {
          provider: "catalog-example",
          status: "success",
          result_count: 2,
          reason: null,
          retry_at: null,
          elapsed_ms: 2,
        },
      ],
    },
    results: ["full", "forced"].map((scope) => ({
      id: `${searchId}-${scope}`,
      search_id: searchId,
      provider: "catalog-example",
      language: "en",
      language_variant: null,
      release: `Breaking.Bad.S02E01.${scope}`,
      scope: scope as "full" | "forced",
      hearing_impaired: false,
      matches: ["imdb_id"],
      compatibility_score: 10,
      compatibility_score_max: 119,
      rating: null,
      uploader: null,
      copy_compatibility: null,
      checked_at: "2026-09-08T10:00:00Z",
      expires_at: new Date(Date.now() + 3600000).toISOString(),
      stale: false,
    })),
  };
}

const episodeDraft = {
  mediaType: "episode" as const,
  manualConfirmed: true,
  imdbId: target.imdb_id,
  title: target.title,
  year: target.year,
  season: "2",
  episode: "1",
  language: "eng",
};

function Controls() {
  const { updateBrowsing, updateDraft } = useDiscover();
  const { toggleColorScheme } = useMantineColorScheme();
  const navigate = useNavigate();
  const selectTitle = () => {
    void navigate("/discover?show=100");
    updateBrowsing({
      selectedId: 100,
      selectedType: "show",
      selectedSource: "tmdb",
      selectedSeason: null,
      selectedEpisode: null,
    });
  };
  return (
    <>
      <Button
        onClick={() => {
          selectTitle();
          updateDraft(episodeDraft);
        }}
      >
        Choose episode
      </Button>
      {/* Two updates, because a single one that changes the target and
          supplies a copy retires the copy by design. */}
      <Button
        onClick={() => {
          selectTitle();
          updateDraft({ copyId: chosenCopy.copy_id });
        }}
      >
        Choose copy
      </Button>
      <Button
        onClick={() => {
          selectTitle();
          updateDraft({ ...episodeDraft, copyId: chosenCopy.copy_id });
        }}
      >
        Choose episode and copy at once
      </Button>
      <Button onClick={() => toggleColorScheme()}>Change appearance</Button>
    </>
  );
}
function renderDiscover() {
  const router = createMemoryRouter(
    [
      {
        path: "/discover",
        element: (
          <>
            <Controls />
            <Discover />
          </>
        ),
      },
      {
        path: "/subtitle-hub",
        element: <Link to="/discover">Return to Discover</Link>,
      },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return { user: userEvent.setup(), router };
}
function row(scope = "forced") {
  return within(
    screen
      .getByRole("heading", { name: `Breaking.Bad.S02E01.${scope}` })
      .closest("article")!,
  );
}
async function search(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Choose episode" }));
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await screen.findByRole("heading", { name: "Breaking.Bad.S02E01.forced" });
}
async function searchWithCopy(user: ReturnType<typeof userEvent.setup>) {
  // The draft carries the copy, so the captured context key and the current
  // draft key both contain it. Without this the recovery replay would be
  // exercised in a state production cannot reach.
  await user.click(screen.getByRole("button", { name: "Choose episode" }));
  await user.click(screen.getByRole("button", { name: "Choose copy" }));
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await screen.findByRole("heading", { name: "Breaking.Bad.S02E01.forced" });
}
let requests: DownloadRequest[];
let tickets: number[];
let searches: unknown[];
let save: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  localStorage.clear();
  cleanNotifications();
  requests = [];
  tickets = [];
  searches = [];
  save = vi.spyOn(files, "saveBlobAs").mockImplementation(() => undefined);
  save.mockClear();
  server.use(
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { name: "English", code2: "en", code3: "eng", enabled: false },
        { name: "Hungarian", code2: "hu", code3: "hun", enabled: false },
      ]),
    ),
    http.post("/api/discover/search", async ({ request }) => {
      searches.push(await request.json());
      return HttpResponse.json(snapshot());
    }),
    ...handlers(),
  );
});

function handlers(
  overrides: Partial<Parameters<typeof downloadJobHandlers>[0]> = {},
) {
  return downloadJobHandlers({
    body: (id) => (id.endsWith("forced") ? forcedSrt : fullSrt),
    filename: (id) =>
      `Breaking_Bad.S02E01.eng.${id.endsWith("forced") ? "forced" : "full"}.srt`,
    requests,
    tickets,
    ...overrides,
  });
}

/** Click Download, wait for the job to finish, then Save from the row. */
async function downloadAndSave(
  user: ReturnType<typeof userEvent.setup>,
  scope = "forced",
) {
  await user.click(row(scope).getByRole("button", { name: "Download SRT" }));
  await user.click(await row(scope).findByRole("button", { name: "Save SRT" }));
}

describe("Discover attachments", () => {
  it("downloads only the clicked forced row with its exact identifiers and device filename", async () => {
    const { user } = renderDiscover();
    await search(user);
    expect(requests).toEqual([]);
    await downloadAndSave(user);
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(requests).toEqual([
      { result: "search-1-forced", search: "search-1", authenticated: true },
    ]);
    expect(save.mock.calls[0][0]).toBeInstanceOf(Blob);
    expect(save.mock.calls[0][0].size).toBe(forcedSrt.length);
    expect(save.mock.calls[0][1]).toBe("Breaking_Bad.S02E01.eng.forced.srt");
    expect(
      await screen.findByText(/Download started for Breaking Bad S02 E01/),
    ).toHaveTextContent(
      /eng.*Forced.*catalog-example.*Breaking.Bad.S02E01.forced/,
    );
  });

  it("retains download feedback through appearance and navigation without submitting providers", async () => {
    const { user, router } = renderDiscover();
    await search(user);
    await downloadAndSave(user);
    await screen.findByText(/Download started for/);
    await user.click(screen.getByRole("button", { name: "Change appearance" }));
    await user.click(screen.getByRole("link", { name: "Subtitle Hub" }));
    await act(async () => {
      await router.navigate(-1);
    });
    expect(screen.getByText(/Download started for/)).toBeInTheDocument();
    expect(searches).toHaveLength(1);
    expect(requests).toHaveLength(1);
  });

  it("retires a 410 handle and recovers by searching the exact captured episode context", async () => {
    const { user } = renderDiscover();
    await search(user);
    server.use(
      http.post("/api/discover/download", () =>
        HttpResponse.json(
          {
            reason: "result_expired",
            recoverable: true,
            message: "Search again",
          },
          { status: 410 },
        ),
      ),
    );
    await user.click(row().getByRole("button", { name: "Download SRT" }));
    expect(
      await screen.findByText(/This result has expired/),
    ).toHaveTextContent("Breaking Bad S02 E01");
    expect(row().getByRole("button", { name: "Download SRT" })).toBeDisabled();
    server.use(
      http.post("/api/discover/search", async ({ request }) => {
        searches.push(await request.json());
        return HttpResponse.json(snapshot("search-2"));
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "Search again for expired result" }),
    );
    await waitFor(() =>
      expect(row().getByRole("button", { name: "Download SRT" })).toBeEnabled(),
    );
    expect(searches[1]).toEqual({
      media_type: "episode",
      imdb_id: target.imdb_id,
      title: target.title,
      year: target.year,
      season: 2,
      episode: 1,
      language: "eng",
      refresh: true,
      manual_confirmed: true,
    });
    expect(
      screen.queryByText(/This result has expired/),
    ).not.toBeInTheDocument();
  });

  it("downloads a usable retained row using its original search ID after partial refresh", async () => {
    const { user } = renderDiscover();
    await search(user);
    server.use(
      http.post("/api/discover/search", () =>
        HttpResponse.json({
          ...snapshot("search-2"),
          status: "partial",
          cache_status: "stale",
          results: [{ ...snapshot().results[1], stale: true }],
          coverage: {
            ...snapshot().coverage,
            complete: false,
            completed_count: 0,
            providers: [
              {
                ...snapshot().coverage.providers[0],
                status: "authentication_required",
                result_count: 0,
              },
            ],
          },
        }),
      ),
    );
    await user.click(screen.getByRole("button", { name: "Search again" }));
    await screen.findByText(/Previous result/);
    expect(
      screen.queryByRole("heading", { name: "Breaking.Bad.S02E01.full" }),
    ).not.toBeInTheDocument();
    await downloadAndSave(user);
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(requests[0]).toMatchObject({
      result: "search-1-forced",
      search: "search-1",
    });
  });

  it("keeps a usable handle after transport failure but never revives a known expired one", async () => {
    const { user } = renderDiscover();
    await search(user);
    server.use(
      http.post("/api/discover/download", () =>
        HttpResponse.json({}, { status: 410 }),
      ),
      http.post("/api/discover/search", () => HttpResponse.error()),
    );
    await user.click(row().getByRole("button", { name: "Download SRT" }));
    await screen.findByText(/This result has expired/);
    await user.click(
      screen.getByRole("button", { name: "Search again for expired result" }),
    );
    await screen.findByText(/Refresh failed/);
    expect(row().getByRole("button", { name: "Download SRT" })).toBeDisabled();
    expect(
      row("full").getByRole("button", { name: "Download SRT" }),
    ).toBeEnabled();
  });

  it.each(["Episode", "Subtitle language", "IMDb ID"])(
    "retires feedback and handles on a change to %s",
    async (field) => {
      const { user } = renderDiscover();
      await search(user);
      await downloadAndSave(user);
      await screen.findByText(/Download started for/);
      if (field !== "Subtitle language")
        await user.click(
          screen.getByRole("button", { name: "Search options" }),
        );
      if (field === "Subtitle language")
        await pickOption(user, field, "Hungarian");
      else if (field === "IMDb ID") {
        // Clearing the IMDb ID in the detail form clears the title selection
        // by design and returns to the homepage. Feedback and handles retire
        // with the results panel, so there is no field left to type into.
        await user.clear(screen.getByRole("textbox", { name: field }));
      } else {
        // "Episode" is also the media-type radio; the number field is the
        // textbox of that name.
        await user.clear(screen.getByRole("textbox", { name: field }));
        await user.type(screen.getByRole("textbox", { name: field }), "2");
      }
      expect(
        screen.queryByText(/Download started for/),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Download SRT" }),
      ).not.toBeInTheDocument();
    },
  );

  it.each(["context", "logout", "retired"])(
    "does not save a late attachment after %s invalidates it",
    async (change) => {
      let finish: (() => void) | undefined;
      server.use(
        ...handlers({
          hold: () =>
            new Promise<void>((resolve) => {
              finish = resolve;
            }),
        }),
      );
      const { user } = renderDiscover();
      await search(user);
      await user.click(row().getByRole("button", { name: "Download SRT" }));
      await waitFor(() => expect(finish).toBeDefined());
      if (change === "context")
        await pickOption(user, "Subtitle language", "Hungarian");
      else if (change === "logout") setAuthenticated(false);
      else {
        server.use(
          http.post("/api/discover/search", () =>
            HttpResponse.json({ ...snapshot("new"), results: [] }),
          ),
        );
        await user.click(
          screen.getByRole("button", { name: /^Search again$/ }),
        );
        await screen.findByText(/No subtitles matched/);
      }
      finish?.();
      await waitFor(() =>
        expect(
          queryClient.isMutating({
            mutationKey: [QueryKeys.Discover, "download"],
          }),
        ).toBe(0),
      );
      await waitFor(() =>
        expect(
          screen.queryByText(/Preparing download for/),
        ).not.toBeInTheDocument(),
      );
      expect(save).not.toHaveBeenCalled();
      expect(
        screen.queryByText(/Download started for/),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Save SRT" }),
      ).not.toBeInTheDocument();
    },
  );

  it.each([401, 503])(
    "reports a refused enqueue without saving anything (%s)",
    async (status) => {
      server.use(
        http.post("/api/discover/download", () =>
          HttpResponse.json({ message: "Could not queue" }, { status }),
        ),
      );
      const { user } = renderDiscover();
      await search(user);
      await user.click(row().getByRole("button", { name: "Download SRT" }));
      await waitFor(() =>
        expect(
          screen.queryByText(/Preparing download for/),
        ).not.toBeInTheDocument(),
      );
      expect(save).not.toHaveBeenCalled();
      if (status !== 401)
        expect(
          await screen.findByText(/Download failed for/),
        ).toBeInTheDocument();
    },
  );

  it.each(["app shell", "expired ticket"])(
    "never saves a ticket answer that is not the subtitle (%s)",
    async (kind) => {
      server.use(
        ...handlers({
          ticket: () =>
            kind === "app shell"
              ? new HttpResponse("<html>app shell</html>", {
                  headers: { "Content-Type": "text/html" },
                })
              : HttpResponse.json(
                  {
                    message:
                      "This download is no longer available. Download it again from the results.",
                    reason: "ticket_expired",
                  },
                  { status: 404 },
                ),
        }),
      );
      const { user } = renderDiscover();
      await search(user);
      await downloadAndSave(user);
      expect(await screen.findByText(/Download failed for/)).toHaveTextContent(
        kind === "app shell"
          ? /could not be saved/
          : /no longer available\. Download it again/,
      );
      expect(save).not.toHaveBeenCalled();
    },
  );
});

describe("Discover download as a standard job", () => {
  function jobId() {
    return Math.max(...jobs.keys());
  }

  it("follows the job through the jobs socket event: pending, then ready with Save in the row and in the notification", async () => {
    server.use(...handlers({ outcome: () => ({ status: "running" }) }));
    const { user } = renderDiscover();
    await search(user);
    await user.click(row().getByRole("button", { name: "Download SRT" }));
    expect(
      await screen.findByText(/Preparing download for Breaking Bad S02 E01/),
    ).toHaveTextContent(/runs in Jobs/);
    await waitFor(() => expect(requests).toHaveLength(1));
    setJob(jobId(), { status: "running" });
    emitJob(jobId());
    await waitFor(() =>
      expect(
        queryClient.getQueryData<System.Jobs[]>([
          QueryKeys.System,
          QueryKeys.Jobs,
        ]),
      ).toEqual([
        expect.objectContaining({ job_id: jobId(), status: "running" }),
      ]),
    );
    expect(screen.getByText(/Preparing download for/)).toBeInTheDocument();
    expect(row().queryByRole("button", { name: "Save SRT" })).toBeNull();
    setJob(jobId(), { status: "completed" });
    emitJob(jobId());
    expect(
      await screen.findByText(/Ready to save Breaking Bad S02 E01/),
    ).toBeInTheDocument();
    expect(row().getByRole("button", { name: "Save SRT" })).toBeEnabled();
    // The standard job notification, with the job's own action.
    const toast = await findJobToast(`Download ${requests[0].result}`);
    expect(within(toast).getByText("Ready to save")).toBeInTheDocument();
    expect(within(toast).getByRole("button", { name: "Save" })).toBeEnabled();
    // Nothing is fetched or saved without a click.
    expect(tickets).toEqual([]);
    expect(save).not.toHaveBeenCalled();
  });

  it("saves from the notification by fetching the ticket, then saveBlobAs", async () => {
    const { user } = renderDiscover();
    await search(user);
    await user.click(row().getByRole("button", { name: "Download SRT" }));
    const toast = await findJobToast(`Download search-1-forced`);
    await user.click(within(toast).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(tickets).toEqual([jobId()]);
    expect(save.mock.calls[0][0]).toBeInstanceOf(Blob);
    expect(save.mock.calls[0][0].size).toBe(forcedSrt.length);
    expect(save.mock.calls[0][1]).toBe("Breaking_Bad.S02E01.eng.forced.srt");
    expect(
      await screen.findByText(/Download started for Breaking Bad S02 E01/),
    ).toBeInTheDocument();
  });

  it("shows the classified failure in the notification and the row, and Retry follows the retried job", async () => {
    const failure: JobOutcome = {
      status: "failed",
      retryable: true,
      error: {
        reason: "invalid_subtitle",
        message:
          "The provider returned a file that is not a usable subtitle. Choose another result.",
      },
    };
    server.use(...handlers({ outcome: () => failure }));
    const { user } = renderDiscover();
    await search(user);
    await user.click(row().getByRole("button", { name: "Download SRT" }));
    const toast = await findJobToast(`Download search-1-forced`);
    expect(within(toast).getByText(failure.error!.message)).toBeInTheDocument();
    // The row says the same thing as the notification.
    expect(await screen.findByText(/Download failed for/)).toHaveTextContent(
      failure.error!.message,
    );
    const failed = jobId();
    await user.click(within(toast).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(jobId()).toBe(failed + 1));
    expect(jobs.get(jobId())?.retry_of).toBe(failed);
    expect(
      await screen.findByText(/Ready to save Breaking Bad S02 E01/),
    ).toBeInTheDocument();
    await user.click(row().getByRole("button", { name: "Save SRT" }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(tickets).toEqual([jobId()]);
  });

  it("fails the row when Save from the notification finds the ticket gone", async () => {
    server.use(
      ...handlers({
        ticket: () =>
          HttpResponse.json(
            {
              message:
                "This download is no longer available. Download it again from the results.",
              reason: "ticket_expired",
            },
            { status: 404 },
          ),
      }),
    );
    const { user } = renderDiscover();
    await search(user);
    await user.click(row().getByRole("button", { name: "Download SRT" }));
    const toast = await findJobToast(`Download search-1-forced`);
    await user.click(within(toast).getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Download failed for/)).toHaveTextContent(
      /no longer available\. Download it again/,
    );
    expect(row().queryByRole("button", { name: "Save SRT" })).toBeNull();
    expect(save).not.toHaveBeenCalled();
  });

  it("releases the row when its queued job is cancelled from the Jobs drawer", async () => {
    server.use(...handlers({ outcome: () => ({ status: "running" }) }));
    const { user } = renderDiscover();
    await search(user);
    await user.click(row().getByRole("button", { name: "Download SRT" }));
    await waitFor(() => expect(requests).toHaveLength(1));
    emitJob(jobId());
    await waitFor(() =>
      expect(
        queryClient.getQueryData([QueryKeys.System, QueryKeys.Jobs]),
      ).toHaveLength(1),
    );
    expect(
      row("full").getByRole("button", { name: "Download SRT" }),
    ).toBeDisabled();
    // Cancel removes a queued job; the drawer's refetch no longer lists it.
    act(() => queryClient.setQueryData([QueryKeys.System, QueryKeys.Jobs], []));
    expect(await screen.findByText(/Download failed for/)).toHaveTextContent(
      "The download was cancelled.",
    );
    expect(
      row("full").getByRole("button", { name: "Download SRT" }),
    ).toBeEnabled();
  });

  it("turns an expired handle found by the job into the expired recovery", async () => {
    server.use(
      ...handlers({
        outcome: () => ({
          status: "failed",
          retryable: false,
          error: {
            reason: "expired_handle",
            message:
              "This result has expired. Search again for this selection.",
          },
        }),
      }),
    );
    const { user } = renderDiscover();
    await search(user);
    await user.click(row().getByRole("button", { name: "Download SRT" }));
    expect(
      await screen.findByRole("button", {
        name: "Search again for expired result",
      }),
    ).toBeEnabled();
    expect(row().getByRole("button", { name: "Download SRT" })).toBeDisabled();
  });
});

it("releases pending download state when transport refresh retires its expired row", async () => {
  const first = snapshot();
  const expiry = Date.now() + 60000;
  first.results[1].expires_at = new Date(expiry).toISOString();
  let finish: (() => void) | undefined;
  server.use(
    http.post("/api/discover/search", () => HttpResponse.json(first)),
    ...handlers({
      hold: () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    }),
  );
  const { user } = renderDiscover();
  await search(user);
  await user.click(row().getByRole("button", { name: "Download SRT" }));
  await waitFor(() => expect(finish).toBeDefined());
  const now = vi.spyOn(Date, "now").mockReturnValue(expiry + 1);
  try {
    server.use(http.post("/api/discover/search", () => HttpResponse.error()));
    await user.click(screen.getByRole("button", { name: /^Search again$/ }));
    await screen.findByText(/Refresh failed/);
    finish?.();
    await waitFor(() =>
      expect(
        queryClient.isMutating({
          mutationKey: [QueryKeys.Discover, "download"],
        }),
      ).toBe(0),
    );
    await waitFor(() =>
      expect(
        row("full").getByRole("button", { name: "Download SRT" }),
      ).toBeEnabled(),
    );
    expect(
      screen.queryByText(/Preparing download for/),
    ).not.toBeInTheDocument();
    expect(save).not.toHaveBeenCalled();
  } finally {
    now.mockRestore();
    finish?.();
  }
});

const chosenCopy = {
  copy_id: "c1.episode.3.1",
  media_type: "episode" as const,
  local_id: 3,
  arr_instance_id: 1,
  instance_name: "Sonarr HD",
  series_local_id: 30,
  title: "Breaking Bad",
  episode_title: "Seven Thirty-Seven",
  release: "Breaking.Bad.S02E01.1080p.WEB.H264-GRP",
  filename: "breaking.bad.s02e01.mkv",
  source: "Web",
  resolution: "1080p",
  video_codec: "H.264",
  audio_codec: null,
  file_size: 2200000000,
  observed_size: 2200000000,
  updated_at: "2026-09-02T09:00:00Z",
};

function copySnapshot(searchId = "search-1"): DiscoverSearchSnapshot {
  const base = snapshot(searchId);
  return {
    ...base,
    context: {
      ...target,
      copy_id: chosenCopy.copy_id,
      file_revision: "revision-a",
      copy: chosenCopy,
    },
    results: base.results.map((row, index) => ({
      ...row,
      copy_compatibility: {
        source: index === 0 ? "match" : "conflict",
        resolution: index === 0 ? "match" : "conflict",
        video_codec: "match",
        audio_codec: "unknown",
        release_group: index === 0 ? "match" : "conflict",
        edition: "unknown",
      },
    })),
  };
}

describe("Discover results against a chosen library copy", () => {
  it("names the copy as search context rather than proof of compatibility", async () => {
    server.use(
      http.post("/api/discover/search", async ({ request }) => {
        searches.push(await request.json());
        return HttpResponse.json(copySnapshot());
      }),
    );
    const { user } = renderDiscover();
    await search(user);
    expect(
      row("full").getByText(/Search context: Sonarr HD/),
    ).toHaveTextContent(/Breaking\.Bad\.S02E01\.1080p\.WEB\.H264-GRP/);
    expect(
      row("full").getByText(
        /Release details from this copy are search context\. They do not verify subtitle synchronization/i,
      ),
    ).toBeInTheDocument();
  });

  it("separates known matches, conflicts and unknown compatibility per result", async () => {
    server.use(
      http.post("/api/discover/search", async ({ request }) => {
        searches.push(await request.json());
        return HttpResponse.json(copySnapshot());
      }),
    );
    const { user } = renderDiscover();
    await search(user);
    expect(row("full").getByText("Matches this copy")).toBeInTheDocument();
    expect(
      row("full").getByText("source, resolution, video codec, release group"),
    ).toBeInTheDocument();
    // The same facts the copy states and the release contradicts.
    expect(
      row("forced").getByText("source, resolution, release group"),
    ).toBeInTheDocument();
    // Silence on both sides is never reported as agreement.
    expect(row("forced").getByText("audio codec, edition")).toBeInTheDocument();
    expect(row("full").getByText("None stated")).toBeInTheDocument();
  });

  it("refuses a copy supplied in the same update that changes the target", async () => {
    server.use(
      http.post("/api/discover/search", async ({ request }) => {
        searches.push(await request.json());
        return HttpResponse.json(copySnapshot());
      }),
    );
    const { user } = renderDiscover();
    // One update that both moves to a new target and names a copy is naming a
    // copy for a target that did not exist when the copy was resolved.
    await user.click(
      screen.getByRole("button", { name: "Choose episode and copy at once" }),
    );
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    await waitFor(() => expect(searches).toHaveLength(1));
    expect(searches[0]).not.toHaveProperty("copy_id");
    // Naming it afterwards, with the target already in place, is accepted.
    await user.click(screen.getByRole("button", { name: "Choose copy" }));
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    await waitFor(() => expect(searches).toHaveLength(2));
    expect(searches[1]).toMatchObject({ copy_id: chosenCopy.copy_id });
  });

  it("recovers an expired handle with the same chosen copy it was captured with", async () => {
    server.use(
      http.post("/api/discover/search", async ({ request }) => {
        searches.push(await request.json());
        return HttpResponse.json(copySnapshot());
      }),
      http.post("/api/discover/download", () =>
        HttpResponse.json(
          { reason: "result_expired", recoverable: true, message: "gone" },
          { status: 410 },
        ),
      ),
    );
    const { user } = renderDiscover();
    await searchWithCopy(user);
    expect(searches[0]).toMatchObject({ copy_id: chosenCopy.copy_id });
    await user.click(row().getByRole("button", { name: "Download SRT" }));
    await screen.findByText(/This result has expired/);
    server.use(
      http.post("/api/discover/search", async ({ request }) => {
        searches.push(await request.json());
        return HttpResponse.json(copySnapshot("search-2"));
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "Search again for expired result" }),
    );
    await waitFor(() => expect(searches).toHaveLength(2));
    expect(searches[1]).toMatchObject({ copy_id: chosenCopy.copy_id });
    // Only the opaque copy identity is an input. The resolved copy and its
    // physical revision are server-owned and must never be sent back.
    expect(searches[1]).not.toHaveProperty("copy");
    expect(searches[1]).not.toHaveProperty("file_revision");
    expect(searches[1]).not.toHaveProperty("matching_mode");
  });
});
