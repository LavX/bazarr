import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it, vi } from "vitest";
import EditorPage from "@/pages/SubtitleEditor/EditorPage";
import { AllProviders } from "@/providers";
import { act, fireEvent, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

vi.mock("@/pages/SubtitleEditor/VideoPreview", () => ({ default: () => null }));
vi.mock("@/pages/SubtitleEditor/WaveformTimeline", () => ({
  default: () => null,
}));

async function openSourceEditor(multipleEngines = false) {
  const user = userEvent.setup();
  const poll = Promise.withResolvers<void>();
  const polled = vi.fn();
  const saved: { language: string; content: string; etag: string | null }[] =
    [];
  server.use(
    http.get("/api/editor/subtitles", () =>
      HttpResponse.json({
        subtitles: [
          { language: "en", format: "srt" },
          { language: "hu", format: "srt" },
        ],
      }),
    ),
    http.get("/api/system/languages", () =>
      HttpResponse.json([{ code2: "de", name: "German", enabled: true }]),
    ),
    http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.get(
      "/api/sources/items/42/subtitles/:language/content",
      ({ params }) =>
        HttpResponse.json(
          {
            content: `1\n00:00:01,000 --> 00:00:02,000\nOriginal ${params.language}\n`,
            format: "srt",
            encoding: "utf-8",
            language: params.language,
            mediaTitle: "Local film",
            mediaId: 42,
            exists: true,
          },
          { headers: { ETag: `"${params.language}"` } },
        ),
    ),
    http.put(
      "/api/sources/items/42/subtitles/:language/content",
      async ({ params, request }) => {
        const body = (await request.json()) as { content: string };
        saved.push({
          language: String(params.language),
          content: body.content,
          etag: request.headers.get("If-Match"),
        });
        return new HttpResponse(null, {
          status: 204,
          headers: { ETag: '"saved"' },
        });
      },
    ),
    http.post("/api/editor/sync", () =>
      HttpResponse.json(
        { jobKey: "sync-en", jobId: 77, jobSession: "selection-test" },
        { status: 202 },
      ),
    ),
    http.get("/api/editor/sync", async () => {
      polled();
      await poll.promise;
      const content = "1\n00:00:03,000 --> 00:00:04,000\nSynced English\n";
      return HttpResponse.json({
        status: "completed",
        content,
        results: multipleEngines
          ? [
              { engine: "ffsubsync", content },
              { engine: "alass", content },
            ]
          : undefined,
      });
    }),
  );
  const router = createMemoryRouter(
    [
      {
        path: "/subtitles/edit/:mediaType/:mediaId/:language",
        element: <EditorPage />,
      },
    ],
    { initialEntries: ["/subtitles/edit/source/42/en"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  await screen.findAllByText("Original en");
  return { user, poll, polled, saved };
}

async function startSync(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Timing Tools" }));
  await user.click(screen.getByRole("button", { name: "Auto-Sync" }));
  await user.click(screen.getByRole("button", { name: "Sync" }));
}

async function switchLanguage(
  user: ReturnType<typeof userEvent.setup>,
  from: "en" | "hu",
  to: "en" | "hu",
) {
  await user.click(screen.getByRole("button", { name: `${from} [srt] ▾` }));
  await user.click(
    await screen.findByRole("menuitem", { name: `${to} [srt]` }),
  );
  await screen.findAllByText(`Original ${to}`);
}

it("offers only existing subtitles in the source language menu", async () => {
  const { user } = await openSourceEditor();
  expect(
    screen.queryByRole("button", { name: "Save As" }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Discover" })).toHaveAttribute(
    "href",
    "/discover",
  );
  expect(
    screen.queryByRole("link", { name: "Local film" }),
  ).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /^en \[srt\]/ }));
  await screen.findByRole("menuitem", { name: "hu [srt]" });
  expect(screen.queryByText("Create new")).not.toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "German (de)" })).toBeNull();
});

it("keeps the newly selected subtitle and its ETag when an earlier sync finishes", async () => {
  const { user, poll, polled, saved } = await openSourceEditor();
  await startSync(user);
  await waitFor(() => expect(polled).toHaveBeenCalled());
  await switchLanguage(user, "en", "hu");
  await act(async () => {
    poll.resolve();
  });
  await screen.findByText(
    "Sync finished for a different subtitle; its cues were not loaded.",
  );
  expect(screen.queryByText("Synced English")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(saved).toHaveLength(1));
  expect(saved[0]).toEqual({
    language: "hu",
    content: "1\n00:00:01,000 --> 00:00:02,000\nOriginal hu",
    etag: "hu",
  });
});

it.each([
  { scenario: "editing in place", leaveAndReturn: false },
  { scenario: "reopening and editing the same subtitle", leaveAndReturn: true },
])("keeps an edit and its undo after $scenario", async ({ leaveAndReturn }) => {
  const { user, poll, polled } = await openSourceEditor();
  await startSync(user);
  await waitFor(() => expect(polled).toHaveBeenCalled());
  if (leaveAndReturn) {
    await switchLanguage(user, "en", "hu");
    await switchLanguage(user, "hu", "en");
  }
  fireEvent.change(screen.getByDisplayValue("Original en"), {
    target: { value: "Edited English" },
  });
  await act(async () => {
    poll.resolve();
  });
  await screen.findByText(/The sync result was not loaded/);
  expect(screen.getByDisplayValue("Edited English")).toBeInTheDocument();
  expect(screen.queryByText("Synced English")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Undo" }));
  expect(screen.getByDisplayValue("Original en")).toBeInTheDocument();
});

it("discards a pending sync after switching away and back without edits", async () => {
  const { user, poll, polled } = await openSourceEditor();
  await startSync(user);
  await waitFor(() => expect(polled).toHaveBeenCalled());
  await switchLanguage(user, "en", "hu");
  await switchLanguage(user, "hu", "en");
  await act(async () => {
    poll.resolve();
  });
  await screen.findByText(/The sync result was not loaded/);
  expect(screen.getByDisplayValue("Original en")).toBeInTheDocument();
});

it("keeps edits made before choosing an engine result", async () => {
  const { user, poll, polled } = await openSourceEditor(true);
  await startSync(user);
  await waitFor(() => expect(polled).toHaveBeenCalled());
  fireEvent.change(screen.getByDisplayValue("Original en"), {
    target: { value: "Edited English" },
  });
  await act(async () => {
    poll.resolve();
  });
  await user.click(await screen.findByRole("button", { name: "Load Result" }));
  await screen.findByText(/The sync result was not loaded/);
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.getByDisplayValue("Edited English")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Undo" }));
  expect(screen.getByDisplayValue("Original en")).toBeInTheDocument();
});

it("loads a chosen engine result when the submitted cues are still current", async () => {
  const { user, poll } = await openSourceEditor(true);
  await startSync(user);
  poll.resolve();
  await user.click(await screen.findByRole("button", { name: "Load Result" }));
  expect(screen.getByDisplayValue("Synced English")).toBeInTheDocument();
});
