/* eslint-disable camelcase */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { vi } from "vitest";
import { useSportsHistoryPagination } from "@/apis/hooks/sports";
import { useSubtitleContent } from "@/apis/hooks/subtitles";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { createDefaultReducer } from "@/modules/socketio/reducer";
import BlacklistSportsView from "@/pages/Blacklist/Sports";
import SportsHistoryView from "@/pages/History/Sports";
import { sportarr } from "@/pages/Settings/Connections/__tests__/fixtures";
import SubtitleViewer from "@/pages/SubtitleEditor";
import { AllProviders } from "@/providers";
import { act, customRender, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { SourceItem } from "./api";
import {
  SourceHistoryButton,
  SourceHistoryRow,
  SourceHistoryView,
  sourceReleaseForSubtitle,
  SourceUpgradeButton,
} from "./History";

const subtitle: Subtitle & { path: string } = {
  path: "/media/movie.en.hi.srt",
  code2: "en",
  name: "English",
  hi: true,
  forced: false,
};
const item: SourceItem = {
  id: 7,
  source_id: 3,
  library_id: 11,
  kind: "movie",
  group_id: null,
  title: "Folder movie",
  parsed_title: "Folder movie",
  year: "2026",
  poster: null,
  imdb_id: null,
  profile_id: 1,
  monitored: true,
  identity_state: "identified",
  identity_candidates: [],
  path: "/media/movie.mkv",
  scene_name: null,
  season: null,
  episode: null,
  arr_shadow: false,
  subtitles: [subtitle],
  missing_subtitles: [],
  upgradable: true,
};
const row: SourceHistoryRow = {
  origin: "source",
  source_key: "source:3",
  source_id: 3,
  item_id: 7,
  item_kind: "movie",
  history_id: 17,
  action: 3,
  provider: "source-provider",
  subs_id: "source-release",
  subtitles_path: subtitle.path,
  video_path: item.path,
  language: subtitle,
  score: "70%",
  matches: [],
  dont_matches: [],
  timestamp: "a minute ago",
  parsed_timestamp: "11/23/2026 05:00:00",
  description: "Downloaded for this source item",
  upgradable: true,
  blacklisted: false,
  upgradedFromId: 12,
};

beforeEach(() => {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: {}, media_sources: { visible: true } }),
    ),
    http.get("/api/system/media-sources", () =>
      HttpResponse.json({
        data: [
          { id: 3, enabled: true, libraries: [{ id: 11, enabled: true }] },
        ],
      }),
    ),
    http.get("/api/sources/items/7", () => HttpResponse.json(item)),
    http.get("/api/sources/items/7/history", () =>
      HttpResponse.json({ data: [row], total: 1 }),
    ),
  );
});

function OpenDocument({
  mediaType = "source",
  id = item.id,
  language = "en:hi",
}: {
  mediaType?: "source" | "movie";
  id?: number;
  language?: string;
}) {
  const { data } = useSubtitleContent(mediaType, id, language);
  return <output>{data?.data.content}</output>;
}

it("refreshes an open source viewer for its own item event only", async () => {
  const reads = vi.fn();
  let content: string | null = "1\n00:00:01,000 --> 00:00:02,000\nOriginal\n";
  server.use(
    http.get(
      "/api/sources/items/:id/subtitles/:language/content",
      ({ params }) => {
        reads("source", params.id);
        if (params.id === "7" && content === null) {
          return HttpResponse.json(
            { message: "Subtitle file no longer exists" },
            { status: 404 },
          );
        }
        return HttpResponse.json({
          content: params.id === "7" ? content : "other source cues",
          format: "srt",
          encoding: "utf-8",
          size: content?.length ?? 0,
          mediaId: Number(params.id),
        });
      },
    ),
    http.get("/api/movies/7/subtitles/:language/content", () => {
      reads("movie", "7");
      return HttpResponse.json({ content: "native movie cues" });
    }),
  );
  const router = createMemoryRouter(
    [
      {
        path: "/subtitles/preview/:mediaType/:mediaId/:language",
        element: (
          <>
            <SubtitleViewer />
            <OpenDocument id={8} />
            <OpenDocument mediaType="movie" />
          </>
        ),
      },
    ],
    { initialEntries: ["/subtitles/preview/source/7/en%3Ahi"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  expect(await screen.findByText("1 cues")).toBeVisible();
  expect(await screen.findByText("other source cues")).toBeVisible();
  expect(await screen.findByText("native movie cues")).toBeVisible();
  reads.mockClear();
  content += "\n2\n00:00:03,000 --> 00:00:04,000\nUpdated\n";
  const reducer = createDefaultReducer().find(
    (entry) => entry.key === "source-item",
  );
  act(() => {
    reducer?.any?.();
    reducer?.update?.([]);
  });
  expect(reads).not.toHaveBeenCalled();
  act(() => reducer?.update?.([7, 7]));
  expect(await screen.findByText("2 cues")).toBeVisible();
  expect(reads.mock.calls).toEqual([["source", "7"]]);
  content = null;
  act(() => reducer?.delete?.([7]));
  expect(await screen.findByRole("alert", { name: "Error" })).toHaveTextContent(
    "Subtitle file no longer exists",
  );
  expect(screen.queryByText("2 cues")).not.toBeInTheDocument();
  expect(reads.mock.calls).toEqual([
    ["source", "7"],
    ["source", "7"],
  ]);
});

it("opens the source history and preserves nullable references without reading Arr history", async () => {
  const user = userEvent.setup();
  queryClient.setQueryData(
    [QueryKeys.Movies, QueryKeys.History, 7],
    [{ provider: "wrong-arr-provider" }],
  );
  server.use(
    http.get("/api/sources/items/7/history", () =>
      HttpResponse.json({
        data: [
          row,
          {
            ...row,
            history_id: 18,
            item_id: null,
            source_id: null,
            language: null,
            provider: null,
            subs_id: null,
            subtitles_path: null,
            upgradable: false,
            upgradedFromId: null,
            description: "Removed source record",
          },
        ],
        total: 2,
      }),
    ),
  );
  customRender(<SourceHistoryButton item={item} />);
  await user.click(screen.getByRole("button", { name: "History" }));
  const dialog = await screen.findByRole("dialog", {
    name: "Source Item History",
  });
  expect(await within(dialog).findByText("source-provider")).toBeVisible();
  expect(within(dialog).getByText("English HI")).toBeVisible();
  expect(within(dialog).getByText("Upgraded from history #12")).toBeVisible();
  expect(within(dialog).getByText("Removed source record")).toBeVisible();
  expect(
    within(dialog).queryByText("wrong-arr-provider"),
  ).not.toBeInTheDocument();
});

it.each([0, 1])(
  "waits for the upgrade job and reports its actual upgraded count %i",
  async (upgraded) => {
    const user = userEvent.setup();
    const writes: FormData[] = [];
    const historyRead = vi.fn();
    const resultRead = vi.fn();
    const contentRead = vi.fn();
    const nativeContentRead = vi.fn();
    let content = "original source cues";
    server.use(
      http.get(
        "/api/sources/items/:id/subtitles/:language/content",
        ({ params }) => {
          contentRead(String(params.id), String(params.language));
          return HttpResponse.json({
            content:
              params.id === "7"
                ? `${content} ${params.language}`
                : "unrelated source cues",
          });
        },
      ),
      http.get("/api/movies/7/subtitles/:language/content", () => {
        nativeContentRead();
        return HttpResponse.json({ content: "native movie cues" });
      }),
      http.get("/api/sources/items/7/history", () => {
        historyRead();
        return HttpResponse.json({ data: [row], total: 1 });
      }),
      http.post("/api/sources/items/7/upgrade", async ({ request }) => {
        writes.push(await request.formData());
        return HttpResponse.json(
          { job_id: 31, session: "run-a" },
          { status: 202 },
        );
      }),
      http.get("/api/system/jobs/result", () => {
        resultRead();
        return HttpResponse.json({
          job_id: 31,
          session: "run-a",
          result: { upgraded },
        });
      }),
    );
    queryClient.setQueryData(
      [QueryKeys.System, QueryKeys.Jobs],
      [{ job_id: 31, status: "running", session: "run-a" }],
    );
    customRender(
      <>
        <SourceHistoryView item={item} />
        <OpenDocument />
        <OpenDocument language="en" />
        <OpenDocument language="en:sync-ffsubsync" />
        <OpenDocument id={8} />
        <OpenDocument mediaType="movie" />
      </>,
    );
    expect(await screen.findByText("original source cues en:hi")).toBeVisible();
    expect(await screen.findByText("original source cues en")).toBeVisible();
    expect(
      await screen.findByText("original source cues en:sync-ffsubsync"),
    ).toBeVisible();
    expect(await screen.findByText("unrelated source cues")).toBeVisible();
    expect(await screen.findByText("native movie cues")).toBeVisible();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Upgrade" })).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "Upgrade" }));
    expect(
      await screen.findByText(
        "Upgrade queued. Waiting for the search to finish.",
      ),
    ).toBeVisible();
    expect(writes).toHaveLength(1);
    expect(writes[0].get("history_id")).toBe("17");
    expect(historyRead).toHaveBeenCalledTimes(2);
    expect(resultRead).not.toHaveBeenCalled();
    queryClient.setQueryData(
      [QueryKeys.System, QueryKeys.Jobs],
      [{ job_id: 31, status: "completed", session: "other-run" }],
    );
    expect(resultRead).not.toHaveBeenCalled();
    expect(contentRead).toHaveBeenCalledTimes(4);
    if (upgraded) content = "upgraded source cues";
    queryClient.setQueryData(
      [QueryKeys.System, QueryKeys.Jobs],
      [{ job_id: 31, status: "completed", session: "run-a" }],
    );
    expect(
      await screen.findByText(
        upgraded ? "1 subtitle upgraded." : "No better subtitle was found.",
      ),
    ).toBeVisible();
    expect(resultRead).toHaveBeenCalledTimes(1);
    expect(historyRead).toHaveBeenCalledTimes(3);
    if (upgraded) {
      expect(
        await screen.findByText("upgraded source cues en:hi"),
      ).toBeVisible();
      expect(await screen.findByText("upgraded source cues en")).toBeVisible();
      expect(
        await screen.findByText("upgraded source cues en:sync-ffsubsync"),
      ).toBeVisible();
    }
    expect(contentRead).toHaveBeenCalledTimes(upgraded ? 7 : 4);
    expect(contentRead.mock.calls.filter(([id]) => id === "8")).toHaveLength(1);
    expect(nativeContentRead).toHaveBeenCalledTimes(1);
  },
);

it("shows a job failure and refreshes history without claiming an upgrade", async () => {
  const user = userEvent.setup();
  const historyRead = vi.fn();
  server.use(
    http.get("/api/sources/items/7/history", () => {
      historyRead();
      return HttpResponse.json({ data: [row], total: 1 });
    }),
    http.post("/api/sources/items/7/upgrade", () =>
      HttpResponse.json({ job_id: 32, session: "run-a" }, { status: 202 }),
    ),
  );
  queryClient.setQueryData(
    [QueryKeys.System, QueryKeys.Jobs],
    [{ job_id: 32, status: "running", session: "run-a" }],
  );
  customRender(<SourceHistoryView item={item} />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Upgrade" })).toBeEnabled(),
  );
  await user.click(screen.getByRole("button", { name: "Upgrade" }));
  await screen.findByText("Upgrade queued. Waiting for the search to finish.");
  queryClient.setQueryData(
    [QueryKeys.System, QueryKeys.Jobs],
    [
      {
        job_id: 32,
        status: "failed",
        session: "run-a",
        error: { message: "Provider failed" },
      },
    ],
  );
  expect(await screen.findByText("Provider failed")).toBeVisible();
  expect(screen.queryByText("1 subtitle upgraded.")).not.toBeInTheDocument();
  expect(historyRead).toHaveBeenCalledTimes(3);
});

it("excludes a confirmed release without deleting or expiring the current subtitle document", async () => {
  const user = userEvent.setup();
  const writes: Record<string, FormDataEntryValue>[] = [];
  const contentRead = vi.fn();
  let excluded = false;
  server.use(
    http.get("/api/sources/items/7/subtitles/:language/content", () => {
      contentRead();
      return HttpResponse.json({ content: "Current source subtitle cues" });
    }),
    http.get("/api/sources/items/7/history", () =>
      HttpResponse.json({
        data: [
          {
            ...row,
            history_id: 18,
            action: 9,
            provider: null,
            subs_id: null,
            upgradable: false,
          },
          { ...row, blacklisted: excluded },
        ],
        total: 2,
      }),
    ),
    http.get("/api/sources/items/7", () =>
      HttpResponse.json({ ...item, profile_id: null, upgradable: false }),
    ),
    http.post("/api/sources/items/7/blacklist", async ({ request }) => {
      writes.push(Object.fromEntries(await request.formData()));
      excluded = true;
      return new HttpResponse(null, { status: 204 });
    }),
  );
  customRender(
    <>
      <SourceHistoryView item={item} />
      <OpenDocument />
    </>,
  );
  expect(await screen.findByText("Current source subtitle cues")).toBeVisible();
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Exclude" })).toBeEnabled(),
  );
  expect(screen.getByRole("button", { name: "Upgrade" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Exclude" }));
  const confirm = await screen.findByRole("dialog", {
    name: "Exclude release from search?",
  });
  expect(writes).toEqual([]);
  await waitFor(() =>
    expect(
      within(confirm).getByText(/without deleting a subtitle file/i),
    ).toBeVisible(),
  );
  await user.click(
    within(confirm).getByRole("button", { name: "Exclude release" }),
  );
  expect(
    await screen.findByText(
      "Release excluded from search. No subtitle file was deleted.",
    ),
  ).toBeVisible();
  expect(writes).toEqual([
    {
      history_id: "17",
      provider: "source-provider",
      subs_id: "source-release",
      delete_file: "false",
    },
  ]);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Exclude" })).toBeDisabled(),
  );
  expect(screen.getByText("Current source subtitle cues")).toBeVisible();
  expect(contentRead).toHaveBeenCalledTimes(1);
});

it.each(["event action", "socket event"])(
  "refreshes combined Sports history and exclusions through the %s",
  async (trigger) => {
    const user = userEvent.setup();
    const event = { ...item, kind: "sports_event" as const };
    const eventRelease = { ...row, item_kind: event.kind, upgradable: false };
    const record = {
      id: row.history_id,
      title: "Folder Sports Final",
      origin: "source",
      source_key: row.source_key,
      source_id: event.source_id,
      source_item_id: event.id,
      arr_instance_id: null,
      language: "en",
      provider: row.provider,
      subs_id: row.subs_id,
      action: row.action,
    };
    const nativeRead = vi.fn();
    let excluded = false;
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { use_sportarr: true },
          media_sources: { visible: true },
        }),
      ),
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([{ ...sportarr, enabled: true }]),
      ),
      http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/system/media-sources", () =>
        HttpResponse.json({
          availability: { sports: { browsable: true } },
          data: [
            {
              id: event.source_id,
              name: "Folder Sports",
              enabled: true,
              libraries: [
                { id: event.library_id, content_kind: "sports", enabled: true },
              ],
            },
          ],
        }),
      ),
      http.get("/api/sources/items/7", () => HttpResponse.json(event)),
      http.get("/api/sources/items/7/history", () =>
        HttpResponse.json({
          data: [{ ...eventRelease, blacklisted: excluded }],
          total: 1,
        }),
      ),
      http.post("/api/sources/items/7/blacklist", () => {
        excluded = true;
        return new HttpResponse(null, { status: 204 });
      }),
      http.get("/api/sports/history", ({ request }) => {
        if (new URL(request.url).searchParams.has("arr_instance_id")) {
          nativeRead();
          return HttpResponse.json({
            data: [{ ...record, origin: "arr", arr_instance_id: sportarr.id }],
            total: 1,
          });
        }
        return HttpResponse.json({
          data: [{ ...record, blacklisted: excluded }],
          total: 1,
        });
      }),
      http.get("/api/sports/blacklist", () =>
        HttpResponse.json({
          data: excluded ? [record] : [],
          total: excluded ? 1 : 0,
        }),
      ),
    );

    function NativeHistory() {
      const native = useSportsHistoryPagination({
        includeSources: true,
        owner: sportarr.id,
        sourceKeys: [`arr:${sportarr.id}`],
      });
      return <output>Native history: {native.data?.total}</output>;
    }

    customRender(
      <>
        <section aria-label="Event history">
          <SourceHistoryView item={event} />
        </section>
        <section aria-label="Combined history">
          <SportsHistoryView />
        </section>
        <section aria-label="Combined exclusions">
          <BlacklistSportsView />
        </section>
        <NativeHistory />
      </>,
    );
    const eventHistory = within(
      screen.getByRole("region", { name: "Event history" }),
    );
    const combinedHistory = within(
      screen.getByRole("region", { name: "Combined history" }),
    );
    const combinedExclusions = within(
      screen.getByRole("region", { name: "Combined exclusions" }),
    );
    expect(
      await combinedHistory.findByRole("button", { name: "Exclude" }),
    ).toBeEnabled();
    await screen.findByText("Native history: 1");
    const nativeReads = nativeRead.mock.calls.length;

    if (trigger === "event action") {
      await waitFor(() =>
        expect(
          eventHistory.getByRole("button", { name: "Exclude" }),
        ).toBeEnabled(),
      );
      await user.click(eventHistory.getByRole("button", { name: "Exclude" }));
      const confirmation = await screen.findByRole("dialog", {
        name: "Exclude release from search?",
      });
      await user.click(
        within(confirmation).getByRole("button", { name: "Exclude release" }),
      );
    } else {
      excluded = true;
      act(() =>
        createDefaultReducer()
          .find(({ key }) => key === "sports-blacklist")
          ?.any?.(),
      );
    }
    expect(await combinedExclusions.findByText(record.title)).toBeVisible();
    await waitFor(() =>
      expect(
        combinedHistory.getByRole("button", { name: "Exclude" }),
      ).toBeDisabled(),
    );
    expect(nativeRead).toHaveBeenCalledTimes(nativeReads);
  },
);

it("refuses a moved item before posting an upgrade", async () => {
  const user = userEvent.setup();
  let moved = false;
  const itemRead = vi.fn();
  const write = vi.fn();
  server.use(
    http.get("/api/sources/items/7", () => {
      itemRead();
      return HttpResponse.json({
        ...item,
        path: moved ? "/media/moved.mkv" : item.path,
      });
    }),
    http.post("/api/sources/items/7/upgrade", () => {
      write();
      return HttpResponse.json({ job_id: 99 });
    }),
  );
  customRender(<SourceUpgradeButton item={item} />);
  const upgrade = screen.getByRole("button", { name: "Upgrade" });
  await waitFor(() => expect(upgrade).toBeEnabled());
  expect(itemRead).not.toHaveBeenCalled();
  moved = true;
  await user.click(upgrade);
  expect(
    await screen.findByText("The source item moved or is no longer available."),
  ).toBeVisible();
  expect(itemRead).toHaveBeenCalledTimes(1);
  expect(write).not.toHaveBeenCalled();
});

it("disables source history writes when the current item is gone", async () => {
  server.use(
    http.get("/api/sources/items/7", () =>
      HttpResponse.json(
        { message: "The source item was removed" },
        { status: 404 },
      ),
    ),
  );
  customRender(<SourceHistoryView item={item} />);
  expect(await screen.findByText("The source item was removed")).toBeVisible();
  expect(screen.getByRole("button", { name: "Upgrade" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Exclude" })).toBeDisabled();
});

it("does not claim an upgrade when the completed job has no result", async () => {
  const user = userEvent.setup();
  server.use(
    http.post("/api/sources/items/7/upgrade", () =>
      HttpResponse.json({ job_id: 33, session: "run-a" }, { status: 202 }),
    ),
    http.get("/api/system/jobs/result", () =>
      HttpResponse.json({ job_id: 33, session: "run-a", result: null }),
    ),
  );
  queryClient.setQueryData(
    [QueryKeys.System, QueryKeys.Jobs],
    [{ job_id: 33, status: "completed", session: "run-a" }],
  );
  customRender(<SourceUpgradeButton item={item} />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Upgrade" })).toBeEnabled(),
  );
  await user.click(screen.getByRole("button", { name: "Upgrade" }));
  expect(
    await screen.findByText("The result is no longer available. Run it again."),
  ).toBeVisible();
  expect(screen.queryByText("1 subtitle upgraded.")).not.toBeInTheDocument();
});

it.each(["edited", "replaced", "missing"] as const)(
  "excludes the selected historical release when the current subtitle was %s",
  async (condition) => {
    const user = userEvent.setup();
    const writes: Record<string, FormDataEntryValue>[] = [];
    const historical = { ...row, language: null, subtitles_path: null };
    const replacement = {
      ...row,
      action: 4,
      history_id: 20,
      provider: null,
      subs_id: null,
      upgradable: false,
      description: "Uploaded replacement subtitle",
    };
    server.use(
      http.get("/api/sources/items/7", () =>
        HttpResponse.json({
          ...item,
          subtitles: condition === "missing" ? [] : item.subtitles,
        }),
      ),
      http.get("/api/sources/items/7/history", () =>
        HttpResponse.json({
          data:
            condition === "replaced" ? [replacement, historical] : [historical],
          total: condition === "replaced" ? 2 : 1,
        }),
      ),
      http.post("/api/sources/items/7/blacklist", async ({ request }) => {
        writes.push(Object.fromEntries(await request.formData()));
        return new HttpResponse(null, { status: 204 });
      }),
    );
    customRender(<SourceHistoryView item={item} />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Exclude" })).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "Exclude" }));
    const confirm = await screen.findByRole("dialog", {
      name: "Exclude release from search?",
    });
    expect(writes).toEqual([]);
    await user.click(
      within(confirm).getByRole("button", { name: "Exclude release" }),
    );
    expect(
      await screen.findByText(
        "Release excluded from search. No subtitle file was deleted.",
      ),
    ).toBeVisible();
    expect(writes).toEqual([
      {
        delete_file: "false",
        history_id: "17",
        provider: "source-provider",
        subs_id: "source-release",
      },
    ]);
    if (condition === "replaced") {
      expect(screen.getByText("Uploaded replacement subtitle")).toBeVisible();
    }
  },
);

it("does not silently exclude different release metadata after confirmation", async () => {
  const user = userEvent.setup();
  let changed = false;
  const writes: Record<string, FormDataEntryValue>[] = [];
  server.use(
    http.get("/api/sources/items/7/history", () =>
      HttpResponse.json({
        data: [
          { ...row, subs_id: changed ? "replacement-release" : row.subs_id },
        ],
        total: 1,
      }),
    ),
    http.post("/api/sources/items/7/blacklist", async ({ request }) => {
      writes.push(Object.fromEntries(await request.formData()));
      return new HttpResponse(null, { status: 204 });
    }),
  );
  customRender(<SourceHistoryView item={item} />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Exclude" })).toBeEnabled(),
  );
  await user.click(screen.getByRole("button", { name: "Exclude" }));
  const confirm = await screen.findByRole("dialog", {
    name: "Exclude release from search?",
  });
  changed = true;
  await user.click(
    within(confirm).getByRole("button", { name: "Exclude release" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    /no longer|changed|unavailable/i,
  );
  expect(writes).toEqual([]);
  expect(
    screen.queryByText(
      "Release excluded from search. No subtitle file was deleted.",
    ),
  ).not.toBeInTheDocument();
});

it("never reuses release metadata from a colliding item or a different language variant", () => {
  expect(sourceReleaseForSubtitle([row], item, subtitle)).toBe(row);
  expect(
    sourceReleaseForSubtitle([{ ...row, source_id: 8 }], item, subtitle),
  ).toBeUndefined();
  expect(
    sourceReleaseForSubtitle(
      [{ ...row, item_kind: "episode" }],
      item,
      subtitle,
    ),
  ).toBeUndefined();
  expect(
    sourceReleaseForSubtitle([row], item, { ...subtitle, hi: false }),
  ).toBeUndefined();
  expect(
    sourceReleaseForSubtitle(
      [{ ...row, action: 4, history_id: 19 }, row],
      item,
      subtitle,
    ),
  ).toBeUndefined();
  expect(
    sourceReleaseForSubtitle(
      [{ ...row, action: 0, history_id: 19 }, row],
      item,
      subtitle,
    ),
  ).toBeUndefined();
  for (const action of [5, 10]) {
    expect(
      sourceReleaseForSubtitle(
        [
          { ...row, action, history_id: 19, provider: null, subs_id: null },
          row,
        ],
        item,
        subtitle,
      ),
    ).toBeUndefined();
  }
});
