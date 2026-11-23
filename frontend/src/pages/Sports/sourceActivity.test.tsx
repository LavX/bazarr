/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it } from "vitest";
import BlacklistSportsView from "@/pages/Blacklist/Sports";
import SportsHistoryView from "@/pages/History/Sports";
import {
  sportarr,
  sportarrSibling,
} from "@/pages/Settings/Connections/__tests__/fixtures";
import WantedSportsView from "@/pages/Wanted/Sports";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { openSelect } from "@/tests/select";

const availability = { browsable: true, searchable: true, retained: true };

beforeEach(() => {
  server.use(
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/system/jobs", () => HttpResponse.json([])),
  );
});
const arr = {
  id: 7,
  arr_instance_id: 42,
  league_id: 17,
  title: "Arr Final",
  missing_subtitles: ["en"],
  language: "en",
  action: 1,
};
const source = {
  ...arr,
  origin: "source",
  arr_instance_id: null,
  league_id: null,
  source_id: 8,
  source_item_id: 70,
  source_group_id: 17,
  source_name: "Local archive",
  source_key: "source:8",
  source_kind: "folder",
  identity_state: "parsed",
  title: "Source Final",
  path: "/media/source.mkv",
  missing_subtitles: [
    { code2: "en", name: "English", hi: true, forced: false },
  ],
};

const pages = [
  ["wanted", WantedSportsView],
  ["history", SportsHistoryView],
  ["blacklist", BlacklistSportsView],
] as const;

it.each(pages)(
  "filters the mixed %s activity by source key",
  async (route, Page) => {
    window.history.replaceState(null, "", "/");
    const user = userEvent.setup();
    let query = new URLSearchParams();
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { use_sportarr: true } }),
      ),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
      http.get("/api/system/media-sources", () =>
        HttpResponse.json({
          data: [
            {
              id: 8,
              name: "Local archive",
              kind: "folder",
              enabled: true,
              libraries: [{ id: 9, enabled: true, content_kind: "sports" }],
            },
          ],
          availability: {
            movie: availability,
            series: availability,
            sports: availability,
          },
        }),
      ),
      http.get(`/api/sports/${route}`, ({ request }) => {
        query = new URL(request.url).searchParams;
        const data =
          query.get("source_key[]") === "source:8" ? [source] : [arr, source];
        return HttpResponse.json({ data, total: data.length });
      }),
    );
    customRender(<Page />);
    expect(
      await screen.findByRole(
        "link",
        { name: "Source Final" },
        { timeout: 8000 },
      ),
    ).toHaveAttribute("href", "/sports/source/17");
    expect(screen.getByRole("link", { name: "Arr Final" })).toHaveAttribute(
      "href",
      "/sports/17?instance=42",
    );
    expect(
      within(screen.getByRole("row", { name: /Source Final/ })).getByText(
        "Local archive",
      ),
    ).toBeVisible();
    if (route === "wanted") expect(screen.getByText("en:HI")).toBeVisible();
    const listbox = await openSelect(user, "All sources");
    await user.click(
      within(listbox).getByRole("option", { name: "Local archive" }),
    );
    await waitFor(() => {
      expect(query.getAll("source_key[]")).toEqual(["source:8"]);
      expect(screen.queryByRole("link", { name: "Arr Final" })).toBeNull();
    });
    expect(query.get("sources")).toBe("1");
    expect(query.has("arr_instance_id")).toBe(false);
  },
);

it.each(pages)(
  "preserves legacy instance links and owner selection on Arr-only %s",
  async (route, Page) => {
    window.history.replaceState(null, "", "/?instance=42");
    const user = userEvent.setup();
    let query = new URLSearchParams();
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { use_sportarr: true } }),
      ),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr, sportarrSibling]),
      ),
      http.get(`/api/sports/${route}`, ({ request }) => {
        query = new URL(request.url).searchParams;
        const row =
          query.get("arr_instance_id") === "43"
            ? { ...arr, id: 8, arr_instance_id: 43, title: "Other Final" }
            : arr;
        return HttpResponse.json({ data: [row], total: 1 });
      }),
    );
    customRender(<Page />);
    expect(
      await screen.findByRole("link", { name: "Arr Final" }),
    ).toHaveAttribute("href", "/sports/17?instance=42");
    expect(query.get("arr_instance_id")).toBe("42");
    expect(query.getAll("source_key[]")).toEqual(["arr:42"]);

    const instance = await screen.findByRole("combobox", { name: "Instance" });
    await waitFor(() => expect(instance).toHaveValue(sportarr.name));
    const listbox = await openSelect(user, "All instances");
    await user.click(
      within(listbox).getByRole("option", { name: sportarrSibling.name }),
    );
    expect(
      await screen.findByRole("link", { name: "Other Final" }),
    ).toHaveAttribute("href", "/sports/17?instance=43");
    expect(query.get("arr_instance_id")).toBe("43");
    expect(query.getAll("source_key[]")).toEqual(["arr:43"]);
  },
);

it("refreshes a stale Sports exclusion and retries with the replacement row key", async () => {
  window.history.replaceState(null, "", "/");
  const user = userEvent.setup();
  const keys: (string | null)[] = [];
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { use_sportarr: true } }),
    ),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.get("/api/system/arr-instances", () => HttpResponse.json([sportarr])),
    http.get("/api/system/media-sources", () =>
      HttpResponse.json({
        data: [],
        availability: {
          movie: availability,
          series: availability,
          sports: availability,
        },
      }),
    ),
    http.get("/api/sports/blacklist", () => {
      const data =
        keys.length === 0
          ? [{ ...source, removal_key: "listed-entry" }]
          : keys.length === 1
            ? [
                {
                  ...source,
                  source_key: "source:9",
                  source_name: "Other folder",
                  title: "Replacement Final",
                  removal_key: "replacement-entry",
                },
              ]
            : [];
      return HttpResponse.json({ data, total: data.length });
    }),
    http.delete(`/api/sports/blacklist/${source.id}`, ({ request }) => {
      const params = new URL(request.url).searchParams;
      expect(params.get("origin")).toBe("source");
      expect(params.get("sources")).toBe("1");
      keys.push(params.get("source_blacklist_key"));
      return keys.length === 1
        ? HttpResponse.json(
            { message: "This exclusion changed." },
            { status: 409 },
          )
        : HttpResponse.json({ removed: true });
    }),
  );
  customRender(<BlacklistSportsView />);
  const first = await screen.findByRole("row", { name: /Source Final/ });
  await user.click(
    within(first).getByRole("button", { name: "Remove exclusion" }),
  );
  expect(
    await screen.findByText(
      "This exclusion changed or was removed. Check the list before trying again.",
    ),
  ).toBeVisible();
  const replacement = await screen.findByRole("row", {
    name: /Replacement Final/,
  });
  expect(within(replacement).getByText("Other folder")).toBeVisible();
  await user.click(
    within(replacement).getByRole("button", { name: "Remove exclusion" }),
  );
  expect(await screen.findByText("Nothing Excluded for Sports")).toBeVisible();
  expect(keys).toEqual(["listed-entry", "replacement-entry"]);
});

it("keeps retained rows in source-only history and offers their owner filter", async () => {
  window.history.replaceState(null, "", "/");
  const user = userEvent.setup();
  const gone = {
    ...source,
    id: 8,
    source_id: null,
    source_item_id: null,
    source_group_id: null,
    source_key: "source:removed",
    source_name: "Former folder",
    title: "Retained Final",
  };
  let keys: string[] = [];
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { use_sportarr: false } }),
    ),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.get("/api/system/arr-instances", () => HttpResponse.json([])),
    http.get("/api/system/media-sources", () =>
      HttpResponse.json({
        data: [
          {
            id: 8,
            name: "Local archive",
            kind: "folder",
            enabled: true,
            libraries: [{ id: 9, enabled: true, content_kind: "sports" }],
          },
        ],
        availability: {
          movie: availability,
          series: availability,
          sports: { ...availability, removed: true },
        },
      }),
    ),
    http.get("/api/sports/history", ({ request }) => {
      keys = new URL(request.url).searchParams.getAll("source_key[]");
      const data = [source, gone].filter((row) =>
        keys.includes(row.source_key),
      );
      return HttpResponse.json({ data, total: data.length });
    }),
  );
  customRender(<SportsHistoryView />);
  expect(
    await screen.findByRole("row", { name: /Retained Final/ }),
  ).toBeVisible();
  expect(screen.getByRole("row", { name: /Source Final/ })).toBeVisible();
  expect(keys).toEqual(["source:8", "source:removed"]);
  const listbox = await openSelect(user, "All sources");
  await user.click(
    within(listbox).getByRole("option", { name: "Removed sources" }),
  );
  await waitFor(() => {
    expect(keys).toEqual(["source:removed"]);
    expect(screen.queryByRole("row", { name: /Source Final/ })).toBeNull();
  });
});

it("confirms the complete mixed bulk scope when a text filter matches only the source", async () => {
  window.history.replaceState(null, "", "/");
  const user = userEvent.setup();
  let clear: URLSearchParams | undefined;
  const { promise: refresh, resolve: finishRefresh } =
    Promise.withResolvers<void>();
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { use_sportarr: true } }),
    ),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.get("/api/system/arr-instances", () => HttpResponse.json([sportarr])),
    http.get("/api/system/media-sources", () =>
      HttpResponse.json({
        data: [
          {
            id: 8,
            name: "Local archive",
            kind: "folder",
            enabled: true,
            libraries: [{ id: 9, enabled: true, content_kind: "sports" }],
          },
        ],
        availability: {
          movie: availability,
          series: availability,
          sports: availability,
        },
      }),
    ),
    http.get("/api/sports/blacklist", async ({ request }) => {
      if (clear) await refresh;
      const provider = new URL(request.url).searchParams.get("provider");
      const data = provider === "fixture" ? [source] : [arr, source];
      return HttpResponse.json({ data, total: data.length });
    }),
    http.delete("/api/sports/blacklist", ({ request }) => {
      clear = new URL(request.url).searchParams;
      return HttpResponse.json({ removed: 2 });
    }),
  );
  customRender(<BlacklistSportsView />);
  await screen.findByRole("link", { name: "Arr Final" });
  await user.type(screen.getByRole("textbox", { name: "Provider" }), "fixture");
  await waitFor(() =>
    expect(screen.queryByRole("link", { name: "Arr Final" })).toBeNull(),
  );
  await user.click(screen.getByRole("button", { name: "Remove All" }));
  const dialog = within(
    await screen.findByRole("dialog", { name: "Clear sports exclusions" }),
  );
  expect(dialog.getByText(/across all sports libraries/)).toBeVisible();
  try {
    await user.click(dialog.getByRole("button", { name: "Clear exclusions" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Clear sports exclusions" }),
      ).toBeNull(),
    );
    expect(clear?.get("sources")).toBe("1");
    expect(clear?.has("arr_instance_id")).toBe(false);
    expect(clear?.has("source_key[]")).toBe(false);
  } finally {
    finishRefresh();
  }
});

it("excludes a source history release by recorded identity without sending a file path", async () => {
  window.history.replaceState(null, "", "/");
  const user = userEvent.setup();
  const requests: unknown[] = [];
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { use_sportarr: true } }),
    ),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.get("/api/system/arr-instances", () => HttpResponse.json([sportarr])),
    http.get("/api/sports/history", () =>
      HttpResponse.json({
        data: [
          {
            ...source,
            action: 1,
            provider: "fixture",
            subs_id: "original-release",
          },
        ],
        total: 1,
      }),
    ),
    http.post(
      `/api/sports/history/${source.id}/blacklist`,
      async ({ request }) => {
        const params = new URL(request.url).searchParams;
        expect(params.get("origin")).toBe("source");
        expect(params.get("sources")).toBe("1");
        requests.push(await request.json());
        return new HttpResponse(null, { status: 204 });
      },
    ),
  );
  customRender(<SportsHistoryView />);
  await user.click(await screen.findByRole("button", { name: "Exclude" }));
  const dialog = within(
    await screen.findByRole("dialog", { name: "Exclude sports release" }),
  );
  await waitFor(() =>
    expect(dialog.getByText(/will stay on disk/)).toBeVisible(),
  );
  await user.click(dialog.getByRole("button", { name: "Exclude release" }));
  await waitFor(() =>
    expect(requests).toEqual([
      {
        source_id: 8,
        source_item_id: 70,
        provider: "fixture",
        subs_id: "original-release",
        language: "en",
      },
    ]),
  );
});

it("filters source-only Wanted by recorded audio and a missing language without an audio track", async () => {
  window.history.replaceState(null, "", "/");
  const user = userEvent.setup();
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { use_sportarr: false } }),
    ),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.get("/api/system/arr-instances", () => HttpResponse.json([])),
    http.get("/api/system/media-sources", () =>
      HttpResponse.json({
        data: [
          {
            id: 8,
            name: "Local archive",
            kind: "folder",
            enabled: true,
            libraries: [{ id: 9, enabled: true, content_kind: "sports" }],
          },
        ],
        availability: {
          movie: availability,
          series: availability,
          sports: availability,
        },
      }),
    ),
    http.get("/api/system/languages/audio", ({ request }) => {
      expect(new URL(request.url).searchParams.get("sources")).toBe("1");
      return HttpResponse.json([
        { code2: "hu", name: "Hungarian" },
        { code2: "de", name: "German" },
      ]);
    }),
    http.get("/api/system/languages", () =>
      HttpResponse.json([{ code2: "en", name: "English", enabled: true }]),
    ),
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({
        data: [
          { ...source, audio_language: ["Hungarian"] },
          {
            ...source,
            id: 8,
            title: "Other Final",
            audio_language: ["German"],
            missing_subtitles: [{ code2: "es", name: "Spanish" }],
          },
        ],
        total: 2,
      }),
    ),
  );
  customRender(<WantedSportsView />);
  await screen.findByRole("row", { name: /Other Final/ });
  await user.click(screen.getByRole("button", { name: "Toggle filters" }));
  const missing = await openSelect(user, "Select a language...");
  await user.click(within(missing).getByRole("option", { name: "English" }));
  await waitFor(() =>
    expect(screen.queryByRole("row", { name: /Other Final/ })).toBeNull(),
  );
  expect(screen.getByRole("row", { name: /Source Final/ })).toBeVisible();
  await user.click(screen.getByText("Clear all"));
  await screen.findByRole("row", { name: /Other Final/ });
  const audio = await openSelect(user, "Select languages to include...");
  await user.click(within(audio).getByRole("option", { name: "Hungarian" }));
  await waitFor(() =>
    expect(screen.queryByRole("row", { name: /Other Final/ })).toBeNull(),
  );
  expect(screen.getByRole("row", { name: /Source Final/ })).toBeVisible();
});
