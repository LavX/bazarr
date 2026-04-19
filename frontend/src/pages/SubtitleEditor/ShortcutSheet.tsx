import { useCallback, useEffect } from "react";

interface ShortcutSheetProps {
  open: boolean;
  onClose: () => void;
}

interface ShortcutEntry {
  keys: string[];
  description: string;
}

interface ShortcutCategory {
  title: string;
  entries: ShortcutEntry[];
}

const SHORTCUTS: ShortcutCategory[] = [
  {
    title: "File",
    entries: [
      { keys: ["Ctrl", "S"], description: "Save" },
      { keys: ["Ctrl", "O"], description: "Upload file" },
      { keys: ["Ctrl", "F"], description: "Search / Replace" },
    ],
  },
  {
    title: "Editing",
    entries: [
      { keys: ["Ctrl", "Z"], description: "Undo" },
      { keys: ["Ctrl", "Y"], description: "Redo" },
      { keys: ["Enter"], description: "Commit text, next cue" },
      { keys: ["Ctrl", "Enter"], description: "Insert newline" },
    ],
  },
  {
    title: "Cues",
    entries: [
      {
        keys: ["Ctrl", "Shift", "Enter"],
        description: "Add cue after current",
      },
      { keys: ["Ctrl", "Shift", "S"], description: "Split cue at cursor" },
      { keys: ["Ctrl", "Shift", "M"], description: "Merge selected cues" },
      { keys: ["Ctrl", "Delete"], description: "Delete cue" },
    ],
  },
  {
    title: "Timing",
    entries: [
      {
        keys: ["Ctrl", "Shift", "\u2190/\u2192"],
        description: "Nudge start time",
      },
      {
        keys: ["Alt", "Shift", "\u2190/\u2192"],
        description: "Nudge end time",
      },
      {
        keys: ["Ctrl", "Alt", "\u2190/\u2192"],
        description: "Move entire cue",
      },
      { keys: ["[", "]"], description: "Nudge both (Shift: large)" },
    ],
  },
  {
    title: "Navigation",
    entries: [
      { keys: ["Alt", "\u2191/\u2193"], description: "Previous / next cue" },
      { keys: ["Ctrl", "G"], description: "Jump to cue #" },
    ],
  },
  {
    title: "Bookmarks",
    entries: [
      { keys: ["Ctrl", "B"], description: "Toggle bookmark" },
      { keys: ["Ctrl", "Shift", "B"], description: "Jump to next bookmark" },
    ],
  },
  {
    title: "Tools",
    entries: [
      { keys: ["Alt", "T"], description: "AI Translate" },
      { keys: ["Ctrl", "Shift", "Space"], description: "Play / Pause video" },
      { keys: ["Alt", "\u2190"], description: "Seek back 5s" },
      { keys: ["Alt", "\u2192"], description: "Seek forward 5s" },
      { keys: ["?"], description: "This shortcut sheet" },
    ],
  },
];

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  zIndex: 1000,
  background: "rgba(0, 0, 0, 0.6)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  backdropFilter: "blur(2px)",
};

const containerStyle: React.CSSProperties = {
  background: "var(--bz-surface-raised)",
  border: "1px solid var(--bz-border-card)",
  borderRadius: "var(--bz-radius-lg)",
  padding: "24px 28px",
  maxWidth: 680,
  width: "90vw",
  maxHeight: "80vh",
  overflow: "auto",
  boxShadow: "var(--bz-shadow-float)",
};

const titleStyle: React.CSSProperties = {
  fontSize: 16,
  fontWeight: 600,
  color: "var(--bz-text-primary)",
  marginBottom: 20,
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
};

const gridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "1fr 1fr",
  gap: "16px 32px",
};

const categoryStyle: React.CSSProperties = {
  marginBottom: 4,
};

const categoryTitleStyle: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 700,
  textTransform: "uppercase",
  letterSpacing: "0.8px",
  color: "#e68a00",
  marginBottom: 8,
};

const entryStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "3px 0",
  gap: 12,
};

const descriptionStyle: React.CSSProperties = {
  fontSize: 13,
  color: "var(--bz-text-secondary)",
  flex: 1,
};

const keysContainerStyle: React.CSSProperties = {
  display: "flex",
  gap: 3,
  flexShrink: 0,
};

const kbdStyle: React.CSSProperties = {
  display: "inline-block",
  fontFamily: "'JetBrains Mono', monospace",
  fontSize: 11,
  fontWeight: 500,
  lineHeight: 1,
  padding: "3px 6px",
  borderRadius: 4,
  background: "var(--bz-surface-base)",
  border: "1px solid var(--bz-border-interactive)",
  color: "var(--bz-stat-processing)",
  whiteSpace: "nowrap",
};

const plusStyle: React.CSSProperties = {
  fontSize: 10,
  color: "var(--bz-text-disabled)",
  lineHeight: 1,
};

const closeHintStyle: React.CSSProperties = {
  fontSize: 11,
  color: "var(--bz-text-disabled)",
};

export default function ShortcutSheet({ open, onClose }: ShortcutSheetProps) {
  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Escape" || e.key === "?") {
        e.preventDefault();
        onClose();
      }
    },
    [onClose],
  );

  useEffect(() => {
    if (!open) return;
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, handleKeyDown]);

  if (!open) return null;

  return (
    <div style={overlayStyle} onClick={onClose}>
      <div style={containerStyle} onClick={(e) => e.stopPropagation()}>
        <div style={titleStyle}>
          <span>Keyboard Shortcuts</span>
          <span style={closeHintStyle}>Press ? or Esc to close</span>
        </div>
        <div style={gridStyle}>
          {SHORTCUTS.map((category) => (
            <div key={category.title} style={categoryStyle}>
              <div style={categoryTitleStyle}>{category.title}</div>
              {category.entries.map((entry, i) => (
                <div key={i} style={entryStyle}>
                  <span style={descriptionStyle}>{entry.description}</span>
                  <span style={keysContainerStyle}>
                    {entry.keys.map((key, ki) => (
                      <span
                        key={ki}
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 3,
                        }}
                      >
                        {ki > 0 && <span style={plusStyle}>+</span>}
                        <kbd style={kbdStyle}>{key}</kbd>
                      </span>
                    ))}
                  </span>
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
