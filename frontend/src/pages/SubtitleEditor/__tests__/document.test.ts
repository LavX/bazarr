import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  computeCueWarnings,
  createAddCue,
  createDeleteCues,
  createEditText,
  createEditTiming,
  createInitialDocumentState,
  createMergeCues,
  createSplitCue,
  DEFAULT_QC_PRESET,
  detectGaps,
  type DocumentAction,
  QC_PRESETS,
  subtitleDocumentReducer,
  type SubtitleDocumentState,
} from "@/pages/SubtitleEditor/document";
import type { Cue } from "@/pages/SubtitleEditor/types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let cueCounter = 0;

function makeCue(overrides: Partial<Cue> = {}): Cue {
  cueCounter += 1;
  return {
    id: `cue-${cueCounter}`,
    startMs: 0,
    endMs: 1000,
    text: "Hello world",
    ...overrides,
  };
}

function makeThreeCues(): Cue[] {
  return [
    makeCue({ startMs: 0, endMs: 1000, text: "First" }),
    makeCue({ startMs: 1500, endMs: 3000, text: "Second" }),
    makeCue({ startMs: 3500, endMs: 5000, text: "Third" }),
  ];
}

function applyAction(
  state: SubtitleDocumentState,
  action: DocumentAction,
): SubtitleDocumentState {
  return subtitleDocumentReducer(state, action);
}

beforeEach(() => {
  cueCounter = 0;
  // crypto.randomUUID is used by split/merge; mock for deterministic IDs
  vi.spyOn(crypto, "randomUUID").mockImplementation(() => {
    cueCounter += 100;
    return `uuid-${cueCounter}` as `${string}-${string}-${string}-${string}-${string}`;
  });
});

// ---------------------------------------------------------------------------
// createEditText
// ---------------------------------------------------------------------------

describe("createEditText", () => {
  it("changes the text of the target cue", () => {
    const cues = makeThreeCues();
    const op = createEditText(1, "Changed");
    const result = op.apply(cues);
    expect(result[1].text).toBe("Changed");
    expect(result[0].text).toBe("First");
    expect(result[2].text).toBe("Third");
  });

  it("inverse restores original text", () => {
    const cues = makeThreeCues();
    const op = createEditText(1, "Changed");
    const inv = op.inverse(cues);
    const applied = op.apply(cues);
    const restored = inv.apply(applied);
    expect(restored[1].text).toBe("Second");
  });

  it("does not mutate the original array", () => {
    const cues = makeThreeCues();
    const original = [...cues];
    createEditText(0, "New").apply(cues);
    expect(cues).toEqual(original);
  });
});

// ---------------------------------------------------------------------------
// createEditTiming
// ---------------------------------------------------------------------------

describe("createEditTiming", () => {
  it("updates startMs and endMs of the target cue", () => {
    const cues = makeThreeCues();
    const op = createEditTiming(0, 100, 900);
    const result = op.apply(cues);
    expect(result[0].startMs).toBe(100);
    expect(result[0].endMs).toBe(900);
  });

  it("inverse restores original timing", () => {
    const cues = makeThreeCues();
    const op = createEditTiming(0, 100, 900);
    const inv = op.inverse(cues);
    const applied = op.apply(cues);
    const restored = inv.apply(applied);
    expect(restored[0].startMs).toBe(0);
    expect(restored[0].endMs).toBe(1000);
  });
});

// ---------------------------------------------------------------------------
// createAddCue
// ---------------------------------------------------------------------------

describe("createAddCue", () => {
  it("inserts a cue after the given index", () => {
    const cues = makeThreeCues();
    const newCue = makeCue({ text: "Inserted" });
    const op = createAddCue(0, newCue);
    const result = op.apply(cues);
    expect(result).toHaveLength(4);
    expect(result[1].text).toBe("Inserted");
  });

  it("inserts at the beginning when afterIndex is -1", () => {
    const cues = makeThreeCues();
    const newCue = makeCue({ text: "First!" });
    const op = createAddCue(-1, newCue);
    const result = op.apply(cues);
    expect(result[0].text).toBe("First!");
    expect(result).toHaveLength(4);
  });

  it("inverse removes the inserted cue", () => {
    const cues = makeThreeCues();
    const newCue = makeCue({ text: "Inserted" });
    const op = createAddCue(1, newCue);
    const inv = op.inverse(cues);
    const applied = op.apply(cues);
    const restored = inv.apply(applied);
    expect(restored).toHaveLength(3);
    expect(restored.map((c) => c.text)).toEqual(["First", "Second", "Third"]);
  });
});

// ---------------------------------------------------------------------------
// createDeleteCues
// ---------------------------------------------------------------------------

describe("createDeleteCues", () => {
  it("removes cues at given indices", () => {
    const cues = makeThreeCues();
    const op = createDeleteCues([0, 2]);
    const result = op.apply(cues);
    expect(result).toHaveLength(1);
    expect(result[0].text).toBe("Second");
  });

  it("inverse re-inserts deleted cues at original positions", () => {
    const cues = makeThreeCues();
    const op = createDeleteCues([1]);
    const inv = op.inverse(cues);
    const applied = op.apply(cues);
    expect(applied).toHaveLength(2);
    const restored = inv.apply(applied);
    expect(restored).toHaveLength(3);
    expect(restored[1].text).toBe("Second");
  });

  it("handles deletion of all cues", () => {
    const cues = makeThreeCues();
    const op = createDeleteCues([0, 1, 2]);
    const result = op.apply(cues);
    expect(result).toHaveLength(0);
  });

  it("works with unsorted indices", () => {
    const cues = makeThreeCues();
    const op = createDeleteCues([2, 0]);
    const result = op.apply(cues);
    expect(result).toHaveLength(1);
    expect(result[0].text).toBe("Second");
  });
});

// ---------------------------------------------------------------------------
// createSplitCue
// ---------------------------------------------------------------------------

describe("createSplitCue", () => {
  it("splits a cue into two at the given character position", () => {
    const cues = [makeCue({ startMs: 0, endMs: 1000, text: "HelloWorld" })];
    const op = createSplitCue(0, 5);
    const result = op.apply(cues);
    expect(result).toHaveLength(2);
    expect(result[0].text).toBe("Hello");
    expect(result[1].text).toBe("World");
  });

  it("proportionally splits timing based on character position", () => {
    const cues = [makeCue({ startMs: 0, endMs: 1000, text: "HelloWorld" })];
    const op = createSplitCue(0, 5);
    const result = op.apply(cues);
    // 5/10 = 0.5 proportion, midpoint = 500
    expect(result[0].startMs).toBe(0);
    expect(result[0].endMs).toBe(500);
    expect(result[1].startMs).toBe(500);
    expect(result[1].endMs).toBe(1000);
  });

  it("handles empty text with 0.5 proportion", () => {
    const cues = [makeCue({ startMs: 0, endMs: 1000, text: "" })];
    const op = createSplitCue(0, 0);
    const result = op.apply(cues);
    expect(result).toHaveLength(2);
    expect(result[0].endMs).toBe(500);
    expect(result[1].startMs).toBe(500);
  });

  it("inverse merges the split cues back together", () => {
    const cues = [makeCue({ startMs: 0, endMs: 1000, text: "HelloWorld" })];
    const op = createSplitCue(0, 5);
    const inv = op.inverse(cues);
    const applied = op.apply(cues);
    expect(applied).toHaveLength(2);
    const restored = inv.apply(applied);
    expect(restored).toHaveLength(1);
    // Merge joins with newline
    expect(restored[0].text).toBe("Hello\nWorld");
  });
});

// ---------------------------------------------------------------------------
// createMergeCues
// ---------------------------------------------------------------------------

describe("createMergeCues", () => {
  it("merges consecutive cues into one", () => {
    const cues = makeThreeCues();
    const op = createMergeCues([0, 1]);
    const result = op.apply(cues);
    expect(result).toHaveLength(2);
    expect(result[0].text).toBe("First\nSecond");
    expect(result[0].startMs).toBe(0);
    expect(result[0].endMs).toBe(3000);
  });

  it("merges three consecutive cues", () => {
    const cues = makeThreeCues();
    const op = createMergeCues([0, 1, 2]);
    const result = op.apply(cues);
    expect(result).toHaveLength(1);
    expect(result[0].text).toBe("First\nSecond\nThird");
    expect(result[0].startMs).toBe(0);
    expect(result[0].endMs).toBe(5000);
  });

  it("throws for non-consecutive indices", () => {
    expect(() => createMergeCues([0, 2])).toThrow(
      "MergeCues requires consecutive indices",
    );
  });

  it("inverse restores individual cues", () => {
    const cues = makeThreeCues();
    const op = createMergeCues([0, 1]);
    const inv = op.inverse(cues);
    const applied = op.apply(cues);
    const restored = inv.apply(applied);
    expect(restored).toHaveLength(3);
    expect(restored[0].text).toBe("First");
    expect(restored[1].text).toBe("Second");
  });
});

// ---------------------------------------------------------------------------
// Reducer: APPLY_OP
// ---------------------------------------------------------------------------

describe("subtitleDocumentReducer APPLY_OP", () => {
  it("applies an operation and marks state as dirty", () => {
    const cues = makeThreeCues();
    const state = createInitialDocumentState(cues);
    const next = applyAction(state, {
      type: "APPLY_OP",
      op: createEditText(0, "Modified"),
    });
    expect(next.cues[0].text).toBe("Modified");
    expect(next.dirty).toBe(true);
  });

  it("pushes inverse onto undo stack", () => {
    const cues = makeThreeCues();
    const state = createInitialDocumentState(cues);
    const next = applyAction(state, {
      type: "APPLY_OP",
      op: createEditText(0, "Modified"),
    });
    expect(next.undoStack).toHaveLength(1);
  });

  it("clears the redo stack", () => {
    const cues = makeThreeCues();
    let state = createInitialDocumentState(cues);
    state = applyAction(state, {
      type: "APPLY_OP",
      op: createEditText(0, "A"),
    });
    state = applyAction(state, { type: "UNDO" });
    expect(state.redoStack).toHaveLength(1);
    state = applyAction(state, {
      type: "APPLY_OP",
      op: createEditText(0, "B"),
    });
    expect(state.redoStack).toHaveLength(0);
  });

  it("auto-sorts cues by startMs after timing change", () => {
    const cues = makeThreeCues();
    const state = createInitialDocumentState(cues);
    // Move the first cue's timing to be after the third cue
    const next = applyAction(state, {
      type: "APPLY_OP",
      op: createEditTiming(0, 6000, 7000),
    });
    expect(next.cues[0].startMs).toBe(1500);
    expect(next.cues[1].startMs).toBe(3500);
    expect(next.cues[2].startMs).toBe(6000);
  });

  it("tracks modified cue IDs", () => {
    const cues = makeThreeCues();
    const state = createInitialDocumentState(cues);
    const editedId = cues[1].id;
    const next = applyAction(state, {
      type: "APPLY_OP",
      op: createEditText(1, "Changed"),
    });
    expect(next.modifiedCueIds.has(editedId)).toBe(true);
  });

  it("caps undo stack at 50 entries", () => {
    const cues = makeThreeCues();
    let state = createInitialDocumentState(cues);
    for (let i = 0; i < 55; i++) {
      state = applyAction(state, {
        type: "APPLY_OP",
        op: createEditText(0, `edit-${i}`),
      });
    }
    expect(state.undoStack.length).toBeLessThanOrEqual(50);
  });
});

// ---------------------------------------------------------------------------
// Reducer: BATCH_OPS
// ---------------------------------------------------------------------------

describe("subtitleDocumentReducer BATCH_OPS", () => {
  it("applies multiple operations in sequence", () => {
    const cues = makeThreeCues();
    const state = createInitialDocumentState(cues);
    const next = applyAction(state, {
      type: "BATCH_OPS",
      ops: [createEditText(0, "A"), createEditText(1, "B")],
    });
    expect(next.cues[0].text).toBe("A");
    expect(next.cues[1].text).toBe("B");
  });

  it("creates a single undo entry for the batch", () => {
    const cues = makeThreeCues();
    const state = createInitialDocumentState(cues);
    const next = applyAction(state, {
      type: "BATCH_OPS",
      ops: [createEditText(0, "A"), createEditText(1, "B")],
    });
    expect(next.undoStack).toHaveLength(1);
  });

  it("returns unchanged state for empty ops array", () => {
    const cues = makeThreeCues();
    const state = createInitialDocumentState(cues);
    const next = applyAction(state, { type: "BATCH_OPS", ops: [] });
    expect(next).toBe(state);
  });

  it("auto-sorts cues after batch timing changes", () => {
    const cues = makeThreeCues();
    const state = createInitialDocumentState(cues);
    const next = applyAction(state, {
      type: "BATCH_OPS",
      ops: [createEditTiming(0, 6000, 7000), createEditTiming(1, 8000, 9000)],
    });
    // Third cue (startMs=3500) should now be first
    expect(next.cues[0].startMs).toBe(3500);
    expect(next.cues[1].startMs).toBe(6000);
    expect(next.cues[2].startMs).toBe(8000);
  });
});

// ---------------------------------------------------------------------------
// Reducer: UNDO / REDO
// ---------------------------------------------------------------------------

describe("subtitleDocumentReducer UNDO/REDO", () => {
  it("undo restores previous state", () => {
    const cues = makeThreeCues();
    let state = createInitialDocumentState(cues);
    const originalText = state.cues[0].text;
    state = applyAction(state, {
      type: "APPLY_OP",
      op: createEditText(0, "Changed"),
    });
    expect(state.cues[0].text).toBe("Changed");
    state = applyAction(state, { type: "UNDO" });
    expect(state.cues[0].text).toBe(originalText);
  });

  it("redo reapplies the undone operation", () => {
    const cues = makeThreeCues();
    let state = createInitialDocumentState(cues);
    state = applyAction(state, {
      type: "APPLY_OP",
      op: createEditText(0, "Changed"),
    });
    state = applyAction(state, { type: "UNDO" });
    state = applyAction(state, { type: "REDO" });
    expect(state.cues[0].text).toBe("Changed");
  });

  it("undo on empty stack returns same state", () => {
    const cues = makeThreeCues();
    const state = createInitialDocumentState(cues);
    const next = applyAction(state, { type: "UNDO" });
    expect(next).toBe(state);
  });

  it("redo on empty stack returns same state", () => {
    const cues = makeThreeCues();
    const state = createInitialDocumentState(cues);
    const next = applyAction(state, { type: "REDO" });
    expect(next).toBe(state);
  });

  it("multiple undo/redo cycles are consistent", () => {
    const cues = makeThreeCues();
    let state = createInitialDocumentState(cues);
    const texts = ["A", "B", "C"];

    for (const t of texts) {
      state = applyAction(state, {
        type: "APPLY_OP",
        op: createEditText(0, t),
      });
    }
    expect(state.cues[0].text).toBe("C");

    state = applyAction(state, { type: "UNDO" });
    expect(state.cues[0].text).toBe("B");

    state = applyAction(state, { type: "UNDO" });
    expect(state.cues[0].text).toBe("A");

    state = applyAction(state, { type: "REDO" });
    expect(state.cues[0].text).toBe("B");

    state = applyAction(state, { type: "REDO" });
    expect(state.cues[0].text).toBe("C");
  });

  it("undo of a batch restores all ops at once", () => {
    const cues = makeThreeCues();
    let state = createInitialDocumentState(cues);
    const origTexts = state.cues.map((c) => c.text);

    state = applyAction(state, {
      type: "BATCH_OPS",
      ops: [createEditText(0, "X"), createEditText(1, "Y")],
    });
    expect(state.cues[0].text).toBe("X");
    expect(state.cues[1].text).toBe("Y");

    state = applyAction(state, { type: "UNDO" });
    expect(state.cues[0].text).toBe(origTexts[0]);
    expect(state.cues[1].text).toBe(origTexts[1]);
  });

  it("redo after undo of batch pushes onto undo stack", () => {
    const cues = makeThreeCues();
    let state = createInitialDocumentState(cues);

    state = applyAction(state, {
      type: "BATCH_OPS",
      ops: [createEditText(0, "X"), createEditText(1, "Y")],
    });

    state = applyAction(state, { type: "UNDO" });
    expect(state.undoStack).toHaveLength(0);
    expect(state.redoStack).toHaveLength(1);

    state = applyAction(state, { type: "REDO" });
    expect(state.undoStack).toHaveLength(1);
    expect(state.redoStack).toHaveLength(0);
  });

  it("undo of add cue removes it", () => {
    const cues = makeThreeCues();
    let state = createInitialDocumentState(cues);
    const newCue = makeCue({ text: "New" });

    state = applyAction(state, {
      type: "APPLY_OP",
      op: createAddCue(0, newCue),
    });
    expect(state.cues).toHaveLength(4);

    state = applyAction(state, { type: "UNDO" });
    expect(state.cues).toHaveLength(3);
  });

  it("undo of delete cue restores it", () => {
    const cues = makeThreeCues();
    let state = createInitialDocumentState(cues);
    const deletedText = state.cues[1].text;

    state = applyAction(state, {
      type: "APPLY_OP",
      op: createDeleteCues([1]),
    });
    expect(state.cues).toHaveLength(2);

    state = applyAction(state, { type: "UNDO" });
    expect(state.cues).toHaveLength(3);
    expect(state.cues[1].text).toBe(deletedText);
  });
});

// ---------------------------------------------------------------------------
// Reducer: LOAD
// ---------------------------------------------------------------------------

describe("subtitleDocumentReducer LOAD", () => {
  it("replaces all state with fresh cues", () => {
    const cues = makeThreeCues();
    let state = createInitialDocumentState(cues);
    state = applyAction(state, {
      type: "APPLY_OP",
      op: createEditText(0, "dirty"),
    });
    expect(state.dirty).toBe(true);

    const newCues = [makeCue({ text: "Fresh" })];
    state = applyAction(state, { type: "LOAD", cues: newCues });
    expect(state.cues).toHaveLength(1);
    expect(state.cues[0].text).toBe("Fresh");
    expect(state.dirty).toBe(false);
    expect(state.undoStack).toHaveLength(0);
    expect(state.redoStack).toHaveLength(0);
    expect(state.modifiedCueIds.size).toBe(0);
  });

  it("does not share reference with input array", () => {
    const cues = [makeCue()];
    const state = applyAction(createInitialDocumentState([]), {
      type: "LOAD",
      cues,
    });
    expect(state.cues).not.toBe(cues);
  });
});

// ---------------------------------------------------------------------------
// Reducer: MARK_SAVED
// ---------------------------------------------------------------------------

describe("subtitleDocumentReducer MARK_SAVED", () => {
  it("clears dirty flag and modifiedCueIds", () => {
    const cues = makeThreeCues();
    let state = createInitialDocumentState(cues);
    state = applyAction(state, {
      type: "APPLY_OP",
      op: createEditText(0, "dirty"),
    });
    expect(state.dirty).toBe(true);
    expect(state.modifiedCueIds.size).toBeGreaterThan(0);

    state = applyAction(state, { type: "MARK_SAVED" });
    expect(state.dirty).toBe(false);
    expect(state.modifiedCueIds.size).toBe(0);
  });

  it("preserves undo/redo stacks", () => {
    const cues = makeThreeCues();
    let state = createInitialDocumentState(cues);
    state = applyAction(state, {
      type: "APPLY_OP",
      op: createEditText(0, "dirty"),
    });
    const undoLen = state.undoStack.length;

    state = applyAction(state, { type: "MARK_SAVED" });
    expect(state.undoStack).toHaveLength(undoLen);
  });
});

// ---------------------------------------------------------------------------
// Reducer: MARK_DIRTY (present in switch but not in type union)
// ---------------------------------------------------------------------------

describe("subtitleDocumentReducer MARK_DIRTY", () => {
  it("sets dirty flag to true", () => {
    const cues = makeThreeCues();
    const state = createInitialDocumentState(cues);
    expect(state.dirty).toBe(false);
    // Cast since MARK_DIRTY is handled by the switch but not in the union
    const next = subtitleDocumentReducer(state, {
      type: "MARK_DIRTY",
    } as unknown as DocumentAction);
    expect(next.dirty).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// createInitialDocumentState
// ---------------------------------------------------------------------------

describe("createInitialDocumentState", () => {
  it("creates clean state from cues", () => {
    const cues = makeThreeCues();
    const state = createInitialDocumentState(cues);
    expect(state.cues).toHaveLength(3);
    expect(state.dirty).toBe(false);
    expect(state.undoStack).toHaveLength(0);
    expect(state.redoStack).toHaveLength(0);
    expect(state.modifiedCueIds.size).toBe(0);
  });

  it("copies the cues array", () => {
    const cues = makeThreeCues();
    const state = createInitialDocumentState(cues);
    expect(state.cues).not.toBe(cues);
    expect(state.cues).toEqual(cues);
  });
});

// ---------------------------------------------------------------------------
// computeCueWarnings: Red checks
// ---------------------------------------------------------------------------

describe("computeCueWarnings", () => {
  it("returns red for end time before start time", () => {
    const cue = makeCue({ startMs: 2000, endMs: 1000, text: "Bad" });
    const result = computeCueWarnings(cue, null, null);
    expect(result).not.toBeNull();
    expect(result!.level).toBe("red");
    expect(result!.message).toContain("End time is before start time");
  });

  it("returns red for overlap with previous cue", () => {
    const prev = makeCue({ startMs: 0, endMs: 2000 });
    const cue = makeCue({ startMs: 1500, endMs: 3000 });
    const result = computeCueWarnings(cue, prev, null);
    expect(result!.level).toBe("red");
    expect(result!.message).toContain("Overlaps");
  });

  it("returns red for empty text", () => {
    const cue = makeCue({ startMs: 0, endMs: 1000, text: "   " });
    const result = computeCueWarnings(cue, null, null);
    expect(result!.level).toBe("red");
    expect(result!.message).toContain("Empty text");
  });

  // Combined outputs stack languages at shared timestamps.
  it("suppresses the overlap warning for combined subtitles", () => {
    const prev = makeCue({ startMs: 1000, endMs: 2000, text: "Hello" });
    const cue = makeCue({ startMs: 1000, endMs: 2000, text: "Szia" });
    expect(computeCueWarnings(cue, prev, null)!.message).toContain("Overlaps");
    expect(computeCueWarnings(cue, prev, null, undefined, true)).toBeNull();
  });

  it("suppresses CPS/line heuristics for combined subtitles", () => {
    const cue = makeCue({ startMs: 0, endMs: 1000, text: "a".repeat(100) });
    expect(computeCueWarnings(cue, null, null)!.level).toBe("orange");
    expect(computeCueWarnings(cue, null, null, undefined, true)).toBeNull();
  });

  it("still flags genuine errors for combined subtitles", () => {
    const reversed = makeCue({ startMs: 2000, endMs: 1000, text: "Bad" });
    expect(
      computeCueWarnings(reversed, null, null, undefined, true)!.level,
    ).toBe("red");
    const empty = makeCue({ startMs: 0, endMs: 1000, text: "  " });
    expect(
      computeCueWarnings(empty, null, null, undefined, true)!.message,
    ).toContain("Empty text");
  });

  // Orange checks
  it("returns orange for CPS exceeding maxCPS", () => {
    // 100 chars in 1 second = 100 CPS
    const longText = "a".repeat(100);
    const cue = makeCue({ startMs: 0, endMs: 1000, text: longText });
    const result = computeCueWarnings(cue, null, null);
    expect(result!.level).toBe("orange");
    expect(result!.message).toContain("CPS too high");
  });

  it("returns orange for duration below minimum", () => {
    // Default minDurationMs is 700
    const cue = makeCue({ startMs: 0, endMs: 500, text: "Hi" });
    const result = computeCueWarnings(cue, null, null);
    expect(result!.level).toBe("orange");
    expect(result!.message).toContain("Duration too short");
  });

  it("returns orange for line too long", () => {
    const longLine = "a".repeat(50); // default maxCharsPerLine is 42
    const cue = makeCue({ startMs: 0, endMs: 5000, text: longLine });
    const result = computeCueWarnings(cue, null, null);
    expect(result!.level).toBe("orange");
    expect(result!.message).toContain("Line too long");
  });

  it("returns orange for too many lines", () => {
    const text = "Line1\nLine2\nLine3"; // default maxLines is 2
    const cue = makeCue({ startMs: 0, endMs: 5000, text });
    const result = computeCueWarnings(cue, null, null);
    expect(result!.level).toBe("orange");
    expect(result!.message).toContain("Too many lines");
  });

  // Yellow checks
  it("returns yellow for CPS between warn and max thresholds", () => {
    // Need CPS >= 18 and <= 25. 20 chars / 1 sec = 20 CPS
    const text = "a".repeat(20);
    const cue = makeCue({ startMs: 0, endMs: 1000, text });
    const result = computeCueWarnings(cue, null, null);
    expect(result!.level).toBe("yellow");
    expect(result!.message).toContain("CPS is high");
  });

  it("returns yellow for duration exceeding max", () => {
    // Default maxDurationMs is 7000
    const cue = makeCue({ startMs: 0, endMs: 10000, text: "Short" });
    const result = computeCueWarnings(cue, null, null);
    expect(result!.level).toBe("yellow");
    expect(result!.message).toContain("Duration too long");
  });

  it("returns yellow for small gap with previous cue", () => {
    // Gap = 50ms, default minGapMs = 83
    const prev = makeCue({ startMs: 0, endMs: 1000 });
    const cue = makeCue({ startMs: 1050, endMs: 3000, text: "Short" });
    const result = computeCueWarnings(cue, prev, null);
    expect(result!.level).toBe("yellow");
    expect(result!.message).toContain("Small gap");
  });

  it("returns null for a valid cue with no issues", () => {
    const prev = makeCue({ startMs: 0, endMs: 1000 });
    // 10 chars / 2 seconds = 5 CPS, well within limits
    const cue = makeCue({ startMs: 2000, endMs: 4000, text: "Good text!" });
    const result = computeCueWarnings(cue, prev, null);
    expect(result).toBeNull();
  });

  it("uses Netflix preset with stricter limits", () => {
    const netflix = QC_PRESETS.netflix;
    // 21 chars / 1 sec = 21 CPS, over Netflix max of 20
    const text = "a".repeat(21);
    const cue = makeCue({ startMs: 0, endMs: 1000, text });
    const result = computeCueWarnings(cue, null, null, netflix);
    expect(result!.level).toBe("orange");
    expect(result!.message).toContain("CPS too high");
  });

  it("strips HTML tags when checking line length", () => {
    // 30 visible chars + tags should be under 42
    const text = "<b>a</b>".repeat(5); // 5 visible chars
    const cue = makeCue({ startMs: 0, endMs: 5000, text });
    const result = computeCueWarnings(cue, null, null);
    // Should not trigger line too long for 5 visible chars
    expect(result?.message ?? "").not.toContain("Line too long");
  });

  it("prioritizes red over orange warnings", () => {
    // Both end < start AND CPS would be high
    const cue = makeCue({
      startMs: 5000,
      endMs: 1000,
      text: "a".repeat(100),
    });
    const result = computeCueWarnings(cue, null, null);
    expect(result!.level).toBe("red");
  });

  it("returns Infinity CPS for zero-duration cue with text", () => {
    const cue = makeCue({ startMs: 1000, endMs: 1000, text: "a" });
    const result = computeCueWarnings(cue, null, null);
    // Zero duration means CPS is Infinity, which is > maxCPS
    expect(result!.level).toBe("orange");
    expect(result!.message).toContain("CPS");
  });
});

// ---------------------------------------------------------------------------
// detectGaps
// ---------------------------------------------------------------------------

describe("detectGaps", () => {
  it("detects gaps larger than minGapMs", () => {
    const cues = [
      makeCue({ startMs: 0, endMs: 1000 }),
      makeCue({ startMs: 2000, endMs: 3000 }),
    ];
    const gaps = detectGaps(cues, 500);
    expect(gaps).toHaveLength(1);
    expect(gaps[0].startMs).toBe(1000);
    expect(gaps[0].endMs).toBe(2000);
  });

  it("ignores gaps smaller than minGapMs", () => {
    const cues = [
      makeCue({ startMs: 0, endMs: 1000 }),
      makeCue({ startMs: 1200, endMs: 2000 }),
    ];
    const gaps = detectGaps(cues, 500);
    expect(gaps).toHaveLength(0);
  });

  it("returns empty array for fewer than 2 cues", () => {
    expect(detectGaps([], 500)).toEqual([]);
    expect(detectGaps([makeCue()], 500)).toEqual([]);
  });

  it("handles out-of-order cues by sorting them first", () => {
    const cues = [
      makeCue({ startMs: 3000, endMs: 4000 }),
      makeCue({ startMs: 0, endMs: 1000 }),
    ];
    const gaps = detectGaps(cues, 500);
    expect(gaps).toHaveLength(1);
    expect(gaps[0].startMs).toBe(1000);
    expect(gaps[0].endMs).toBe(3000);
  });

  it("uses default minGapMs of 500 when not specified", () => {
    const cues = [
      makeCue({ startMs: 0, endMs: 1000 }),
      makeCue({ startMs: 1400, endMs: 2000 }), // 400ms gap, under 500
    ];
    const gaps = detectGaps(cues);
    expect(gaps).toHaveLength(0);

    const cues2 = [
      makeCue({ startMs: 0, endMs: 1000 }),
      makeCue({ startMs: 1600, endMs: 2000 }), // 600ms gap, over 500
    ];
    const gaps2 = detectGaps(cues2);
    expect(gaps2).toHaveLength(1);
  });

  it("detects multiple gaps", () => {
    const cues = [
      makeCue({ startMs: 0, endMs: 1000 }),
      makeCue({ startMs: 2000, endMs: 3000 }),
      makeCue({ startMs: 5000, endMs: 6000 }),
    ];
    const gaps = detectGaps(cues, 500);
    expect(gaps).toHaveLength(2);
  });

  it("reports afterIndex from the original array order", () => {
    // cues[0] has later timing, cues[1] has earlier timing
    const cues = [
      makeCue({ startMs: 3000, endMs: 4000 }),
      makeCue({ startMs: 0, endMs: 1000 }),
    ];
    const gaps = detectGaps(cues, 500);
    expect(gaps).toHaveLength(1);
    // After sorting, cues[1] (originalIndex=1) comes first
    expect(gaps[0].afterIndex).toBe(1);
  });

  it("does not report gap for adjacent cues with no space", () => {
    const cues = [
      makeCue({ startMs: 0, endMs: 1000 }),
      makeCue({ startMs: 1000, endMs: 2000 }),
    ];
    const gaps = detectGaps(cues, 0);
    expect(gaps).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// QC_PRESETS
// ---------------------------------------------------------------------------

describe("QC_PRESETS", () => {
  it("contains expected presets", () => {
    expect(QC_PRESETS).toHaveProperty("default");
    expect(QC_PRESETS).toHaveProperty("netflix");
    expect(QC_PRESETS).toHaveProperty("amazon");
    expect(QC_PRESETS).toHaveProperty("bbc");
  });

  it("DEFAULT_QC_PRESET is the default preset", () => {
    expect(DEFAULT_QC_PRESET).toBe(QC_PRESETS.default);
  });

  it("Netflix has stricter CPS than default", () => {
    expect(QC_PRESETS.netflix.maxCPS).toBeLessThan(QC_PRESETS.default.maxCPS);
  });

  it("BBC has shorter max chars per line", () => {
    expect(QC_PRESETS.bbc.maxCharsPerLine).toBeLessThan(
      QC_PRESETS.default.maxCharsPerLine,
    );
  });
});

// ---------------------------------------------------------------------------
// Edge cases
// ---------------------------------------------------------------------------

describe("edge cases", () => {
  it("operations on empty cues array", () => {
    const state = createInitialDocumentState([]);
    const newCue = makeCue({ text: "New" });
    const next = applyAction(state, {
      type: "APPLY_OP",
      op: createAddCue(-1, newCue),
    });
    expect(next.cues).toHaveLength(1);
    expect(next.cues[0].text).toBe("New");
  });

  it("single cue operations", () => {
    const cues = [makeCue({ text: "Only" })];
    const state = createInitialDocumentState(cues);
    let next = applyAction(state, {
      type: "APPLY_OP",
      op: createEditText(0, "Modified"),
    });
    expect(next.cues[0].text).toBe("Modified");

    next = applyAction(next, { type: "UNDO" });
    expect(next.cues[0].text).toBe("Only");
  });

  it("delete then undo then redo round-trips correctly", () => {
    const cues = makeThreeCues();
    let state = createInitialDocumentState(cues);
    const originalLen = state.cues.length;

    state = applyAction(state, {
      type: "APPLY_OP",
      op: createDeleteCues([0, 2]),
    });
    expect(state.cues).toHaveLength(1);

    state = applyAction(state, { type: "UNDO" });
    expect(state.cues).toHaveLength(originalLen);

    state = applyAction(state, { type: "REDO" });
    expect(state.cues).toHaveLength(1);
  });

  it("complex sequence: add, edit, delete, undo all", () => {
    let state = createInitialDocumentState([makeCue({ text: "Start" })]);

    const newCue = makeCue({ startMs: 2000, endMs: 3000, text: "Added" });
    state = applyAction(state, {
      type: "APPLY_OP",
      op: createAddCue(0, newCue),
    });
    expect(state.cues).toHaveLength(2);

    state = applyAction(state, {
      type: "APPLY_OP",
      op: createEditText(0, "Edited"),
    });

    state = applyAction(state, {
      type: "APPLY_OP",
      op: createDeleteCues([1]),
    });
    expect(state.cues).toHaveLength(1);

    // Undo delete
    state = applyAction(state, { type: "UNDO" });
    expect(state.cues).toHaveLength(2);

    // Undo edit
    state = applyAction(state, { type: "UNDO" });
    expect(state.cues[0].text).toBe("Start");

    // Undo add
    state = applyAction(state, { type: "UNDO" });
    expect(state.cues).toHaveLength(1);
  });
});
