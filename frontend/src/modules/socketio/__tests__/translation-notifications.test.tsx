import { MantineProvider } from "@mantine/core";
import { Notifications, notifications } from "@mantine/notifications";
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createDefaultReducer } from "@/modules/socketio/reducer";

const progress = createDefaultReducer().find(
  (entry) => entry.key === "progress",
)!;
const update = progress.update as (items: Site.Progress[]) => void;
const remove = progress.delete as (ids: string[]) => void;

beforeEach(() => {
  vi.useFakeTimers();
  notifications.clean();
});

function renderManager() {
  render(
    <MantineProvider>
      <Notifications position="bottom-left" limit={5} />
    </MantineProvider>,
  );
}

afterEach(() => {
  act(() => notifications.clean());
  vi.clearAllTimers();
  vi.useRealTimers();
});

const translation = (id: number, value = 100): Site.Progress => ({
  id: `translate_progress_operation-${id}`,
  header: `Translation ${id}`,
  name: "Same destination.srt",
  value,
  count: 100,
});

describe("translation notifications in the real manager", () => {
  it("keeps four 100-percent active operations visible until their own terminal events", async () => {
    renderManager();
    act(() => update([1, 2, 3, 4].map((id) => translation(id))));
    await act(() => vi.advanceTimersByTimeAsync(15000));
    for (const id of [1, 2, 3, 4]) {
      expect(
        screen.getByText(`Translation ${id}`, { exact: true }),
      ).toBeVisible();
    }
    expect(screen.queryByText("All Tasks Completed")).not.toBeInTheDocument();
    expect(screen.getAllByText("[100/100] Same destination.srt")).toHaveLength(
      4,
    );

    act(() => remove([translation(2).id]));
    await act(() => vi.advanceTimersByTimeAsync(11000));
    expect(
      screen.queryByText("Translation 2", { exact: true }),
    ).not.toBeInTheDocument();
    for (const id of [1, 3, 4]) {
      expect(
        screen.getByText(`Translation ${id}`, { exact: true }),
      ).toBeVisible();
    }
    act(() => remove([1, 3, 4].map((id) => translation(id).id)));
    await act(() => vi.advanceTimersByTimeAsync(11000));
    expect(screen.queryByText(/Translation [134]/)).not.toBeInTheDocument();
  });

  it("updates only its own operation and preserves ordinary completion", async () => {
    renderManager();
    act(() => update([translation(1, 30), translation(2, 30)]));
    act(() => update([{ ...translation(1), name: "Finalizing first job" }]));
    expect(screen.getByText("[100/100] Finalizing first job")).toBeVisible();
    expect(screen.getByText("[30/100] Same destination.srt")).toBeVisible();
    act(() =>
      update([
        {
          id: "ordinary-task",
          header: "Library sync",
          name: "Done",
          value: 1,
          count: 1,
        },
      ]),
    );
    expect(screen.getByText("All Tasks Completed")).toBeVisible();
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(screen.queryByText("Library sync")).not.toBeInTheDocument();
    expect(screen.getByText("Translation 1", { exact: true })).toBeVisible();
    expect(screen.getByText("Translation 2", { exact: true })).toBeVisible();
  });
});
