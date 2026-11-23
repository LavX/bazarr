/* eslint-disable camelcase -- backend API contract */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, expect, it, vi } from "vitest";
import { latestWhatsNewVersion } from "@/data/whatsNew";
import * as editorJobs from "@/pages/SubtitleEditor/editorJobs";
import { buildEditorAutosaveKey } from "@/pages/SubtitleEditor/editorScope";
import { AllProviders } from "@/providers";
import { Router } from "@/Router";
import { act, fireEvent, rawRender, screen, waitFor, within } from "@/tests";
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

function assContent(text: string, refreshed = false) {
  return `[Script Info]
ScriptType: v4.00+
Title: ${refreshed ? "Fresh title" : "Original title"}

[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Default,${refreshed ? "Verdana,32" : "Arial,20"}
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,${refreshed ? "0:00:05.00,0:00:08.00" : "0:00:01.00,0:00:02.00"},Default,,0,0,0,,${text}`;
}

function subtitleResponse(content: string, format = "srt", encoding = "utf-8") {
  return HttpResponse.json(
    {
      content,
      format,
      encoding,
      language: "en",
      mediaTitle: "Local film",
      mediaId: 42,
    },
    { headers: { ETag: `"${refreshedEtag}"` } },
  );
}

function documentControlsAreInert(element: HTMLElement) {
  // jsdom does not implement inert's native input blocking or expose a role.
  return element.closest("[inert]") !== null;
}

const originalEtag = `${"a".repeat(64)}:gzip`;
const refreshedEtag = `${"b".repeat(64)}:gzip`;
const sourceDraftKey = buildEditorAutosaveKey("source", "42", "en")!;

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
  localStorage.removeItem(WHATS_NEW_SEEN_KEY);
  localStorage.removeItem(sourceDraftKey);
  setAuthenticated(false);
});

async function openEditor(
  mediaType: "source" | "movie",
  format: "srt" | "ass" = "srt",
  extraCues = "",
  acceptFirstSave = false,
) {
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
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({ data: { available: false } }),
    ),
    http.get("/api/system/searches", () => HttpResponse.json([])),
    http.get("/api/editor/subtitles", () =>
      HttpResponse.json({
        subtitles: [
          { language: "en", format: "srt" },
          { language: "hu", format: "srt" },
        ],
      }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.get(contentPath, () => {
      reads();
      return HttpResponse.json(
        {
          content:
            (format === "ass" ? assContent : subtitleContent)(
              writes.length === 0 ? "Original subtitle" : "Changed on disk",
            ) + extraCues,
          format,
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
      if (writes.length === 1 && !acceptFirstSave) {
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
  const { container } = rawRender(
    <AllProviders>
      <Router />
    </AllProviders>,
  );
  const editor = await screen.findByDisplayValue(/^Original subtitle(?:$|\s)/);
  return { user, editor, reads, writes, contentPath, container };
}

it("keeps source edits after a rejected conditional save until explicitly reloaded", async () => {
  const { user, editor, reads, writes, contentPath } =
    await openEditor("source");
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
  await waitFor(
    () => expect(localStorage.getItem(sourceDraftKey)).toContain("Local edit"),
    { timeout: 3500 },
  );

  let finishReload!: (response: Response) => void;
  const reloadResponse = new Promise<Response>((resolve) => {
    finishReload = resolve;
  });
  server.use(
    http.get(contentPath, async () => {
      reads();
      return await reloadResponse;
    }),
  );
  const reload = screen.getByRole("button", {
    name: "Discard edits and reload",
  });
  await user.click(reload);
  expect(reload).toBeDisabled();
  expect(screen.getByDisplayValue("Local edit")).toBeInTheDocument();
  expect(documentControlsAreInert(editor)).toBe(true);
  fireEvent.keyDown(editor, {
    key: "ArrowRight",
    ctrlKey: true,
    shiftKey: true,
  });
  expect(screen.getByTitle("Start time")).toHaveValue("00:00:01.000");
  fireEvent.keyDown(editor, { key: "s", ctrlKey: true });
  expect(writes).toHaveLength(1);
  finishReload(subtitleResponse(subtitleContent("Changed on disk")));
  const reloadedEditor = await screen.findByDisplayValue("Changed on disk");
  expect(documentControlsAreInert(reloadedEditor)).toBe(false);
  expect(reads).toHaveBeenCalledTimes(2);
  expect(writes).toHaveLength(1);
  expect(
    screen.queryByText("Subtitle changed on disk"),
  ).not.toBeInTheDocument();
  expect(status.queryByText("Unsaved")).not.toBeInTheDocument();
  expect(localStorage.getItem(sourceDraftKey)).toBeNull();

  fireEvent.change(reloadedEditor, { target: { value: "Edit after reload" } });
  await user.click(save);
  await screen.findByText("Saved");
  expect(writes).toHaveLength(2);
  expect(writes[1]).toEqual({
    etag: refreshedEtag,
    body: { content: subtitleContent("Edit after reload"), encoding: "utf-8" },
  });
});

it.each([
  {
    reason: "a read failure",
    respond: () =>
      HttpResponse.json("The subtitle is unavailable", { status: 404 }),
  },
  {
    reason: "invalid SRT timing",
    respond: () => subtitleResponse("1\nbad --> bad\nUnreadable cue"),
  },
  {
    reason: "backwards SRT timing",
    respond: () =>
      subtitleResponse("1\n00:00:02,000 --> 00:00:01,000\nBackwards cue"),
  },
  {
    reason: "a WebVTT file with one valid and one malformed cue",
    respond: () =>
      subtitleResponse(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nValid cue\n\nbad --> bad",
        "vtt",
      ),
  },
  {
    reason: "a MicroDVD file with one valid and one malformed cue",
    respond: () =>
      subtitleResponse("{0}{24}Valid cue\n{bad}{48}Broken cue", "sub"),
  },
  {
    reason: "invalid ASS timing",
    respond: () =>
      subtitleResponse(
        assContent("Unreadable cue").replace("0:00:01.00", "bad"),
        "ass",
      ),
  },
  {
    reason: "invalid WebVTT timing",
    respond: () =>
      subtitleResponse("WEBVTT\n\nbad --> bad\nUnreadable cue", "vtt"),
  },
  {
    reason: "an invalid SAMI marker",
    respond: () =>
      subtitleResponse(
        "<SAMI><BODY><SYNC Start=bad>Unreadable cue</BODY></SAMI>",
        "smi",
      ),
  },
])("keeps the conflict and saved draft after $reason", async ({ respond }) => {
  const { user, editor, writes, contentPath } = await openEditor("source");
  fireEvent.change(editor, { target: { value: "Local edit" } });
  await user.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Subtitle changed on disk");
  await waitFor(
    () => expect(localStorage.getItem(sourceDraftKey)).toContain("Local edit"),
    { timeout: 3500 },
  );

  server.use(http.get(contentPath, respond));
  await user.click(
    screen.getByRole("button", { name: "Discard edits and reload" }),
  );

  await screen.findByText("Could not reload the subtitle");
  expect(screen.getByText("Subtitle changed on disk")).toBeInTheDocument();
  expect(screen.getByDisplayValue("Local edit")).toBeInTheDocument();
  expect(localStorage.getItem(sourceDraftKey)).toContain("Local edit");
  expect(
    within(screen.getByRole("status", { name: "Editor status" })).getByText(
      "Unsaved",
    ),
  ).toBeInTheDocument();
  expect(writes).toHaveLength(1);
});

it("accepts a valid SRT cue starting at zero during reload", async () => {
  const { user, editor, contentPath } = await openEditor("source");
  fireEvent.change(editor, { target: { value: "Local edit" } });
  await user.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Subtitle changed on disk");

  server.use(
    http.get(contentPath, () =>
      subtitleResponse("1\n00:00:00,000 --> 00:00:01,000\nStarts at zero"),
    ),
  );
  await user.click(
    screen.getByRole("button", { name: "Discard edits and reload" }),
  );

  expect(await screen.findByDisplayValue("Starts at zero")).toBeInTheDocument();
  expect(screen.getByTitle("Start time")).toHaveValue("00:00:00.000");
  expect(
    screen.queryByText("Subtitle changed on disk"),
  ).not.toBeInTheDocument();
});

it("saves ordinary ASS edits when the opened dialogue contains a bare CR", async () => {
  const { user, editor, writes } = await openEditor(
    "source",
    "ass",
    "\rLegacy text",
    true,
  );
  expect(editor).toHaveValue("Original subtitle\nLegacy text");
  fireEvent.change(editor, { target: { value: "Edited ASS dialogue" } });
  const start = screen.getByTitle("Start time");
  act(() => start.focus());
  fireEvent.change(start, { target: { value: "00:00:01.500" } });
  await user.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Saved");

  expect(writes).toEqual([
    {
      etag: originalEtag,
      body: {
        content: assContent("Edited ASS dialogue").replace(
          "Dialogue: 0,0:00:01.00",
          "Dialogue: 0,0:00:01.50",
        ),
        encoding: "utf-8",
      },
    },
  ]);
});

it("adopts reloaded ASS text, timing, styles and encoding together before saving", async () => {
  const { user, editor, writes, contentPath } = await openEditor(
    "source",
    "ass",
  );
  fireEvent.change(editor, { target: { value: "Local ASS draft" } });
  await user.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Subtitle changed on disk");

  server.use(
    http.get(contentPath, () =>
      subtitleResponse(assContent("Fresh ASS text", true), "ass", "utf-16"),
    ),
  );
  await user.click(
    screen.getByRole("button", { name: "Discard edits and reload" }),
  );
  const reloadedEditor = await screen.findByDisplayValue("Fresh ASS text");
  expect(screen.getByTitle("Start time")).toHaveValue("00:00:05.000");
  expect(screen.getByTitle("End time")).toHaveValue("00:00:08.000");
  const status = within(screen.getByRole("status", { name: "Editor status" }));
  expect(status.getByText("ASS")).toBeInTheDocument();
  expect(status.getByText("utf-16")).toBeInTheDocument();
  expect(status.queryByText("Unsaved")).not.toBeInTheDocument();

  fireEvent.change(reloadedEditor, { target: { value: "Fresh ASS edit" } });
  await user.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Saved");
  expect(writes[1]).toEqual({
    etag: refreshedEtag,
    body: { content: assContent("Fresh ASS edit", true), encoding: "utf-16" },
  });
});

it("retains ASS draft timing and metadata after an unsuccessful reload", async () => {
  const { user, editor, writes, contentPath } = await openEditor(
    "source",
    "ass",
  );
  fireEvent.change(editor, { target: { value: "Local ASS draft" } });
  await user.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Subtitle changed on disk");
  const start = screen.getByTitle("Start time");
  act(() => start.focus());
  fireEvent.change(start, { target: { value: "00:00:00.500" } });
  expect(start).toHaveFocus();

  server.use(
    http.get(contentPath, () =>
      subtitleResponse("No usable subtitle cues", "srt", "utf-16"),
    ),
  );
  // Activate without the browser first moving focus to the button.
  fireEvent.click(
    screen.getByRole("button", { name: "Discard edits and reload" }),
  );
  await screen.findByText("Could not reload the subtitle");
  expect(screen.getByDisplayValue("Local ASS draft")).toBeInTheDocument();
  expect(screen.getByTitle("Start time")).toHaveValue("00:00:00.500");
  expect(screen.getByTitle("End time")).toHaveValue("00:00:02.000");
  expect(documentControlsAreInert(editor)).toBe(false);
  const status = within(screen.getByRole("status", { name: "Editor status" }));
  expect(status.getByText("ASS")).toBeInTheDocument();
  expect(status.getByText("utf-8")).toBeInTheDocument();
  expect(status.getByText("Unsaved")).toBeInTheDocument();
  expect(writes).toHaveLength(1);
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  await waitFor(
    () =>
      expect(localStorage.getItem(sourceDraftKey)).toContain(
        "Dialogue: 0,0:00:00.50,0:00:02.00,Default,,0,0,0,,Local ASS draft",
      ),
    { timeout: 3500 },
  );
});

it.each([
  ["srt", ""],
  ["vtt", "WEBVTT\n\n\n\nNOTE Subtitle is intentionally empty"],
  ["ass", "[Script Info]\nScriptType: v4.00+\n\n[Events]"],
  ["smi", "<SAMI>\n<BODY>\n</BODY>\n</SAMI>"],
  ["smi", "<SAMI><BODY><SYNC Start=0><P Class=ENCC>&nbsp;</BODY></SAMI>"],
  ["smi", "<SAMI><BODY><!-- <SYNC Start=example> --></BODY></SAMI>"],
])("can reload a valid empty %s document", async (format, content) => {
  const { user, editor, contentPath } = await openEditor("source");
  fireEvent.change(editor, { target: { value: "Discarded draft" } });
  await user.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Subtitle changed on disk");

  server.use(http.get(contentPath, () => subtitleResponse(content, format)));
  await user.click(
    screen.getByRole("button", { name: "Discard edits and reload" }),
  );
  await waitFor(() =>
    expect(
      screen.queryByText("Subtitle changed on disk"),
    ).not.toBeInTheDocument(),
  );
  expect(screen.queryByDisplayValue("Discarded draft")).not.toBeInTheDocument();
  expect(
    within(screen.getByRole("status", { name: "Editor status" })).queryByText(
      "Unsaved",
    ),
  ).not.toBeInTheDocument();
});

it("keeps an already dispatched file import when conflict reload fails", async () => {
  const { user, editor, contentPath, container } = await openEditor("source");
  fireEvent.change(editor, { target: { value: "Local edit" } });
  await user.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Subtitle changed on disk");

  let finishReading!: (content: string) => void;
  const readFile = vi
    .spyOn(FileReader.prototype, "readAsText")
    .mockImplementation(function (this: FileReader) {
      finishReading = (content) => {
        Object.defineProperty(this, "result", { value: content });
        this.dispatchEvent(new ProgressEvent("load"));
      };
    });
  try {
    await user.click(screen.getByRole("button", { name: "Upload" }));
    // The toolbar opens a hidden file input without an accessible role.
    const input =
      // eslint-disable-next-line testing-library/no-container, testing-library/no-node-access
      container.querySelector<HTMLInputElement>('input[type="file"]')!;
    const imported = subtitleContent("Imported while waiting");
    await user.upload(input, new File([imported], "draft.srt"));
    expect(readFile).toHaveBeenCalledOnce();

    const { promise: reloadResponse, resolve: finishReload } =
      Promise.withResolvers<Response>();
    server.use(http.get(contentPath, () => reloadResponse));
    await user.click(
      screen.getByRole("button", { name: "Discard edits and reload" }),
    );
    expect(documentControlsAreInert(editor)).toBe(true);

    act(() => finishReading(imported));
    finishReload(
      HttpResponse.json("The subtitle is unavailable", { status: 404 }),
    );

    await screen.findByText("Could not reload the subtitle");
    expect(
      screen.getByDisplayValue("Imported while waiting"),
    ).toBeInTheDocument();
    expect(screen.getByText("Subtitle changed on disk")).toBeInTheDocument();
  } finally {
    readFile.mockRestore();
  }
});

it.each([
  { heldStage: "queue", reloadSucceeds: false },
  { heldStage: "result", reloadSucceeds: false },
  { heldStage: "result", reloadSucceeds: true },
])(
  "keeps translation tied to its cue with $heldStage held and reload success $reloadSucceeds",
  async ({ heldStage, reloadSucceeds }) => {
    const sibling = "\n\n2\n00:00:03,000 --> 00:00:04,000\nSibling cue";
    const { user, editor, contentPath } = await openEditor(
      "source",
      "srt",
      sibling,
    );
    fireEvent.change(editor, { target: { value: "Local edit" } });
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Subtitle changed on disk");

    const { promise: translationGate, resolve: releaseTranslation } =
      Promise.withResolvers<void>();
    const queueRequested = vi.fn();
    const resultRequested = vi.fn();
    const readTranslation = vi.spyOn(editorJobs, "readEditorTranslation");
    server.use(
      http.get("/api/sources/items/42/subtitles/hu/content", () =>
        subtitleResponse(subtitleContent("Hungarian reference")),
      ),
      http.post("/api/translator/editor", async () => {
        queueRequested();
        if (heldStage === "queue") await translationGate;
        return HttpResponse.json({ jobId: 7, session: "editor-reload-test" });
      }),
      http.get("/api/translator/editor", async () => {
        resultRequested();
        if (heldStage === "result") await translationGate;
        return HttpResponse.json({
          jobId: 7,
          status: "completed",
          lines: [{ position: 0, line: "Translated while waiting" }],
        });
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "Reference Subtitle" }),
    );
    const referenceSelect = screen
      .getAllByRole("combobox")
      .find((select) =>
        within(select).queryByRole("option", { name: "Select language..." }),
      )!;
    await user.selectOptions(referenceSelect, "hu");
    await screen.findByText("Loaded 1 reference cues (hu)");
    await user.click(screen.getByTitle("Translate this cue"));
    await waitFor(() =>
      expect(
        heldStage === "queue" ? queueRequested : resultRequested,
      ).toHaveBeenCalled(),
    );

    const { promise: reloadResponse, resolve: finishReload } =
      Promise.withResolvers<Response>();
    server.use(http.get(contentPath, () => reloadResponse));
    const end = screen.getByTitle("End time");
    act(() => end.focus());
    fireEvent.change(end, { target: { value: "00:00:06.000" } });
    const start = screen.getByTitle("Start time");
    act(() => start.focus());
    fireEvent.change(start, { target: { value: "00:00:05.000" } });
    expect(start).toHaveFocus();

    fireEvent.click(
      screen.getByRole("button", { name: "Discard edits and reload" }),
    );
    expect(documentControlsAreInert(editor)).toBe(true);

    if (reloadSucceeds) {
      finishReload(subtitleResponse(subtitleContent("Replacement cue")));
      await screen.findByDisplayValue("Replacement cue");
      expect(readTranslation).toHaveBeenCalledWith(7);
      await act(async () => {
        releaseTranslation();
        await readTranslation.mock.results[0].value;
      });
      expect(screen.getByDisplayValue("Replacement cue")).toBeInTheDocument();
      expect(
        screen.queryByText("Translated while waiting"),
      ).not.toBeInTheDocument();
      return;
    }

    releaseTranslation();
    await waitFor(() => expect(readTranslation).toHaveBeenCalledWith(7));
    await act(async () => {
      await readTranslation.mock.results[0].value;
    });
    finishReload(
      HttpResponse.json("The subtitle is unavailable", { status: 404 }),
    );
    await screen.findByText("Could not reload the subtitle");
    const expectedDraft = [
      "1\n00:00:03,000 --> 00:00:04,000\nSibling cue",
      "2\n00:00:05,000 --> 00:00:06,000\nTranslated while waiting",
    ].join("\n\n");
    await waitFor(
      () =>
        expect(
          JSON.parse(localStorage.getItem(sourceDraftKey) ?? "{}").content,
        ).toBe(expectedDraft),
      { timeout: 3500 },
    );
    expect(screen.getByText("Subtitle changed on disk")).toBeInTheDocument();
  },
);

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
