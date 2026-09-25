/* eslint-disable camelcase -- API fixtures use the server's field names. */

import { focusManager } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
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

// A server that pages and filters like the real one, and remembers every
// query it was sent.
function serveLogs(
  all: System.Log[],
  extra: Partial<System.LogPage> = {},
): URLSearchParams[] {
  const requests: URLSearchParams[] = [];
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", debug: false },
        log: {
          include_filter: "",
          exclude_filter: "",
          ignore_case: false,
          use_regex: false,
        },
      }),
    ),
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
          (!level || RANK[entry.type] >= RANK[level.toUpperCase()]) &&
          (!contains || entry.message.toLowerCase().includes(contains)),
      );
      return HttpResponse.json({
        data: matching.slice(offset, offset + limit),
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
