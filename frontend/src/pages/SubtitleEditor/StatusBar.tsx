import type { Cue } from "./types";

interface StatusBarProps {
  cueIndex: number;
  cueCount: number;
  format: string;
  encoding: string;
  dirty: boolean;
  cursorPosition?: { line: number; col: number };
  selectedCue: Cue | null;
  multiSelectCount: number;
  bookmarkCount: number;
  totalDurationMs: number;
}

const barStyle: React.CSSProperties = {
  height: 24,
  background: "var(--bz-surface-ground)",
  borderTop: "1px solid var(--bz-border-card)",
  display: "flex",
  alignItems: "center",
  padding: "0 12px",
  gap: 16,
  fontSize: 11,
  fontFamily: "monospace",
  color: "var(--bz-text-disabled)",
  flexShrink: 0,
};

const dotStyle: React.CSSProperties = {
  width: 6,
  height: 6,
  borderRadius: "50%",
  background: "#f59e0b",
  flexShrink: 0,
};

function computeCps(cue: Cue): { value: number | null; color: string } {
  const durationMs = cue.endMs - cue.startMs;
  if (durationMs <= 0) return { value: null, color: "var(--bz-text-tertiary)" };
  const chars = cue.text.replace(/\n/g, "").length;
  const cps = chars / (durationMs / 1000);
  const color = cps > 25 ? "#EF4444" : cps > 18 ? "#fbbf24" : "#22c55e";
  return { value: cps, color };
}

export default function StatusBar({
  cueIndex,
  cueCount,
  format,
  encoding,
  dirty,
  selectedCue,
  multiSelectCount,
  bookmarkCount,
}: StatusBarProps) {
  const cps = selectedCue ? computeCps(selectedCue) : null;

  return (
    <div style={barStyle} role="status" aria-label="Editor status">
      <span>
        Cue {cueIndex >= 0 ? cueIndex + 1 : "-"}/{cueCount}
      </span>
      {cps && (
        <span style={{ color: cps.color }}>
          {cps.value !== null ? `${cps.value.toFixed(1)} CPS` : "-- CPS"}
        </span>
      )}
      {multiSelectCount > 0 && (
        <span style={{ color: "#60a5fa" }}>{multiSelectCount} selected</span>
      )}
      {bookmarkCount > 0 && (
        <span style={{ color: "#fbbf24" }}>
          {bookmarkCount} bookmarked
        </span>
      )}
      <span style={{ flex: 1 }} />
      <span>{format.toUpperCase()}</span>
      <span>{encoding}</span>
      {dirty && (
        <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <span style={dotStyle} />
          Unsaved
        </span>
      )}
    </div>
  );
}
