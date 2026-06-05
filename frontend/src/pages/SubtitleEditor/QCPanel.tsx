import { type CSSProperties, useCallback, useMemo } from "react";
import {
  computeCueWarnings,
  createDeleteCues,
  createEditText,
  createEditTiming,
  type CueOperation,
  type CueWarning,
  QC_PRESETS,
  type QCPreset,
} from "./document";
import { stripTags } from "./stripTags";
import type { Cue } from "./types";

interface QCPanelProps {
  open: boolean;
  cues: Cue[];
  preset: QCPreset;
  // Combined outputs stack languages at shared timestamps; their overlaps and
  // zero gaps are intentional, so the overlap/gap checks are suppressed.
  combined?: boolean;
  onPresetChange: (preset: QCPreset) => void;
  onApplyFixes: (ops: CueOperation[]) => void;
  onNavigate: (cueIndex: number) => void;
  onClose: () => void;
}

// ---------------------------------------------------------------------------
// Fix functions
// ---------------------------------------------------------------------------

function fixRemoveHI(text: string): string {
  return text
    .replace(/\[.*?\]/g, "")
    .replace(/\(.*?\)/g, "")
    .replace(/♪.*?♪/gs, "")
    .replace(/^\s*[-\u2013]\s*/gm, "")
    .replace(/\n{2,}/g, "\n")
    .trim();
}

function fixWhitespace(text: string): string {
  return text
    .split("\n")
    .map((line) => line.replace(/  +/g, " ").trim())
    .filter((line) => line.length > 0)
    .join("\n");
}

function normalizeEllipsis(text: string): string {
  return text
    .replace(/\.{2}\s/g, "... ")
    .replace(/\.{4,}/g, "...")
    .replace(/\.\.\s\./g, "...")
    .replace(/\.{2}(?!\.)/g, "...");
}

function rebreakLines(text: string, maxChars: number): string {
  const rawLines = text.split("\n");
  const needsRebreak = rawLines.some((l) => stripTags(l).length > maxChars);
  if (!needsRebreak) return text;

  const words = text.replace(/\n/g, " ").split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let current = "";

  for (const word of words) {
    const test = current ? current + " " + word : word;
    if (stripTags(test).length <= maxChars) {
      current = test;
    } else {
      if (current) lines.push(current);
      current = word;
    }
  }
  if (current) lines.push(current);

  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// Count helpers
// ---------------------------------------------------------------------------

function countHI(cues: Cue[]): number {
  return cues.filter((c) => /\[.*?\]|\(.*?\)|♪.*?♪/s.test(c.text)).length;
}

function countWhitespace(cues: Cue[]): number {
  return cues.filter((c) => {
    const fixed = fixWhitespace(c.text);
    return fixed !== c.text;
  }).length;
}

function countOverlaps(cues: Cue[]): number {
  let count = 0;
  for (let i = 1; i < cues.length; i++) {
    if (cues[i].startMs < cues[i - 1].endMs) count++;
  }
  return count;
}

function countShortGaps(cues: Cue[], minGapMs: number): number {
  let count = 0;
  for (let i = 1; i < cues.length; i++) {
    const gap = cues[i].startMs - cues[i - 1].endMs;
    if (gap > 0 && gap < minGapMs) count++;
  }
  return count;
}

function countEmpty(cues: Cue[]): number {
  return cues.filter((c) => c.text.trim() === "").length;
}

function countEllipsis(cues: Cue[]): number {
  return cues.filter((c) => normalizeEllipsis(c.text) !== c.text).length;
}

function countLongLines(cues: Cue[], maxChars: number): number {
  return cues.filter((c) =>
    c.text.split("\n").some((l) => stripTags(l).length > maxChars),
  ).length;
}

// ---------------------------------------------------------------------------
// Build operations for fixes
// ---------------------------------------------------------------------------

function buildRemoveHIOps(cues: Cue[]): CueOperation[] {
  const ops: CueOperation[] = [];
  const pattern = /\[.*?\]|\(.*?\)|♪.*?♪/gs;
  for (let i = 0; i < cues.length; i++) {
    pattern.lastIndex = 0;
    if (pattern.test(cues[i].text)) {
      const fixed = fixRemoveHI(cues[i].text);
      if (fixed !== cues[i].text) {
        ops.push(createEditText(i, fixed));
      }
    }
  }
  return ops;
}

function buildWhitespaceOps(cues: Cue[]): CueOperation[] {
  const ops: CueOperation[] = [];
  for (let i = 0; i < cues.length; i++) {
    const fixed = fixWhitespace(cues[i].text);
    if (fixed !== cues[i].text) {
      ops.push(createEditText(i, fixed));
    }
  }
  return ops;
}

function buildOverlapOps(cues: Cue[], minGapMs: number): CueOperation[] {
  const ops: CueOperation[] = [];
  for (let i = 1; i < cues.length; i++) {
    if (cues[i].startMs < cues[i - 1].endMs) {
      const newEnd = cues[i].startMs - minGapMs;
      ops.push(
        createEditTiming(
          i - 1,
          cues[i - 1].startMs,
          Math.max(cues[i - 1].startMs, newEnd),
        ),
      );
    }
  }
  return ops;
}

function buildShortGapOps(cues: Cue[], minGapMs: number): CueOperation[] {
  const ops: CueOperation[] = [];
  for (let i = 1; i < cues.length; i++) {
    const gap = cues[i].startMs - cues[i - 1].endMs;
    if (gap > 0 && gap < minGapMs) {
      ops.push(createEditTiming(i - 1, cues[i - 1].startMs, cues[i].startMs));
    }
  }
  return ops;
}

function buildRemoveEmptyOps(cues: Cue[]): CueOperation[] {
  const indices: number[] = [];
  for (let i = 0; i < cues.length; i++) {
    if (cues[i].text.trim() === "") indices.push(i);
  }
  if (indices.length === 0) return [];
  return [createDeleteCues(indices)];
}

function buildEllipsisOps(cues: Cue[]): CueOperation[] {
  const ops: CueOperation[] = [];
  for (let i = 0; i < cues.length; i++) {
    const fixed = normalizeEllipsis(cues[i].text);
    if (fixed !== cues[i].text) {
      ops.push(createEditText(i, fixed));
    }
  }
  return ops;
}

function buildLineBreakOps(cues: Cue[], maxChars: number): CueOperation[] {
  const ops: CueOperation[] = [];
  for (let i = 0; i < cues.length; i++) {
    const fixed = rebreakLines(cues[i].text, maxChars);
    if (fixed !== cues[i].text) {
      ops.push(createEditText(i, fixed));
    }
  }
  return ops;
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const panelStyle: CSSProperties = {
  position: "absolute",
  top: 8,
  right: 8,
  zIndex: 10,
  width: 360,
  maxHeight: "calc(100% - 16px)",
  overflowY: "auto",
  background: "var(--bz-surface-raised)",
  border: "1px solid var(--bz-border-card)",
  borderRadius: "var(--bz-radius-sm)",
  padding: "10px 14px",
  boxShadow: "var(--bz-shadow-float)",
  display: "flex",
  flexDirection: "column",
  gap: 10,
};

const headerStyle: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
};

const titleStyle: CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: "var(--bz-text-primary)",
  letterSpacing: "0.3px",
};

const selectStyle: CSSProperties = {
  width: "100%",
  fontFamily: "monospace",
  fontSize: 13,
  background: "var(--bz-surface-base)",
  border: "1px solid var(--bz-border-interactive)",
  color: "var(--bz-text-primary)",
  borderRadius: 4,
  padding: "6px 8px",
  outline: "none",
};

const summaryRow: CSSProperties = {
  display: "flex",
  gap: 8,
  fontSize: 12,
  alignItems: "center",
};

const dotStyle = (color: string): CSSProperties => ({
  width: 8,
  height: 8,
  borderRadius: "50%",
  background: color,
  flexShrink: 0,
});

const fixRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  fontSize: 12,
  color: "var(--bz-text-secondary)",
  padding: "4px 0",
};

const fixCountStyle: CSSProperties = {
  fontSize: 11,
  color: "var(--bz-text-tertiary)",
  marginLeft: "auto",
  flexShrink: 0,
};

const btnStyle: CSSProperties = {
  background: "var(--bz-surface-card)",
  border: "1px solid var(--bz-border-interactive)",
  color: "var(--bz-text-primary)",
  borderRadius: 4,
  padding: "3px 8px",
  cursor: "pointer",
  fontSize: 11,
  flexShrink: 0,
};

const btnDisabled: CSSProperties = {
  ...btnStyle,
  opacity: 0.4,
  cursor: "default",
};

const closeBtnStyle: CSSProperties = {
  background: "transparent",
  border: "1px solid var(--bz-border-interactive)",
  color: "var(--bz-text-secondary)",
  borderRadius: 4,
  padding: "2px 6px",
  cursor: "pointer",
  fontSize: 12,
  lineHeight: 1,
};

const separatorStyle: CSSProperties = {
  height: 1,
  background: "var(--bz-border-divider)",
  margin: "4px 0",
};

const sectionTitle: CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: "var(--bz-text-secondary)",
  textTransform: "uppercase",
  letterSpacing: "0.5px",
  marginBottom: 2,
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function QCPanel({
  open,
  cues,
  preset,
  combined = false,
  onPresetChange,
  onApplyFixes,
  onNavigate,
  onClose,
}: QCPanelProps) {
  const handlePresetSelect = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      const key = e.target.value;
      if (QC_PRESETS[key]) {
        onPresetChange(QC_PRESETS[key]);
      }
    },
    [onPresetChange],
  );

  // Compute warning summary
  const warningSummary = useMemo(() => {
    let red = 0;
    let orange = 0;
    let yellow = 0;
    const warningMap = new Map<number, CueWarning>();
    cues.forEach((cue, i) => {
      const w = computeCueWarnings(
        cue,
        cues[i - 1] ?? null,
        cues[i + 1] ?? null,
        preset,
        combined,
      );
      if (w) {
        warningMap.set(i, w);
        if (w.level === "red") red++;
        else if (w.level === "orange") orange++;
        else yellow++;
      }
    });
    return { red, orange, yellow, warningMap };
  }, [cues, preset, combined]);

  // Navigate to first warning of a given level
  const navigateToLevel = useCallback(
    (level: "red" | "orange" | "yellow") => {
      for (const [idx, w] of warningSummary.warningMap) {
        if (w.level === level) {
          onNavigate(idx);
          return;
        }
      }
    },
    [warningSummary.warningMap, onNavigate],
  );

  // Fix counts
  const counts = useMemo(
    () => ({
      hi: countHI(cues),
      whitespace: countWhitespace(cues),
      // Combined outputs stack languages at the same timestamp; their overlaps
      // and zero gaps are intentional, so do not offer to "fix" them.
      overlaps: combined ? 0 : countOverlaps(cues),
      shortGaps: combined ? 0 : countShortGaps(cues, preset.minGapMs),
      empty: countEmpty(cues),
      ellipsis: countEllipsis(cues),
      longLines: countLongLines(cues, preset.maxCharsPerLine),
    }),
    [cues, preset, combined],
  );

  const applyFix = useCallback(
    (fixId: string) => {
      let ops: CueOperation[] = [];
      switch (fixId) {
        case "hi":
          ops = buildRemoveHIOps(cues);
          break;
        case "whitespace":
          ops = buildWhitespaceOps(cues);
          break;
        case "overlaps":
          ops = buildOverlapOps(cues, preset.minGapMs);
          break;
        case "shortGaps":
          ops = buildShortGapOps(cues, preset.minGapMs);
          break;
        case "empty":
          ops = buildRemoveEmptyOps(cues);
          break;
        case "ellipsis":
          ops = buildEllipsisOps(cues);
          break;
        case "longLines":
          ops = buildLineBreakOps(cues, preset.maxCharsPerLine);
          break;
      }
      if (ops.length > 0) {
        onApplyFixes(ops);
      }
    },
    [cues, preset, onApplyFixes],
  );

  if (!open) return null;

  const presetKey =
    Object.entries(QC_PRESETS).find(([, p]) => p.name === preset.name)?.[0] ??
    "default";

  const fixes: Array<{ id: string; label: string; count: number }> = [
    { id: "hi", label: "Remove HI tags", count: counts.hi },
    { id: "whitespace", label: "Fix whitespace", count: counts.whitespace },
    { id: "overlaps", label: "Fix overlaps", count: counts.overlaps },
    { id: "shortGaps", label: "Fix short gaps", count: counts.shortGaps },
    { id: "empty", label: "Remove empty cues", count: counts.empty },
    { id: "ellipsis", label: "Normalize ellipsis", count: counts.ellipsis },
    { id: "longLines", label: "Fix line breaks", count: counts.longLines },
  ];

  return (
    <div style={panelStyle}>
      {/* Header */}
      <div style={headerStyle}>
        <span style={titleStyle}>Quality Control</span>
        <button
          type="button"
          style={closeBtnStyle}
          onClick={onClose}
          title="Close"
        >
          &times;
        </button>
      </div>

      {/* Preset selector */}
      <div>
        <div style={sectionTitle}>QC Preset</div>
        <select
          style={selectStyle}
          value={presetKey}
          onChange={handlePresetSelect}
        >
          {Object.entries(QC_PRESETS).map(([key, p]) => (
            <option key={key} value={key}>
              {p.name}
            </option>
          ))}
        </select>
        <div
          style={{
            display: "flex",
            gap: 12,
            fontSize: 11,
            color: "var(--bz-text-tertiary)",
            marginTop: 4,
            flexWrap: "wrap",
          }}
        >
          <span>
            CPS: {preset.warnCPS}/{preset.maxCPS}
          </span>
          <span>Line: {preset.maxCharsPerLine}ch</span>
          <span>Min: {preset.minDurationMs}ms</span>
          <span>Gap: {preset.minGapMs}ms</span>
        </div>
      </div>

      {/* Warning summary */}
      <div>
        <div style={sectionTitle}>Violations</div>
        <div style={summaryRow}>
          {warningSummary.red > 0 && (
            <span
              style={{
                display: "flex",
                alignItems: "center",
                gap: 4,
                cursor: "pointer",
              }}
              onClick={() => navigateToLevel("red")}
              title="Click to navigate to first error"
            >
              <span style={dotStyle("#EF4444")} />
              <span style={{ color: "#EF4444" }}>
                {warningSummary.red} error{warningSummary.red !== 1 ? "s" : ""}
              </span>
            </span>
          )}
          {warningSummary.orange > 0 && (
            <span
              style={{
                display: "flex",
                alignItems: "center",
                gap: 4,
                cursor: "pointer",
              }}
              onClick={() => navigateToLevel("orange")}
              title="Click to navigate to first warning"
            >
              <span style={dotStyle("#F97316")} />
              <span style={{ color: "#F97316" }}>
                {warningSummary.orange} warn
              </span>
            </span>
          )}
          {warningSummary.yellow > 0 && (
            <span
              style={{
                display: "flex",
                alignItems: "center",
                gap: 4,
                cursor: "pointer",
              }}
              onClick={() => navigateToLevel("yellow")}
              title="Click to navigate to first notice"
            >
              <span style={dotStyle("#EAB308")} />
              <span style={{ color: "#EAB308" }}>
                {warningSummary.yellow} notice
                {warningSummary.yellow !== 1 ? "s" : ""}
              </span>
            </span>
          )}
          {warningSummary.red === 0 &&
            warningSummary.orange === 0 &&
            warningSummary.yellow === 0 && (
              <span style={{ color: "#22c55e" }}>All clear</span>
            )}
        </div>
      </div>

      <div style={separatorStyle} />

      {/* Fix common errors */}
      <div>
        <div style={sectionTitle}>Fix Common Errors</div>
        {fixes.map((fix) => (
          <div key={fix.id} style={fixRowStyle}>
            <span>{fix.label}</span>
            <span style={fixCountStyle}>
              {fix.count > 0
                ? `${fix.count} cue${fix.count !== 1 ? "s" : ""}`
                : "none"}
            </span>
            <button
              type="button"
              style={fix.count > 0 ? btnStyle : btnDisabled}
              disabled={fix.count === 0}
              onClick={() => {
                if (fix.count > 0) applyFix(fix.id);
              }}
            >
              Apply
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
