/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { createMemoryRouter, Link, RouterProvider } from "react-router";
import { Button, useMantineColorScheme } from "@mantine/core";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { useDiscover } from "@/contexts/Discover";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import type { DiscoverSearchSnapshot } from "@/types/discover";
import { setAuthenticated } from "@/utilities/event";
import * as files from "@/utilities/files";
import { pickOption } from "./selectTestHelpers";
import Discover from ".";

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
  const { updateDraft } = useDiscover();
  const { toggleColorScheme } = useMantineColorScheme();
  return (
    <>
      <Button onClick={() => updateDraft(episodeDraft)}>Choose episode</Button>
      {/* Two updates, because a single one that changes the target and
          supplies a copy retires the copy by design. */}
      <Button onClick={() => updateDraft({ copyId: chosenCopy.copy_id })}>
        Choose copy
      </Button>
      <Button
        onClick={() =>
          updateDraft({ ...episodeDraft, copyId: chosenCopy.copy_id })
        }
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
  return { user: userEvent.setup() };
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
let requests: {
  result: string | null;
  search: string | null;
  authenticated: boolean;
}[];
let searches: unknown[];
let save: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  localStorage.clear();
  requests = [];
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
    http.get("/api/discover/download", ({ request }) => {
      const params = new URL(request.url).searchParams;
      requests.push({
        result: params.get("result_id"),
        search: params.get("search_id"),
        authenticated: !!request.headers.get("X-API-KEY"),
      });
      const forced = params.get("result_id")?.endsWith("forced");
      return new HttpResponse(forced ? forcedSrt : fullSrt, {
        headers: {
          "Content-Type": "application/x-subrip",
          "Content-Disposition": `attachment; filename="Breaking_Bad.S02E01.eng.${forced ? "forced" : "full"}.srt"`,
        },
      });
    }),
  );
});

describe("Discover attachments", () => {
  it("downloads only the clicked forced row with its exact identifiers and device filename", async () => {
    const { user } = renderDiscover();
    await search(user);
    expect(requests).toEqual([]);
    await user.click(row().getByRole("button", { name: "Download SRT" }));
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
    const { user } = renderDiscover();
    await search(user);
    await user.click(row().getByRole("button", { name: "Download SRT" }));
    await screen.findByText(/Download started for/);
    await user.click(screen.getByRole("button", { name: "Change appearance" }));
    await user.click(screen.getByRole("link", { name: "Provider settings" }));
    await user.click(screen.getByRole("link", { name: "Return to Discover" }));
    expect(screen.getByText(/Download started for/)).toBeInTheDocument();
    expect(searches).toHaveLength(1);
    expect(requests).toHaveLength(1);
  });

  it("retires a 410 handle and recovers by searching the exact captured episode context", async () => {
    const { user } = renderDiscover();
    await search(user);
    server.use(
      http.get("/api/discover/download", () =>
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
    await user.click(screen.getByRole("button", { name: "Search again" }));
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
    await user.click(screen.getByRole("button", { name: "Refresh subtitles" }));
    await screen.findByText("Previous result");
    expect(
      screen.queryByRole("heading", { name: "Breaking.Bad.S02E01.full" }),
    ).not.toBeInTheDocument();
    await user.click(row().getByRole("button", { name: "Download SRT" }));
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
      http.get("/api/discover/download", () =>
        HttpResponse.json({}, { status: 410 }),
      ),
      http.post("/api/discover/search", () => HttpResponse.error()),
    );
    await user.click(row().getByRole("button", { name: "Download SRT" }));
    await screen.findByText(/This result has expired/);
    await user.click(screen.getByRole("button", { name: "Search again" }));
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
      await user.click(row().getByRole("button", { name: "Download SRT" }));
      await screen.findByText(/Download started for/);
      if (field === "Subtitle language")
        await pickOption(user, field, "Hungarian");
      else {
        // "Episode" is also the media-type radio; the number field is the
        // textbox of that name.
        await user.clear(screen.getByRole("textbox", { name: field }));
        await user.type(
          screen.getByRole("textbox", { name: field }),
          field === "Episode" ? "2" : "tt0133093",
        );
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
        http.get("/api/discover/download", async () => {
          await new Promise<void>((resolve) => {
            finish = resolve;
          });
          return new HttpResponse(forcedSrt, {
            headers: { "Content-Type": "application/x-subrip" },
          });
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
          screen.getByRole("button", { name: "Refresh subtitles" }),
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
    },
  );

  it.each([401, 502, 200])(
    "never saves an error or app-shell payload (%s)",
    async (status) => {
      server.use(
        http.get("/api/discover/download", () =>
          status === 200
            ? new HttpResponse("<html>app shell</html>", {
                headers: { "Content-Type": "text/html" },
              })
            : HttpResponse.json({ message: "Could not download" }, { status }),
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
});

it("releases pending download state when transport refresh retires its expired row", async () => {
  const first = snapshot();
  const expiry = Date.now() + 60000;
  first.results[1].expires_at = new Date(expiry).toISOString();
  let finish: (() => void) | undefined;
  server.use(
    http.post("/api/discover/search", () => HttpResponse.json(first)),
    http.get("/api/discover/download", async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return new HttpResponse(forcedSrt, {
        headers: { "Content-Type": "application/x-subrip" },
      });
    }),
  );
  const { user } = renderDiscover();
  await search(user);
  await user.click(row().getByRole("button", { name: "Download SRT" }));
  await waitFor(() => expect(finish).toBeDefined());
  const now = vi.spyOn(Date, "now").mockReturnValue(expiry + 1);
  try {
    server.use(http.post("/api/discover/search", () => HttpResponse.error()));
    await user.click(screen.getByRole("button", { name: "Refresh subtitles" }));
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
    expect(screen.getByText(/Search context: Sonarr HD/)).toHaveTextContent(
      /Breaking\.Bad\.S02E01\.1080p\.WEB\.H264-GRP/,
    );
    expect(
      screen.getByText(
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
      http.get("/api/discover/download", () =>
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
    await user.click(screen.getByRole("button", { name: "Search again" }));
    await waitFor(() => expect(searches).toHaveLength(2));
    expect(searches[1]).toMatchObject({ copy_id: chosenCopy.copy_id });
    // Only the opaque copy identity is an input. The resolved copy and its
    // physical revision are server-owned and must never be sent back.
    expect(searches[1]).not.toHaveProperty("copy");
    expect(searches[1]).not.toHaveProperty("file_revision");
    expect(searches[1]).not.toHaveProperty("matching_mode");
  });
});
