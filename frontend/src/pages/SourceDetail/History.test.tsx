/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { customRender, screen, waitFor, within } from "@/tests";
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
    server.use(
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
    customRender(<SourceHistoryView item={item} />);
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

it("excludes the current source release with its language flags and refreshes its history", async () => {
  const user = userEvent.setup();
  const writes: Record<string, FormDataEntryValue>[] = [];
  server.use(
    http.get("/api/sources/items/7/history", () =>
      HttpResponse.json({
        data: [
          {
            ...row,
            history_id: 18,
            action: 10,
            provider: null,
            subs_id: null,
            upgradable: false,
          },
          row,
        ],
        total: 2,
      }),
    ),
    http.get("/api/sources/items/7", () =>
      HttpResponse.json({ ...item, profile_id: null, upgradable: false }),
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
  expect(screen.getByRole("button", { name: "Upgrade" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Exclude" }));
  expect(
    await screen.findByText("Subtitle excluded from search."),
  ).toBeVisible();
  expect(writes).toEqual([
    {
      provider: "source-provider",
      subs_id: "source-release",
      subtitles_path: subtitle.path,
      language: "en",
      hi: "true",
      forced: "false",
    },
  ]);
});

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

it("refuses exclusion when an upload replaced the release after opening history", async () => {
  const user = userEvent.setup();
  let replaced = false;
  const write = vi.fn();
  server.use(
    http.get("/api/sources/items/7/history", () =>
      HttpResponse.json({
        data: replaced ? [{ ...row, action: 4, history_id: 20 }, row] : [row],
        total: replaced ? 2 : 1,
      }),
    ),
    http.post("/api/sources/items/7/blacklist", () => {
      write();
      return new HttpResponse(null, { status: 204 });
    }),
  );
  customRender(<SourceHistoryView item={item} />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Exclude" })).toBeEnabled(),
  );
  replaced = true;
  await user.click(screen.getByRole("button", { name: "Exclude" }));
  expect(
    await screen.findByText(
      "This subtitle is no longer available for exclusion.",
    ),
  ).toBeVisible();
  expect(write).not.toHaveBeenCalled();
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
  expect(
    sourceReleaseForSubtitle(
      [
        { ...row, action: 5, history_id: 19, provider: null, subs_id: null },
        row,
      ],
      item,
      subtitle,
    ),
  ).toBe(row);
});
