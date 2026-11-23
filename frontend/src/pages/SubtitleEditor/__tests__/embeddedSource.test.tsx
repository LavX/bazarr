/**
 * The editor and the viewer opened on an embedded track (?source=embedded).
 *
 * The content route answers exists: false with the track's text, so the
 * editor opens in create mode already filled in. Save creates the file next
 * to the video through the create route, a 409 opens the overwrite dialog,
 * and a confirmed overwrite replaces exactly the file the route refused.
 * Moving between the track and the file that shares its key loads the other
 * one: the editor never holds one's text under the other's name. The row's
 * codec travels with the source, because two text tracks of one key are two
 * rows.
 */

import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSubtitleContent, useSubtitleSave } from "@/apis/hooks/subtitles";
import queryClient from "@/apis/queries";
import { expireSourceSubtitleDocument } from "@/apis/queries/sourceSubtitleDocument";
import SubtitleViewer from "@/pages/SubtitleEditor";
import EditorPage from "@/pages/SubtitleEditor/EditorPage";
import { buildEditorAutosaveKey } from "@/pages/SubtitleEditor/editorScope";
import { AllProviders } from "@/providers";
import { act, rawRender, renderHook, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

vi.mock("@/pages/SubtitleEditor/VideoPreview", () => ({ default: () => null }));
vi.mock("@/pages/SubtitleEditor/WaveformTimeline", () => ({
  default: () => null,
}));

const TRACK = "1\n00:00:01,000 --> 00:00:02,000\nFrom the container\n";
const FILE = "1\n00:00:01,000 --> 00:00:02,000\nFrom the file on disk\n";

// What the page asks for besides the subtitle itself.
const pageHandlers = [
  http.get("/api/editor/subtitles", () => HttpResponse.json({ subtitles: [] })),
  http.get("/api/system/languages", () => HttpResponse.json([])),
  http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
  http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
];

// The serializer ends the last cue without a trailing newline.
const SAVED = TRACK.trimEnd();

interface Created {
  body: Record<string, unknown>;
  arrInstanceId: string | null;
}

interface Put {
  content: unknown;
  ifMatch: string | null;
}

// A movie with an embedded track, and the file next to the video that shares
// its key once there is one (`conflict` puts one there from the start).
// `listed` is what the editor's list of files on disk names: the editor reads
// it once, and the language label names a listed file as "<key> (HI) [srt]",
// which is how a test sees the editor is on the file and not the track.
// `holdFile()` keeps the file's answers back until the function it returns is
// called, as a slow disk would.
function serve({
  conflict = false,
  listed = [],
}: { conflict?: boolean; listed?: string[] } = {}) {
  const asked: (string | null)[] = [];
  const codecs: (string | null)[] = [];
  const created: Created[] = [];
  const puts: Put[] = [];
  let onDisk: string | null = conflict ? FILE : null;
  let held: Promise<void> | null = null;
  const holdFile = () => {
    const { promise, resolve } = Promise.withResolvers<void>();
    held = promise;
    return resolve;
  };
  server.use(
    http.get(
      "/api/movies/30/subtitles/:language/content",
      async ({ request, params }) => {
        const url = new URL(request.url);
        const source = url.searchParams.get("source");
        codecs.push(url.searchParams.get("codec"));
        const language = String(params.language);
        asked.push(source);
        if (source !== "embedded" && held) {
          await held;
        }
        if (source === "embedded") {
          return HttpResponse.json({
            exists: false,
            source: "embedded",
            content: TRACK,
            encoding: "utf-8",
            format: "srt",
            language,
            size: TRACK.length,
            lastModified: 0,
            codec: "subrip",
            fileName: "Film.en.hi.srt",
            mediaTitle: "Film",
            mediaId: 1,
            arrInstanceId: 2,
          });
        }
        if (onDisk === null) {
          return HttpResponse.json({
            exists: false,
            content: "",
            encoding: "utf-8",
            format: "srt",
            language,
            size: 0,
            lastModified: 0,
            mediaTitle: "Film",
            mediaId: 1,
          });
        }
        return HttpResponse.json(
          {
            content: onDisk,
            encoding: "utf-8",
            format: "srt",
            language,
            size: onDisk.length,
            lastModified: 1,
            mediaTitle: "Film",
            mediaId: 1,
          },
          { headers: { ETag: '"abc"' } },
        );
      },
    ),
    http.post("/api/movies/30/subtitles", async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      created.push({
        body,
        arrInstanceId: new URL(request.url).searchParams.get("arr_instance_id"),
      });
      if (onDisk !== null && body.overwrite !== true) {
        return HttpResponse.json("Subtitle file already exists", {
          status: 409,
        });
      }
      const replaced = onDisk !== null;
      onDisk = String(body.content);
      const modifier = body.forced ? ":forced" : body.hi ? ":hi" : "";
      return HttpResponse.json(
        {
          path: "/movies/Film/Film.en.srt",
          language: `${String(body.language)}${modifier}`,
        },
        { status: replaced ? 200 : 201 },
      );
    }),
    http.put(
      "/api/movies/30/subtitles/:language/content",
      async ({ request }) => {
        const body = (await request.json()) as { content: unknown };
        puts.push({
          content: body.content,
          ifMatch: request.headers.get("If-Match"),
        });
        onDisk = String(body.content);
        return new HttpResponse(null, {
          status: 204,
          headers: { ETag: '"def"' },
        });
      },
    ),
    // Before pageHandlers, whose empty list it replaces.
    http.get("/api/editor/subtitles", () =>
      HttpResponse.json({
        subtitles: listed.map((language) => ({ language, format: "srt" })),
      }),
    ),
    ...pageHandlers,
  );
  return { asked, codecs, created, puts, holdFile };
}

function openAt(path: string) {
  const router = createMemoryRouter(
    [
      {
        path: "/subtitles/edit/:mediaType/:mediaId/:language",
        element: <EditorPage />,
      },
      {
        path: "/subtitles/preview/:mediaType/:mediaId/:language",
        element: <SubtitleViewer />,
      },
    ],
    { initialEntries: [path] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return router;
}

const EDIT_EMBEDDED =
  "/subtitles/edit/movie/30/en%3Ahi?arr_instance_id=2&source=embedded&codec=subrip";
const EDIT_FILE = "/subtitles/edit/movie/30/en%3Ahi?arr_instance_id=2";

// The language label once the editor is on the listed en:hi file.
const onTheFile = () =>
  screen.findByRole("button", { name: /^en:hi \(HI\) \[srt\]/ });

function locationOf(router: ReturnType<typeof openAt>) {
  return `${router.state.location.pathname}${router.state.location.search}`;
}

describe("the editor on an embedded track", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("opens in create mode, filled in with the track's text", async () => {
    const { asked, codecs } = serve();

    openAt(EDIT_EMBEDDED);

    expect(await screen.findByText("FROM VIDEO FILE")).toBeInTheDocument();
    expect(
      (await screen.findAllByText("From the container")).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("NEW")).not.toBeInTheDocument();
    expect(asked).toEqual(["embedded"]);
    // The row's codec travels with the request, not just the source.
    expect(codecs).toEqual(["subrip"]);
  });

  it("saves through the create route and then edits the new file", async () => {
    const user = userEvent.setup();
    const { asked, created, puts, holdFile } = serve({ listed: ["en:hi"] });
    const router = openAt(EDIT_EMBEDDED);
    await screen.findByText("FROM VIDEO FILE");
    const release = holdFile();

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0]).toEqual({
      body: {
        content: SAVED,
        language: "en",
        format: "srt",
        forced: false,
        hi: true,
      },
      arrInstanceId: "2",
    });
    await waitFor(() => expect(locationOf(router)).toBe(EDIT_FILE));
    await waitFor(() => expect(asked).toContain(null));
    // Until the file answers, a Save writes nothing: the editor still holds
    // the track it left.
    await user.click(screen.getByRole("button", { name: "Save" }));
    release();
    await onTheFile();
    expect(screen.queryByText("FROM VIDEO FILE")).not.toBeInTheDocument();
    // The file, read back, is what was saved, and the next Save edits it.
    expect(
      (await screen.findAllByText("From the container")).length,
    ).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(puts).toEqual([{ content: SAVED, ifMatch: "abc" }]),
    );
    expect(created).toHaveLength(1);
  });

  it("asks before replacing a file that is already there", async () => {
    const user = userEvent.setup();
    const { created } = serve({ conflict: true, listed: ["en:hi"] });
    const router = openAt(EDIT_EMBEDDED);
    await screen.findByText("FROM VIDEO FILE");

    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText(/already exists\. Do you want to overwrite it\?/),
    ).toBeInTheDocument();
    expect(created).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Overwrite" }));

    await waitFor(() => expect(created).toHaveLength(2));
    expect(created[1].body).toEqual({
      content: SAVED,
      language: "en",
      format: "srt",
      forced: false,
      hi: true,
      overwrite: true,
    });
    await waitFor(() => expect(locationOf(router)).toBe(EDIT_FILE));
    await onTheFile();
    expect(
      (await screen.findAllByText("From the container")).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("From the file on disk")).not.toBeInTheDocument();
  });

  it("shows the file as saved, not an older copy, after an overwrite", async () => {
    // The file was open a moment ago, so its old text is still cached.
    const user = userEvent.setup();
    const { created } = serve({ conflict: true, listed: ["en:hi"] });
    const router = openAt(EDIT_FILE);
    await onTheFile();
    expect(
      (await screen.findAllByText("From the file on disk")).length,
    ).toBeGreaterThan(0);

    await router.navigate(EDIT_EMBEDDED);
    await screen.findByText("FROM VIDEO FILE");
    expect(
      (await screen.findAllByText("From the container")).length,
    ).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await user.click(await screen.findByRole("button", { name: "Overwrite" }));

    await waitFor(() => expect(created).toHaveLength(2));
    await waitFor(() => expect(locationOf(router)).toBe(EDIT_FILE));
    await onTheFile();
    expect(
      (await screen.findAllByText("From the container")).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("From the file on disk")).not.toBeInTheDocument();
  });

  it("writes nothing when the overwrite is cancelled", async () => {
    const user = userEvent.setup();
    const { created } = serve({ conflict: true });
    openAt(EDIT_EMBEDDED);
    await screen.findByText("FROM VIDEO FILE");

    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText(/already exists/);
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(screen.queryByText(/already exists/)).not.toBeInTheDocument(),
    );
    expect(created).toHaveLength(1);
  });

  it("moves to the key the file was saved under", async () => {
    // A track that is both HI and forced saves as forced, like a download.
    const user = userEvent.setup();
    const { asked, created, holdFile } = serve({ listed: ["en:forced"] });
    const router = openAt(
      "/subtitles/edit/movie/30/en%3Ahi%3Aforced?arr_instance_id=2&source=embedded",
    );
    await screen.findByText("FROM VIDEO FILE");
    const release = holdFile();

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0].body).toMatchObject({ hi: false, forced: true });
    await waitFor(() =>
      expect(locationOf(router)).toBe(
        "/subtitles/edit/movie/30/en%3Aforced?arr_instance_id=2",
      ),
    );
    await waitFor(() => expect(asked).toContain(null));
    await user.click(screen.getByRole("button", { name: "Save" }));
    release();
    await screen.findByRole("button", {
      name: /^en:forced \(FORCED\) \[srt\]/,
    });
    expect(
      (await screen.findAllByText("From the container")).length,
    ).toBeGreaterThan(0);
    expect(created).toHaveLength(1);
  });

  it("switches to the file that shares the track's key, and edits that file", async () => {
    const user = userEvent.setup();
    const { asked, created, puts, holdFile } = serve({
      conflict: true,
      listed: ["en:hi"],
    });
    const router = openAt(EDIT_EMBEDDED);
    await screen.findByText("FROM VIDEO FILE");
    const release = holdFile();

    await user.click(
      await screen.findByRole("button", { name: /\(embedded track\)/ }),
    );
    await user.click(
      await screen.findByRole("menuitem", {
        name: "en:hi (HI) [srt]",
        hidden: true,
      }),
    );

    await waitFor(() => expect(locationOf(router)).toBe(EDIT_FILE));
    await waitFor(() => expect(asked).toContain(null));
    // Until the file answers, a Save writes nothing, least of all the
    // track's text into the file.
    await user.click(screen.getByRole("button", { name: "Save" }));
    release();
    await onTheFile();
    expect(
      (await screen.findAllByText("From the file on disk")).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("From the container")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(puts).toEqual([{ content: FILE.trimEnd(), ifMatch: "abc" }]),
    );
    expect(created).toHaveLength(0);
  });

  it("does not offer the track's draft to the file that shares its key", async () => {
    const user = userEvent.setup();
    serve({ conflict: true, listed: ["en:hi"] });
    const trackKey = buildEditorAutosaveKey(
      "movie",
      "30",
      "en:hi",
      2,
      "embedded",
      "subrip",
    );
    localStorage.setItem(
      trackKey!,
      JSON.stringify({
        content: TRACK,
        format: "srt",
        cueCount: 1,
        timestamp: Date.now(),
      }),
    );
    const router = openAt(EDIT_EMBEDDED);
    await screen.findByText("FROM VIDEO FILE");
    expect(await screen.findByText(/Unsaved draft found/)).toBeInTheDocument();

    await user.click(
      await screen.findByRole("button", { name: /\(embedded track\)/ }),
    );
    await user.click(
      await screen.findByRole("menuitem", {
        name: "en:hi (HI) [srt]",
        hidden: true,
      }),
    );

    await waitFor(() => expect(locationOf(router)).toBe(EDIT_FILE));
    await onTheFile();
    await waitFor(() =>
      expect(screen.queryByText(/Unsaved draft found/)).not.toBeInTheDocument(),
    );
  });

  it("says why an image-based track cannot be opened", async () => {
    server.use(
      http.get("/api/movies/30/subtitles/:language/content", () =>
        HttpResponse.json(
          "This embedded track is image-based (PGS). Extract it to a file, or OCR it to text, first.",
          { status: 415 },
        ),
      ),
      ...pageHandlers,
    );

    openAt(EDIT_EMBEDDED);

    expect(
      await screen.findByText(
        "This embedded track is image-based (PGS). Extract it to a file, or OCR it to text, first.",
      ),
    ).toBeInTheDocument();
  });
});

describe("the viewer on an embedded track", () => {
  it("shows the track's text and says where it came from", async () => {
    const { asked, codecs } = serve();

    openAt(
      "/subtitles/preview/movie/30/en%3Ahi?arr_instance_id=2&source=embedded&codec=subrip",
    );

    expect(await screen.findByText("Embedded track")).toBeInTheDocument();
    // The cue table is virtualised and draws no rows in jsdom; the count
    // is what says the track was parsed.
    expect(screen.getByText("1 cues")).toBeInTheDocument();
    expect(asked).toEqual(["embedded"]);
    expect(codecs).toEqual(["subrip"]);
  });
});

it("loads a replaced source document into the open editor and saves only with its current version", async () => {
  const user = userEvent.setup();
  const put = vi.fn();
  let content = FILE;
  let revision = "old";
  server.use(
    ...pageHandlers,
    http.get("/api/sources/items/30/subtitles/:language/content", () =>
      HttpResponse.json(
        {
          content,
          encoding: "utf-8",
          format: "srt",
          language: "en:hi",
          size: content.length,
          mediaId: 30,
        },
        { headers: { ETag: `"${revision}"` } },
      ),
    ),
    http.put(
      "/api/sources/items/30/subtitles/:language/content",
      async ({ request }) => {
        const received = request.headers.get("If-Match");
        put(received, await request.json());
        return new HttpResponse(null, { status: 412 });
      },
    ),
  );
  openAt("/subtitles/edit/source/30/en%3Ahi");
  expect(
    (await screen.findAllByText("From the file on disk")).length,
  ).toBeGreaterThan(0);

  content = TRACK;
  revision = "replacement";
  await act(() => expireSourceSubtitleDocument(queryClient, 30, "en:hi"));
  expect(
    (await screen.findAllByText("From the container")).length,
  ).toBeGreaterThan(0);
  expect(screen.queryByText("From the file on disk")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Save" }));
  expect(
    await screen.findByText("The source file changed while you were editing."),
  ).toBeVisible();
  expect(put).toHaveBeenCalledExactlyOnceWith(
    "replacement",
    expect.objectContaining({ content: SAVED }),
  );
});

it("keeps saved source cues and their ETag through two successful saves", async () => {
  const user = userEvent.setup();
  const puts: Put[] = [];
  const staleRead = Promise.withResolvers<void>();
  const staleResponse = Promise.withResolvers<void>();
  let reads = 0;
  let content = FILE;
  let revision = "original";
  server.use(
    ...pageHandlers,
    http.get("/api/sources/items/30/subtitles/:language/content", async () => {
      reads += 1;
      const response = HttpResponse.json(
        {
          content,
          encoding: "utf-8",
          format: "srt",
          language: "en:hi",
          size: content.length,
          lastModified: 1,
          mediaId: 30,
        },
        { headers: { ETag: `"${revision}"` } },
      );
      if (reads === 2) {
        await staleRead.promise;
        staleResponse.resolve();
      }
      return response;
    }),
    http.put(
      "/api/sources/items/30/subtitles/:language/content",
      async ({ request }) => {
        const body = (await request.json()) as { content: string };
        const ifMatch = request.headers.get("If-Match");
        if (ifMatch !== revision) {
          return new HttpResponse(null, { status: 412 });
        }
        puts.push({ content: body.content, ifMatch });
        content = body.content;
        revision = `saved-${puts.length}`;
        return new HttpResponse(null, {
          status: 204,
          headers: { ETag: `"${revision}"` },
        });
      },
    ),
  );
  openAt("/subtitles/edit/source/30/en%3Ahi");
  const textarea = await screen.findByDisplayValue("From the file on disk");
  await user.clear(textarea);
  await user.type(textarea, "First source edit");
  const refetch = queryClient.invalidateQueries({
    queryKey: ["subtitles", "content", "source", 30, "en:hi"],
  });
  await waitFor(() => expect(reads).toBe(2));
  await user.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() => {
    expect(puts).toHaveLength(1);
    expect(screen.queryByText("Unsaved")).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("First source edit")).toBeVisible();
    expect(
      queryClient.getQueryData([
        "subtitles",
        "content",
        "source",
        30,
        "en:hi",
        undefined,
        null,
        null,
      ]),
    ).toMatchObject({
      data: { content: "1\n00:00:01,000 --> 00:00:02,000\nFirst source edit" },
      etag: '"saved-1"',
    });
    expect(
      queryClient.getQueryState([
        "subtitles",
        "content",
        "source",
        30,
        "en:hi",
        undefined,
        null,
        null,
      ])?.fetchStatus,
    ).toBe("idle");
  });

  await act(async () => {
    staleRead.resolve();
    await Promise.all([staleResponse.promise, refetch]);
  });

  await user.clear(screen.getByDisplayValue("First source edit"));
  await user.type(textarea, "Second source edit");
  await user.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() => {
    expect(puts).toEqual([
      {
        content: "1\n00:00:01,000 --> 00:00:02,000\nFirst source edit",
        ifMatch: "original",
      },
      {
        content: "1\n00:00:01,000 --> 00:00:02,000\nSecond source edit",
        ifMatch: "saved-1",
      },
    ]);
    expect(screen.queryByText("Unsaved")).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("Second source edit")).toBeVisible();
    expect(
      queryClient.getQueryData([
        "subtitles",
        "content",
        "source",
        30,
        "en:hi",
        undefined,
        null,
        null,
      ]),
    ).toMatchObject({
      data: { content: "1\n00:00:01,000 --> 00:00:02,000\nSecond source edit" },
      etag: '"saved-2"',
    });
  });
});

it("retains a dirty source draft when a background read or save fails", async () => {
  const user = userEvent.setup();
  const puts: Put[] = [];
  let reads = 0;
  server.use(
    ...pageHandlers,
    http.get("/api/sources/items/30/subtitles/:language/content", () => {
      reads += 1;
      if (reads > 1) {
        return HttpResponse.json("Source temporarily unavailable", {
          status: 404,
        });
      }
      return HttpResponse.json(
        {
          content: FILE,
          encoding: "utf-8",
          format: "srt",
          language: "en:hi",
          size: FILE.length,
          lastModified: 1,
          mediaId: 30,
        },
        { headers: { ETag: '"original"' } },
      );
    }),
    http.put(
      "/api/sources/items/30/subtitles/:language/content",
      async ({ request }) => {
        const body = (await request.json()) as { content: string };
        puts.push({
          content: body.content,
          ifMatch: request.headers.get("If-Match"),
        });
        return new HttpResponse(null, { status: 412 });
      },
    ),
  );
  openAt("/subtitles/edit/source/30/en%3Ahi");
  const textarea = await screen.findByDisplayValue("From the file on disk");
  await user.clear(textarea);
  await user.type(textarea, "Keep my unsaved draft");

  await act(() =>
    queryClient.invalidateQueries({
      queryKey: ["subtitles", "content", "source", 30, "en:hi"],
    }),
  );

  expect(reads).toBe(2);
  expect(screen.getByDisplayValue("Keep my unsaved draft")).toBeVisible();
  expect(screen.getByText("Unsaved")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Save" }));
  expect(
    await screen.findByText("The source file changed while you were editing."),
  ).toBeVisible();
  expect(screen.getByDisplayValue("Keep my unsaved draft")).toBeVisible();
  expect(screen.getByText("Unsaved")).toBeVisible();
  expect(puts).toEqual([
    {
      content: "1\n00:00:01,000 --> 00:00:02,000\nKeep my unsaved draft",
      ifMatch: "original",
    },
  ]);
  expect(
    queryClient.getQueryData([
      "subtitles",
      "content",
      "source",
      30,
      "en:hi",
      undefined,
      null,
      null,
    ]),
  ).toMatchObject({ data: { content: FILE }, etag: '"original"' });
});

it.each(["completed", "pending"] as const)(
  "preserves an external revision read after PUT writes but before its response (%s GET)",
  async (readState) => {
    const putStarted = Promise.withResolvers<void>();
    const releasePut = Promise.withResolvers<void>();
    const releaseRead = Promise.withResolvers<void>();
    let reads = 0;
    let content = FILE;
    let revision = "original";
    server.use(
      ...pageHandlers,
      http.get(
        "/api/sources/items/30/subtitles/:language/content",
        async () => {
          reads += 1;
          const response = HttpResponse.json(
            {
              content,
              encoding: "utf-8",
              format: "srt",
              language: "en:hi",
              size: content.length,
              lastModified: 1,
              mediaId: 30,
            },
            { headers: { ETag: `"${revision}"` } },
          );
          if (readState === "pending" && reads === 2) await releaseRead.promise;
          return response;
        },
      ),
      http.put(
        "/api/sources/items/30/subtitles/:language/content",
        async ({ request }) => {
          const body = (await request.json()) as { content: string };
          content = body.content;
          revision = "saved";
          putStarted.resolve();
          await releasePut.promise;
          return new HttpResponse(null, {
            status: 204,
            headers: { ETag: '"saved"' },
          });
        },
      ),
    );
    const { result } = renderHook(
      () => ({
        document: useSubtitleContent("source", 30, "en:hi"),
        save: useSubtitleSave(),
      }),
      { wrapper: AllProviders },
    );
    await waitFor(() =>
      expect(result.current.document.data?.etag).toBe('"original"'),
    );
    const saving = result.current.save.mutateAsync({
      mediaType: "source",
      mediaId: 30,
      language: "en:hi",
      content: "1\n00:00:01,000 --> 00:00:02,000\nMy saved edit",
      encoding: "utf-8",
      etag: "original",
    });
    await putStarted.promise;
    content = TRACK;
    revision = "external";
    const read = result.current.document.refetch();
    if (readState === "pending") {
      await waitFor(() => expect(reads).toBe(2));
    } else {
      await act(() => read);
      await waitFor(() =>
        expect(result.current.document.data).toMatchObject({
          data: { content: TRACK },
          etag: '"external"',
        }),
      );
    }

    await act(async () => {
      releasePut.resolve();
      await saving;
    });
    await waitFor(() =>
      expect(result.current.document.data).toMatchObject({
        data: { content: TRACK },
        etag: '"external"',
      }),
    );
    await act(async () => {
      releaseRead.resolve();
      await read;
    });

    expect(
      queryClient.getQueryData([
        "subtitles",
        "content",
        "source",
        30,
        "en:hi",
        undefined,
        null,
        null,
      ]),
    ).toMatchObject({ data: { content: TRACK }, etag: '"external"' });
  },
);
