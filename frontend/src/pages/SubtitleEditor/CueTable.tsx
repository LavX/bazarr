import { Fragment, ReactNode, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { Cue } from "./types";

function formatTimestamp(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const millis = ms % 1000;

  return (
    String(hours).padStart(2, "0") +
    ":" +
    String(minutes).padStart(2, "0") +
    ":" +
    String(seconds).padStart(2, "0") +
    "." +
    String(millis).padStart(3, "0")
  );
}

function formatDuration(ms: number): string {
  const seconds = ms / 1000;
  if (Number.isInteger(seconds)) {
    return seconds + ".0s";
  }
  return parseFloat(seconds.toFixed(1)) + "s";
}

// Tag colors by type
// Badge color categories
const TAG_COLORS: Record<string, { bg: string; text: string }> = {
  // HTML style tags
  i: { bg: "rgba(139, 92, 246, 0.2)", text: "#a78bfa" },
  b: { bg: "rgba(251, 146, 60, 0.2)", text: "#fb923c" },
  u: { bg: "rgba(52, 211, 153, 0.2)", text: "#34d399" },
  font: { bg: "rgba(96, 165, 250, 0.2)", text: "#60a5fa" },
  s: { bg: "rgba(248, 113, 113, 0.2)", text: "#f87171" },
  // ASS categories
  pos: { bg: "rgba(251, 191, 36, 0.2)", text: "#fbbf24" },
  align: { bg: "rgba(251, 191, 36, 0.2)", text: "#fbbf24" },
  effect: { bg: "rgba(168, 85, 247, 0.2)", text: "#a855f7" },
  color: { bg: "rgba(96, 165, 250, 0.2)", text: "#60a5fa" },
  font_ass: { bg: "rgba(96, 165, 250, 0.2)", text: "#60a5fa" },
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

/**
 * Categorize an ASS override tag block like {\an8} or {\pos(320,50)\fad(1200,0)}
 * into a readable badge label and color category.
 */
function parseAssTagBlock(block: string): { label: string; colorKey: string } {
  // Remove the outer braces
  const inner = block.slice(1, -1);

  // Common ASS tags and their human-readable labels
  const patterns: [RegExp, string, string][] = [
    // Alignment: \an1-\an9, \a1-\a11
    [/^\\an(\d)$/, "align-$1", "align"],
    [/^\\a(\d+)$/, "align-$1", "align"],
    // Position
    [/\\pos\([^)]+\)/, "pos", "pos"],
    // Move
    [/\\move\([^)]+\)/, "move", "pos"],
    // Origin
    [/\\org\([^)]+\)/, "org", "pos"],
    // Fade
    [/\\fad\([^)]+\)/, "fade", "effect"],
    [/\\fade\([^)]+\)/, "fade", "effect"],
    // Blur
    [/\\be\d/, "blur", "effect"],
    [/\\blur[\d.]/, "blur", "effect"],
    // Border/shadow
    [/\\bord[\d.]/, "border", "effect"],
    [/\\shad[\d.]/, "shadow", "effect"],
    // Rotation
    [/\\fr[xyz]?[\d.-]/, "rotate", "effect"],
    // Scale
    [/\\fsc[xy][\d.]/, "scale", "effect"],
    // Color
    [/\\1?c&H/, "color", "color"],
    [/\\2c&H/, "color2", "color"],
    [/\\3c&H/, "border-color", "color"],
    [/\\4c&H/, "shadow-color", "color"],
    // Alpha
    [/\\alpha/, "alpha", "effect"],
    [/\\[1234]a&H/, "alpha", "effect"],
    // Font
    [/\\fn/, "font", "font_ass"],
    [/\\fs[\d.]/, "size", "font_ass"],
    // Bold/italic in ASS
    [/\\b[01]/, "bold", "b"],
    [/\\i[01]/, "italic", "i"],
    [/\\u[01]/, "underline", "u"],
    [/\\s[01]/, "strike", "s"],
    // Karaoke
    [/\\k[fo]?\d/, "karaoke", "effect"],
    // Clip
    [/\\i?clip\(/, "clip", "effect"],
    // Drawing
    [/\\p[01]/, "drawing", "effect"],
    // Letter spacing
    [/\\fsp[\d.-]/, "spacing", "font_ass"],
    // Encoding
    [/\\fe\d/, "encoding", "font_ass"],
    // Animated transform
    [/\\t\(/, "animate", "effect"],
    // Wrap
    [/\\q\d/, "wrap", "align"],
    // Reset
    [/\\r/, "reset", "effect"],
  ];

  // Try to match known tags
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

  // Fallback: show the raw content, abbreviated
  const abbreviated = inner.length > 12 ? inner.slice(0, 12) + ".." : inner;
  return { label: abbreviated, colorKey: "effect" };
}

/**
 * Renders cue text with formatting tags shown as small colored badges.
 * Handles both HTML tags (<i>, <b>, <font>) and ASS override tags ({\an8}, {\pos}).
 */
function renderStyledText(text: string): ReactNode {
  // Match HTML tags and ASS override tag blocks
  // HTML: <i>, </i>, <b>, </b>, <font color=...>, etc.
  // ASS: {\an8}, {\pos(320,50)}, {\fad(1200,0)\be1}, etc.
  const tagPattern = /<\/?([a-zA-Z]+)(?:\s[^>]*)?>|\{\\[^}]+\}/g;
  const parts: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;

  while ((match = tagPattern.exec(text)) !== null) {
    // Text before the tag
    if (match.index > lastIndex) {
      parts.push(
        <Fragment key={key++}>{text.slice(lastIndex, match.index)}</Fragment>,
      );
    }

    const fullMatch = match[0];

    if (fullMatch.startsWith("{\\")) {
      // ASS override tag block
      const { label, colorKey } = parseAssTagBlock(fullMatch);
      parts.push(
        <span key={key++} style={tagBadgeStyle(colorKey)}>
          {label}
        </span>,
      );
    } else {
      // HTML tag
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

  // Remaining text after last tag
  if (lastIndex < text.length) {
    parts.push(<Fragment key={key++}>{text.slice(lastIndex)}</Fragment>);
  }

  return parts.length > 0 ? parts : text;
}

const gridTemplate = "56px 140px 140px 72px 1fr";

const headerStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: gridTemplate,
  position: "sticky",
  top: 0,
  zIndex: 1,
  backgroundColor: "var(--mantine-color-body)",
  borderBottom: "2px solid var(--mantine-color-default-border)",
  fontWeight: 700,
  fontSize: "0.7rem",
  textTransform: "uppercase",
  letterSpacing: "0.8px",
  color: "var(--mantine-color-dimmed)",
};

const headerCellStyle: React.CSSProperties = {
  padding: "10px 12px",
};

const rowBaseStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: gridTemplate,
  alignItems: "start",
  borderBottom: "1px solid rgba(255, 255, 255, 0.04)",
  transition: "background-color 0.1s ease",
};

const cellStyle: React.CSSProperties = {
  padding: "10px 12px",
  fontSize: "0.85rem",
  lineHeight: 1.5,
};

const monoStyle: React.CSSProperties = {
  ...cellStyle,
  fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
  fontSize: "0.8rem",
  color: "var(--mantine-color-yellow-4)",
  letterSpacing: "-0.2px",
};

const durationStyle: React.CSSProperties = {
  ...cellStyle,
  fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
  fontSize: "0.8rem",
  color: "var(--mantine-color-dimmed)",
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
  color: "var(--mantine-color-dimmed)",
  fontSize: "0.75rem",
  textAlign: "right",
  fontFamily: "monospace",
  paddingRight: "16px",
};

const COLUMNS = ["#", "START", "END", "DURATION", "TEXT"] as const;

interface CueTableProps {
  cues: Cue[];
}

export default function CueTable({ cues }: CueTableProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  const virtualizer = useVirtualizer({
    count: cues.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 48,
    overscan: 10,
  });

  return (
    <div
      role="grid"
      aria-label="Subtitle cues"
      style={{
        display: "flex",
        flexDirection: "column",
        flex: 1,
        minHeight: 0,
        overflow: "hidden",
        border: "1px solid var(--mantine-color-default-border)",
        borderRadius: "var(--mantine-radius-md)",
        backgroundColor: "var(--mantine-color-body)",
      }}
    >
      <div role="row" style={headerStyle}>
        {COLUMNS.map((col) => (
          <div
            key={col}
            role="columnheader"
            style={{
              ...headerCellStyle,
              textAlign: col === "#" ? "right" : col === "DURATION" ? "center" : undefined,
              paddingRight: col === "#" ? "16px" : undefined,
            }}
          >
            {col}
          </div>
        ))}
      </div>

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
            const cue = cues[virtualRow.index];
            const isOdd = virtualRow.index % 2 === 1;

            return (
              <div
                key={cue.id}
                role="row"
                data-index={virtualRow.index}
                ref={virtualizer.measureElement}
                style={{
                  ...rowBaseStyle,
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  transform: `translateY(${virtualRow.start}px)`,
                  backgroundColor: isOdd
                    ? "rgba(255, 255, 255, 0.02)"
                    : "transparent",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.backgroundColor =
                    "rgba(255, 255, 255, 0.05)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.backgroundColor = isOdd
                    ? "rgba(255, 255, 255, 0.02)"
                    : "transparent";
                }}
              >
                <div role="gridcell" style={indexCellStyle}>
                  {virtualRow.index + 1}
                </div>
                <div role="gridcell" style={monoStyle}>
                  {formatTimestamp(cue.startMs)}
                </div>
                <div role="gridcell" style={monoStyle}>
                  {formatTimestamp(cue.endMs)}
                </div>
                <div role="gridcell" style={durationStyle}>
                  {formatDuration(cue.endMs - cue.startMs)}
                </div>
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

export { formatTimestamp, formatDuration };
