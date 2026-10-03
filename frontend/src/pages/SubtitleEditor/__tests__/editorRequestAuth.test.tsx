/* eslint-disable camelcase */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import EditorPage from "@/pages/SubtitleEditor/EditorPage";
import TimingToolsPanel from "@/pages/SubtitleEditor/TimingToolsPanel";
import VideoPreview from "@/pages/SubtitleEditor/VideoPreview";
import { AllProviders } from "@/providers";
import { customRender, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

// The editor's own fetch() calls bypass the axios client, so each one has to
// send the X-API-KEY header itself. A key in the URL ends up in reverse proxy
// access logs and makes the server log a deprecation warning on every call.
const API_KEY = "editor key/+&=";

interface SeenRequest {
  url: URL;
  key: string | null;
}

function seen(request: Request): SeenRequest {
  return { url: new URL(request.url), key: request.headers.get("X-API-KEY") };
}

function expectHeaderOnly(requests: SeenRequest[]) {
  expect(requests.length).toBeGreaterThan(0);
  for (const { url, key } of requests) {
    expect(key).toBe(API_KEY);
    expect(url.searchParams.has("apikey")).toBe(false);
    expect(url.search).not.toContain(encodeURIComponent(API_KEY));
  }
}

beforeEach(() => {
  vi.stubGlobal("Bazarr", { apiKey: API_KEY, baseUrl: "" });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("editor requests authenticate with a header", () => {
  it("asks for the media info with the key in the header", async () => {
    // jsdom has no media loading, and the player resets its element whenever
    // its effect cleans up, so the test unmounts while the stub is in place.
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(
      () => undefined,
    );
    const requests: SeenRequest[] = [];
    server.use(
      http.get("/api/editor/info", ({ request }) => {
        requests.push(seen(request));
        return HttpResponse.json({
          duration: 12.5,
          videoCodec: "h264",
          audioCodec: "aac",
          resolution: "1920x1080",
          container: "mkv",
          audioTracks: [],
        });
      }),
    );

    const { unmount } = customRender(
      <VideoPreview
        mediaType="episode"
        mediaId={7}
        arrInstanceId={2}
        currentTimeMs={0}
      />,
    );

    await waitFor(() => expect(requests.length).toBeGreaterThan(0));
    expectHeaderOnly(requests);
    expect(Object.fromEntries(requests[0].url.searchParams)).toEqual({
      mediaType: "episode",
      mediaId: "7",
      arr_instance_id: "2",
    });
    unmount();
  });

  it("asks for the subtitle list with the key in the header", async () => {
    const requests: SeenRequest[] = [];
    server.use(
      http.get("/api/editor/subtitles", ({ request }) => {
        requests.push(seen(request));
        return HttpResponse.json({ subtitles: [] });
      }),
      http.get("/api/system/languages", () => HttpResponse.json([])),
      http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
      http.get(
        "/api/movies/:id/subtitles/:language/content",
        () => new HttpResponse(null, { status: 404 }),
      ),
    );
    const router = createMemoryRouter(
      [
        {
          path: "/subtitles/edit/:mediaType/:mediaId/:language",
          element: <EditorPage />,
        },
      ],
      { initialEntries: ["/subtitles/edit/movie/50/en?arr_instance_id=3"] },
    );

    rawRender(
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>,
    );

    await waitFor(() => expect(requests.length).toBeGreaterThan(0));
    expectHeaderOnly(requests);
    expect(Object.fromEntries(requests[0].url.searchParams)).toEqual({
      mediaType: "movie",
      mediaId: "50",
      arr_instance_id: "3",
    });
  });

  it("submits and polls a sync with the key only in the header", async () => {
    const submits: SeenRequest[] = [];
    const polls: SeenRequest[] = [];
    server.use(
      http.post("/api/editor/sync", ({ request }) => {
        submits.push(seen(request));
        return HttpResponse.json(
          { jobKey: "job 1", status: "running" },
          { status: 202 },
        );
      }),
      http.get("/api/editor/sync", ({ request }) => {
        polls.push(seen(request));
        return HttpResponse.json({
          status: "completed",
          content: "1\n00:00:02,000 --> 00:00:03,000\nHello\n",
        });
      }),
    );
    const onApplySyncedContent = vi.fn();

    customRender(
      <TimingToolsPanel
        open
        cues={[]}
        selectedIndices={new Set()}
        mediaType="movie"
        mediaId={50}
        arrInstanceId={3}
        language="en"
        onApplyBatch={vi.fn()}
        onGetContent={() => ({
          content: "1\n00:00:01,000 --> 00:00:02,000\nHello\n",
          format: "srt",
          encoding: "utf-8",
        })}
        onApplySyncedContent={onApplySyncedContent}
        onClose={vi.fn()}
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Auto-Sync" }));
    await user.click(screen.getByRole("button", { name: "Sync" }));

    // The first poll follows the submit after two seconds.
    await waitFor(() => expect(onApplySyncedContent).toHaveBeenCalled());
    expectHeaderOnly(submits);
    expectHeaderOnly(polls);
    expect(Object.fromEntries(polls[0].url.searchParams)).toEqual({
      jobKey: "job 1",
    });
  });
});
