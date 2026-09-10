/* eslint-disable camelcase */

import { http, HttpResponse } from "msw";
import server from "@/tests/mocks/node";
import api from "./index";

const id = "2af88684-d7d2-4534-82bb-a6d839cc5c10";
const item = `/api/system/media-server-instances/${id}`;

const input = {
  url: "https://silo.example/prefix",
  apikey: "0007",
  verify_ssl: false,
};

it.each(["emby", "silo"] as const)(
  "sends the current %s connection as JSON without credentials in the URL",
  async (kind) => {
    let received: unknown;
    let requestUrl = "";
    server.use(
      http.post(`/api/${kind}/test-connection`, async ({ request }) => {
        received = await request.json();
        requestUrl = request.url;
        return HttpResponse.json({
          success: true,
          server_name: "Media server",
        });
      }),
    );
    expect(await api.mediaServers.testConnection(kind, input)).toEqual({
      success: true,
      server_name: "Media server",
    });
    expect(received).toEqual({
      url: "https://silo.example/prefix",
      apikey: "0007",
      verify_ssl: false,
    });
    expect(new URL(requestUrl).search).toBe("");
  },
);

it("preserves Silo native library types, paths and string IDs", async () => {
  server.use(
    http.post("/api/silo/libraries", () =>
      HttpResponse.json({
        data: [
          { id: "0007", name: "TV", type: "series", paths: ["/media/tv"] },
          {
            id: "0010",
            name: "Films",
            type: "movies",
            paths: ["/media/films"],
          },
        ],
        error_code: null,
      }),
    ),
  );
  expect(await api.mediaServers.libraries(input)).toEqual([
    { id: "0007", name: "TV", type: "series", paths: ["/media/tv"] },
    { id: "0010", name: "Films", type: "movies", paths: ["/media/films"] },
  ]);
});

it("distinguishes an empty library response from a failed probe", async () => {
  server.use(
    http.post("/api/silo/libraries", () =>
      HttpResponse.json({ data: [], error_code: null }),
    ),
  );
  expect(await api.mediaServers.libraries(input)).toEqual([]);
  server.use(
    http.post("/api/silo/libraries", () =>
      HttpResponse.json({ data: [], error_code: "connection_error" }),
    ),
  );
  await expect(api.mediaServers.libraries(input)).rejects.toThrow();
});

it("does not treat missing or malformed status endpoints as idle success", async () => {
  server.use(
    http.get(`${item}/status`, () => HttpResponse.json({}, { status: 404 })),
  );
  await expect(api.mediaServers.status(id)).rejects.toThrow();
  server.use(
    http.get(`${item}/status`, () => HttpResponse.json({ success: true })),
  );
  await expect(api.mediaServers.status(id)).rejects.toThrow();
});

it.each(["emby", "silo"] as const)(
  "reads %s refresh state and queues a retry without connection fields",
  async () => {
    let body: unknown;
    server.use(
      http.get(`${item}/status`, () =>
        HttpResponse.json({
          pending: 2,
          state: "unconfirmed",
          error_code: "timeout",
        }),
      ),
      http.post(`${item}/retry-pending`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ queued: 2 });
      }),
    );
    expect(await api.mediaServers.status(id)).toEqual({
      pending: 2,
      state: "unconfirmed",
      error_code: "timeout",
    });
    expect(await api.mediaServers.retryPending(id)).toEqual({ queued: 2 });
    expect(body).toEqual({});
  },
);

it.each([
  { id: 7, name: "TV", type: "series", paths: ["/tv"] },
  { id: "7", name: "Audio", type: "music", paths: ["/audio"] },
  { id: "7", name: "TV", type: "series", paths: null },
])(
  "rejects invalid native library choices without coercing IDs or types",
  async (library) => {
    server.use(
      http.post("/api/silo/libraries", () =>
        HttpResponse.json({ data: [library], error_code: null }),
      ),
    );
    await expect(api.mediaServers.libraries(input)).rejects.toThrow(
      "Could not load Silo libraries",
    );
  },
);

it("rejects malformed retry acceptance instead of claiming work was queued", async () => {
  server.use(
    http.post(`${item}/retry-pending`, () =>
      HttpResponse.json({ success: true }),
    ),
  );
  await expect(api.mediaServers.retryPending(id)).rejects.toThrow(
    "Could not queue pending refreshes",
  );
});

it("uses item probes with saved-key overrides and omits credential fields from DTOs", async () => {
  const row = {
    id,
    kind: "silo",
    name: "TV",
    enabled: false,
    url: input.url,
    verify_ssl: true,
    api_key_set: true,
    path_mappings: [],
  };
  const received: unknown[] = [];
  server.use(
    http.get("/api/system/media-server-instances", ({ request }) => {
      expect(new URL(request.url).searchParams.get("kind")).toBe("silo");
      return HttpResponse.json({ data: [row] });
    }),
    http.post(`${item}/test-connection`, async ({ request }) => {
      received.push(await request.json());
      return HttpResponse.json({ success: true });
    }),
    http.patch(item, async ({ request }) => {
      received.push(await request.json());
      return HttpResponse.json(row);
    }),
  );
  expect(await api.mediaServers.list("silo")).toEqual([row]);
  expect(
    await api.mediaServers.testExisting(id, {
      url: "https://changed.example",
      verify_ssl: false,
    }),
  ).toEqual({ success: true });
  expect(await api.mediaServers.update(id, { api_key: "0007" })).toEqual(row);
  expect(received).toEqual([
    { url: "https://changed.example", verify_ssl: false },
    { api_key: "0007" },
  ]);
});

it("discards credential-bearing transport errors", async () => {
  server.use(
    http.post(`${item}/test-connection`, () =>
      HttpResponse.json({}, { status: 502 }),
    ),
  );
  const error = await api.mediaServers
    .testExisting(id, { api_key: "sentinel-private-key" })
    .catch((error) => error);
  expect(error).toBeInstanceOf(Error);
  expect(error).not.toHaveProperty("config");
  expect(JSON.stringify(error)).not.toContain("sentinel-private-key");
});
