/* eslint-disable camelcase */

/**
 * The movie details page reads the metadata of a title indexed before codecs
 * were stored, once, in the background. Only a row the server flags as
 * codec_pending triggers it: a current row whose codec is null stays null
 * however often it is scanned.
 */

import { StrictMode } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import MovieDetailView from "@/pages/Movies/Details";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

function movieWith(subtitles: Subtitle[]) {
  return {
    id: 12,
    radarrId: 7,
    arr_instance_id: 2,
    title: "Test Movie",
    path: "/movies/test.mkv",
    profileId: 1,
    fanart: "",
    overview: "",
    imdbId: "tt0000001",
    alternativeTitles: [],
    poster: "",
    year: "2024",
    monitored: true,
    tags: [],
    audio_language: [],
    subtitles,
    missing_subtitles: [],
  };
}

const track = (codec: string | null, extra: Partial<Subtitle> = {}) =>
  ({
    code2: "en",
    name: "English",
    hi: false,
    forced: false,
    path: null,
    codec,
    ...extra,
  }) as Subtitle;

interface Page {
  scans: unknown[];
  setMovie: (subtitles: Subtitle[]) => void;
  unmount: () => void;
}

function openPage(initial: Subtitle[], scanAnswer: () => Response): Page {
  let subtitles = initial;
  const scans: unknown[] = [];
  server.use(
    http.get("/api/movies", () =>
      HttpResponse.json({ data: [movieWith(subtitles)], total: 1 }),
    ),
    http.get("/api/movies/history", () =>
      HttpResponse.json({ data: [], total: 0 }),
    ),
    http.get("/api/subtitles/sync-status", () =>
      HttpResponse.json({ data: null }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.patch("/api/movies", async ({ request }) => {
      scans.push(Object.fromEntries((await request.formData()).entries()));
      return scanAnswer();
    }),
  );
  const router = createMemoryRouter(
    [{ path: "/movies/:id", element: <MovieDetailView /> }],
    { initialEntries: ["/movies/12"] },
  );
  const { unmount } = rawRender(
    <StrictMode>
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>
    </StrictMode>,
  );
  return {
    scans,
    unmount,
    setMovie: (next) => {
      subtitles = next;
    },
  };
}

const jobs = (status: string) => [{ job_id: 31, status }];

describe("Movie details, background metadata scan", () => {
  it("scans once for a row indexed before codecs were stored, and follows the job", async () => {
    const page = openPage([track(null, { codec_pending: true })], () =>
      HttpResponse.json({ job_id: 31 }, { status: 202 }),
    );

    expect(await screen.findByText("Reading track metadata…")).toBeVisible();
    await waitFor(() => expect(page.scans).toHaveLength(1));
    expect(page.scans[0]).toMatchObject({
      action: "scan-disk",
      radarrid: "7",
      arr_instance_id: "2",
    });

    // The scan job ends and the refetched movie carries the codec.
    page.setMovie([track("pgs")]);
    await act(async () => {
      queryClient.setQueryData(
        [QueryKeys.System, QueryKeys.Jobs],
        jobs("completed"),
      );
    });

    await waitFor(() =>
      expect(screen.queryByText("Reading track metadata…")).toBeNull(),
    );
    expect(await screen.findByText("PGS")).toBeInTheDocument();
    expect(page.scans).toHaveLength(1);
  });

  it("never scans for a current row whose codec is null", async () => {
    const page = openPage([track(null)], () =>
      HttpResponse.json({ job_id: 31 }, { status: 202 }),
    );

    expect(await screen.findByText("Video File Subtitle Track")).toBeVisible();
    // Give the effect every chance to fire before asserting it did not.
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(page.scans).toHaveLength(0);
    expect(screen.queryByText("Reading track metadata…")).toBeNull();
  });

  it("stops waiting when a refetch fills the codecs before the job's terminal event", async () => {
    const page = openPage([track(null, { codec_pending: true })], () =>
      HttpResponse.json({ job_id: 31 }, { status: 202 }),
    );
    expect(await screen.findByText("Reading track metadata…")).toBeVisible();
    await waitFor(() => expect(page.scans).toHaveLength(1));

    // A socket refetch brings the codecs; no job event has arrived.
    page.setMovie([track("pgs")]);
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: [QueryKeys.Movies] });
    });

    await waitFor(() =>
      expect(screen.queryByText("Reading track metadata…")).toBeNull(),
    );
    expect(page.scans).toHaveLength(1);

    // Nothing is pending any more, so a row flagged later is not shown as
    // being read, and this visit does not scan the title again.
    page.setMovie([track(null, { codec_pending: true })]);
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: [QueryKeys.Movies] });
    });
    expect(
      await screen.findByText("Video File Subtitle Track"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Reading track metadata…")).toBeNull();
    expect(page.scans).toHaveLength(1);
  });

  it("stops following the scan job when the page unmounts", async () => {
    const page = openPage([track(null, { codec_pending: true })], () =>
      HttpResponse.json({ job_id: 31 }, { status: 202 }),
    );
    expect(await screen.findByText("Reading track metadata…")).toBeVisible();
    await waitFor(() => expect(page.scans).toHaveLength(1));

    page.unmount();
    // A terminal job event after the page is gone refetches nothing.
    let refetched = 0;
    server.use(
      http.get("/api/movies", () => {
        refetched += 1;
        return HttpResponse.json({ data: [], total: 0 });
      }),
    );
    await act(async () => {
      queryClient.setQueryData(
        [QueryKeys.System, QueryKeys.Jobs],
        jobs("completed"),
      );
    });
    await new Promise((resolve) => setTimeout(resolve, 200));

    expect(refetched).toBe(0);
  });
});
