/* eslint-disable camelcase -- backend API contract */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, expect, it, vi } from "vitest";
import { latestWhatsNewVersion } from "@/data/whatsNew";
import { AllProviders } from "@/providers";
import { Router } from "@/Router";
import { fireEvent, rawRender, screen, within } from "@/tests";
import server from "@/tests/mocks/node";
import { setAuthenticated } from "@/utilities/event";
import { WHATS_NEW_SEEN_KEY } from "@/utilities/whatsNew";

vi.mock("@/pages/SubtitleEditor/VideoPreview", () => ({ default: () => null }));
vi.mock("@/pages/SubtitleEditor/WaveformTimeline", () => ({
  default: () => null,
}));

function subtitleContent(text: string) {
  return `1\n00:00:01,000 --> 00:00:02,000\n${text}`;
}

const originalEtag = `${"a".repeat(64)}:gzip`;
const refreshedEtag = `${"b".repeat(64)}:gzip`;

afterEach(() => {
  window.history.replaceState(null, "", "/");
  localStorage.removeItem(WHATS_NEW_SEEN_KEY);
  setAuthenticated(false);
});

async function openEditor(mediaType: "source" | "movie") {
  const user = userEvent.setup();
  const base = mediaType === "source" ? "sources/items" : "movies";
  const contentPath = `/api/${base}/42/subtitles/en/content`;
  const reads = vi.fn();
  const writes: { etag: string | null; body: unknown }[] = [];
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "light", setup_complete: true },
        auth: { type: "form" },
      }),
    ),
    http.get("/api/badges", () => HttpResponse.json({})),
    http.get("/api/system/searches", () => HttpResponse.json([])),
    http.get("/api/editor/subtitles", () =>
      HttpResponse.json({ subtitles: [{ language: "en", format: "srt" }] }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.get(contentPath, () => {
      reads();
      return HttpResponse.json(
        {
          content: subtitleContent(
            writes.length === 0 ? "Original subtitle" : "Changed on disk",
          ),
          format: "srt",
          encoding: "utf-8",
          language: "en",
          mediaTitle: "Local film",
          mediaId: 42,
        },
        {
          headers: {
            ETag: `"${writes.length ? refreshedEtag : originalEtag}"`,
          },
        },
      );
    }),
    http.put(contentPath, async ({ request }) => {
      writes.push({
        etag: request.headers.get("If-Match"),
        body: await request.json(),
      });
      if (writes.length === 1) {
        return HttpResponse.json(
          "Subtitle file has been modified since last read",
          { status: 412 },
        );
      }
      return new HttpResponse(null, {
        status: 204,
        headers: { ETag: '"saved"' },
      });
    }),
  );

  window.history.replaceState(null, "", `/subtitles/edit/${mediaType}/42/en`);
  localStorage.setItem(WHATS_NEW_SEEN_KEY, latestWhatsNewVersion);
  setAuthenticated(true);
  rawRender(
    <AllProviders>
      <Router />
    </AllProviders>,
  );
  const editor = await screen.findByDisplayValue("Original subtitle");
  return { user, editor, reads, writes, contentPath };
}

it("keeps source edits after a rejected conditional save until explicitly reloaded", async () => {
  const { user, editor, reads, writes } = await openEditor("source");
  const status = within(screen.getByRole("status", { name: "Editor status" }));
  const save = screen.getByRole("button", { name: "Save" });
  fireEvent.change(editor, { target: { value: "Local edit" } });
  await user.click(save);

  await screen.findByText("Subtitle changed on disk");
  expect(writes).toEqual([
    {
      etag: originalEtag,
      body: { content: subtitleContent("Local edit"), encoding: "utf-8" },
    },
  ]);
  expect(save).toBeDisabled();
  expect(screen.getByDisplayValue("Local edit")).toBeInTheDocument();
  expect(status.getByText("Unsaved")).toBeInTheDocument();
  expect(reads).toHaveBeenCalledTimes(1);

  await user.click(
    screen.getByRole("button", { name: "Discard edits and reload" }),
  );
  const reloadedEditor = await screen.findByDisplayValue("Changed on disk");
  expect(reads).toHaveBeenCalledTimes(2);
  expect(writes).toHaveLength(1);
  expect(
    screen.queryByText("Subtitle changed on disk"),
  ).not.toBeInTheDocument();
  expect(status.queryByText("Unsaved")).not.toBeInTheDocument();

  fireEvent.change(reloadedEditor, { target: { value: "Edit after reload" } });
  await user.click(save);
  await screen.findByText("Saved");
  expect(writes).toHaveLength(2);
  expect(writes[1]).toEqual({
    etag: refreshedEtag,
    body: { content: subtitleContent("Edit after reload"), encoding: "utf-8" },
  });
});

it("keeps the conflict and unsaved edits when reloading fails", async () => {
  const { user, editor, writes, contentPath } = await openEditor("source");
  fireEvent.change(editor, { target: { value: "Local edit" } });
  await user.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Subtitle changed on disk");

  server.use(
    http.get(contentPath, () =>
      HttpResponse.json("The subtitle is unavailable", { status: 404 }),
    ),
  );
  await user.click(
    screen.getByRole("button", { name: "Discard edits and reload" }),
  );

  await screen.findByText("Could not reload the subtitle");
  expect(screen.getByText("Subtitle changed on disk")).toBeInTheDocument();
  expect(screen.getByDisplayValue("Local edit")).toBeInTheDocument();
  expect(
    within(screen.getByRole("status", { name: "Editor status" })).getByText(
      "Unsaved",
    ),
  ).toBeInTheDocument();
  expect(writes).toHaveLength(1);
});

it("keeps the legacy movie editor's overwrite retry on 412", async () => {
  const { user, editor, writes } = await openEditor("movie");
  fireEvent.change(editor, { target: { value: "Local edit" } });
  await user.click(screen.getByRole("button", { name: "Save" }));

  await screen.findByText("Saved (overwrite)");
  const body = { content: subtitleContent("Local edit"), encoding: "utf-8" };
  expect(writes).toEqual([
    { etag: originalEtag, body },
    { etag: null, body },
  ]);
  expect(
    screen.queryByText("Subtitle changed on disk"),
  ).not.toBeInTheDocument();
});
