/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import TranslatePanel from "@/pages/SubtitleEditor/TranslatePanel";
import WaveformTimeline from "@/pages/SubtitleEditor/WaveformTimeline";
import { act, customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const wavesurfer = vi.hoisted(() => ({
  load: vi.fn(),
  handlers: {} as Record<string, () => void>,
}));

vi.mock("wavesurfer.js", () => ({
  default: {
    create: () => ({
      on: (event: string, handler: () => void) => {
        wavesurfer.handlers[event] = handler;
      },
      load: wavesurfer.load,
      destroy: vi.fn(),
      zoom: vi.fn(),
      getDuration: () => 0,
      setTime: vi.fn(),
      seekTo: vi.fn(),
    }),
  },
}));
vi.mock("wavesurfer.js/dist/plugins/regions.esm.js", () => ({
  default: {
    create: () => ({
      on: vi.fn(),
      un: vi.fn(),
      clearRegions: vi.fn(),
      addRegion: vi.fn(),
      getRegions: () => [],
    }),
  },
}));
vi.mock("wavesurfer.js/dist/plugins/timeline.esm.js", () => ({
  default: { create: () => ({}) },
}));

const JOBS_KEY = [QueryKeys.System, QueryKeys.Jobs];

function job(overrides: Partial<System.Jobs>): System.Jobs {
  return {
    job_id: 7,
    job_name: "Translating Example in the editor (English to Hungarian)",
    status: "running",
    last_run_time: "",
    is_progress: true,
    is_signalr: false,
    progress_value: 0,
    progress_max: 100,
    progress_message: "",
    ...overrides,
  };
}

// What the jobs socket reducer does with an update: patch the shared cache.
function socketUpdate(update: Partial<System.Jobs>) {
  act(() => {
    queryClient.setQueryData(JOBS_KEY, [job(update)]);
  });
}

beforeEach(() => {
  wavesurfer.load.mockReset();
  server.use(
    http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { code2: "en", code3: "eng", name: "English", enabled: true },
        { code2: "hu", code3: "hun", name: "Hungarian", enabled: true },
      ]),
    ),
    http.get("/api/translator/models", () =>
      HttpResponse.json({ models: [], default_model: "deepseek/example" }),
    ),
  );
});

function renderPanel(onApplyTranslation = vi.fn()) {
  customRender(
    <TranslatePanel
      open
      cues={[
        { id: "a", text: "Hello" },
        { id: "b", text: "World" },
      ]}
      currentLanguage="hu"
      mediaTitle="Example"
      onApplyTranslation={onApplyTranslation}
      onSetReference={vi.fn()}
      onLoadReference={vi.fn()}
      onImportReference={vi.fn()}
      onClose={vi.fn()}
    />,
  );
}

describe("TranslatePanel on the job path", () => {
  it("queues a job, shows its progress from the jobs cache and applies the result", async () => {
    const submitted: unknown[] = [];
    let resultReads = 0;
    server.use(
      http.post("/api/translator/editor", async ({ request }) => {
        submitted.push(await request.json());
        return HttpResponse.json({ jobId: 7 }, { status: 202 });
      }),
      http.get("/api/translator/editor", ({ request }) => {
        resultReads += 1;
        expect(new URL(request.url).searchParams.get("jobId")).toBe("7");
        return HttpResponse.json({
          jobId: 7,
          status: "completed",
          partial: null,
          lines: [
            { position: 0, line: "Szia" },
            { position: 1, line: "Világ" },
          ],
        });
      }),
    );
    const onApply = vi.fn();
    renderPanel(onApply);
    const user = userEvent.setup();

    const translate = await screen.findByRole("button", { name: "Translate" });
    await waitFor(() => expect(translate).toBeEnabled());
    await user.click(translate);

    expect(await screen.findByRole("button", { name: "Cancel" })).toBeVisible();
    expect(submitted).toEqual([
      {
        lines: [
          { position: 0, line: "Hello" },
          { position: 1, line: "World" },
        ],
        sourceLanguage: "",
        targetLanguage: "Hungarian",
        title: "Example",
        mediaType: "",
      },
    ]);

    socketUpdate({ progress_value: 40, progress_message: "Batch 2 of 5" });
    expect(await screen.findByText("Batch 2 of 5 (40%)")).toBeVisible();
    // A running job is never read: the panel waits for the terminal event.
    expect(resultReads).toBe(0);

    socketUpdate({ status: "completed", progress_value: 100 });
    expect(
      await screen.findByText(/Translation complete\. 2\/2 lines translated\./),
    ).toBeVisible();
    expect(resultReads).toBe(1);

    await user.click(screen.getByRole("button", { name: "Apply to Cues" }));
    expect(onApply).toHaveBeenCalledWith(
      new Map([
        [0, "Szia"],
        [1, "Világ"],
      ]),
    );
  });

  it("shows the job's failure reason", async () => {
    server.use(
      http.post("/api/translator/editor", () =>
        HttpResponse.json({ jobId: 7 }, { status: 202 }),
      ),
      http.get("/api/translator/editor", () =>
        HttpResponse.json({
          jobId: 7,
          status: "failed",
          error: "Cannot connect to the AI Subtitle Translator service.",
        }),
      ),
    );
    renderPanel();
    const user = userEvent.setup();

    const translate = await screen.findByRole("button", { name: "Translate" });
    await waitFor(() => expect(translate).toBeEnabled());
    await user.click(translate);
    await screen.findByRole("button", { name: "Cancel" });

    socketUpdate({ status: "failed" });

    expect(
      await screen.findByText(
        "Cannot connect to the AI Subtitle Translator service.",
      ),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Try Again" })).toBeVisible();
  });
});

describe("TranslatePanel recovery and cancel", () => {
  it("picks up a job that finished while its terminal event was missed", async () => {
    server.use(
      http.post("/api/translator/editor", () =>
        HttpResponse.json({ jobId: 7 }, { status: 202 }),
      ),
      http.get("/api/system/jobs", ({ request }) =>
        HttpResponse.json({
          data:
            new URL(request.url).searchParams.get("id") === "7"
              ? [job({ status: "completed", progress_value: 100 })]
              : [],
        }),
      ),
      http.get("/api/translator/editor", () =>
        HttpResponse.json({
          jobId: 7,
          status: "completed",
          lines: [
            { position: 0, line: "Szia" },
            { position: 1, line: "Világ" },
          ],
        }),
      ),
    );
    renderPanel();
    const user = userEvent.setup();

    const translate = await screen.findByRole("button", { name: "Translate" });
    await waitFor(() => expect(translate).toBeEnabled());
    await user.click(translate);

    // No socket update at all: the re-read of the tracked job finds it done.
    expect(
      await screen.findByText(/Translation complete\. 2\/2 lines translated\./),
    ).toBeVisible();
  });

  it("asks the server to stop the job, queued or running", async () => {
    const cancelled: string[] = [];
    server.use(
      http.post("/api/translator/editor", () =>
        HttpResponse.json({ jobId: 7 }, { status: 202 }),
      ),
      http.delete("/api/translator/editor", ({ request }) => {
        cancelled.push(new URL(request.url).searchParams.get("jobId") ?? "");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderPanel();
    const user = userEvent.setup();

    const translate = await screen.findByRole("button", { name: "Translate" });
    await waitFor(() => expect(translate).toBeEnabled());
    await user.click(translate);
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(cancelled).toEqual(["7"]));
    expect(screen.getByRole("button", { name: "Translate" })).toBeVisible();
  });
});

describe("WaveformTimeline on the job path", () => {
  it("waits for the peaks job's terminal event, then fetches the peaks", async () => {
    let peakRequests = 0;
    server.use(
      http.get("/api/editor/peaks", () => {
        peakRequests += 1;
        if (peakRequests === 1) {
          return HttpResponse.json(
            { jobId: 9, status: "pending" },
            { status: 202 },
          );
        }
        return HttpResponse.json({
          peaks: [0.5, -1],
          duration: 0.2,
          sampleRate: 10,
        });
      }),
    );

    customRender(
      <WaveformTimeline
        mediaType="movie"
        mediaId={1}
        cues={[]}
        selectedIndex={-1}
        onSelect={vi.fn()}
      />,
    );

    expect(await screen.findByText("Generating waveform...")).toBeVisible();
    expect(peakRequests).toBe(1);

    act(() => {
      queryClient.setQueryData(JOBS_KEY, [
        job({
          job_id: 9,
          job_name: "Generating waveform for film.mkv",
          progress_value: 50,
        }),
      ]);
    });
    expect(
      await screen.findByText("Generating waveform (50%)..."),
    ).toBeVisible();
    expect(peakRequests).toBe(1);

    act(() => {
      queryClient.setQueryData(JOBS_KEY, [
        job({ job_id: 9, status: "completed", progress_value: 100 }),
      ]);
    });

    await waitFor(() => expect(wavesurfer.load).toHaveBeenCalledTimes(1));
    expect(peakRequests).toBe(2);
    const [, channels, duration] = wavesurfer.load.mock.calls[0];
    expect(Array.from(channels[0] as Float32Array)).toEqual([0.5, -1]);
    expect(duration).toBe(0.2);
  });

  it("shows why the waveform failed instead of asking again", async () => {
    let peakRequests = 0;
    server.use(
      http.get("/api/editor/peaks", () => {
        peakRequests += 1;
        return HttpResponse.json({ jobId: 9 }, { status: 202 });
      }),
    );

    customRender(
      <WaveformTimeline
        mediaType="movie"
        mediaId={1}
        cues={[]}
        selectedIndex={-1}
        onSelect={vi.fn()}
      />,
    );
    await screen.findByText("Generating waveform...");

    act(() => {
      queryClient.setQueryData(JOBS_KEY, [
        job({
          job_id: 9,
          status: "failed",
          progress_message: "ffmpeg could not read audio track 1",
        }),
      ]);
    });

    expect(
      await screen.findByText("ffmpeg could not read audio track 1"),
    ).toBeVisible();
    expect(peakRequests).toBe(1);
  });
});
