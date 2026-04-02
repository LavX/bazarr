import React, { useState, useEffect, useCallback, useRef, useImperativeHandle, forwardRef } from "react";
import type { Cue } from "./types";

export interface DetailPaneHandle {
  focus(): void;
  /** Return the current textarea draft if it differs from the cue text. */
  getUncommittedText(): string | null;
}

interface DetailPaneProps {
  cue: Cue | null;
  cueIndex: number;
  onTextChange: (text: string) => void;
  onTimingChange: (startMs: number, endMs: number) => void;
  onAdvance: () => void;
  cursorPosRef?: React.MutableRefObject<number>;
  autoFocus?: boolean;
  referenceText?: string;
  onUseReference?: () => void;
  onTranslateLine?: () => void;
  translatingLine?: boolean;
  currentTimeMs?: number;
}

function formatTimestamp(ms: number): string {
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  const millis = ms % 1000;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

function parseTimestamp(input: string): number | null {
  const match = input.trim().match(/^(\d+):(\d+):(\d+)[.,](\d+)$/);
  if (match) {
    const [, h, m, s, ms] = match;
    return (
      parseInt(h) * 3600000 +
      parseInt(m) * 60000 +
      parseInt(s) * 1000 +
      parseInt(ms.padEnd(3, "0").slice(0, 3))
    );
  }
  const num = parseInt(input.trim());
  if (!isNaN(num) && num >= 0) return num;
  return null;
}

const labelStyle: React.CSSProperties = {
  fontSize: 10,
  color: "var(--bz-text-tertiary)",
  textTransform: "uppercase",
  letterSpacing: "0.5px",
  marginBottom: 4,
};

const timeInputStyle: React.CSSProperties = {
  fontFamily: "'JetBrains Mono', monospace",
  fontSize: 13,
  background: "var(--bz-surface-raised)",
  border: "1px solid var(--bz-border-interactive)",
  borderRadius: "var(--bz-radius-xs)",
  padding: "8px 10px",
  color: "var(--bz-stat-processing)",
  outline: "none",
  width: "100%",
  boxSizing: "border-box",
};

const DetailPane = forwardRef<DetailPaneHandle, DetailPaneProps>(function DetailPane({
  cue,
  cueIndex,
  onTextChange,
  onTimingChange,
  onAdvance,
  cursorPosRef,
  autoFocus,
  referenceText,
  onUseReference,
  onTranslateLine,
  translatingLine,
  currentTimeMs = 0,
}, ref) {
  const [textDraft, setTextDraft] = useState("");
  const [startDraft, setStartDraft] = useState("");
  const [endDraft, setEndDraft] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Expose imperative handle for parent to focus and read uncommitted text
  useImperativeHandle(ref, () => ({
    focus() {
      textareaRef.current?.focus();
    },
    getUncommittedText() {
      if (cue && textDraft !== cue.text) {
        return textDraft;
      }
      return null;
    },
  }), [cue, textDraft]);

  // Auto-focus textarea when requested
  useEffect(() => {
    if (autoFocus && cue && textareaRef.current) {
      textareaRef.current.focus();
    }
  }, [autoFocus, cue, cueIndex]);

  // Keep cursor position ref updated for split-at-cursor
  const updateCursorPos = useCallback(() => {
    if (cursorPosRef && textareaRef.current) {
      cursorPosRef.current = textareaRef.current.selectionStart ?? 0;
    }
  }, [cursorPosRef]);

  // Sync local state when a DIFFERENT cue is selected (by id).
  // We intentionally do NOT sync when the same cue's text changes from
  // our own keystrokes, to avoid resetting timing fields mid-edit and
  // causing cursor position jumps in the textarea.
  const prevCueIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (cue && cue.id !== prevCueIdRef.current) {
      prevCueIdRef.current = cue.id;
      setTextDraft(cue.text);
      setStartDraft(formatTimestamp(cue.startMs));
      setEndDraft(formatTimestamp(cue.endMs));
    }
  }, [cue]);

  // Sync timing fields when timing changes externally (e.g. nudge shortcuts)
  useEffect(() => {
    if (cue && cue.id === prevCueIdRef.current) {
      setStartDraft(formatTimestamp(cue.startMs));
      setEndDraft(formatTimestamp(cue.endMs));
    }
  }, [cue?.startMs, cue?.endMs, cue?.id]);

  // Sync text when it changes externally (e.g. AI translate, sync, use reference)
  // Only if the textarea is not focused (user is not typing)
  const textareaFocusedRef = useRef(false);
  useEffect(() => {
    if (cue && cue.id === prevCueIdRef.current && !textareaFocusedRef.current) {
      if (cue.text !== textDraft) {
        setTextDraft(cue.text);
      }
    }
  }, [cue?.text, cue?.id]);

  const wrapSelection = useCallback((prefix: string, suffix: string) => {
    const ta = textareaRef.current;
    if (!ta) return;
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    const text = textDraft;
    const selected = text.substring(start, end);
    const newText = text.substring(0, start) + prefix + selected + suffix + text.substring(end);
    setTextDraft(newText);
    onTextChange(newText);
    requestAnimationFrame(() => {
      ta.focus();
      ta.setSelectionRange(start + prefix.length, end + prefix.length);
    });
  }, [textDraft, onTextChange]);

  const insertAtCursor = useCallback((chars: string) => {
    const ta = textareaRef.current;
    if (!ta) return;
    const start = ta.selectionStart;
    const text = textDraft;
    const newText = text.substring(0, start) + chars + text.substring(start);
    setTextDraft(newText);
    onTextChange(newText);
    requestAnimationFrame(() => {
      ta.focus();
      ta.setSelectionRange(start + chars.length, start + chars.length);
    });
  }, [textDraft, onTextChange]);

  const handleTextKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.ctrlKey && !e.shiftKey && !e.altKey) {
        // Plain Enter: commit and advance to next cue
        e.preventDefault();
        onTextChange(textDraft);
        onAdvance();
      }
      // Ctrl+I: wrap in italic tags
      if (e.key === "i" && (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey) {
        e.preventDefault();
        wrapSelection("<i>", "</i>");
      }
      // Ctrl+B: wrap in bold tags
      if (e.key === "b" && (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey) {
        e.preventDefault();
        wrapSelection("<b>", "</b>");
      }
      // Ctrl+U: wrap in underline tags
      if (e.key === "u" && (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey) {
        e.preventDefault();
        wrapSelection("<u>", "</u>");
      }
      // Ctrl+Enter / Shift+Enter: browser default inserts newline
    },
    [textDraft, onTextChange, onAdvance, wrapSelection],
  );

  const handleStartBlur = useCallback(() => {
    if (!cue) return;
    const parsed = parseTimestamp(startDraft);
    if (parsed !== null) {
      onTimingChange(parsed, cue.endMs);
    } else {
      setStartDraft(formatTimestamp(cue.startMs));
    }
  }, [startDraft, cue, onTimingChange]);

  const handleEndBlur = useCallback(() => {
    if (!cue) return;
    const parsed = parseTimestamp(endDraft);
    if (parsed !== null) {
      onTimingChange(cue.startMs, parsed);
    } else {
      setEndDraft(formatTimestamp(cue.endMs));
    }
  }, [endDraft, cue, onTimingChange]);

  const handleWheel = useCallback(
    (field: "start" | "end") => (e: React.WheelEvent<HTMLInputElement>) => {
      if (!cue) return;
      e.preventDefault();
      const step = e.shiftKey ? 500 : 100;
      const delta = e.deltaY < 0 ? -step : step;
      if (field === "start") {
        const newStart = Math.max(0, cue.startMs + delta);
        onTimingChange(newStart, cue.endMs);
      } else {
        const newEnd = Math.max(0, cue.endMs + delta);
        onTimingChange(cue.startMs, newEnd);
      }
    },
    [cue, onTimingChange],
  );

  if (!cue) {
    return (
      <div
        style={{
          background: "var(--bz-surface-ground)",
          borderTop: "1px solid var(--bz-border-card)",
          padding: "8px 10px",
          display: "flex",
          flexDirection: "column",
          gap: 6,
          flexShrink: 0,
          overflow: "auto",
          flex: 1,
          minHeight: 0,
        }}
      >
        {/* Empty timing row */}
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexShrink: 0 }}>
          <input
            type="text"
            value={formatTimestamp(currentTimeMs)}
            readOnly
            title="Start time (no cue selected)"
            style={{ ...timeInputStyle, padding: "4px 6px", fontSize: 12, flex: 1, opacity: 0.5 }}
          />
          <span style={{ color: "var(--bz-text-disabled)", fontSize: 10 }}>to</span>
          <input
            type="text"
            value={formatTimestamp(currentTimeMs + 3000)}
            readOnly
            title="End time (no cue selected)"
            style={{ ...timeInputStyle, padding: "4px 6px", fontSize: 12, flex: 1, opacity: 0.5 }}
          />
          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: "var(--bz-text-tertiary)", flexShrink: 0 }}>
            --
          </span>
          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: "var(--bz-text-tertiary)", fontWeight: 600, flexShrink: 0 }}>
            --
          </span>
        </div>
        {/* Empty text area */}
        <div style={{ minWidth: 0 }}>
          <div style={labelStyle}>No Cue Selected</div>
          <textarea
            value=""
            readOnly
            placeholder="No cue at current position. Press Ctrl+Shift+Enter to create one."
            style={{
              fontFamily: "'JetBrains Mono', monospace",
              fontSize: 14,
              lineHeight: 1.6,
              background: "var(--bz-surface-raised)",
              border: "1px solid var(--bz-border-interactive)",
              borderRadius: "var(--bz-radius-xs)",
              padding: "10px 12px",
              minHeight: 60,
              color: "var(--bz-text-disabled)",
              resize: "vertical",
              width: "100%",
              boxSizing: "border-box",
              outline: "none",
              opacity: 0.6,
            }}
          />
          {/* Reference text */}
          {referenceText !== undefined && (
            <div style={{ marginTop: 6 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
                <span style={labelStyle}>Reference</span>
                <div style={{ display: "flex", gap: 4, marginLeft: "auto" }}>
                  {onTranslateLine && (
                    <button
                      type="button"
                      onClick={onTranslateLine}
                      disabled={translatingLine}
                      title="Translate and create cue"
                      style={{
                        background: "var(--mantine-color-brand-5)",
                        border: "none",
                        borderRadius: 3,
                        padding: "2px 6px",
                        cursor: translatingLine ? "wait" : "pointer",
                        fontSize: 10,
                        color: "#fff",
                        opacity: translatingLine ? 0.5 : 1,
                      }}
                    >
                      {translatingLine ? "..." : "AI"}
                    </button>
                  )}
                  {onUseReference && referenceText && (
                    <button
                      type="button"
                      onClick={onUseReference}
                      title="Create cue with reference text"
                      style={{
                        background: "#059669",
                        border: "none",
                        borderRadius: 3,
                        padding: "2px 6px",
                        cursor: "pointer",
                        fontSize: 10,
                        color: "#fff",
                      }}
                    >
                      &#x2192; Use
                    </button>
                  )}
                </div>
              </div>
              <div
                style={{
                  fontFamily: "'JetBrains Mono', monospace",
                  fontSize: 12,
                  lineHeight: 1.5,
                  background: "var(--bz-surface-base)",
                  border: "1px solid var(--bz-border-card)",
                  borderRadius: "var(--bz-radius-xs)",
                  padding: "6px 8px",
                  color: "var(--bz-text-secondary)",
                  whiteSpace: "pre-wrap",
                }}
              >
                {referenceText || <span style={{ color: "var(--bz-text-disabled)", fontStyle: "italic" }}>No reference at current position</span>}
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }

  const durationMs = cue.endMs - cue.startMs;
  const durationSec = (durationMs / 1000).toFixed(3);
  const stripTags = (s: string) => s.replace(/<[^>]+>/g, "");
  const lines = textDraft.split("\n");

  const cpsValue = durationMs > 0
    ? stripTags(textDraft).replace(/\n/g, "").length / (durationMs / 1000)
    : null;
  const cpsDisplay = cpsValue !== null ? cpsValue.toFixed(1) : "--";
  const cpsColor = cpsValue === null
    ? "var(--bz-text-tertiary)"
    : cpsValue > 25
      ? "#EF4444"
      : cpsValue > 18
        ? "#fbbf24"
        : "#22c55e";

  return (
    <div
      style={{
        background: "var(--bz-surface-ground)",
        borderTop: "1px solid var(--bz-border-card)",
        padding: "8px 10px",
        display: "flex",
        flexDirection: "column",
        gap: 6,
        flexShrink: 0,
        overflow: "auto",
        flex: 1,
        minHeight: 0,
      }}
    >
      {/* Timing row */}
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexShrink: 0 }}>
        <input
          type="text"
          value={startDraft}
          onChange={(e) => setStartDraft(e.target.value)}
          onBlur={handleStartBlur}
          onWheel={handleWheel("start")}
          title="Start time"
          style={{
            ...timeInputStyle,
            padding: "4px 6px",
            fontSize: 12,
            flex: 1,
          }}
        />
        <span style={{ color: "var(--bz-text-disabled)", fontSize: 10 }}>to</span>
        <input
          type="text"
          value={endDraft}
          onChange={(e) => setEndDraft(e.target.value)}
          onBlur={handleEndBlur}
          onWheel={handleWheel("end")}
          title="End time"
          style={{
            ...timeInputStyle,
            padding: "4px 6px",
            fontSize: 12,
            flex: 1,
          }}
        />
        <span style={{
          fontFamily: "'JetBrains Mono', monospace",
          fontSize: 11,
          color: "var(--bz-text-tertiary)",
          flexShrink: 0,
        }}>
          {durationSec}s
        </span>
        <span style={{
          fontFamily: "'JetBrains Mono', monospace",
          fontSize: 11,
          color: cpsColor,
          fontWeight: 600,
          flexShrink: 0,
        }}>
          {cpsDisplay}
        </span>
      </div>
      {/* Text area */}
      <div style={{ minWidth: 0 }}>
        <div style={labelStyle}>Cue #{cueIndex + 1} Text</div>
        <textarea
          ref={textareaRef}
          value={textDraft}
          onChange={(e) => {
            const val = e.target.value;
            setTextDraft(val);
            // Dispatch to document model immediately so docState.cues
            // always reflects the latest text (prevents data loss on save)
            onTextChange(val);
          }}
          onKeyDown={handleTextKeyDown}
          style={{
            fontFamily: "'JetBrains Mono', monospace",
            fontSize: 14,
            lineHeight: 1.6,
            background: "var(--bz-surface-raised)",
            border: "1px solid var(--bz-border-interactive)",
            borderRadius: "var(--bz-radius-xs)",
            padding: "10px 12px",
            minHeight: 60,
            color: "var(--bz-text-primary)",
            resize: "vertical",
            width: "100%",
            boxSizing: "border-box",
            outline: "none",
          }}
          onClick={updateCursorPos}
          onKeyUp={updateCursorPos}
          onSelect={updateCursorPos}
          onFocus={(e) => {
            e.currentTarget.style.borderColor = "#e68a00";
            textareaFocusedRef.current = true;
            updateCursorPos();
          }}
          onBlur={(e) => {
            e.currentTarget.style.borderColor = "var(--bz-border-interactive)";
            textareaFocusedRef.current = false;
          }}
        />
        <div style={{ marginTop: 2, display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          {lines.map((line, i) => {
            const visLen = stripTags(line).length;
            return (
              <span
                key={i}
                style={{
                  fontSize: 10,
                  color: visLen > 42 ? "#EF4444" : "var(--bz-text-tertiary)",
                  fontFamily: "'JetBrains Mono', monospace",
                }}
              >
                L{i + 1}: {visLen}
              </span>
            );
          })}
          <span style={{ flex: 1 }} />
          {/* Quick style & insert buttons */}
          {[
            { label: "I", title: "Italic (Ctrl+I)", action: () => wrapSelection("<i>", "</i>") },
            { label: "B", title: "Bold (Ctrl+B in text)", action: () => wrapSelection("<b>", "</b>") },
            { label: "U", title: "Underline", action: () => wrapSelection("<u>", "</u>") },
            { label: "\u266A", title: "Music note", action: () => insertAtCursor("\u266A ") },
            { label: "\u2026", title: "Ellipsis", action: () => insertAtCursor("\u2026") },
            { label: "\u2014", title: "Em dash", action: () => insertAtCursor("\u2014") },
            { label: "\u00BF", title: "Inverted question mark", action: () => insertAtCursor("\u00BF") },
            { label: "\u00A1", title: "Inverted exclamation", action: () => insertAtCursor("\u00A1") },
          ].map((btn) => (
            <button
              key={btn.label}
              type="button"
              title={btn.title}
              onClick={btn.action}
              style={{
                background: "var(--bz-surface-raised)",
                border: "1px solid var(--bz-border-interactive)",
                borderRadius: 3,
                padding: "0 5px",
                height: 20,
                fontSize: 11,
                color: "var(--bz-text-secondary)",
                cursor: "pointer",
                fontWeight: btn.label.length === 1 && btn.label.match(/[BIU]/) ? 600 : 400,
                fontStyle: btn.label === "I" ? "italic" : "normal",
                textDecoration: btn.label === "U" ? "underline" : "none",
                lineHeight: "18px",
              }}
            >
              {btn.label}
            </button>
          ))}
        </div>
        {/* Reference text */}
        {referenceText !== undefined && (
          <div style={{ marginTop: 6 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
              <span style={labelStyle}>Reference</span>
              <div style={{ display: "flex", gap: 4, marginLeft: "auto" }}>
                {onTranslateLine && (
                  <button
                    type="button"
                    onClick={onTranslateLine}
                    disabled={translatingLine}
                    title="Translate this cue"
                    style={{
                      background: "var(--mantine-color-brand-5)",
                      border: "none",
                      borderRadius: 3,
                      padding: "2px 6px",
                      cursor: translatingLine ? "wait" : "pointer",
                      fontSize: 10,
                      color: "#fff",
                      opacity: translatingLine ? 0.5 : 1,
                    }}
                  >
                    {translatingLine ? "..." : "AI"}
                  </button>
                )}
                {onUseReference && referenceText && (
                  <button
                    type="button"
                    onClick={onUseReference}
                    title="Use reference text"
                    style={{
                      background: "#059669",
                      border: "none",
                      borderRadius: 3,
                      padding: "2px 6px",
                      cursor: "pointer",
                      fontSize: 10,
                      color: "#fff",
                    }}
                  >
                    &#x2192; Use
                  </button>
                )}
              </div>
            </div>
            <div
              style={{
                fontFamily: "'JetBrains Mono', monospace",
                fontSize: 12,
                lineHeight: 1.5,
                background: "var(--bz-surface-base)",
                border: "1px solid var(--bz-border-card)",
                borderRadius: "var(--bz-radius-xs)",
                padding: "6px 8px",
                color: "var(--bz-text-secondary)",
                minHeight: 20,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              {referenceText || <span style={{ color: "var(--bz-text-disabled)", fontStyle: "italic" }}>No reference for this cue</span>}
            </div>
          </div>
        )}
      </div>

    </div>
  );
});

export default DetailPane;
