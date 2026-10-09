/* eslint-disable camelcase -- API fixtures use the server's field names. */

import { focusManager } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { pickOption } from "@/pages/Discover/selectTestHelpers";
import { act, customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import SystemLogsView from ".";

const LEVELS: System.LogType[] = [
  "DEBUG",
  "INFO",
  "WARNING",
  "ERROR",
  "CRITICAL",
];
const RANK: Record<string, number> = {
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
};

// Newest first, as the server sends them. Every tenth is a whisperai line.
function entries(count: number): System.Log[] {
  return Array.from({ length: count }, (_, index) => {
    const number = count - 1 - index;
    return {
      timestamp: `2026-09-25 10:00:${String(number % 60).padStart(2, "0")}`,
      type: LEVELS[number % 5],
      message:
        number % 10 === 0
          ? `Provider 'whisperai' is discarded ${number}`
          : `record ${number}`,
      exception: null,
    };
  });
}

interface StoredFilter {
  include_filter: string;
  exclude_filter: string;
  ignore_case: boolean;
  use_regex: boolean;
}

// A server that pages and filters like the real one, and remembers every
// query it was sent. The stored filter is what the Filter modal saves, and the
// server applies it to every read, as the real one does.
function serveLogs(
  all: System.Log[],
  extra: Partial<System.LogPage> = {},
  general: Record<string, unknown> = {},
  stored: StoredFilter = {
    include_filter: "",
    exclude_filter: "",
    ignore_case: false,
    use_regex: false,
  },
): URLSearchParams[] {
  const requests: URLSearchParams[] = [];
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", debug: false, ...general },
        log: { ...stored },
      }),
    ),
    http.post("/api/system/settings", async ({ request }) => {
      const form = await request.formData();
      const include = form.get("settings-log-include_filter");
      if (typeof include === "string") {
        stored.include_filter = include;
      }
      return new HttpResponse(null, { status: 204 });
    }),
    http.delete("/api/system/logs", () => {
      // Emptying leaves the one line the server writes to say so.
      all.splice(0, all.length, {
        timestamp: "2026-09-25 12:00:00",
        type: "INFO",
        message: "BAZARR Log file emptied",
        exception: null,
      });
      return new HttpResponse(null, { status: 204 });
    }),
    http.get("/api/system/status", () =>
      HttpResponse.json({ data: { bazarr_version: "unknown" } }),
    ),
    http.get("/api/system/logs", ({ request }) => {
      const params = new URL(request.url).searchParams;
      requests.push(params);
      const limit = Number(params.get("limit"));
      const offset = Number(params.get("offset"));
      const level = params.get("level");
      const contains = params.get("contains")?.toLowerCase() ?? "";
      const matching = all.filter(
        (entry) =>
          (!stored.include_filter ||
            entry.message.includes(stored.include_filter)) &&
          (!level || RANK[entry.type] >= RANK[level.toUpperCase()]) &&
          (!contains || entry.message.toLowerCase().includes(contains)),
      );
      // Entries that arrived since the baseline are skipped, as the server does.
      const baseline = params.get("baseline_total");
      const skip =
        baseline === null ? 0 : Math.max(0, matching.length - Number(baseline));
      return HttpResponse.json({
        data: matching.slice(offset + skip, offset + skip + limit),
        total: matching.length,
        offset,
        limit,
        filter_errors: [],
        ...extra,
      });
    }),
  );
  return requests;
}

const last = (requests: URLSearchParams[]) => requests[requests.length - 1];

function refocusWindow() {
  act(() => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
}

describe("System Logs", () => {
  afterEach(() => {
    focusManager.setFocused(undefined);
    vi.restoreAllMocks();
  });

  it("asks for one page, newest first, and shows the total", async () => {
    const requests = serveLogs(entries(120));
    customRender(<SystemLogsView />);

    expect(await screen.findByText("record 119")).toBeInTheDocument();
    expect(screen.getByText("Show 1 to 50 of 120 entries")).toBeInTheDocument();
    expect(screen.queryByText("record 69")).not.toBeInTheDocument();

    expect(last(requests).get("limit")).toBe("50");
    expect(last(requests).get("offset")).toBe("0");
    expect(last(requests).has("level")).toBe(false);
    expect(last(requests).has("contains")).toBe(false);
  });

  it("asks for pages of the size set in the settings", async () => {
    const requests = serveLogs(entries(120), {}, { page_size: 25 });
    customRender(<SystemLogsView />);

    expect(
      await screen.findByText("Show 1 to 25 of 120 entries"),
    ).toBeInTheDocument();
    expect(last(requests).get("limit")).toBe("25");
    expect(screen.queryByText("record 94")).not.toBeInTheDocument();
  });

  it("holds an older page still while new entries arrive", async () => {
    const all = entries(120);
    const requests = serveLogs(all);
    const user = userEvent.setup();
    customRender(<SystemLogsView />);
    await screen.findByText("record 119");

    await user.click(screen.getByRole("button", { name: "2" }));
    await screen.findByText("record 69");
    expect(last(requests).get("baseline_total")).toBe("120");

    // Seven lines are written while the reader is on page two.
    all.unshift(
      ...Array.from({ length: 7 }, (_, index) => ({
        timestamp: "2026-09-25 11:00:00",
        type: "INFO" as System.LogType,
        message: `arrived ${index}`,
        exception: null,
      })),
    );
    await user.click(screen.getByRole("button", { name: "3" }));

    // Page three carries on from page two instead of repeating seven rows.
    expect(await screen.findByText("record 19")).toBeInTheDocument();
    expect(screen.queryByText("record 26")).not.toBeInTheDocument();
    expect(last(requests).get("baseline_total")).toBe("120");
    expect(last(requests).get("offset")).toBe("100");
    expect(
      screen.getByText("Show 101 to 120 of 120 entries"),
    ).toBeInTheDocument();

    // Back on the newest page, which refreshes itself, the new lines are there.
    await user.click(screen.getByRole("button", { name: "1" }));
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByText("arrived 0")).toBeInTheDocument();
    expect(last(requests).has("baseline_total")).toBe(false);
  });

  it("says so when the log cannot be loaded", async () => {
    serveLogs([]);
    server.use(
      http.get("/api/system/logs", () =>
        HttpResponse.json({ message: "broken" }, { status: 500 }),
      ),
    );
    customRender(<SystemLogsView />);

    // In the alert above the table, and in the table where the rows would be.
    expect(
      await screen.findAllByText("The log could not be loaded"),
    ).toHaveLength(2);
    expect(screen.queryByText("The log is empty")).not.toBeInTheDocument();
  });

  it("pages through older entries one request at a time", async () => {
    const requests = serveLogs(entries(120));
    const user = userEvent.setup();
    customRender(<SystemLogsView />);
    await screen.findByText("record 119");

    await user.click(screen.getByRole("button", { name: "2" }));

    expect(await screen.findByText("record 69")).toBeInTheDocument();
    expect(screen.queryByText("record 119")).not.toBeInTheDocument();
    expect(last(requests).get("offset")).toBe("50");
    expect(last(requests).get("limit")).toBe("50");
  });

  it("filters by text typed on the page, starting again from the newest", async () => {
    const requests = serveLogs(entries(120));
    const user = userEvent.setup();
    customRender(<SystemLogsView />);
    await screen.findByText("record 119");
    await user.click(screen.getByRole("button", { name: "2" }));
    await screen.findByText("record 69");

    const filter = screen.getByLabelText("Filter log entries");
    await user.type(filter, "WhisperAI");

    expect(filter).toHaveValue("WhisperAI");
    await waitFor(() =>
      expect(last(requests).get("contains")).toBe("WhisperAI"),
    );
    expect(last(requests).get("offset")).toBe("0");
    expect(
      await screen.findByText("Show 1 to 12 of 12 entries"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Provider 'whisperai' is discarded 110"),
    ).toBeInTheDocument();
    expect(screen.queryByText("record 119")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(filter).toHaveValue("");
    expect(await screen.findByText("record 119")).toBeInTheDocument();
    expect(screen.getByText("Show 1 to 50 of 120 entries")).toBeInTheDocument();
  });

  it("keeps typing through a term that already matched", async () => {
    const requests = serveLogs(entries(120));
    const user = userEvent.setup();
    customRender(<SystemLogsView />);
    await screen.findByText("record 119");

    const filter = screen.getByLabelText("Filter log entries");
    // "record 1" matches, and the typing carries on past it.
    await user.type(filter, "record 11");
    await waitFor(() =>
      expect(last(requests).get("contains")).toBe("record 11"),
    );
    expect(filter).toHaveValue("record 11");
    expect(
      await screen.findByText("Show 1 to 10 of 10 entries"),
    ).toBeInTheDocument();

    await user.type(filter, "9");
    await waitFor(() =>
      expect(last(requests).get("contains")).toBe("record 119"),
    );
    expect(filter).toHaveValue("record 119");
    expect(
      await screen.findByText("Show 1 to 1 of 1 entries"),
    ).toBeInTheDocument();
  });

  it("filters by minimum level on the page", async () => {
    const requests = serveLogs(entries(120));
    const user = userEvent.setup();
    customRender(<SystemLogsView />);
    await screen.findByText("record 119");

    await pickOption(user, "Minimum level", "Errors and above");

    await waitFor(() => expect(last(requests).get("level")).toBe("error"));
    expect(last(requests).get("offset")).toBe("0");
    expect(
      await screen.findByText("Show 1 to 48 of 48 entries"),
    ).toBeInTheDocument();
    expect(screen.queryByText("record 117")).not.toBeInTheDocument();
    expect(screen.getByText("record 119")).toBeInTheDocument();
  });

  it("refreshes the newest page on focus, and leaves an older page alone", async () => {
    const requests = serveLogs(entries(120));
    const user = userEvent.setup();
    customRender(<SystemLogsView />);
    await screen.findByText("record 119");

    const beforeFocus = requests.length;
    refocusWindow();
    await waitFor(() => expect(requests.length).toBe(beforeFocus + 1));
    expect(last(requests).get("offset")).toBe("0");

    await user.click(screen.getByRole("button", { name: "2" }));
    await screen.findByText("record 69");
    const onOlderPage = requests.length;
    refocusWindow();
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(requests.length).toBe(onOlderPage);
  });

  it("starts again from the newest entry when the stored filter is saved", async () => {
    // Only the plain records match at first, 108 of the 120.
    const stored = {
      include_filter: "record",
      exclude_filter: "",
      ignore_case: false,
      use_regex: false,
    };
    const requests = serveLogs(entries(120), {}, {}, stored);
    const user = userEvent.setup();
    customRender(<SystemLogsView />);
    await screen.findByText("Show 1 to 50 of 108 entries");

    await user.click(screen.getByRole("button", { name: "2" }));
    await screen.findByText("Show 51 to 100 of 108 entries");
    expect(last(requests).get("baseline_total")).toBe("108");

    // Returning nothing hands the save on to the server above, once the
    // number of log reads before it is noted.
    let readsBeforeSave = -1;
    server.use(
      http.post("/api/system/settings", () => {
        readsBeforeSave = requests.length;
      }),
    );

    // Relaxing the stored filter adds twelve older entries. Read against the
    // old baseline they would look new and shift page two by twelve rows. The
    // Filter button carries a badge while a stored filter is set.
    await user.click(screen.getByRole("button", { name: /^Filter/ }));
    await user.clear(await screen.findByLabelText("Include Filter"));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText("Show 1 to 50 of 120 entries"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Provider 'whisperai' is discarded 110"),
    ).toBeInTheDocument();
    // Not one read after the save asks for page two against the old baseline,
    // not even the reload the save itself sets off.
    const afterSave = requests.slice(readsBeforeSave);
    expect(readsBeforeSave).toBeGreaterThan(0);
    expect(afterSave.length).toBeGreaterThan(0);
    for (const params of afterSave) {
      expect(params.get("offset")).toBe("0");
      expect(params.has("baseline_total")).toBe(false);
    }
  });

  it("starts again from the newest entry when the stored filter changes elsewhere", async () => {
    const stored = {
      include_filter: "record",
      exclude_filter: "",
      ignore_case: false,
      use_regex: false,
    };
    const requests = serveLogs(entries(120), {}, {}, stored);
    const user = userEvent.setup();
    customRender(<SystemLogsView />);
    await screen.findByText("Show 1 to 50 of 108 entries");

    await user.click(screen.getByRole("button", { name: "2" }));
    await screen.findByText("Show 51 to 100 of 108 entries");
    expect(last(requests).get("baseline_total")).toBe("108");

    // Settings > General, or a save in another tab, changes the stored filter
    // without this page's Filter modal. Either one reloads the settings the
    // way this does, and only the reloaded filter tells the page.
    stored.include_filter = "";
    act(() => {
      void queryClient.invalidateQueries({ queryKey: [QueryKeys.System] });
    });

    expect(
      await screen.findByText("Show 1 to 50 of 120 entries"),
    ).toBeInTheDocument();
    expect(screen.getByText("record 119")).toBeInTheDocument();
    expect(last(requests).get("offset")).toBe("0");
    expect(last(requests).has("baseline_total")).toBe(false);
  });

  it("goes back to a real page when an older page runs out of entries", async () => {
    const requests = serveLogs(entries(120));
    const user = userEvent.setup();
    customRender(<SystemLogsView />);
    await screen.findByText("record 119");

    await user.type(screen.getByLabelText("Filter log entries"), "record");
    await screen.findByText("Show 1 to 50 of 108 entries");
    await user.click(screen.getByRole("button", { name: "2" }));
    await screen.findByText("Show 51 to 100 of 108 entries");

    // Emptying leaves only the server's own note, which the text filter hides,
    // so nothing matches any more and page two no longer exists.
    await user.click(screen.getByRole("button", { name: "Empty" }));

    expect(
      await screen.findByText("No log entries match these filters"),
    ).toBeInTheDocument();
    await waitFor(() => expect(last(requests).get("offset")).toBe("0"));
    expect(last(requests).get("contains")).toBe("record");
    expect(last(requests).has("baseline_total")).toBe(false);
    expect(screen.getByText("Show 0 to 0 of 0 entries")).toBeInTheDocument();
  });

  it("keeps the reader on an older page whose load failed", async () => {
    const requests = serveLogs(entries(120));
    const user = userEvent.setup();
    customRender(<SystemLogsView />);
    await screen.findByText("record 119");

    server.use(
      http.get("/api/system/logs", ({ request }) => {
        requests.push(new URL(request.url).searchParams);
        return HttpResponse.json({ message: "broken" }, { status: 500 });
      }),
    );
    await user.click(screen.getByRole("button", { name: "2" }));

    // A failed load says so where page two would be, rather than dropping the
    // reader back onto the newest page as if page two no longer existed.
    expect(
      await screen.findAllByText("The log could not be loaded"),
    ).toHaveLength(2);
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(screen.queryByText("record 119")).not.toBeInTheDocument();
    expect(last(requests).get("offset")).toBe("50");

    const beforeRetry = requests.length;
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(requests.length).toBe(beforeRetry + 1));
    expect(last(requests).get("offset")).toBe("50");
  });

  it("says when a stored filter could not be applied", async () => {
    serveLogs(entries(5), {
      filter_errors: [
        {
          filter: "include",
          pattern: "(whisperai",
          message: "missing ), unterminated subpattern at position 0",
        },
      ],
    });
    customRender(<SystemLogsView />);

    expect(
      await screen.findByText("A stored filter is not applied"),
    ).toBeInTheDocument();
    expect(screen.getByText("(whisperai")).toBeInTheDocument();
  });

  it("tells an empty log from a filter that matches nothing", async () => {
    serveLogs([]);
    const user = userEvent.setup();
    customRender(<SystemLogsView />);
    expect(await screen.findByText("The log is empty")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Filter log entries"), "nothing");
    expect(
      await screen.findByText("No log entries match these filters"),
    ).toBeInTheDocument();
  });

  it("still downloads the whole file", async () => {
    serveLogs(entries(3));
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const user = userEvent.setup();
    customRender(<SystemLogsView />);
    await screen.findByText("record 2");

    await user.click(screen.getByRole("button", { name: "Download" }));

    expect(open).toHaveBeenCalledWith(expect.stringMatching(/\/bazarr\.log$/));
  });
});
