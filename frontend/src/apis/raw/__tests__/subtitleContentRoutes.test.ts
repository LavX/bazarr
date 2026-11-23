/* eslint-disable camelcase -- backend API contract */

import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import api from "@/apis/raw";
import server from "@/tests/mocks/node";

const mediaCases = [
  { type: "source", route: "sources/items", owner: undefined },
  { type: "movie", route: "movies", owner: 3 },
  { type: "episode", route: "episodes", owner: 4 },
  { type: "sports", route: "sports/events", owner: 5 },
];

const content = {
  content: "1\n00:00:01,000 --> 00:00:02,000\nHello\n",
  encoding: "utf-8",
  format: "srt",
  language: "en:hi",
  size: 38,
  lastModified: 1700000000,
  mediaTitle: "Selected item",
  mediaId: 42,
};

it.each([
  { type: "movie", route: "/api/movies/42/subtitles", owner: 3 },
  { type: "episode", route: "/api/episodes/42/subtitles", owner: 4 },
  { type: "sports", route: "/api/sports/events/42/subtitles/create", owner: 5 },
])("preserves $type subtitle creation", async (media) => {
  let requestUrl: URL | undefined;
  let body: unknown;
  const created = { path: "/media/selected.en.srt", language: "en" };
  server.use(
    http.post("/api/*", async ({ request }) => {
      requestUrl = new URL(request.url);
      body = await request.json();
      return HttpResponse.json(created);
    }),
  );

  const result = await api.subtitles.createSubtitle(
    media.type,
    42,
    "New subtitle",
    "en",
    "srt",
    false,
    false,
    media.owner,
    true,
  );

  expect(requestUrl?.pathname).toBe(media.route);
  expect(Object.fromEntries(requestUrl!.searchParams)).toEqual({
    arr_instance_id: String(media.owner),
  });
  expect(body).toEqual({
    content: "New subtitle",
    language: "en",
    format: "srt",
    forced: false,
    hi: false,
    overwrite: true,
  });
  expect(result).toEqual(created);
});

describe.each(mediaCases)("subtitle editor client for $type", (media) => {
  it("opens the selected subtitle and returns its content and ETag", async () => {
    let requestUrl: URL | undefined;
    server.use(
      http.get("/api/*", ({ request }) => {
        requestUrl = new URL(request.url);
        return HttpResponse.json(content, { headers: { ETag: '"opened"' } });
      }),
    );

    const result = await api.subtitles.getContent(
      media.type,
      42,
      "en:hi",
      media.owner,
    );

    expect(requestUrl?.pathname).toBe(
      `/api/${media.route}/42/subtitles/en%3Ahi/content`,
    );
    expect(Object.fromEntries(requestUrl!.searchParams)).toEqual(
      media.owner === undefined ? {} : { arr_instance_id: String(media.owner) },
    );
    expect(result).toEqual({ data: content, etag: '"opened"' });
  });

  it("saves with the selected subtitle's ETag and returns the new ETag", async () => {
    let requestUrl: URL | undefined;
    let ifMatch: string | null = null;
    let body: unknown;
    server.use(
      http.put("/api/*", async ({ request }) => {
        requestUrl = new URL(request.url);
        ifMatch = request.headers.get("If-Match");
        body = await request.json();
        return new HttpResponse(null, {
          status: 204,
          headers: { ETag: '"saved"' },
        });
      }),
    );

    const result = await api.subtitles.saveContent(
      media.type,
      42,
      "en:hi",
      "Edited subtitle",
      "utf-8",
      '"opened"',
      media.owner,
    );

    expect(requestUrl?.pathname).toBe(
      `/api/${media.route}/42/subtitles/en%3Ahi/content`,
    );
    expect(Object.fromEntries(requestUrl!.searchParams)).toEqual(
      media.owner === undefined ? {} : { arr_instance_id: String(media.owner) },
    );
    expect(ifMatch).toBe('"opened"');
    expect(body).toEqual({ content: "Edited subtitle", encoding: "utf-8" });
    expect(result).toEqual({ etag: '"saved"' });
  });

  it("reads the selected subtitle's sync status without changing its shape", async () => {
    let requestUrl: URL | undefined;
    const status = {
      synced: true,
      confirmed: true,
      editedAfterSync: false,
      lastModified: 1700000000,
      lastSyncTimestamp: "2023-11-14T22:13:20Z",
      jobStatus: null,
      jobId: null,
    };
    server.use(
      http.get("/api/*", ({ request }) => {
        requestUrl = new URL(request.url);
        return HttpResponse.json(status);
      }),
    );

    const result = await api.subtitles.getSyncStatus(
      media.type,
      42,
      "en:hi",
      media.owner,
    );

    expect(requestUrl?.pathname).toBe(
      `/api/${media.route}/42/subtitles/en%3Ahi/sync-status`,
    );
    expect(Object.fromEntries(requestUrl!.searchParams)).toEqual(
      media.owner === undefined ? {} : { arr_instance_id: String(media.owner) },
    );
    expect(result).toEqual(status);
  });

  it("promotes a sync output onto the selected subtitle", async () => {
    let requestUrl: URL | undefined;
    let body: unknown;
    const promoted = {
      sourceLanguage: "en:hi:sync-ffsubsync",
      targetLanguage: "en:hi",
      targetPath: "/media/selected.en.hi.srt",
    };
    server.use(
      http.post("/api/*", async ({ request }) => {
        requestUrl = new URL(request.url);
        body = await request.json();
        return HttpResponse.json(promoted);
      }),
    );

    const result = await api.subtitles.promoteSyncOutput(
      media.type,
      42,
      "en:hi",
      "en:hi:sync-ffsubsync",
      media.owner,
    );

    expect(requestUrl?.pathname).toBe(
      `/api/${media.route}/42/subtitles/en%3Ahi/promote`,
    );
    expect(Object.fromEntries(requestUrl!.searchParams)).toEqual(
      media.owner === undefined ? {} : { arr_instance_id: String(media.owner) },
    );
    expect(body).toEqual({ sourceLanguage: "en:hi:sync-ffsubsync" });
    expect(result).toEqual(promoted);
  });
});

it("preserves an Arr embedded track selection when opening content", async () => {
  let requestUrl: URL | undefined;
  server.use(
    http.get("/api/*", ({ request }) => {
      requestUrl = new URL(request.url);
      return HttpResponse.json({
        ...content,
        source: "embedded",
        codec: "ass",
      });
    }),
  );

  const result = await api.subtitles.getContent(
    "episode",
    42,
    "en:hi",
    4,
    "embedded",
    "ass",
  );

  expect(requestUrl?.pathname).toBe(
    "/api/episodes/42/subtitles/en%3Ahi/content",
  );
  expect(Object.fromEntries(requestUrl!.searchParams)).toEqual({
    arr_instance_id: "4",
    source: "embedded",
    codec: "ass",
  });
  expect(result.data).toEqual({ ...content, source: "embedded", codec: "ass" });
});

it("rejects unknown editor media types before sending any request", async () => {
  let requests = 0;
  server.use(
    http.all("/api/*", () => {
      requests += 1;
      return HttpResponse.json(content);
    }),
  );

  await expect(api.subtitles.getContent("unknown", 42, "en")).rejects.toThrow(
    "Unsupported subtitle media type",
  );
  await expect(
    api.subtitles.saveContent("unknown", 42, "en", "Edited", "utf-8"),
  ).rejects.toThrow("Unsupported subtitle media type");
  await expect(
    api.subtitles.getSyncStatus("unknown", 42, "en"),
  ).rejects.toThrow("Unsupported subtitle media type");
  await expect(
    api.subtitles.promoteSyncOutput("unknown", 42, "en", "en:sync-ffsubsync"),
  ).rejects.toThrow("Unsupported subtitle media type");
  expect(requests).toBe(0);
});
it("refuses source subtitle creation before sending an unsupported request", async () => {
  let requests = 0;
  server.use(
    http.post("/api/*", () => {
      requests += 1;
      return HttpResponse.json({
        path: "/media/selected.en.srt",
        language: "en",
      });
    }),
  );

  await expect(
    api.subtitles.createSubtitle(
      "source",
      42,
      "New subtitle",
      "en",
      "srt",
      false,
      false,
    ),
  ).rejects.toThrow("Creating subtitles for source items is not supported");
  expect(requests).toBe(0);
});
