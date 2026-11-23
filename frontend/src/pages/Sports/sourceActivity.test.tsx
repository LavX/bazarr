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

it.each([
  ["one native owner", [sportarr]],
  ["multiple native owners", [sportarr, sportarrSibling]],
] as const)(
  "clears native exclusions from the unfiltered picker after the last retained source disappears with %s",
  async (_, instances) => {
    window.history.replaceState(null, "", "/");
    const user = userEvent.setup();
    let retained = true;
    const nativeOwners = new Set<number>(instances.map(({ id }) => id));
    const clears: URLSearchParams[] = [];
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { use_sportarr: true } }),
      ),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/system/arr-instances", () => HttpResponse.json(instances)),
      http.get("/api/system/media-sources", () =>
        HttpResponse.json({
          data: [
            {
              id: 8,
              name: "Local archive",
              kind: "folder",
              enabled: true,
              libraries: [{ id: 9, enabled: true, content_kind: "movie" }],
            },
          ],
          availability: {
            sports: {
              browsable: false,
              searchable: false,
              retained,
              ...(retained ? { retained_source_ids: [8] } : {}),
            },
          },
        }),
      ),
      http.get("/api/sports/blacklist", ({ request }) => {
        const params = new URL(request.url).searchParams;
        const keys = params.getAll("source_key[]");
        const owner = params.get("arr_instance_id");
        const data = [
          ...instances
            .filter(({ id }) => nativeOwners.has(id))
            .map(({ id }) => ({
              ...arr,
              id,
              arr_instance_id: id,
              source_key: `arr:${id}`,
              title: `Native final ${id}`,
            })),
          ...(retained ? [{ ...source, source_group_id: null }] : []),
        ].filter(
          (row) =>
            (!owner || row.arr_instance_id === Number(owner)) &&
            (keys.length === 0 || keys.includes(row.source_key)),
        );
        return HttpResponse.json({ data, total: data.length });
      }),
      http.delete("/api/sports/blacklist", ({ request }) => {
        const params = new URL(request.url).searchParams;
        clears.push(params);
        if (params.getAll("source_key[]").includes("source:8")) {
          retained = false;
          return HttpResponse.json({ removed: 1 });
        }
        const owner = params.get("arr_instance_id");
        if (owner) nativeOwners.delete(Number(owner));
        else if (
          params.get("sources") === "1" &&
          params.getAll("source_key[]").length === 0
        ) {
          nativeOwners.clear();
        }
        return HttpResponse.json({ removed: instances.length });
      }),
    );
    customRender(<BlacklistSportsView />);
    await screen.findByRole("row", { name: /Source Final/ });
    const choices = await openSelect(user, "All sources");
    await user.click(
      within(choices).getByRole("option", { name: "Local archive" }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("row", { name: /Native final/ })).toBeNull(),
    );
    await user.click(screen.getByRole("button", { name: "Remove All" }));
    const sourceDialog = within(await screen.findByRole("dialog"));
    await waitFor(() =>
      expect(sourceDialog.getByText(/for this source again/)).toBeVisible(),
    );
    await user.click(
      sourceDialog.getByRole("button", { name: "Clear exclusions" }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await screen.findByRole("combobox", { name: "Instance" });
    await user.click(
      screen.getByRole("button", { name: "Clear instance filter" }),
    );
    for (const { id } of instances) {
      expect(
        await screen.findByRole("row", {
          name: new RegExp(`Native final ${id}`),
        }),
      ).toBeVisible();
    }
    expect(clears[0].getAll("source_key[]")).toEqual(["source:8"]);
    expect(clears[0].has("arr_instance_id")).toBe(false);

    await user.click(screen.getByRole("button", { name: "Remove All" }));
    const nativeDialog = within(await screen.findByRole("dialog"));
    await waitFor(() =>
      expect(
        nativeDialog.getByText(
          instances.length > 1
            ? /across all sports libraries again/
            : /for this instance again/,
        ),
      ).toBeVisible(),
    );
    await user.click(
      nativeDialog.getByRole("button", { name: "Clear exclusions" }),
    );
    expect(
      await screen.findByText("Nothing Excluded for Sports"),
    ).toBeVisible();
    expect(clears[1].getAll("source_key[]")).toEqual([]);
    expect(clears[1].get("arr_instance_id")).toBe(
      instances.length === 1 ? "42" : null,
    );
    expect(clears[1].get("sources")).toBe(instances.length > 1 ? "1" : null);
  },
);

it.each(pages.slice(1))(
  "keeps both kinds of retained owner on source-only %s",
  async (route, Page) => {
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
      title: "Removed owner's final",
    };
    const retained = {
      ...gone,
      id: 9,
      source_id: 12,
      source_key: "source:12",
      source_name: "Former sports folder",
      title: "Former sports final",
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
            {
              id: 12,
              name: "Former sports folder",
              kind: "folder",
              enabled: true,
              libraries: [{ id: 13, enabled: true, content_kind: "movie" }],
            },
          ],
          availability: {
            movie: availability,
            series: availability,
            sports: {
              ...availability,
              removed: true,
              retained_source_ids: [12],
            },
          },
        }),
      ),
      http.get(`/api/sports/${route}`, ({ request }) => {
        keys = new URL(request.url).searchParams.getAll("source_key[]");
        const data = [source, gone, retained].filter((row) =>
          keys.includes(row.source_key),
        );
        return HttpResponse.json({ data, total: data.length });
      }),
    );
    customRender(<Page />);
    expect(
      await screen.findByRole("row", { name: /Former sports final/i }),
    ).toBeVisible();
    expect(screen.getByRole("row", { name: /Source Final/ })).toBeVisible();
    expect(
      screen.getByRole("row", { name: /Removed owner's final/i }),
    ).toBeVisible();
    expect(keys).toEqual(["source:8", "source:12", "source:removed"]);
    const listbox = await openSelect(user, "All sources");
    expect(
      within(listbox).getByRole("option", { name: "Removed sources" }),
    ).toBeVisible();
    await user.click(
      within(listbox).getByRole("option", { name: "Former sports folder" }),
    );
    await waitFor(() => {
      expect(keys).toEqual(["source:12"]);
      expect(screen.queryByRole("row", { name: /Source Final/ })).toBeNull();
      expect(
        screen.queryByRole("row", { name: /Removed owner's final/i }),
      ).toBeNull();
    });
    expect(
      screen.getByRole("row", { name: /Former sports final/i }),
    ).toBeVisible();
  },
);

it.each(pages.slice(1))(
  "keeps the native single-instance picker on Sports %s",
  async (route, Page) => {
    window.history.replaceState(null, "", "/");
    const user = userEvent.setup();
    let current = new URLSearchParams();
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { use_sportarr: true } }),
      ),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
      http.get(`/api/sports/${route}`, ({ request }) => {
        current = new URL(request.url).searchParams;
        return HttpResponse.json({ data: [arr], total: 1 });
      }),
    );
    customRender(<Page />);
    await screen.findByRole("link", { name: "Arr Final" });
    expect(screen.getByRole("combobox", { name: "Instance" })).toHaveValue("");
    const listbox = await openSelect(user, "All instances");
    await user.click(
      within(listbox).getByRole("option", { name: sportarr.name }),
    );
    await waitFor(() => {
      expect(current.get("arr_instance_id")).toBe("42");
      expect(current.getAll("source_key[]")).toEqual(["arr:42"]);
    });
  },
);

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
  await waitFor(() =>
    expect(dialog.getByText(/across all sports libraries/)).toBeVisible(),
  );
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
