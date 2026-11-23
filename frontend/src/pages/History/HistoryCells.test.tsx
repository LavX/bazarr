/* eslint-disable camelcase -- API field names. */
/* eslint-disable testing-library/no-node-access -- Browser focus is the behavior under test. */
import { BrowserRouter } from "react-router";
import { MantineProvider } from "@mantine/core";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import {
  HistoryAction,
  historyAnchors,
  HistoryUpgrade,
} from "@/pages/History/HistoryCells";

const original = {
  action: 1,
  blacklisted: false,
  description: "Original download",
  dont_matches: [],
  history_id: 7,
  matches: [],
  parsed_timestamp: "November 23, 2026",
  subtitles_path: "/archive/original.en.srt",
  tags: [],
  timestamp: "Today",
  upgradable: false,
  origin: "source",
  source_key: "source:8",
  source_id: 8,
  source_kind: "folder",
  source_name: "Archive",
  id: 31,
  item_id: 31,
  item_title: "Archive original",
  group_title: null,
  video_path: "/archive/original.mkv",
} satisfies History.MovieRow;

const upgrade: History.MovieRow = {
  ...original,
  action: 3,
  history_id: 12,
  original: {
    action: original.action,
    description: original.description,
    history_id: original.history_id,
    origin: original.origin,
    provider: "subdl",
    source_key: original.source_key,
    timestamp_iso: "2026-11-23T08:00:00",
  },
};
const query = "?page=2&source=arr%3A5&source=source%3A8";

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

describe.each(["/", "/bazarr/"])("History original links under %s", (base) => {
  function showHistory(kind = "series") {
    const current = `${base}history/${kind}${query}#kept-fragment`;
    window.history.replaceState(null, "", current);
    render(
      <>
        <base href={base} />
        <MantineProvider>
          <BrowserRouter basename={base}>
            <table>
              <tbody>
                <tr>
                  <td>
                    <HistoryUpgrade
                      row={upgrade}
                      anchors={historyAnchors([upgrade, original])}
                    />
                  </td>
                </tr>
                <tr>
                  <td>
                    <HistoryAction row={original} />
                  </td>
                  <td>Archive original</td>
                </tr>
              </tbody>
            </table>
          </BrowserRouter>
        </MantineProvider>
      </>,
    );
    expect(new URL(document.baseURI).pathname).toBe(base);
    return current;
  }

  it.each(["series", "movies"])(
    "focuses and scrolls the original without changing the %s route or query",
    async (kind) => {
      const user = userEvent.setup();
      const scrollIntoView = vi.spyOn(HTMLElement.prototype, "scrollIntoView");
      const current = showHistory(kind);
      const link = screen.getByRole("link", { name: "Original entry" });

      await user.click(link);
      expect(
        window.location.pathname +
          window.location.search +
          window.location.hash,
      ).toBe(current);
      expect(document.activeElement).toHaveAttribute("id", "history-source-7");
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "center" });
      expect(scrollIntoView.mock.contexts).toEqual([document.activeElement]);
      expect(link).toHaveAttribute(
        "href",
        `${base}history/${kind}${query}#history-source-7`,
      );

      scrollIntoView.mockClear();
      link.focus();
      await user.keyboard("{Enter}");
      expect(document.activeElement).toHaveAttribute("id", "history-source-7");
      expect(scrollIntoView.mock.contexts).toEqual([document.activeElement]);
      expect(
        window.location.pathname +
          window.location.search +
          window.location.hash,
      ).toBe(current);
    },
  );

  it.each(["ctrlKey", "metaKey", "shiftKey", "altKey"])(
    "preserves native link handling with %s",
    (modifier) => {
      const scrollIntoView = vi.spyOn(HTMLElement.prototype, "scrollIntoView");
      showHistory();
      const observed = vi.fn((event: MouseEvent) => {
        expect(event.defaultPrevented).toBe(false);
        // Keep jsdom from attempting the native navigation after observing it.
        event.preventDefault();
      });
      document.addEventListener("click", observed, { once: true });
      fireEvent.click(screen.getByRole("link", { name: "Original entry" }), {
        [modifier]: true,
      });
      expect(observed).toHaveBeenCalledTimes(1);
      expect(scrollIntoView).not.toHaveBeenCalled();
    },
  );
});
