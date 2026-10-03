/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { SportsSearchModal } from "@/components/modals/SportsSearchModal";
import { useModals } from "@/modules/modals";
import { act, customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const QUEUED =
  "Queued in Jobs. You can close this window; Jobs reports the result.";

const candidate: SearchResultType = {
  provider: "fixture",
  url: "https://example.test/subtitle/1",
  language: "en",
  forced: "False",
  hearing_impaired: "False",
  score: 90,
  orig_score: 90,
  score_without_hash: 88,
  release_info: ["Race.2026.1080p"],
  matches: ["title"],
  dont_matches: [],
  subtitle: null,
  original_format: "False",
};

function Launch() {
  const modals = useModals();
  return (
    <button
      onClick={() =>
        modals.openContextModal(SportsSearchModal, {
          item: { id: 61, arr_instance_id: 1, profileId: null },
          language: "en",
        })
      }
    >
      Open search
    </button>
  );
}

function finishJob(overrides: Partial<System.Jobs>) {
  act(() => {
    queryClient.setQueryData(
      [QueryKeys.System, QueryKeys.Jobs],
      [
        {
          job_id: 7,
          job_name: "Downloading Race subtitles",
          status: "completed",
          last_run_time: "",
          is_progress: false,
          is_signalr: false,
          progress_value: 0,
          progress_max: 0,
          progress_message: "",
          ...overrides,
        },
      ],
    );
  });
}

async function searchAndDownload(jobId: number | null) {
  // The endpoint answers 202 with the job it queued.
  vi.spyOn(api.sports, "downloadSubtitle").mockResolvedValue({
    job_id: jobId,
  });
  const user = userEvent.setup();
  customRender(<Launch />);
  await user.click(screen.getByRole("button", { name: "Open search" }));
  await user.click(await screen.findByRole("button", { name: "Search" }));
  await user.click(await screen.findByLabelText("Download"));
}

beforeEach(() => {
  act(() => queryClient.setQueryData([QueryKeys.System, QueryKeys.Jobs], []));
  server.use(
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { code2: "en", code3: "eng", name: "English", enabled: true },
      ]),
    ),
    http.get("/api/system/languages/profiles", () => HttpResponse.json([])),
    http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
    http.post("/api/sports/events/:id/search", () =>
      HttpResponse.json({ data: [candidate] }),
    ),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SportsSearchModal download", () => {
  it("waits for the job and keeps a failed download retryable with its reason", async () => {
    await searchAndDownload(7);

    expect(await screen.findByText(QUEUED)).toBeInTheDocument();
    expect(screen.getByLabelText("Download")).toBeDisabled();

    finishJob({
      status: "failed",
      error: { reason: "failed", message: "Provider refused the download" },
    });

    expect(
      await screen.findByText("Provider refused the download"),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByLabelText("Download")).toBeEnabled(),
    );
    expect(screen.queryByText(QUEUED)).toBeNull();
  });

  it("marks the result and keeps it disabled once the job completes", async () => {
    await searchAndDownload(7);
    await screen.findByText(QUEUED);

    finishJob({ status: "completed" });

    await waitFor(() => expect(screen.queryByText(QUEUED)).toBeNull());
    expect(screen.getByLabelText("Download")).toBeDisabled();
  });

  it("marks the result at once when no job was queued to follow", async () => {
    await searchAndDownload(null);

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Search Again" }),
      ).toBeEnabled(),
    );
    expect(screen.getByLabelText("Download")).toBeDisabled();
    expect(screen.queryByText(QUEUED)).toBeNull();
  });

  it("keeps a stopped download retryable", async () => {
    await searchAndDownload(7);
    await screen.findByText(QUEUED);

    finishJob({ status: "completed", stopped: true });

    expect(
      await screen.findByText("Downloading Race subtitles was stopped"),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByLabelText("Download")).toBeEnabled(),
    );
  });

  it("shows the warning a completed job left as its progress message", async () => {
    await searchAndDownload(7);
    await screen.findByText(QUEUED);
    expect(screen.queryByText(/media server was not notified/)).toBeNull();

    // Published with warnings: the queue records the warning sentence as the
    // job's last progress message, and the job still resolves as completed.
    finishJob({
      status: "completed",
      progress_message:
        "Subtitle published, but the media server was not notified.",
    });

    expect(
      await screen.findByText(
        "Subtitle published, but the media server was not notified.",
      ),
    ).toBeInTheDocument();
    // The row is still marked: the download did happen.
    expect(screen.getByLabelText("Download")).toBeDisabled();
    expect(screen.queryByText(QUEUED)).toBeNull();
  });
});
