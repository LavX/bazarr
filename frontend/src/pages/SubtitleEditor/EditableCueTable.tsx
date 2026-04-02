import { Fragment, ReactNode, useCallback, useEffect, useMemo, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { Cue } from "./types";
import type { CueWarning, GhostCue } from "./document";
import { formatTimestamp, formatDuration } from "./CueTable";

// ---- Tag rendering (copied from CueTable since not exported) ----

const TAG_COLORS: Record<string, { bg: string; text: string }> = {
  i: { bg: "rgba(139, 92, 246, 0.2)", text: "#a78bfa" },
  b: { bg: "rgba(251, 146, 60, 0.2)", text: "#fb923c" },
  u: { bg: "rgba(52, 211, 153, 0.2)", text: "#34d399" },
  font: { bg: "rgba(96, 165, 250, 0.2)", text: "#60a5fa" },
  s: { bg: "rgba(248, 113, 113, 0.2)", text: "#f87171" },
  pos: { bg: "rgba(251, 191, 36, 0.2)", text: "#fbbf24" },
  align: { bg: "rgba(251, 191, 36, 0.2)", text: "#fbbf24" },
  effect: { bg: "rgba(168, 85, 247, 0.2)", text: "#a855f7" },
  color: { bg: "rgba(96, 165, 250, 0.2)", text: "#60a5fa" },
  fontAss: { bg: "rgba(96, 165, 250, 0.2)", text: "#60a5fa" },
};

const DEFAULT_TAG_COLOR = { bg: "rgba(148, 163, 184, 0.15)", text: "#94a3b8" };

function tagBadgeStyle(colorKey: string): React.CSSProperties {
  const colors = TAG_COLORS[colorKey] ?? DEFAULT_TAG_COLOR;
  return {
    display: "inline-block",
    fontSize: "0.6rem",
    fontFamily: "monospace",
    fontWeight: 600,
    lineHeight: 1,
    padding: "2px 5px",
    borderRadius: "3px",
    backgroundColor: colors.bg,
    color: colors.text,
    verticalAlign: "middle",
    marginInline: "1px",
  };
}

function parseAssTagBlock(block: string): { label: string; colorKey: string } {
  const inner = block.slice(1, -1);
  const patterns: [RegExp, string, string][] = [
    [/^\\an(\d)$/, "align-$1", "align"],
    [/^\\a(\d+)$/, "align-$1", "align"],
    [/\\pos\([^)]+\)/, "pos", "pos"],
    [/\\move\([^)]+\)/, "move", "pos"],
    [/\\org\([^)]+\)/, "org", "pos"],
    [/\\fad\([^)]+\)/, "fade", "effect"],
    [/\\fade\([^)]+\)/, "fade", "effect"],
    [/\\be\d/, "blur", "effect"],
    [/\\blur[\d.]/, "blur", "effect"],
    [/\\bord[\d.]/, "border", "effect"],
    [/\\shad[\d.]/, "shadow", "effect"],
    [/\\fr[xyz]?[\d.-]/, "rotate", "effect"],
    [/\\fsc[xy][\d.]/, "scale", "effect"],
    [/\\1?c&H/, "color", "color"],
    [/\\2c&H/, "color2", "color"],
    [/\\3c&H/, "border-color", "color"],
    [/\\4c&H/, "shadow-color", "color"],
    [/\\alpha/, "alpha", "effect"],
    [/\\[1234]a&H/, "alpha", "effect"],
    [/\\fn/, "font", "fontAss"],
    [/\\fs[\d.]/, "size", "fontAss"],
    [/\\b[01]/, "bold", "b"],
    [/\\i[01]/, "italic", "i"],
    [/\\u[01]/, "underline", "u"],
    [/\\s[01]/, "strike", "s"],
    [/\\k[fo]?\d/, "karaoke", "effect"],
    [/\\i?clip\(/, "clip", "effect"],
    [/\\p[01]/, "drawing", "effect"],
    [/\\fsp[\d.-]/, "spacing", "fontAss"],
    [/\\fe\d/, "encoding", "fontAss"],
    [/\\t\(/, "animate", "effect"],
    [/\\q\d/, "wrap", "align"],
    [/\\r/, "reset", "effect"],
  ];

  const labels: string[] = [];
  let colorKey = "effect";

  for (const [pattern, label, category] of patterns) {
    if (pattern.test(inner)) {
      labels.push(label.replace("$1", inner.match(/\d+/)?.[0] ?? ""));
      colorKey = category;
    }
  }

  if (labels.length > 0) {
    return { label: labels.join(" "), colorKey };
  }

  const abbreviated = inner.length > 12 ? inner.slice(0, 12) + ".." : inner;
  return { label: abbreviated, colorKey: "effect" };
}

function renderStyledText(text: string): ReactNode {
  const tagPattern = /<\/?([a-zA-Z]+)(?:\s[^>]*)?>|\{\\[^}]+\}/g;
  const parts: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;

  while ((match = tagPattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(
        <Fragment key={key++}>{text.slice(lastIndex, match.index)}</Fragment>,
      );
    }

    const fullMatch = match[0];

    if (fullMatch.startsWith("{\\")) {
      const { label, colorKey } = parseAssTagBlock(fullMatch);
      parts.push(
        <span key={key++} style={tagBadgeStyle(colorKey)}>
          {label}
        </span>,
      );
    } else {
      const tagName = match[1].toLowerCase();
      const isClosing = fullMatch.startsWith("</");
      parts.push(
        <span key={key++} style={tagBadgeStyle(tagName)}>
          {isClosing ? `/${tagName}` : tagName}
        </span>,
      );
    }

    lastIndex = match.index + fullMatch.length;
  }

  if (lastIndex < text.length) {
    parts.push(<Fragment key={key++}>{text.slice(lastIndex)}</Fragment>);
  }

  return parts.length > 0 ? parts : text;
}

// ---- Merged row types ----

type MergedRow =
  | { type: "cue"; index: number; cue: Cue }
  | { type: "ghost"; ghost: GhostCue };

// ---- Warning gutter colors ----

const WARNING_COLORS: Record<CueWarning["level"], string> = {
  red: "#EF4444",
  orange: "#F97316",
  yellow: "#EAB308",
};

const WARNING_SYMBOLS: Record<CueWarning["level"], string> = {
  red: "\u2716",     // heavy X
  orange: "\u26A0",  // warning triangle
  yellow: "\u25CF",  // filled circle
};

// ---- Grid template ----

const gridTemplate = "20px 24px 40px 140px 140px 72px 1fr";

// ---- Styles ----

const headerStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: gridTemplate,
  minWidth: 724,
  position: "sticky",
  top: 0,
  zIndex: 1,
  backgroundColor: "var(--mantine-color-body)",
  borderBottom: "2px solid var(--bz-border-card)",
  fontWeight: 700,
  fontSize: "10px",
  textTransform: "uppercase",
  letterSpacing: "0.8px",
  color: "var(--bz-text-tertiary)",
};

const headerCellStyle: React.CSSProperties = {
  padding: "6px 10px",
};

const rowBaseStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: gridTemplate,
  minWidth: 724,
  alignItems: "stretch",
  borderBottom: "1px solid rgba(255,255,255,0.04)",
  cursor: "pointer",
  transition: "background-color 0.1s ease",
};

const cellStyle: React.CSSProperties = {
  padding: "5px 10px",
  fontSize: "0.82rem",
  lineHeight: 1.4,
};

const monoStyle: React.CSSProperties = {
  ...cellStyle,
  fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
  fontSize: "0.8rem",
  color: "var(--bz-stat-processing)",
  letterSpacing: "-0.2px",
};

const durationStyle: React.CSSProperties = {
  ...cellStyle,
  fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
  fontSize: "0.8rem",
  color: "var(--bz-text-tertiary)",
  textAlign: "center",
};

const textCellStyle: React.CSSProperties = {
  ...cellStyle,
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
  color: "var(--mantine-color-text)",
};

const indexCellStyle: React.CSSProperties = {
  ...cellStyle,
  color: "var(--bz-text-tertiary)",
  fontSize: "0.7rem",
  textAlign: "right",
  fontFamily: "monospace",
  paddingRight: "12px",
};

// ---- Props ----

interface EditableCueTableProps {
  cues: Cue[];
  selectedIndex: number;
  multiSelect: Set<number>;
  warnings: Map<number, CueWarning>;
  ghostCues: GhostCue[];
  modifiedCueIds: Set<string>;
  bookmarkedIds: Set<string>;
  visibleCueIds?: Set<string>;
  onSelect: (index: number) => void;
  onMultiSelect: (indices: Set<number>) => void;
  onActivate: (index: number) => void;
  onCreateFromGap: (startMs: number, endMs: number, afterIndex: number) => void;
  onToggleBookmark: (cueId: string) => void;
}

const HEADER_COLUMNS = [
  { label: "", align: undefined },           // gutter
  { label: "", align: "center" as const },   // bookmark
  { label: "#", align: "right" as const },
  { label: "START", align: undefined },
  { label: "END", align: undefined },
  { label: "DURATION", align: "center" as const },
  { label: "TEXT", align: undefined },
] as const;

export default function EditableCueTable({
  cues,
  selectedIndex,
  multiSelect,
  warnings,
  ghostCues,
  modifiedCueIds,
  bookmarkedIds,
  visibleCueIds,
  onSelect,
  onMultiSelect,
  onActivate,
  onCreateFromGap,
  onToggleBookmark,
}: EditableCueTableProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);

  // Build merged array of cue rows and ghost rows, sorted by time
  const mergedRows = useMemo<MergedRow[]>(() => {
    const rows: MergedRow[] = cues.map((cue, index) => ({
      type: "cue" as const,
      index,
      cue,
    }));

    // Insert ghost cues at their correct positions
    // ghostCue.afterIndex means it goes between cues[afterIndex] and cues[afterIndex+1]
    // We insert them in reverse order so splice indices stay valid
    const ghostsSorted = [...ghostCues].sort(
      (a, b) => a.afterIndex - b.afterIndex,
    );

    let offset = 0;
    for (const ghost of ghostsSorted) {
      // Insert after the cue row at ghost.afterIndex
      const insertAt = ghost.afterIndex + 1 + offset;
      rows.splice(insertAt, 0, { type: "ghost", ghost });
      offset++;
    }

    return rows;
  }, [cues, ghostCues]);

  const displayRows = useMemo(() => {
    if (!visibleCueIds) return mergedRows;
    return mergedRows.filter(
      (r) => r.type === "cue" && visibleCueIds.has(r.cue.id),
    );
  }, [mergedRows, visibleCueIds]);

  const virtualizer = useVirtualizer({
    count: displayRows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 36,
    measureElement: (el) => el?.getBoundingClientRect().height ?? 36,
    overscan: 10,
  });

  // Re-measure all rows when cue content changes (text edits may change row height)
  useEffect(() => {
    virtualizer.measure();
  }, [cues, virtualizer]);

  // Scroll to selected cue when selectedIndex changes (e.g. from search, jump-to-cue)
  useEffect(() => {
    if (selectedIndex < 0) return;
    // Find the merged row index for this cue
    const mergedIdx = displayRows.findIndex(
      (r) => r.type === "cue" && r.index === selectedIndex,
    );
    if (mergedIdx >= 0) {
      virtualizer.scrollToIndex(mergedIdx, { align: "auto" });
    }
  }, [selectedIndex, displayRows, virtualizer]);

  const handleRowClick = useCallback(
    (e: React.MouseEvent, cueIndex: number) => {
      if (e.shiftKey) {
        // Range select
        const start = Math.min(selectedIndex, cueIndex);
        const end = Math.max(selectedIndex, cueIndex);
        const newSet = new Set(multiSelect);
        for (let i = start; i <= end; i++) {
          newSet.add(i);
        }
        onMultiSelect(newSet);
      } else if (e.ctrlKey || e.metaKey) {
        // Toggle in multiSelect
        const newSet = new Set(multiSelect);
        if (newSet.has(cueIndex)) {
          newSet.delete(cueIndex);
        } else {
          newSet.add(cueIndex);
        }
        onMultiSelect(newSet);
        onSelect(cueIndex);
      } else {
        onSelect(cueIndex);
        onMultiSelect(new Set());
      }
    },
    [selectedIndex, multiSelect, onSelect, onMultiSelect],
  );

  const handleRowDoubleClick = useCallback(
    (_e: React.MouseEvent, cueIndex: number) => {
      onActivate(cueIndex);
    },
    [onActivate],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        const next = Math.min(selectedIndex + 1, cues.length - 1);
        onSelect(next);
        onMultiSelect(new Set());
        // Scroll into view
        const mergedIdx = displayRows.findIndex(
          (r) => r.type === "cue" && r.index === next,
        );
        if (mergedIdx >= 0) virtualizer.scrollToIndex(mergedIdx);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        const prev = Math.max(selectedIndex - 1, 0);
        onSelect(prev);
        onMultiSelect(new Set());
        const mergedIdx = displayRows.findIndex(
          (r) => r.type === "cue" && r.index === prev,
        );
        if (mergedIdx >= 0) virtualizer.scrollToIndex(mergedIdx);
      } else if (e.key === "Enter" || e.key === "F2") {
        e.preventDefault();
        onActivate(selectedIndex);
      }
    },
    [selectedIndex, cues.length, displayRows, virtualizer, onSelect, onMultiSelect, onActivate],
  );

  const selectedRowId =
    selectedIndex >= 0 && selectedIndex < cues.length
      ? `editable-cue-row-${selectedIndex}`
      : undefined;

  return (
    <div
      ref={gridRef}
      role="grid"
      aria-label="Subtitle cues"
      aria-rowcount={cues.length}
      aria-activedescendant={selectedRowId}
      tabIndex={0}
      onKeyDown={handleKeyDown}
      style={{
        display: "flex",
        flexDirection: "column",
        flex: 1,
        minHeight: 0,
        overflow: "hidden",
        border: "1px solid var(--bz-border-divider)",
        borderRadius: "var(--mantine-radius-md)",
        backgroundColor: "var(--bz-surface-base)",
        minWidth: 0,
        outline: "none",
      }}
    >
      {/* Header */}
      <div role="row" style={headerStyle}>
        {HEADER_COLUMNS.map((col, i) => (
          <div
            key={i}
            role="columnheader"
            style={{
              ...headerCellStyle,
              textAlign: col.align,
              paddingRight: col.label === "#" ? "16px" : undefined,
              padding: col.label === "" ? 0 : undefined,
            }}
          >
            {col.label}
          </div>
        ))}
      </div>

      {/* Scrollable body */}
      <div
        ref={scrollRef}
        style={{
          flex: 1,
          overflow: "auto",
          minHeight: 0,
        }}
      >
        <div
          style={{
            height: virtualizer.getTotalSize(),
            position: "relative",
            width: "100%",
          }}
        >
          {virtualizer.getVirtualItems().map((virtualRow) => {
            const row = displayRows[virtualRow.index];

            if (row.type === "ghost") {
              return (
                <div
                  key={`ghost-${row.ghost.afterIndex}`}
                  data-index={virtualRow.index}
                  ref={virtualizer.measureElement}
                  role="row"
                  onClick={() =>
                    onCreateFromGap(
                      row.ghost.startMs,
                      row.ghost.endMs,
                      row.ghost.afterIndex,
                    )
                  }
                  style={{
                    ...rowBaseStyle,
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    transform: `translateY(${virtualRow.start}px)`,
                    backgroundColor: "rgba(255,255,255,0.01)",
                    cursor: "pointer",
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.backgroundColor = "rgba(230,138,0,0.04)";
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.backgroundColor = "rgba(255,255,255,0.01)";
                  }}
                >
                  <div style={{ width: 20 }} />
                  <div role="gridcell" style={{ width: 24 }} />
                  <div role="gridcell" style={{ ...indexCellStyle, fontStyle: "italic", color: "var(--bz-text-disabled)" }}>+</div>
                  <div role="gridcell" style={{ ...monoStyle, fontStyle: "italic", color: "var(--bz-text-disabled)" }}>{formatTimestamp(row.ghost.startMs)}</div>
                  <div role="gridcell" style={{ ...monoStyle, fontStyle: "italic", color: "var(--bz-text-disabled)" }}>{formatTimestamp(row.ghost.endMs)}</div>
                  <div role="gridcell" style={{ ...durationStyle, fontStyle: "italic", color: "var(--bz-text-disabled)" }}>{formatDuration(row.ghost.endMs - row.ghost.startMs)}</div>
                  <div role="gridcell" style={{ ...textCellStyle, fontStyle: "italic", color: "var(--bz-text-disabled)" }}>gap detected, click to add cue</div>
                </div>
              );
            }

            // Regular cue row
            const cueIndex = row.index;
            const cue = row.cue;
            const isSelected = cueIndex === selectedIndex;
            const isMultiSelected = multiSelect.has(cueIndex);
            const isModified = modifiedCueIds.has(cue.id);
            const warning = warnings.get(cueIndex);

            // Determine left border and background
            let borderLeft: string | undefined;
            let backgroundColor = "transparent";

            if (isSelected) {
              borderLeft = "3px solid var(--mantine-color-brand-5)";
              backgroundColor = "rgba(230,138,0,0.08)";
            } else if (isMultiSelected) {
              backgroundColor = "rgba(59,130,246,0.06)";
              if (isModified) {
                borderLeft = "2px solid #f59e0b";
              }
            } else if (isModified) {
              borderLeft = "2px solid #f59e0b";
            }

            const gutterColor = warning
              ? WARNING_COLORS[warning.level]
              : "transparent";

            return (
              <div
                key={cue.id}
                id={`editable-cue-row-${cueIndex}`}
                data-index={virtualRow.index}
                ref={virtualizer.measureElement}
                role="row"
                aria-selected={isSelected}
                aria-rowindex={cueIndex + 1}
                onClick={(e) => handleRowClick(e, cueIndex)}
                onDoubleClick={(e) => handleRowDoubleClick(e, cueIndex)}
                style={{
                  ...rowBaseStyle,
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  transform: `translateY(${virtualRow.start}px)`,
                  backgroundColor,
                  borderLeft,
                }}
                onMouseEnter={(e) => {
                  if (!isSelected && !isMultiSelected) {
                    e.currentTarget.style.backgroundColor =
                      "rgba(255,255,255,0.05)";
                  }
                }}
                onMouseLeave={(e) => {
                  if (isSelected) {
                    e.currentTarget.style.backgroundColor =
                      "rgba(230,138,0,0.08)";
                  } else if (isMultiSelected) {
                    e.currentTarget.style.backgroundColor =
                      "rgba(59,130,246,0.06)";
                  } else {
                    e.currentTarget.style.backgroundColor = "transparent";
                  }
                }}
              >
                {/* Quality marker */}
                <div
                  title={warning?.message}
                  style={{
                    width: 20,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: warning ? 12 : 0,
                    color: gutterColor,
                    cursor: warning ? "help" : "default",
                    alignSelf: "stretch",
                    userSelect: "none",
                  }}
                >
                  {warning ? WARNING_SYMBOLS[warning.level] : ""}
                </div>
                {/* Bookmark */}
                <div
                  role="gridcell"
                  onClick={(e) => {
                    e.stopPropagation();
                    onToggleBookmark(cue.id);
                  }}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    cursor: "pointer",
                    fontSize: 11,
                    color: bookmarkedIds.has(cue.id) ? "var(--bz-stat-processing)" : "var(--bz-text-disabled)",
                    transition: "color 0.15s ease",
                  }}
                  title={bookmarkedIds.has(cue.id) ? "Remove bookmark" : "Add bookmark"}
                >
                  {bookmarkedIds.has(cue.id) ? "\u2605" : "\u2606"}
                </div>
                {/* # */}
                <div role="gridcell" style={indexCellStyle}>
                  {cueIndex + 1}
                </div>
                {/* Start */}
                <div role="gridcell" style={monoStyle}>
                  {formatTimestamp(cue.startMs)}
                </div>
                {/* End */}
                <div role="gridcell" style={monoStyle}>
                  {formatTimestamp(cue.endMs)}
                </div>
                {/* Duration */}
                <div role="gridcell" style={durationStyle}>
                  {formatDuration(cue.endMs - cue.startMs)}
                </div>
                {/* Text */}
                <div role="gridcell" style={textCellStyle}>
                  {renderStyledText(cue.text)}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
