import { fireEvent,render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import DetailPane from "@/pages/SubtitleEditor/DetailPane";
import type { Cue } from "@/pages/SubtitleEditor/types";

const noop = () => { /* noop */ };

function makeCue(overrides: Partial<Cue> = {}): Cue {
  return {
    id: "cue-1",
    startMs: 5000,
    endMs: 8000,
    text: "Hello world",
    ...overrides,
  };
}

const defaultProps = {
  cue: null as Cue | null,
  cueIndex: 0,
  onTextChange: noop,
  onTimingChange: noop,
  onAdvance: noop,
};

describe("DetailPane", () => {
  describe("empty state (cue is null)", () => {
    it("shows placeholder text when no cue is selected", () => {
      render(<DetailPane {...defaultProps} />);

      expect(
        screen.getByPlaceholderText(
          /No cue at current position/,
        ),
      ).toBeInTheDocument();
    });

    it("shows 'No Cue Selected' label", () => {
      render(<DetailPane {...defaultProps} />);

      expect(screen.getByText("No Cue Selected")).toBeInTheDocument();
    });

    it("renders read-only timing fields based on currentTimeMs", () => {
      render(<DetailPane {...defaultProps} currentTimeMs={61500} />);

      const startInput = screen.getByTitle("Start time (no cue selected)");
      expect(startInput).toHaveValue("00:01:01.500");

      const endInput = screen.getByTitle("End time (no cue selected)");
      // currentTimeMs + 3000 = 64500
      expect(endInput).toHaveValue("00:01:04.500");
    });
  });

  describe("with cue provided", () => {
    it("renders timing fields with correct formatted values", () => {
      const cue = makeCue({ startMs: 3661500, endMs: 3665000 });
      render(<DetailPane {...defaultProps} cue={cue} />);

      const startInput = screen.getByTitle("Start time");
      expect(startInput).toHaveValue("01:01:01.500");

      const endInput = screen.getByTitle("End time");
      expect(endInput).toHaveValue("01:01:05.000");
    });

    it("renders textarea with cue text", () => {
      const cue = makeCue({ text: "Test subtitle line" });
      render(<DetailPane {...defaultProps} cue={cue} />);

      expect(screen.getByDisplayValue("Test subtitle line")).toBeInTheDocument();
    });

    it("shows cue number label", () => {
      const cue = makeCue();
      render(<DetailPane {...defaultProps} cue={cue} cueIndex={4} />);

      expect(screen.getByText("Cue #5 Text")).toBeInTheDocument();
    });

    it("displays duration in seconds", () => {
      const cue = makeCue({ startMs: 1000, endMs: 3500 });
      render(<DetailPane {...defaultProps} cue={cue} />);

      expect(screen.getByText("2.500s")).toBeInTheDocument();
    });
  });

  describe("CPS calculation strips HTML tags", () => {
    it("calculates CPS from plain text, ignoring tags", () => {
      // "Hello" is 5 chars, duration 2.5s, CPS = 2.0
      const cue = makeCue({
        text: "<i>Hello</i>",
        startMs: 0,
        endMs: 2500,
      });
      render(<DetailPane {...defaultProps} cue={cue} />);

      expect(screen.getByText("2.0")).toBeInTheDocument();
    });

    it("shows -- when duration is zero", () => {
      const cue = makeCue({ startMs: 1000, endMs: 1000, text: "test" });
      render(<DetailPane {...defaultProps} cue={cue} />);

      // Two "--" spans exist in the CPS display area
      const dashes = screen.getAllByText("--");
      expect(dashes.length).toBeGreaterThanOrEqual(1);
    });
  });

  describe("line length calculation strips HTML tags", () => {
    it("shows line lengths without counting HTML tags", () => {
      // Line 1: "<b>Hi</b>" -> plain "Hi" = 2 chars
      // Line 2: "<i>World</i>" -> plain "World" = 5 chars
      const cue = makeCue({ text: "<b>Hi</b>\n<i>World</i>" });
      render(<DetailPane {...defaultProps} cue={cue} />);

      expect(screen.getByText("L1: 2")).toBeInTheDocument();
      expect(screen.getByText("L2: 5")).toBeInTheDocument();
    });

    it("flags lines over 42 characters", () => {
      const longLine = "A".repeat(43);
      const cue = makeCue({ text: longLine });
      render(<DetailPane {...defaultProps} cue={cue} />);

      const lineIndicator = screen.getByText("L1: 43");
      // The color should be red (#EF4444) for lines > 42
      expect(lineIndicator).toHaveStyle({ color: "#EF4444" });
    });
  });

  describe("reference text display", () => {
    it("shows reference section when referenceText prop is provided", () => {
      const cue = makeCue();
      render(
        <DetailPane
          {...defaultProps}
          cue={cue}
          referenceText="Original text here"
        />,
      );

      expect(screen.getByText("Reference")).toBeInTheDocument();
      expect(screen.getByText("Original text here")).toBeInTheDocument();
    });

    it("shows fallback when referenceText is empty string", () => {
      const cue = makeCue();
      render(
        <DetailPane {...defaultProps} cue={cue} referenceText="" />,
      );

      expect(
        screen.getByText("No reference for this cue"),
      ).toBeInTheDocument();
    });

    it("shows reference in empty state with no cue", () => {
      render(
        <DetailPane
          {...defaultProps}
          referenceText="Some reference"
        />,
      );

      expect(screen.getByText("Some reference")).toBeInTheDocument();
    });

    it("shows Use button when referenceText and onUseReference are provided", () => {
      const cue = makeCue();
      const onUseReference = vi.fn();
      render(
        <DetailPane
          {...defaultProps}
          cue={cue}
          referenceText="Ref text"
          onUseReference={onUseReference}
        />,
      );

      const useBtn = screen.getByTitle("Use reference text");
      fireEvent.click(useBtn);
      expect(onUseReference).toHaveBeenCalledOnce();
    });

    it("shows AI translate button when onTranslateLine is provided", () => {
      const cue = makeCue();
      const onTranslateLine = vi.fn();
      render(
        <DetailPane
          {...defaultProps}
          cue={cue}
          referenceText="Ref"
          onTranslateLine={onTranslateLine}
        />,
      );

      const aiBtn = screen.getByTitle("Translate this cue");
      expect(aiBtn).toHaveTextContent("AI");
      fireEvent.click(aiBtn);
      expect(onTranslateLine).toHaveBeenCalledOnce();
    });

    it("disables AI button and shows ... when translatingLine is true", () => {
      const cue = makeCue();
      render(
        <DetailPane
          {...defaultProps}
          cue={cue}
          referenceText="Ref"
          onTranslateLine={noop}
          translatingLine={true}
        />,
      );

      const aiBtn = screen.getByTitle("Translate this cue");
      expect(aiBtn).toBeDisabled();
      expect(aiBtn).toHaveTextContent("...");
    });
  });

  describe("style buttons", () => {
    it("renders all style and symbol buttons", () => {
      const cue = makeCue();
      render(<DetailPane {...defaultProps} cue={cue} />);

      expect(screen.getByTitle("Italic (Ctrl+I)")).toBeInTheDocument();
      expect(screen.getByTitle(/Bold/)).toBeInTheDocument();
      expect(screen.getByTitle("Underline")).toBeInTheDocument();
      expect(screen.getByTitle("Music note")).toBeInTheDocument();
      expect(screen.getByTitle("Ellipsis")).toBeInTheDocument();
      expect(screen.getByTitle("Em dash")).toBeInTheDocument();
      expect(screen.getByTitle("Inverted question mark")).toBeInTheDocument();
      expect(screen.getByTitle("Inverted exclamation")).toBeInTheDocument();
    });

    it("italic button has italic font style", () => {
      const cue = makeCue();
      render(<DetailPane {...defaultProps} cue={cue} />);

      const btn = screen.getByTitle("Italic (Ctrl+I)");
      expect(btn).toHaveStyle({ fontStyle: "italic" });
    });

    it("underline button has underline text decoration", () => {
      const cue = makeCue();
      render(<DetailPane {...defaultProps} cue={cue} />);

      const btn = screen.getByTitle("Underline");
      expect(btn).toHaveStyle({ textDecoration: "underline" });
    });
  });

  describe("wrapSelection", () => {
    it("wraps selected text with tags when bold button is clicked", async () => {
      const onTextChange = vi.fn();
      const cue = makeCue({ text: "Hello world" });
      render(
        <DetailPane {...defaultProps} cue={cue} onTextChange={onTextChange} />,
      );

      const textarea = screen.getByDisplayValue("Hello world") as HTMLTextAreaElement;

      // Simulate selecting "world" (index 6 to 11)
      textarea.setSelectionRange(6, 11);

      const boldBtn = screen.getByTitle(/Bold/);
      fireEvent.click(boldBtn);

      expect(onTextChange).toHaveBeenCalledWith("Hello <b>world</b>");
    });

    it("wraps empty selection (inserts empty tags at cursor)", () => {
      const onTextChange = vi.fn();
      const cue = makeCue({ text: "Hello" });
      render(
        <DetailPane {...defaultProps} cue={cue} onTextChange={onTextChange} />,
      );

      const textarea = screen.getByDisplayValue("Hello") as HTMLTextAreaElement;
      textarea.setSelectionRange(5, 5);

      const italicBtn = screen.getByTitle("Italic (Ctrl+I)");
      fireEvent.click(italicBtn);

      expect(onTextChange).toHaveBeenCalledWith("Hello<i></i>");
    });
  });

  describe("insertAtCursor", () => {
    it("inserts character at cursor position", () => {
      const onTextChange = vi.fn();
      const cue = makeCue({ text: "Hello world" });
      render(
        <DetailPane {...defaultProps} cue={cue} onTextChange={onTextChange} />,
      );

      const textarea = screen.getByDisplayValue("Hello world") as HTMLTextAreaElement;
      textarea.setSelectionRange(5, 5);

      const ellipsisBtn = screen.getByTitle("Ellipsis");
      fireEvent.click(ellipsisBtn);

      expect(onTextChange).toHaveBeenCalledWith("Hello\u2026 world");
    });

    it("inserts music note with trailing space", () => {
      const onTextChange = vi.fn();
      const cue = makeCue({ text: "Song" });
      render(
        <DetailPane {...defaultProps} cue={cue} onTextChange={onTextChange} />,
      );

      const textarea = screen.getByDisplayValue("Song") as HTMLTextAreaElement;
      textarea.setSelectionRange(0, 0);

      const musicBtn = screen.getByTitle("Music note");
      fireEvent.click(musicBtn);

      expect(onTextChange).toHaveBeenCalledWith("\u266A Song");
    });
  });

  describe("timing input interactions", () => {
    it("calls onTimingChange when start time is edited and blurred", async () => {
      const onTimingChange = vi.fn();
      const cue = makeCue({ startMs: 5000, endMs: 8000 });
      const user = userEvent.setup();

      render(
        <DetailPane
          {...defaultProps}
          cue={cue}
          onTimingChange={onTimingChange}
        />,
      );

      const startInput = screen.getByTitle("Start time");
      await user.clear(startInput);
      await user.type(startInput, "00:00:06.000");
      await user.tab();

      expect(onTimingChange).toHaveBeenCalledWith(6000, 8000);
    });

    it("calls onTimingChange when end time is edited and blurred", async () => {
      const onTimingChange = vi.fn();
      const cue = makeCue({ startMs: 5000, endMs: 8000 });
      const user = userEvent.setup();

      render(
        <DetailPane
          {...defaultProps}
          cue={cue}
          onTimingChange={onTimingChange}
        />,
      );

      const endInput = screen.getByTitle("End time");
      await user.clear(endInput);
      await user.type(endInput, "00:00:10.000");
      await user.tab();

      expect(onTimingChange).toHaveBeenCalledWith(5000, 10000);
    });
  });

  describe("text editing", () => {
    it("calls onTextChange as user types", async () => {
      const onTextChange = vi.fn();
      const cue = makeCue({ text: "" });
      const user = userEvent.setup();

      render(
        <DetailPane {...defaultProps} cue={cue} onTextChange={onTextChange} />,
      );

      // Multiple textboxes exist (timing inputs + textarea), select the textarea specifically
      // eslint-disable-next-line testing-library/no-node-access
      const textarea = document.querySelector("textarea")!;

      await user.type(textarea, "Hi");

      expect(onTextChange).toHaveBeenCalled();
    });
  });
});
