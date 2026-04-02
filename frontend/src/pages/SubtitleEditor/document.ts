import type { Cue } from "./types";

// ---------------------------------------------------------------------------
// QC Presets
// ---------------------------------------------------------------------------

export interface QCPreset {
  name: string;
  maxCPS: number;
  warnCPS: number;
  maxCharsPerLine: number;
  maxLines: number;
  minDurationMs: number;
  maxDurationMs: number;
  minGapMs: number;
}

export const QC_PRESETS: Record<string, QCPreset> = {
  default: { name: "Default", maxCPS: 25, warnCPS: 18, maxCharsPerLine: 42, maxLines: 2, minDurationMs: 700, maxDurationMs: 7000, minGapMs: 83 },
  netflix: { name: "Netflix", maxCPS: 20, warnCPS: 17, maxCharsPerLine: 42, maxLines: 2, minDurationMs: 833, maxDurationMs: 7000, minGapMs: 83 },
  amazon: { name: "Amazon", maxCPS: 25, warnCPS: 20, maxCharsPerLine: 42, maxLines: 2, minDurationMs: 1000, maxDurationMs: 7000, minGapMs: 83 },
  bbc: { name: "BBC", maxCPS: 20, warnCPS: 16, maxCharsPerLine: 37, maxLines: 2, minDurationMs: 1000, maxDurationMs: 7000, minGapMs: 100 },
};

export const DEFAULT_QC_PRESET = QC_PRESETS.default;

// ---------------------------------------------------------------------------
// CueOperation
// ---------------------------------------------------------------------------

export type CueOperationType =
  | "ADD_CUE"
  | "DELETE_CUES"
  | "EDIT_TEXT"
  | "EDIT_TIMING"
  | "SPLIT_CUE"
  | "MERGE_CUES";

export interface CueOperation {
  type: CueOperationType;
  apply(cues: Cue[]): Cue[];
  inverse(cues: Cue[]): CueOperation;
}

// ---------------------------------------------------------------------------
// Operation factories
// ---------------------------------------------------------------------------

export function createAddCue(afterIndex: number, cue: Cue): CueOperation {
  return {
    type: "ADD_CUE",
    apply(cues) {
      const out = [...cues];
      out.splice(afterIndex + 1, 0, cue);
      return out;
    },
    inverse(_cues) {
      return createDeleteCues([afterIndex + 1]);
    },
  };
}

export function createDeleteCues(indices: number[]): CueOperation {
  const sorted = [...indices].sort((a, b) => a - b);

  return {
    type: "DELETE_CUES",
    apply(cues) {
      return cues.filter((_, i) => !sorted.includes(i));
    },
    inverse(cues) {
      // Capture the cues that will be deleted (before apply runs)
      const snap = sorted.map((i) => ({ index: i, cue: cues[i] }));
      return {
        type: "ADD_CUE" as CueOperationType,
        apply(cues) {
          const out = [...cues];
          for (const { index, cue } of snap) {
            out.splice(index, 0, cue);
          }
          return out;
        },
        inverse() {
          return createDeleteCues(snap.map((s) => s.index));
        },
      };
    },
  };
}

export function createEditText(index: number, newText: string): CueOperation {
  return {
    type: "EDIT_TEXT",
    apply(cues) {
      return cues.map((c, i) => (i === index ? { ...c, text: newText } : c));
    },
    inverse(cues) {
      return createEditText(index, cues[index].text);
    },
  };
}

export function createEditTiming(
  index: number,
  startMs: number,
  endMs: number,
): CueOperation {
  return {
    type: "EDIT_TIMING",
    apply(cues) {
      return cues.map((c, i) =>
        i === index ? { ...c, startMs, endMs } : c,
      );
    },
    inverse(cues) {
      return createEditTiming(index, cues[index].startMs, cues[index].endMs);
    },
  };
}

export function createSplitCue(
  index: number,
  splitAtChar: number,
): CueOperation {
  let firstId: string | null = null;
  let secondId: string | null = null;

  return {
    type: "SPLIT_CUE",
    apply(cues) {
      const cue = cues[index];
      const textBefore = cue.text.slice(0, splitAtChar);
      const textAfter = cue.text.slice(splitAtChar);
      const duration = cue.endMs - cue.startMs;
      const totalChars = cue.text.length;
      const proportion = totalChars > 0 ? splitAtChar / totalChars : 0.5;
      const midpoint = cue.startMs + Math.round(duration * proportion);

      firstId = crypto.randomUUID();
      secondId = crypto.randomUUID();

      const first: Cue = {
        id: firstId,
        startMs: cue.startMs,
        endMs: midpoint,
        text: textBefore,
      };
      const second: Cue = {
        id: secondId,
        startMs: midpoint,
        endMs: cue.endMs,
        text: textAfter,
      };

      const out = [...cues];
      out.splice(index, 1, first, second);
      return out;
    },
    inverse(cues) {
      // The cues array here is the state BEFORE this operation was applied,
      // so we need to merge the two cues that resulted from the split.
      // But inverse is called on the state before apply, so we use the
      // indices that the split will produce.
      return createMergeCues([index, index + 1]);
    },
  };
}

export function createMergeCues(indices: number[]): CueOperation {
  const sorted = [...indices].sort((a, b) => a - b);
  // Validate consecutive
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i] !== sorted[i - 1] + 1) {
      throw new Error("MergeCues requires consecutive indices");
    }
  }

  return {
    type: "MERGE_CUES",
    apply(cues) {
      const targets = sorted.map((i) => cues[i]);
      const texts = targets.map((c) => c.text);
      const starts = targets.map((c) => c.startMs);
      const ends = targets.map((c) => c.endMs);

      const merged: Cue = {
        id: crypto.randomUUID(),
        startMs: Math.min(...starts),
        endMs: Math.max(...ends),
        text: texts.join("\n"),
      };

      const out = [...cues];
      out.splice(sorted[0], sorted.length, merged);
      return out;
    },
    inverse(cues) {
      // Capture the cues that will be merged (before apply runs)
      const snap = sorted.map((i) => cues[i]);
      const insertAt = sorted[0];
      return {
        type: "DELETE_CUES" as CueOperationType,
        apply(cues) {
          const out = [...cues];
          out.splice(insertAt, 1, ...snap);
          return out;
        },
        inverse() {
          return createMergeCues(sorted.map((_, i) => insertAt + i));
        },
      };
    },
  };
}

// ---------------------------------------------------------------------------
// SubtitleDocumentState
// ---------------------------------------------------------------------------

export interface SubtitleDocumentState {
  cues: Cue[];
  undoStack: CueOperation[];
  redoStack: CueOperation[];
  dirty: boolean;
  modifiedCueIds: Set<string>;
}

const MAX_UNDO = 50;

export function createInitialDocumentState(
  cues: Cue[],
): SubtitleDocumentState {
  return {
    cues: [...cues],
    undoStack: [],
    redoStack: [],
    dirty: false,
    modifiedCueIds: new Set(),
  };
}

// ---------------------------------------------------------------------------
// Reducer
// ---------------------------------------------------------------------------

export type DocumentAction =
  | { type: "APPLY_OP"; op: CueOperation }
  | { type: "BATCH_OPS"; ops: CueOperation[] }
  | { type: "UNDO" }
  | { type: "REDO" }
  | { type: "LOAD"; cues: Cue[] }
  | { type: "MARK_SAVED" };

function affectedCueIds(before: Cue[], after: Cue[]): string[] {
  const ids = new Set<string>();
  const beforeIds = new Set(before.map((c) => c.id));
  const afterIds = new Set(after.map((c) => c.id));

  // New cues
  for (const id of afterIds) {
    if (!beforeIds.has(id)) ids.add(id);
  }
  // Removed cues
  for (const id of beforeIds) {
    if (!afterIds.has(id)) ids.add(id);
  }
  // Modified cues (same id, different content)
  const afterMap = new Map(after.map((c) => [c.id, c]));
  for (const c of before) {
    const a = afterMap.get(c.id);
    if (a && (a.text !== c.text || a.startMs !== c.startMs || a.endMs !== c.endMs)) {
      ids.add(c.id);
    }
  }

  return [...ids];
}

export function subtitleDocumentReducer(
  state: SubtitleDocumentState,
  action: DocumentAction,
): SubtitleDocumentState {
  switch (action.type) {
    case "APPLY_OP": {
      const inv = action.op.inverse(state.cues);
      const newCues = action.op.apply(state.cues);
      // Keep cues sorted by start time
      newCues.sort((a, b) => a.startMs - b.startMs);
      const changed = affectedCueIds(state.cues, newCues);
      const newModified = new Set(state.modifiedCueIds);
      for (const id of changed) newModified.add(id);

      const undoStack = [...state.undoStack, inv];
      if (undoStack.length > MAX_UNDO) {
        undoStack.splice(0, undoStack.length - MAX_UNDO);
      }

      return {
        cues: newCues,
        undoStack,
        redoStack: [],
        dirty: true,
        modifiedCueIds: newModified,
      };
    }

    case "BATCH_OPS": {
      // Apply multiple operations as a single undoable unit
      if (action.ops.length === 0) return state;
      let cues = state.cues;
      const inverses: CueOperation[] = [];
      for (const op of action.ops) {
        inverses.push(op.inverse(cues));
        cues = op.apply(cues);
      }
      const changed = affectedCueIds(state.cues, cues);
      const newModified = new Set(state.modifiedCueIds);
      for (const id of changed) newModified.add(id);

      // Create a compound inverse that undoes all ops in reverse order
      const compoundInverse: CueOperation = {
        type: "DELETE_CUES" as CueOperationType, // label doesn't matter
        apply(currentCues) {
          let result = currentCues;
          for (let i = inverses.length - 1; i >= 0; i--) {
            result = inverses[i].apply(result);
          }
          return result;
        },
        inverse(currentCues) {
          // Re-applying the original batch
          let result = currentCues;
          for (const op of action.ops) {
            result = op.apply(result);
          }
          // Return an op that undoes again
          const reInverses = action.ops.map((op, idx) => inverses[idx]);
          return {
            type: "DELETE_CUES" as CueOperationType,
            apply(c) {
              let r = c;
              for (let i = reInverses.length - 1; i >= 0; i--) {
                r = reInverses[i].apply(r);
              }
              return r;
            },
            inverse() {
              return compoundInverse;
            },
          };
        },
      };

      const undoStack = [...state.undoStack, compoundInverse];
      if (undoStack.length > MAX_UNDO) {
        undoStack.splice(0, undoStack.length - MAX_UNDO);
      }

      // Keep cues sorted by start time
      cues.sort((a, b) => a.startMs - b.startMs);

      return {
        cues,
        undoStack,
        redoStack: [],
        dirty: true,
        modifiedCueIds: newModified,
      };
    }

    case "UNDO": {
      if (state.undoStack.length === 0) return state;
      const op = state.undoStack[state.undoStack.length - 1];
      const inv = op.inverse(state.cues);
      const newCues = op.apply(state.cues);
      const changed = affectedCueIds(state.cues, newCues);
      const newModified = new Set(state.modifiedCueIds);
      for (const id of changed) newModified.add(id);

      return {
        cues: newCues,
        undoStack: state.undoStack.slice(0, -1),
        redoStack: [...state.redoStack, inv],
        dirty: true,
        modifiedCueIds: newModified,
      };
    }

    case "REDO": {
      if (state.redoStack.length === 0) return state;
      const op = state.redoStack[state.redoStack.length - 1];
      const inv = op.inverse(state.cues);
      const newCues = op.apply(state.cues);
      const changed = affectedCueIds(state.cues, newCues);
      const newModified = new Set(state.modifiedCueIds);
      for (const id of changed) newModified.add(id);

      return {
        cues: newCues,
        undoStack: [...state.undoStack, inv],
        redoStack: state.redoStack.slice(0, -1),
        dirty: true,
        modifiedCueIds: newModified,
      };
    }

    case "LOAD": {
      return createInitialDocumentState(action.cues);
    }

    case "MARK_DIRTY": {
      return { ...state, dirty: true };
    }

    case "MARK_SAVED": {
      return {
        ...state,
        dirty: false,
        modifiedCueIds: new Set(),
      };
    }
  }
}

// ---------------------------------------------------------------------------
// Quality warnings
// ---------------------------------------------------------------------------

export interface CueWarning {
  level: "red" | "orange" | "yellow";
  message: string;
}

export function computeCueWarnings(
  cue: Cue,
  prevCue: Cue | null,
  nextCue: Cue | null,
  preset: QCPreset = DEFAULT_QC_PRESET,
): CueWarning | null {
  const durationMs = cue.endMs - cue.startMs;
  const durationSec = durationMs / 1000;
  const charCount = cue.text.replace(/\n/g, "").length;
  const cps = durationSec > 0 ? charCount / durationSec : Infinity;
  const lines = cue.text.split("\n");

  // Red checks
  if (cue.endMs < cue.startMs) {
    return { level: "red", message: "End time is before start time" };
  }
  if (prevCue && cue.startMs < prevCue.endMs) {
    return { level: "red", message: "Overlaps with previous cue" };
  }
  if (cue.text.trim() === "") {
    return { level: "red", message: "Empty text" };
  }

  // Orange checks
  if (cps > preset.maxCPS) {
    return {
      level: "orange",
      message: `CPS too high (${cps.toFixed(1)}, max ${preset.maxCPS})`,
    };
  }
  if (durationMs < preset.minDurationMs && durationMs > 0) {
    return {
      level: "orange",
      message: `Duration too short (${durationMs.toFixed(0)}ms, min ${preset.minDurationMs}ms)`,
    };
  }
  if (lines.some((l) => l.replace(/<[^>]*>|\{\\[^}]*\}/g, "").length > preset.maxCharsPerLine)) {
    const longest = Math.max(...lines.map((l) => l.replace(/<[^>]*>|\{\\[^}]*\}/g, "").length));
    return {
      level: "orange",
      message: `Line too long (${longest} chars, max ${preset.maxCharsPerLine})`,
    };
  }
  if (lines.length > preset.maxLines) {
    return {
      level: "orange",
      message: `Too many lines (${lines.length}, max ${preset.maxLines})`,
    };
  }

  // Yellow checks
  if (cps >= preset.warnCPS && cps <= preset.maxCPS) {
    return {
      level: "yellow",
      message: `CPS is high (${cps.toFixed(1)}, warn at ${preset.warnCPS})`,
    };
  }
  if (durationMs > preset.maxDurationMs) {
    return {
      level: "yellow",
      message: `Duration too long (${(durationSec).toFixed(1)}s, max ${(preset.maxDurationMs / 1000).toFixed(1)}s)`,
    };
  }
  if (
    prevCue &&
    cue.startMs - prevCue.endMs < preset.minGapMs &&
    cue.startMs - prevCue.endMs > 0
  ) {
    return {
      level: "yellow",
      message: `Small gap with previous cue (${cue.startMs - prevCue.endMs}ms, min ${preset.minGapMs}ms)`,
    };
  }

  void nextCue;
  return null;
}

// ---------------------------------------------------------------------------
// Gap detection (ghost cues)
// ---------------------------------------------------------------------------

export interface GhostCue {
  startMs: number;
  endMs: number;
  afterIndex: number;
}

export function detectGaps(cues: Cue[], minGapMs = 500): GhostCue[] {
  if (cues.length < 2) return [];
  // Sort by start time to handle out-of-order cues
  const sorted = [...cues].map((c, i) => ({ cue: c, originalIndex: i }));
  sorted.sort((a, b) => a.cue.startMs - b.cue.startMs);

  const gaps: GhostCue[] = [];
  for (let i = 0; i < sorted.length - 1; i++) {
    const gap = sorted[i + 1].cue.startMs - sorted[i].cue.endMs;
    if (gap > minGapMs) {
      gaps.push({
        startMs: sorted[i].cue.endMs,
        endMs: sorted[i + 1].cue.startMs,
        afterIndex: sorted[i].originalIndex,
      });
    }
  }
  return gaps;
}
