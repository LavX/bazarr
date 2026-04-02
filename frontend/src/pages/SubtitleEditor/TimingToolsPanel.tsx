import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { showNotification } from "@mantine/notifications";
import { Environment } from "@/utilities/env";
import { createEditTiming, type CueOperation } from "./document";
import type { Cue } from "./types";

interface TimingToolsPanelProps {
  open: boolean;
  cues: Cue[];
  selectedIndices: Set<number>;
  mediaType?: string;
  mediaId?: number;
  language?: string;
  onApplyBatch: (ops: CueOperation[]) => void;
  onGetContent: () => { content: string; format: string; encoding: string } | null;
  onApplySyncedContent: (content: string) => void;
  onClose: () => void;
}

type TabId = "shift" | "linear" | "autosync";

const styles = {
  container: {
    position: "absolute",
    top: 8,
    right: 8,
    zIndex: 10,
    width: 420,
    background: "var(--bz-surface-raised)",
    border: "1px solid var(--bz-border-card)",
    borderRadius: "var(--bz-radius-sm)",
    padding: "10px 14px",
    boxShadow: "var(--bz-shadow-float)",
    display: "flex",
    flexDirection: "column",
    gap: 10,
  } satisfies CSSProperties,

  tabRow: {
    display: "flex",
    gap: 0,
    borderBottom: "1px solid var(--bz-border-divider)",
    marginBottom: 2,
  } satisfies CSSProperties,

  tab: {
    flex: 1,
    padding: "6px 8px",
    background: "transparent",
    border: "none",
    borderBottom: "2px solid transparent",
    color: "var(--bz-text-secondary)",
    fontSize: 12,
    fontWeight: 500,
    cursor: "pointer",
    textAlign: "center",
    transition: "color 0.15s, border-color 0.15s",
  } satisfies CSSProperties,

  tabActive: {
    color: "var(--bz-text-primary)",
    borderBottomColor: "#e68a00",
  } satisfies CSSProperties,

  row: {
    display: "flex",
    alignItems: "center",
    gap: 8,
  } satisfies CSSProperties,

  label: {
    fontSize: 12,
    color: "var(--bz-text-secondary)",
    minWidth: 80,
    flexShrink: 0,
  } satisfies CSSProperties,

  input: {
    flex: 1,
    fontFamily: "'JetBrains Mono', monospace",
    fontSize: 13,
    background: "var(--bz-surface-base)",
    border: "1px solid var(--bz-border-interactive)",
    color: "var(--bz-text-primary)",
    borderRadius: 4,
    padding: "6px 8px",
    outline: "none",
    width: "100%",
  } satisfies CSSProperties,

  select: {
    flex: 1,
    fontFamily: "'JetBrains Mono', monospace",
    fontSize: 13,
    background: "var(--bz-surface-base)",
    border: "1px solid var(--bz-border-interactive)",
    color: "var(--bz-text-primary)",
    borderRadius: 4,
    padding: "6px 8px",
    outline: "none",
    appearance: "auto",
  } satisfies CSSProperties,

  btn: {
    background: "var(--mantine-color-brand-5)",
    border: "none",
    color: "var(--bz-text-primary)",
    borderRadius: 4,
    padding: "6px 14px",
    cursor: "pointer",
    fontSize: 12,
    fontWeight: 600,
    flexShrink: 0,
  } satisfies CSSProperties,

  btnDisabled: {
    opacity: 0.4,
    cursor: "not-allowed",
  } satisfies CSSProperties,

  btnClose: {
    background: "transparent",
    border: "1px solid var(--bz-border-interactive)",
    color: "var(--bz-text-secondary)",
    borderRadius: 4,
    padding: "4px 8px",
    cursor: "pointer",
    fontSize: 12,
    lineHeight: 1,
    flexShrink: 0,
    marginLeft: "auto",
  } satisfies CSSProperties,

  checkbox: {
    accentColor: "#e68a00",
  } satisfies CSSProperties,

  preview: {
    fontSize: 11,
    fontFamily: "'JetBrains Mono', monospace",
    color: "var(--bz-stat-processing)",
    background: "var(--bz-surface-base)",
    borderRadius: 4,
    padding: "6px 8px",
    border: "1px solid var(--bz-border-card)",
  } satisfies CSSProperties,

  hint: {
    fontSize: 11,
    color: "var(--bz-text-tertiary)",
    lineHeight: 1.4,
  } satisfies CSSProperties,

  spinner: {
    display: "inline-block",
    width: 14,
    height: 14,
    border: "2px solid var(--bz-border-interactive)",
    borderTopColor: "#e68a00",
    borderRadius: "50%",
    animation: "spin 0.8s linear infinite",
  } satisfies CSSProperties,
};

function msToTimecode(ms: number): string {
  const neg = ms < 0;
  const abs = Math.abs(ms);
  const h = Math.floor(abs / 3600000);
  const m = Math.floor((abs % 3600000) / 60000);
  const s = Math.floor((abs % 60000) / 1000);
  const frac = abs % 1000;
  const prefix = neg ? "-" : "";
  return `${prefix}${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(frac).padStart(3, "0")}`;
}

function parseTimecodeMs(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;

  const plain = Number(trimmed);
  if (!isNaN(plain) && trimmed.match(/^-?\d+$/)) return plain;

  const match = trimmed.match(
    /^(-?)(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,3}))?$/,
  );
  if (!match) return null;

  const sign = match[1] === "-" ? -1 : 1;
  const h = parseInt(match[2], 10);
  const m = parseInt(match[3], 10);
  const s = parseInt(match[4], 10);
  const ms = match[5] ? parseInt(match[5].padEnd(3, "0"), 10) : 0;
  return sign * (h * 3600000 + m * 60000 + s * 1000 + ms);
}

function BulkShiftTab({
  cues,
  selectedIndices,
  onApplyBatch,
}: {
  cues: Cue[];
  selectedIndices: Set<number>;
  onApplyBatch: (ops: CueOperation[]) => void;
}) {
  const [offsetStr, setOffsetStr] = useState("0");
  const [scopeSelected, setScopeSelected] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const t = setTimeout(() => inputRef.current?.focus(), 50);
    return () => clearTimeout(t);
  }, []);

  const handleApply = useCallback(() => {
    const offsetMs = parseTimecodeMs(offsetStr);
    if (offsetMs === null || offsetMs === 0) return;

    const ops: CueOperation[] = [];
    for (let i = 0; i < cues.length; i++) {
      if (scopeSelected && !selectedIndices.has(i)) continue;
      const cue = cues[i];
      const newStart = Math.max(0, cue.startMs + offsetMs);
      const newEnd = Math.max(0, cue.endMs + offsetMs);
      ops.push(createEditTiming(i, newStart, newEnd));
    }

    if (ops.length > 0) {
      onApplyBatch(ops);
      showNotification({
        message: `Shifted ${ops.length} cue${ops.length > 1 ? "s" : ""} by ${offsetMs > 0 ? "+" : ""}${offsetMs}ms`,
        color: "green",
        autoClose: 2000,
      });
    }
  }, [offsetStr, cues, scopeSelected, selectedIndices, onApplyBatch]);

  const parsedOffset = parseTimecodeMs(offsetStr);
  const canApply = parsedOffset !== null && parsedOffset !== 0 && cues.length > 0;
  const affectedCount = scopeSelected ? selectedIndices.size : cues.length;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={styles.row}>
        <span style={styles.label}>Offset</span>
        <input
          ref={inputRef}
          type="text"
          value={offsetStr}
          onChange={(e) => setOffsetStr(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && canApply) handleApply();
          }}
          placeholder="ms or HH:MM:SS.mmm"
          style={styles.input}
          spellCheck={false}
        />
      </div>
      <div style={styles.row}>
        <span style={styles.label}>Scope</span>
        <label style={{ fontSize: 12, color: "var(--bz-text-primary)", cursor: "pointer", display: "flex", alignItems: "center", gap: 4 }}>
          <input
            type="radio"
            name="shift-scope"
            checked={!scopeSelected}
            onChange={() => setScopeSelected(false)}
            style={styles.checkbox}
          />
          All cues ({cues.length})
        </label>
        <label style={{ fontSize: 12, color: selectedIndices.size > 0 ? "var(--bz-text-primary)" : "var(--bz-text-disabled)", cursor: selectedIndices.size > 0 ? "pointer" : "default", display: "flex", alignItems: "center", gap: 4 }}>
          <input
            type="radio"
            name="shift-scope"
            checked={scopeSelected}
            onChange={() => setScopeSelected(true)}
            disabled={selectedIndices.size === 0}
            style={styles.checkbox}
          />
          Selected ({selectedIndices.size})
        </label>
      </div>
      {parsedOffset !== null && parsedOffset !== 0 && (
        <div style={styles.preview}>
          {parsedOffset > 0 ? "+" : ""}{parsedOffset}ms applied to {affectedCount} cue{affectedCount !== 1 ? "s" : ""}
        </div>
      )}
      <div style={styles.hint}>
        Enter milliseconds (e.g. 500, -1200) or timecode (e.g. 00:00:01.500, -00:00:02.000)
      </div>
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <button
          type="button"
          style={{
            ...styles.btn,
            ...(canApply ? {} : styles.btnDisabled),
          }}
          disabled={!canApply}
          onClick={handleApply}
        >
          Apply Shift
        </button>
      </div>
    </div>
  );
}

function LinearCorrectionTab({
  cues,
  onApplyBatch,
}: {
  cues: Cue[];
  onApplyBatch: (ops: CueOperation[]) => void;
}) {
  const [cue1Index, setCue1Index] = useState("0");
  const [correct1, setCorrect1] = useState("");
  const [cue2Index, setCue2Index] = useState("");
  const [correct2, setCorrect2] = useState("");

  const idx1 = parseInt(cue1Index, 10);
  const idx2 = parseInt(cue2Index, 10);
  const correct1Ms = parseTimecodeMs(correct1);
  const correct2Ms = parseTimecodeMs(correct2);

  const point1Valid =
    !isNaN(idx1) && idx1 >= 0 && idx1 < cues.length && correct1Ms !== null;
  const point2Valid =
    !isNaN(idx2) && idx2 >= 0 && idx2 < cues.length && correct2Ms !== null;
  const bothValid =
    point1Valid &&
    point2Valid &&
    idx1 !== idx2 &&
    cues[idx1].startMs !== cues[idx2].startMs;

  const preview = useMemo(() => {
    if (!bothValid) return null;
    const currentMs1 = cues[idx1].startMs;
    const currentMs2 = cues[idx2].startMs;
    const scale =
      (correct2Ms! - correct1Ms!) / (currentMs2 - currentMs1);
    const offset = correct1Ms! - currentMs1 * scale;
    return { scale, offset };
  }, [bothValid, cues, idx1, idx2, correct1Ms, correct2Ms]);

  const handleApply = useCallback(() => {
    if (!preview) return;
    const { scale, offset } = preview;

    const ops: CueOperation[] = [];
    for (let i = 0; i < cues.length; i++) {
      const cue = cues[i];
      const newStart = Math.max(0, Math.round(cue.startMs * scale + offset));
      const newEnd = Math.max(0, Math.round(cue.endMs * scale + offset));
      ops.push(createEditTiming(i, newStart, newEnd));
    }

    if (ops.length > 0) {
      onApplyBatch(ops);
      showNotification({
        message: `Linear correction applied to ${ops.length} cues (scale: ${preview.scale.toFixed(6)})`,
        color: "green",
        autoClose: 2000,
      });
    }
  }, [cues, preview, onApplyBatch]);

  const cueOptions = cues.map((c, i) => ({
    value: String(i),
    label: `#${i + 1} ${msToTimecode(c.startMs)}`,
  }));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ fontSize: 11, color: "var(--bz-text-tertiary)", marginBottom: 2 }}>
        Pick two reference points. Set each to its correct time, then apply.
      </div>

      <div style={{ ...styles.row, gap: 6 }}>
        <span style={{ ...styles.label, minWidth: 60 }}>Point 1</span>
        <select
          value={cue1Index}
          onChange={(e) => setCue1Index(e.target.value)}
          style={{ ...styles.select, flex: 1 }}
        >
          {cueOptions.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      <div style={{ ...styles.row, gap: 6 }}>
        <span style={{ ...styles.label, minWidth: 60 }}>Correct</span>
        <input
          type="text"
          value={correct1}
          onChange={(e) => setCorrect1(e.target.value)}
          placeholder={
            point1Valid
              ? msToTimecode(cues[idx1].startMs)
              : "HH:MM:SS.mmm"
          }
          style={styles.input}
          spellCheck={false}
        />
      </div>

      <div
        style={{
          height: 1,
          background: "var(--bz-border-divider)",
          margin: "4px 0",
        }}
      />

      <div style={{ ...styles.row, gap: 6 }}>
        <span style={{ ...styles.label, minWidth: 60 }}>Point 2</span>
        <select
          value={cue2Index}
          onChange={(e) => setCue2Index(e.target.value)}
          style={{ ...styles.select, flex: 1 }}
        >
          <option value="">Select cue...</option>
          {cueOptions.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      <div style={{ ...styles.row, gap: 6 }}>
        <span style={{ ...styles.label, minWidth: 60 }}>Correct</span>
        <input
          type="text"
          value={correct2}
          onChange={(e) => setCorrect2(e.target.value)}
          placeholder={
            point2Valid
              ? msToTimecode(cues[idx2].startMs)
              : "HH:MM:SS.mmm"
          }
          style={styles.input}
          spellCheck={false}
        />
      </div>

      {preview && (
        <div style={styles.preview}>
          Scale: {preview.scale.toFixed(6)}x | Offset:{" "}
          {preview.offset > 0 ? "+" : ""}
          {preview.offset.toFixed(1)}ms
        </div>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <button
          type="button"
          style={{
            ...styles.btn,
            ...(bothValid ? {} : styles.btnDisabled),
          }}
          disabled={!bothValid}
          onClick={handleApply}
        >
          Apply Correction
        </button>
      </div>
    </div>
  );
}

function AutoSyncTab({
  mediaType,
  mediaId,
  language,
  onGetContent,
  onApplySyncedContent,
}: {
  mediaType?: string;
  mediaId?: number;
  language?: string;
  onGetContent: () => { content: string; format: string; encoding: string } | null;
  onApplySyncedContent: (content: string) => void;
}) {
  const [maxOffset, setMaxOffset] = useState("120");
  const [gss, setGss] = useState(false);
  const [noFixFramerate, setNoFixFramerate] = useState(true);
  const [vad, setVad] = useState("");
  const [syncing, setSyncing] = useState(false);
  const [progressMsg, setProgressMsg] = useState("");

  const canSync =
    !!mediaType && mediaId !== undefined && !!language && !syncing;

  const handleSync = useCallback(async () => {
    if (!canSync || mediaId === undefined) return;

    const editorData = onGetContent();
    if (!editorData) {
      showNotification({ title: "Sync", message: "No content to sync", color: "yellow" });
      return;
    }

    setSyncing(true);
    setProgressMsg("Submitting sync job...");

    try {
      // Submit sync job
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        "X-API-KEY": Environment.apiKey ?? "",
      };
      const submitResp = await fetch(`${Environment.baseUrl}/api/editor/sync`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          mediaType,
          mediaId,
          content: editorData.content,
          format: editorData.format,
          encoding: editorData.encoding,
          maxOffsetSeconds: parseInt(maxOffset, 10) || 120,
          gss,
          noFixFramerate: noFixFramerate,
          ...(vad ? { vad } : {}),
        }),
      });

      const submitResult = await submitResp.json();
      if (!submitResp.ok || !submitResult.jobKey) {
        showNotification({ title: "Sync Failed", message: submitResult.message || "Failed to start sync", color: "red" });
        setSyncing(false);
        return;
      }

      const jobKey = submitResult.jobKey;
      setProgressMsg("ffsubsync running (check Jobs Manager for progress)...");

      // Poll for completion
      for (let i = 0; i < 600; i++) {
        await new Promise((r) => setTimeout(r, 2000));

        try {
          const pollResp = await fetch(
            `${Environment.baseUrl}/api/editor/sync?jobKey=${encodeURIComponent(jobKey)}&apikey=${encodeURIComponent(Environment.apiKey ?? "")}`,
            { headers: { "X-API-KEY": Environment.apiKey ?? "" } },
          );
          const pollResult = await pollResp.json();

          if (pollResult.status === "completed" && pollResult.content) {
            onApplySyncedContent(pollResult.content);
            showNotification({ message: "Sync complete, cues updated", color: "green", autoClose: 3000 });
            setProgressMsg("");
            setSyncing(false);
            return;
          }

          if (pollResult.status === "failed") {
            showNotification({ title: "Sync Failed", message: pollResult.message || "ffsubsync error", color: "red", autoClose: 5000 });
            setProgressMsg("");
            setSyncing(false);
            return;
          }

          if (pollResult.status === "not_found") {
            showNotification({ title: "Sync", message: "Sync job lost. It may have completed or been cleaned up.", color: "yellow" });
            setProgressMsg("");
            setSyncing(false);
            return;
          }

          if (pollResult.message) {
            setProgressMsg(pollResult.message);
          }
        } catch {
          // Network hiccup, keep polling
        }
      }

      showNotification({ title: "Sync", message: "Timed out waiting for sync", color: "yellow" });
    } catch (err) {
      showNotification({ title: "Sync Failed", message: (err as Error).message || "Network error", color: "red" });
    } finally {
      setSyncing(false);
      setProgressMsg("");
    }
  }, [canSync, mediaType, mediaId, maxOffset, gss, noFixFramerate, vad, onGetContent, onApplySyncedContent]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ fontSize: 11, padding: "4px 8px", borderRadius: 4, background: "rgba(245, 158, 11, 0.15)", color: "#f59e0b", marginBottom: 2 }}>
        Experimental: results may vary depending on audio quality and subtitle timing.
      </div>
      <div style={styles.hint}>
        Runs ffsubsync on the server to align this subtitle to the audio track.
        Synced cues will be loaded into the editor (save manually to write to disk).
      </div>

      <div style={styles.row}>
        <span style={styles.label}>Max Offset</span>
        <select
          value={maxOffset}
          onChange={(e) => setMaxOffset(e.target.value)}
          style={styles.select}
          disabled={syncing}
        >
          <option value="60">60 seconds</option>
          <option value="120">120 seconds</option>
          <option value="300">300 seconds</option>
          <option value="600">600 seconds</option>
        </select>
      </div>

      <div style={styles.row}>
        <span style={styles.label}>GSS</span>
        <label
          style={{
            fontSize: 12,
            color: "var(--bz-text-primary)",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            gap: 4,
          }}
        >
          <input
            type="checkbox"
            checked={gss}
            onChange={(e) => setGss(e.target.checked)}
            disabled={syncing}
            style={styles.checkbox}
          />
          Golden-Section Search (slower, more accurate)
        </label>
      </div>

      <div style={styles.row}>
        <span style={styles.label}>No Fix Framerate</span>
        <label
          style={{
            fontSize: 12,
            color: "var(--bz-text-primary)",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            gap: 4,
          }}
        >
          <input
            type="checkbox"
            checked={noFixFramerate}
            onChange={(e) => setNoFixFramerate(e.target.checked)}
            disabled={syncing}
            style={styles.checkbox}
          />
          Assume identical framerates
        </label>
      </div>

      <div style={styles.row}>
        <span style={styles.label}>VAD</span>
        <select
          value={vad}
          onChange={(e) => setVad(e.target.value)}
          style={styles.select}
          disabled={syncing}
        >
          <option value="">Default</option>
          <option value="subs_then_webrtc">subs_then_webrtc</option>
          <option value="subs_then_auditok">subs_then_auditok</option>
          <option value="webrtc">webrtc</option>
          <option value="auditok">auditok</option>
        </select>
      </div>

      {!mediaId && (
        <div style={{ ...styles.hint, color: "#f59e0b" }}>
          Save the subtitle first before using auto-sync.
        </div>
      )}

      {/* Progress */}
      {syncing && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <div style={{
            height: 6,
            borderRadius: 3,
            background: "var(--bz-surface-base)",
            overflow: "hidden",
          }}>
            <div style={{
              height: "100%",
              borderRadius: 3,
              background: "var(--mantine-color-brand-5)",
              width: "100%",
              animation: "pulse 1.5s ease-in-out infinite",
            }} />
          </div>
          <div style={{ fontSize: 11, color: "var(--bz-text-secondary)" }}>
            {progressMsg}
          </div>
          <style>{`@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }`}</style>
        </div>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <button
          type="button"
          style={{
            ...styles.btn,
            ...(canSync ? {} : styles.btnDisabled),
          }}
          disabled={!canSync}
          onClick={handleSync}
        >
          {syncing ? "Syncing..." : "Sync"}
        </button>
      </div>
    </div>
  );
}

export default function TimingToolsPanel({
  open,
  cues,
  selectedIndices,
  mediaType,
  mediaId,
  language,
  onApplyBatch,
  onGetContent,
  onApplySyncedContent,
  onClose,
}: TimingToolsPanelProps) {
  const [activeTab, setActiveTab] = useState<TabId>("shift");

  // Close on Escape from anywhere inside the panel
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    },
    [onClose],
  );

  if (!open) return null;

  return (
    <div style={styles.container} onKeyDown={handleKeyDown}>
      <div style={{ display: "flex", alignItems: "center" }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--bz-text-primary)" }}>
          Timing Tools
        </span>
        <button
          type="button"
          style={styles.btnClose}
          onClick={onClose}
          title="Close (Escape)"
        >
          &times;
        </button>
      </div>

      <div style={styles.tabRow}>
        {(
          [
            { id: "shift", label: "Bulk Shift" },
            { id: "linear", label: "Linear Correction" },
            { id: "autosync", label: "Auto-Sync" },
          ] as { id: TabId; label: string }[]
        ).map((tab) => (
          <button
            key={tab.id}
            type="button"
            style={{
              ...styles.tab,
              ...(activeTab === tab.id ? styles.tabActive : {}),
            }}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === "shift" && (
        <BulkShiftTab
          cues={cues}
          selectedIndices={selectedIndices}
          onApplyBatch={onApplyBatch}
        />
      )}
      {activeTab === "linear" && (
        <LinearCorrectionTab cues={cues} onApplyBatch={onApplyBatch} />
      )}
      {activeTab === "autosync" && (
        <AutoSyncTab
          mediaType={mediaType}
          mediaId={mediaId}
          language={language}
          onGetContent={onGetContent}
          onApplySyncedContent={onApplySyncedContent}
        />
      )}
    </div>
  );
}
