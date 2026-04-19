/* eslint-disable testing-library/no-container, testing-library/no-node-access */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import StatusBar from "@/pages/SubtitleEditor/StatusBar";
import type { Cue } from "@/pages/SubtitleEditor/types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeCue(overrides: Partial<Cue> = {}): Cue {
  return {
    id: "cue-1",
    startMs: 0,
    endMs: 2000,
    text: "Hello world",
    ...overrides,
  };
}

const defaultProps = {
  cueIndex: 0,
  cueCount: 10,
  format: "srt",
  encoding: "UTF-8",
  dirty: false,
  selectedCue: null,
  multiSelectCount: 0,
  bookmarkCount: 0,
  totalDurationMs: 60000,
};

// ---------------------------------------------------------------------------
// computeCps logic (tested indirectly through rendered output)
// ---------------------------------------------------------------------------
describe("CPS computation", () => {
  it("strips HTML tags before counting characters", () => {
    // "<b>Hi</b>" has 2 visible chars. Duration 1s => 2.0 CPS
    const cue = makeCue({
      text: "<b>Hi</b>",
      startMs: 0,
      endMs: 1000,
    });
    const { container } = render(
      <StatusBar {...defaultProps} selectedCue={cue} />,
    );
    expect(container.textContent).toContain("2.0 CPS");
  });

  it("returns null (-- CPS) for zero-duration cues", () => {
    const cue = makeCue({ startMs: 5000, endMs: 5000 });
    const { container } = render(
      <StatusBar {...defaultProps} selectedCue={cue} />,
    );
    expect(container.textContent).toContain("-- CPS");
  });

  it("returns null (-- CPS) for negative-duration cues", () => {
    const cue = makeCue({ startMs: 5000, endMs: 3000 });
    const { container } = render(
      <StatusBar {...defaultProps} selectedCue={cue} />,
    );
    expect(container.textContent).toContain("-- CPS");
  });

  it("shows green color for CPS < 18", () => {
    // 10 chars in 1 second = 10.0 CPS (green)
    const cue = makeCue({ text: "0123456789", startMs: 0, endMs: 1000 });
    const { container } = render(
      <StatusBar {...defaultProps} selectedCue={cue} />,
    );
    const cpsSpan = Array.from(container.querySelectorAll("span")).find((el) =>
      el.textContent?.includes("10.0 CPS"),
    );
    expect(cpsSpan).toBeDefined();
    expect(cpsSpan!.style.color).toBe("rgb(34, 197, 94)");
  });

  it("shows yellow color for CPS between 18 and 25", () => {
    // 20 chars in 1 second = 20.0 CPS (yellow)
    const cue = makeCue({
      text: "01234567890123456789",
      startMs: 0,
      endMs: 1000,
    });
    const { container } = render(
      <StatusBar {...defaultProps} selectedCue={cue} />,
    );
    const cpsSpan = Array.from(container.querySelectorAll("span")).find((el) =>
      el.textContent?.includes("20.0 CPS"),
    );
    expect(cpsSpan).toBeDefined();
    expect(cpsSpan!.style.color).toBe("rgb(251, 191, 36)");
  });

  it("shows red color for CPS > 25", () => {
    // 30 chars in 1 second = 30.0 CPS (red)
    const cue = makeCue({
      text: "012345678901234567890123456789",
      startMs: 0,
      endMs: 1000,
    });
    const { container } = render(
      <StatusBar {...defaultProps} selectedCue={cue} />,
    );
    const cpsSpan = Array.from(container.querySelectorAll("span")).find((el) =>
      el.textContent?.includes("30.0 CPS"),
    );
    expect(cpsSpan).toBeDefined();
    expect(cpsSpan!.style.color).toBe("rgb(239, 68, 68)");
  });

  it("does not render CPS when no cue is selected", () => {
    const { container } = render(
      <StatusBar {...defaultProps} selectedCue={null} />,
    );
    expect(container.textContent).not.toContain("CPS");
  });
});

// ---------------------------------------------------------------------------
// Cue index display
// ---------------------------------------------------------------------------
describe("Cue index display", () => {
  it("shows 1-based cue index and total count", () => {
    const { container } = render(
      <StatusBar {...defaultProps} cueIndex={4} cueCount={20} />,
    );
    expect(container.textContent).toContain("Cue 5/20");
  });

  it("shows dash when cueIndex is -1 (no selection)", () => {
    const { container } = render(
      <StatusBar {...defaultProps} cueIndex={-1} cueCount={10} />,
    );
    expect(container.textContent).toContain("Cue -/10");
  });

  it("shows Cue 1/1 for single-cue documents", () => {
    const { container } = render(
      <StatusBar {...defaultProps} cueIndex={0} cueCount={1} />,
    );
    expect(container.textContent).toContain("Cue 1/1");
  });
});

// ---------------------------------------------------------------------------
// Dirty indicator
// ---------------------------------------------------------------------------
describe("Dirty indicator", () => {
  it("shows Unsaved when dirty is true", () => {
    const { container } = render(<StatusBar {...defaultProps} dirty={true} />);
    expect(container.textContent).toContain("Unsaved");
  });

  it("does not show Unsaved when dirty is false", () => {
    const { container } = render(<StatusBar {...defaultProps} dirty={false} />);
    expect(container.textContent).not.toContain("Unsaved");
  });
});

// ---------------------------------------------------------------------------
// Bookmark count
// ---------------------------------------------------------------------------
describe("Bookmark count", () => {
  it("shows bookmark count when > 0", () => {
    const { container } = render(
      <StatusBar {...defaultProps} bookmarkCount={3} />,
    );
    expect(container.textContent).toContain("3 bookmarked");
  });

  it("hides bookmark count when 0", () => {
    const { container } = render(
      <StatusBar {...defaultProps} bookmarkCount={0} />,
    );
    expect(container.textContent).not.toContain("bookmarked");
  });
});

// ---------------------------------------------------------------------------
// Multi-select count
// ---------------------------------------------------------------------------
describe("Multi-select count", () => {
  it("shows selected count when > 0", () => {
    const { container } = render(
      <StatusBar {...defaultProps} multiSelectCount={5} />,
    );
    expect(container.textContent).toContain("5 selected");
  });

  it("hides selected count when 0", () => {
    const { container } = render(
      <StatusBar {...defaultProps} multiSelectCount={0} />,
    );
    expect(container.textContent).not.toContain("selected");
  });
});

// ---------------------------------------------------------------------------
// Format and encoding display
// ---------------------------------------------------------------------------
describe("Format and encoding display", () => {
  it("displays format in uppercase", () => {
    const { container } = render(<StatusBar {...defaultProps} format="srt" />);
    expect(container.textContent).toContain("SRT");
  });

  it("displays encoding as-is", () => {
    const { container } = render(
      <StatusBar {...defaultProps} encoding="ISO-8859-1" />,
    );
    expect(container.textContent).toContain("ISO-8859-1");
  });

  it("has the correct ARIA role and label", () => {
    render(<StatusBar {...defaultProps} />);
    const statusBar = screen.getByRole("status");
    expect(statusBar).toBeDefined();
    expect(statusBar.getAttribute("aria-label")).toBe("Editor status");
  });
});
