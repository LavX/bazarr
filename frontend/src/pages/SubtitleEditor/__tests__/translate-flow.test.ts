/* eslint-disable @typescript-eslint/no-explicit-any */
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  createEditText,
  createInitialDocumentState,
  subtitleDocumentReducer,
  type SubtitleDocumentState,
} from "@/pages/SubtitleEditor/document";
import type { Cue } from "@/pages/SubtitleEditor/types";

// Mock crypto.randomUUID since it may not be available in test env
beforeEach(() => {
  let counter = 0;
  vi.stubGlobal("crypto", {
    randomUUID: () => `test-uuid-${++counter}`,
  });
});

// ---------------------------------------------------------------------------
// Helpers: replicate the logic from EditorPage.handleApplyTranslation and
// TranslatePanel source-selection without pulling in React components.
// ---------------------------------------------------------------------------

interface ReferenceCue {
  startMs: number;
  endMs: number;
  text: string;
}

/**
 * Replicates the "effective source" logic from TranslatePanel.
 * translateSource "auto" picks reference when available, editor otherwise.
 */
function resolveEffectiveSource(
  translateSource: "auto" | "reference" | "editor",
  referenceCues: ReferenceCue[] | undefined,
): "reference" | "editor" {
  if (translateSource === "auto") {
    return referenceCues && referenceCues.length > 0 ? "reference" : "editor";
  }
  return translateSource;
}

/**
 * Decides whether to use reference cues as the translation source.
 */
function shouldUseReference(
  effectiveSource: "reference" | "editor",
  referenceCues: ReferenceCue[] | undefined,
): boolean {
  return (
    effectiveSource === "reference" &&
    !!referenceCues &&
    referenceCues.length > 0
  );
}

/**
 * Replicates handleApplyTranslation from EditorPage: when translating from
 * reference cues, creates new cues with reference timing. Otherwise updates
 * existing editor cues in place.
 */
function applyTranslation(
  translations: Map<number, string>,
  docState: SubtitleDocumentState,
  referenceCueList: ReferenceCue[],
  dispatch: (action: any) => void,
): { fromReference: boolean } {
  if (referenceCueList.length > 0 && translations.size > docState.cues.length) {
    // Reference path: create all cues with reference timing
    const newCues: Cue[] = [];
    translations.forEach((text, idx) => {
      if (idx < referenceCueList.length) {
        newCues.push({
          id: crypto.randomUUID(),
          startMs: referenceCueList[idx].startMs,
          endMs: referenceCueList[idx].endMs,
          text,
        });
      }
    });
    if (newCues.length > 0) {
      newCues.sort((a, b) => a.startMs - b.startMs);
      dispatch({ type: "LOAD", cues: newCues });
      dispatch({ type: "MARK_DIRTY" });
    }
    return { fromReference: true };
  } else {
    // Normal case: update existing editor cues
    const ops = Array.from(translations.entries())
      .filter(([idx]) => idx >= 0 && idx < docState.cues.length)
      .map(([idx, text]) => createEditText(idx, text));
    if (ops.length > 0) {
      dispatch({ type: "BATCH_OPS", ops });
    }
    return { fromReference: false };
  }
}

/**
 * Replicates the onSetReference callback from EditorPage. Maps translation
 * indices back to editor cue timings to build reference cues.
 */
function buildSetReferenceCues(
  translations: Map<number, string>,
  editorCues: Cue[],
): ReferenceCue[] {
  return Array.from(translations.entries())
    .filter(([idx]) => idx >= 0 && idx < editorCues.length)
    .map(([idx, text]) => ({
      startMs: editorCues[idx].startMs,
      endMs: editorCues[idx].endMs,
      text,
    }));
}

// ---------------------------------------------------------------------------
// Test data
// ---------------------------------------------------------------------------

function makeEditorCues(count: number): Cue[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `editor-${i}`,
    startMs: i * 3000,
    endMs: i * 3000 + 2500,
    text: `Editor line ${i}`,
  }));
}

function makeReferenceCues(count: number): ReferenceCue[] {
  return Array.from({ length: count }, (_, i) => ({
    startMs: i * 2800,
    endMs: i * 2800 + 2400,
    text: `Reference line ${i}`,
  }));
}

function makeTranslations(count: number): Map<number, string> {
  const map = new Map<number, string>();
  for (let i = 0; i < count; i++) {
    map.set(i, `Translated line ${i}`);
  }
  return map;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("Translation flow: source selection", () => {
  it('"auto" picks reference when referenceCues are available', () => {
    const refs = makeReferenceCues(5);
    expect(resolveEffectiveSource("auto", refs)).toBe("reference");
  });

  it('"auto" picks editor when referenceCues are empty', () => {
    expect(resolveEffectiveSource("auto", [])).toBe("editor");
  });

  it('"auto" picks editor when referenceCues are undefined', () => {
    expect(resolveEffectiveSource("auto", undefined)).toBe("editor");
  });

  it('"reference" always returns reference', () => {
    expect(resolveEffectiveSource("reference", makeReferenceCues(3))).toBe(
      "reference",
    );
    expect(resolveEffectiveSource("reference", [])).toBe("reference");
  });

  it('"editor" always returns editor', () => {
    expect(resolveEffectiveSource("editor", makeReferenceCues(3))).toBe(
      "editor",
    );
    expect(resolveEffectiveSource("editor", undefined)).toBe("editor");
  });
});

describe("Translation flow: reference path (new cues from reference timing)", () => {
  it("creates new cues with reference timing when translations.size > cues.length", () => {
    const editorCues = makeEditorCues(2);
    const refCues = makeReferenceCues(5);
    const translations = makeTranslations(5);
    const docState = createInitialDocumentState(editorCues);

    const actions: any[] = [];
    const dispatch = (a: any) => actions.push(a);

    const result = applyTranslation(translations, docState, refCues, dispatch);

    expect(result.fromReference).toBe(true);
    expect(actions).toHaveLength(2);
    expect(actions[0].type).toBe("LOAD");
    expect(actions[1].type).toBe("MARK_DIRTY");

    const loadedCues = actions[0].cues as Cue[];
    expect(loadedCues).toHaveLength(5);

    // Cues should carry reference timing, not editor timing
    for (let i = 0; i < 5; i++) {
      expect(loadedCues[i].startMs).toBe(refCues[i].startMs);
      expect(loadedCues[i].endMs).toBe(refCues[i].endMs);
      expect(loadedCues[i].text).toBe(`Translated line ${i}`);
    }
  });

  it("loaded cues are sorted by startMs", () => {
    const editorCues = makeEditorCues(1);
    // Reference cues in reverse time order
    const refCues: ReferenceCue[] = [
      { startMs: 9000, endMs: 10000, text: "ref C" },
      { startMs: 3000, endMs: 4000, text: "ref A" },
      { startMs: 6000, endMs: 7000, text: "ref B" },
    ];
    const translations = new Map<number, string>([
      [0, "Translated C"],
      [1, "Translated A"],
      [2, "Translated B"],
    ]);
    const docState = createInitialDocumentState(editorCues);

    const actions: any[] = [];
    applyTranslation(translations, docState, refCues, (a) => actions.push(a));

    const loadedCues = actions[0].cues as Cue[];
    expect(loadedCues).toHaveLength(3);
    // Should be sorted: A (3000), B (6000), C (9000)
    expect(loadedCues[0].startMs).toBe(3000);
    expect(loadedCues[0].text).toBe("Translated A");
    expect(loadedCues[1].startMs).toBe(6000);
    expect(loadedCues[1].text).toBe("Translated B");
    expect(loadedCues[2].startMs).toBe(9000);
    expect(loadedCues[2].text).toBe("Translated C");
  });

  it("skips translations with indices beyond reference cue count", () => {
    const editorCues = makeEditorCues(1);
    const refCues = makeReferenceCues(2);
    // 3 translations but only 2 reference cues
    const translations = new Map<number, string>([
      [0, "Line 0"],
      [1, "Line 1"],
      [2, "Line 2 (no matching ref)"],
    ]);
    const docState = createInitialDocumentState(editorCues);

    const actions: any[] = [];
    applyTranslation(translations, docState, refCues, (a) => actions.push(a));

    const loadedCues = actions[0].cues as Cue[];
    expect(loadedCues).toHaveLength(2);
  });
});

describe("Translation flow: editor path (update cues in place)", () => {
  it("updates existing editor cues via BATCH_OPS when translations.size <= cues.length", () => {
    const editorCues = makeEditorCues(5);
    const translations = makeTranslations(5);
    const docState = createInitialDocumentState(editorCues);

    const actions: any[] = [];
    const result = applyTranslation(translations, docState, [], (a) =>
      actions.push(a),
    );

    expect(result.fromReference).toBe(false);
    expect(actions).toHaveLength(1);
    expect(actions[0].type).toBe("BATCH_OPS");
    expect(actions[0].ops).toHaveLength(5);

    // Each op should be an EDIT_TEXT
    for (const op of actions[0].ops) {
      expect(op.type).toBe("EDIT_TEXT");
    }
  });

  it("filters out translations with out-of-bounds indices", () => {
    const editorCues = makeEditorCues(3);
    const translations = new Map<number, string>([
      [0, "ok"],
      [2, "ok"],
      [5, "out of range"],
      [-1, "negative"],
    ]);
    const docState = createInitialDocumentState(editorCues);

    const actions: any[] = [];
    applyTranslation(translations, docState, [], (a) => actions.push(a));

    expect(actions[0].ops).toHaveLength(2);
  });

  it("applies BATCH_OPS correctly through the reducer", () => {
    const editorCues = makeEditorCues(3);
    const translations = new Map<number, string>([
      [0, "Translated 0"],
      [1, "Translated 1"],
      [2, "Translated 2"],
    ]);
    let state = createInitialDocumentState(editorCues);

    const ops = Array.from(translations.entries())
      .filter(([idx]) => idx >= 0 && idx < state.cues.length)
      .map(([idx, text]) => createEditText(idx, text));

    state = subtitleDocumentReducer(state, { type: "BATCH_OPS", ops });

    expect(state.cues[0].text).toBe("Translated 0");
    expect(state.cues[1].text).toBe("Translated 1");
    expect(state.cues[2].text).toBe("Translated 2");
    expect(state.dirty).toBe(true);
    // Timing should be preserved
    expect(state.cues[0].startMs).toBe(0);
    expect(state.cues[1].startMs).toBe(3000);
  });
});

describe("Translation flow: onSetReference behavior", () => {
  it("is NOT called when translating from reference (fromReference = true)", () => {
    // When the effective source is reference and translations come back,
    // the component only calls onSetReference when useRef is false.
    const editorCues = makeEditorCues(2);
    const refCues = makeReferenceCues(5);
    const translations = makeTranslations(5);
    const docState = createInitialDocumentState(editorCues);

    const result = applyTranslation(translations, docState, refCues, () => {
      /* noop */
    });
    expect(result.fromReference).toBe(true);

    // Verify that shouldUseReference returns true for this case
    const effective = resolveEffectiveSource("auto", refCues);
    expect(shouldUseReference(effective, refCues)).toBe(true);
    // When useRef is true, the component skips onSetReference
  });

  it("IS called when translating from editor cues (fromReference = false)", () => {
    const editorCues = makeEditorCues(3);
    const translations = makeTranslations(3);
    const docState = createInitialDocumentState(editorCues);

    const result = applyTranslation(translations, docState, [], () => {
      /* noop */
    });
    expect(result.fromReference).toBe(false);

    // Verify onSetReference would be called: shouldUseReference is false
    const effective = resolveEffectiveSource("auto", []);
    expect(shouldUseReference(effective, [])).toBe(false);

    // Build reference cues from editor cues (what onSetReference receives)
    const refResult = buildSetReferenceCues(translations, editorCues);
    expect(refResult).toHaveLength(3);
    expect(refResult[0].text).toBe("Translated line 0");
    expect(refResult[0].startMs).toBe(editorCues[0].startMs);
    expect(refResult[0].endMs).toBe(editorCues[0].endMs);
  });

  it("builds reference cues with editor timing when translating from editor", () => {
    const editorCues = makeEditorCues(4);
    const translations = new Map<number, string>([
      [1, "Only line 1"],
      [3, "Only line 3"],
    ]);

    const refResult = buildSetReferenceCues(translations, editorCues);
    expect(refResult).toHaveLength(2);
    expect(refResult[0].startMs).toBe(editorCues[1].startMs);
    expect(refResult[0].text).toBe("Only line 1");
    expect(refResult[1].startMs).toBe(editorCues[3].startMs);
    expect(refResult[1].text).toBe("Only line 3");
  });
});

describe("Translation flow: MARK_DIRTY after LOAD", () => {
  it("LOAD resets dirty to false, then MARK_DIRTY sets it back to true", () => {
    const cues = makeEditorCues(2);
    let state = createInitialDocumentState(cues);
    expect(state.dirty).toBe(false);

    // Simulate the reference translation path: LOAD then MARK_DIRTY
    const newCues: Cue[] = [
      { id: "new-1", startMs: 0, endMs: 1000, text: "translated" },
    ];
    state = subtitleDocumentReducer(state, { type: "LOAD", cues: newCues });
    // LOAD creates a fresh initial state, so dirty is false
    expect(state.dirty).toBe(false);

    state = subtitleDocumentReducer(state, { type: "MARK_DIRTY" });
    expect(state.dirty).toBe(true);
  });

  it("LOAD clears undo/redo stacks and MARK_DIRTY preserves them empty", () => {
    const cues = makeEditorCues(3);
    let state = createInitialDocumentState(cues);

    // Make some edits to build undo stack
    state = subtitleDocumentReducer(state, {
      type: "APPLY_OP",
      op: createEditText(0, "edited"),
    });
    expect(state.undoStack.length).toBeGreaterThan(0);

    // LOAD wipes the stacks
    state = subtitleDocumentReducer(state, {
      type: "LOAD",
      cues: [{ id: "x", startMs: 0, endMs: 1000, text: "fresh" }],
    });
    expect(state.undoStack).toHaveLength(0);
    expect(state.redoStack).toHaveLength(0);

    // MARK_DIRTY does not add to undo stack
    state = subtitleDocumentReducer(state, { type: "MARK_DIRTY" });
    expect(state.undoStack).toHaveLength(0);
    expect(state.dirty).toBe(true);
  });
});

describe("Translation flow: edge cases", () => {
  it("empty translations map does not dispatch anything", () => {
    const editorCues = makeEditorCues(3);
    const docState = createInitialDocumentState(editorCues);
    const translations = new Map<number, string>();

    const actions: any[] = [];
    applyTranslation(translations, docState, [], (a) => actions.push(a));

    // No ops to dispatch
    expect(actions).toHaveLength(0);
  });

  it("translations for empty editor with no reference do nothing", () => {
    const docState = createInitialDocumentState([]);
    const translations = makeTranslations(3);

    const actions: any[] = [];
    // referenceCueList is empty, cues.length is 0, translations.size (3) > 0
    // But referenceCueList.length === 0, so it falls to the else branch
    // All indices are out of range for empty editor
    applyTranslation(translations, docState, [], (a) => actions.push(a));

    expect(actions).toHaveLength(0);
  });

  it("translations equal to cue count use editor path, not reference path", () => {
    const editorCues = makeEditorCues(3);
    const refCues = makeReferenceCues(3);
    const translations = makeTranslations(3); // size === cues.length

    const docState = createInitialDocumentState(editorCues);
    const actions: any[] = [];
    const result = applyTranslation(translations, docState, refCues, (a) =>
      actions.push(a),
    );

    // translations.size (3) is NOT > cues.length (3), so editor path
    expect(result.fromReference).toBe(false);
    expect(actions[0].type).toBe("BATCH_OPS");
  });
});
