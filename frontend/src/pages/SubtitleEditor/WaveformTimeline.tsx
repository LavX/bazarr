import {
  type CSSProperties,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import WaveSurfer from "wavesurfer.js";
import RegionsPlugin from "wavesurfer.js/dist/plugins/regions.esm.js";
import TimelinePlugin from "wavesurfer.js/dist/plugins/timeline.esm.js";
import { Environment } from "@/utilities/env";
import { isTerminalJob, useEditorJob } from "./editorJobs";
import { appendArrInstanceParam } from "./editorScope";
import type { Cue } from "./types";

interface WaveformTimelineProps {
  mediaType?: string;
  mediaId?: number;
  arrInstanceId?: number;
  cues: Cue[];
  selectedIndex: number;
  currentTimeMs?: number;
  onSelect: (index: number) => void;
  onTimingChange?: (index: number, startMs: number, endMs: number) => void;
  onSeek?: (ms: number) => void;
  audioTrack?: number;
}

export function formatMs(ms: number): string {
  const totalSec = Math.floor(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  const frac = Math.floor((ms % 1000) / 100);
  return `${m}:${String(s).padStart(2, "0")}.${frac}`;
}

const containerStyle: CSSProperties = {
  background: "var(--bz-surface-ground)",
  borderTop: "1px solid var(--bz-border-card)",
  position: "relative",
  flexShrink: 0,
};

const controlsStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "4px 8px",
  background: "var(--bz-surface-base)",
  borderTop: "1px solid var(--bz-border-card)",
  fontSize: 11,
  color: "var(--bz-text-secondary)",
};

const zoomBtnStyle: CSSProperties = {
  background: "none",
  border: "1px solid var(--bz-border-interactive)",
  borderRadius: 3,
  color: "var(--bz-text-secondary)",
  cursor: "pointer",
  padding: "2px 8px",
  fontSize: 11,
};

export function cueColor(index: number, selectedIndex: number): string {
  if (index === selectedIndex) return "rgba(230, 138, 0, 0.45)";
  return "rgba(230, 138, 0, 0.15)";
}

export default function WaveformTimeline({
  mediaType,
  mediaId,
  arrInstanceId,
  cues,
  selectedIndex,
  onSelect,
  currentTimeMs,
  onTimingChange,
  onSeek,
  audioTrack = 0,
}: WaveformTimelineProps) {
  const waveformRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WaveSurfer | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const regionsRef = useRef<any>(null);
  const [zoom, setZoom] = useState(50);
  const [ready, setReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const updatingFromRegion = useRef(false);
  const seekingFromCue = useRef(false);
  // Peaks the server has not generated yet come back as 202 with a job id. The
  // editor then waits for that job's terminal event on the jobs socket and
  // asks again, instead of holding a request open while ffmpeg runs.
  const [peaksJobId, setPeaksJobId] = useState<number | null>(null);
  const [waveformError, setWaveformError] = useState("");
  const peaksAttempts = useRef(0);
  const peaksGeneration = useRef(0);

  const apiKey = Environment.apiKey ?? "";
  const peaksUrl =
    mediaType && mediaId
      ? appendArrInstanceParam(
          `${Environment.baseUrl}/api/editor/peaks?mediaType=${mediaType}&mediaId=${mediaId}&audioTrack=${audioTrack}&apikey=${encodeURIComponent(apiKey)}`,
          arrInstanceId,
        )
      : null;

  const loadPeaks = useCallback(() => {
    const ws = wsRef.current;
    if (!ws || !peaksUrl) return;
    const generation = peaksGeneration.current;
    peaksAttempts.current += 1;
    fetch(peaksUrl)
      .then(async (r) => {
        if (r.status === 202) {
          const body = (await r.json()) as { jobId?: number };
          if (generation !== peaksGeneration.current) return;
          if (body?.jobId) {
            setPeaksJobId(body.jobId);
          } else {
            setLoading(false);
          }
          return;
        }
        if (!r.ok) throw new Error("Failed to fetch peaks");
        const data = await r.json();
        if (generation !== peaksGeneration.current) return;
        if (data?.peaks && data?.duration) {
          ws.load("", [new Float32Array(data.peaks)], data.duration);
        } else {
          setLoading(false);
        }
      })
      .catch(() => {
        if (generation === peaksGeneration.current) setLoading(false);
      });
  }, [peaksUrl]);

  const peaksJob = useEditorJob(peaksJobId);
  const peaksJobFinished = isTerminalJob(peaksJob);
  const peaksJobStatus = peaksJob?.status;
  const peaksJobMessage = peaksJob?.error?.message;
  useEffect(() => {
    if (peaksJobId == null || !peaksJobFinished) return;
    setPeaksJobId(null);
    if (peaksJobStatus === "completed" && peaksAttempts.current < 3) {
      loadPeaks();
    } else {
      setLoading(false);
      setWaveformError(
        peaksJobStatus === "failed" && peaksJobMessage
          ? peaksJobMessage
          : "The waveform could not be generated.",
      );
    }
  }, [
    peaksJobId,
    peaksJobFinished,
    peaksJobStatus,
    peaksJobMessage,
    loadPeaks,
  ]);

  // Initialize wavesurfer once, load peaks separately
  useEffect(() => {
    if (!waveformRef.current || !peaksUrl) return;

    const regions = RegionsPlugin.create();
    regionsRef.current = regions;

    const ws = WaveSurfer.create({
      container: waveformRef.current,
      waveColor: "#475569",
      progressColor: "#e68a00",
      cursorColor: "#fbbf24",
      cursorWidth: 2,
      height: 110,
      barWidth: 2,
      barGap: 1,
      barRadius: 1,
      normalize: true,
      interact: true,
      plugins: [
        regions,
        TimelinePlugin.create({
          height: 18,
          timeInterval: 5,
          primaryLabelInterval: 30,
          secondaryLabelInterval: 10,
          style: {
            fontSize: "10px",
            color: "#475569",
          },
          formatTimeCallback: (seconds: number) => {
            const m = Math.floor(seconds / 60);
            const s = Math.floor(seconds % 60);
            return `${m}:${String(s).padStart(2, "0")}`;
          },
        }),
      ],
    });

    wsRef.current = ws;

    ws.on("ready", () => {
      setReady(true);
      setLoading(false);
    });

    // Click on waveform to seek
    ws.on("click", (relativeX: number) => {
      const duration = ws.getDuration();
      if (duration > 0 && onSeek) {
        const ms = Math.round(relativeX * duration * 1000);
        onSeek(ms);
      }
    });

    // Fetch peaks and load
    peaksGeneration.current += 1;
    peaksAttempts.current = 0;
    setPeaksJobId(null);
    setWaveformError("");
    loadPeaks();

    return () => {
      peaksGeneration.current += 1;
      ws.destroy();
      wsRef.current = null;
      regionsRef.current = null;
      setReady(false);
      setLoading(true);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [peaksUrl]);

  // Sync zoom
  useEffect(() => {
    if (wsRef.current && ready) {
      wsRef.current.zoom(zoom);
    }
  }, [ready, zoom]);

  // Build a stable key from cue timings + selected index (ignore text changes)
  const regionKey = useMemo(() => {
    return (
      cues.map((c) => `${c.id}:${c.startMs}:${c.endMs}`).join("|") +
      `|sel:${selectedIndex}`
    );
  }, [cues, selectedIndex]);

  // Sync regions with cues (only when timing or selection changes, not on text edits)
  useEffect(() => {
    const regions = regionsRef.current;
    if (!regions || !ready) return;

    regions.clearRegions();

    cues.forEach((cue, i) => {
      regions.addRegion({
        id: cue.id,
        start: cue.startMs / 1000,
        end: cue.endMs / 1000,
        content: cue.text.split("\n")[0].substring(0, 25),
        color: cueColor(i, selectedIndex),
        drag: true,
        resize: true,
      });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [regionKey, ready]);

  // Region event handlers
  useEffect(() => {
    const regions = regionsRef.current;
    if (!regions) return;

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const handleClick = (region: any, e: Event) => {
      e.stopPropagation();
      const idx = cues.findIndex((c) => c.id === region.id);
      if (idx >= 0) onSelect(idx);
    };

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const handleUpdate = (region: any) => {
      if (!onTimingChange) return;
      const idx = cues.findIndex((c) => c.id === region.id);
      if (idx >= 0) {
        updatingFromRegion.current = true;
        onTimingChange(
          idx,
          Math.round(region.start * 1000),
          Math.round(region.end * 1000),
        );
        setTimeout(() => {
          updatingFromRegion.current = false;
        }, 100);
      }
    };

    regions.on("region-clicked", handleClick);
    regions.on("region-updated", handleUpdate);

    return () => {
      regions.un("region-clicked", handleClick);
      regions.un("region-updated", handleUpdate);
    };
  }, [cues, onSelect, onTimingChange, ready]);

  // Scroll waveform to selected cue
  useEffect(() => {
    const ws = wsRef.current;
    if (!ws || !ready || selectedIndex < 0 || selectedIndex >= cues.length)
      return;
    if (updatingFromRegion.current) return;

    const cue = cues[selectedIndex];
    const duration = ws.getDuration();
    if (duration > 0) {
      seekingFromCue.current = true;
      const progress = Math.min(1, Math.max(0, cue.startMs / 1000 / duration));
      ws.seekTo(progress);
      setTimeout(() => {
        seekingFromCue.current = false;
      }, 100);
    }
  }, [selectedIndex, ready, cues]);

  // Sync waveform cursor to current time (from playback or cue selection)
  const lastSyncTimeRef = useRef(0);
  useEffect(() => {
    const ws = wsRef.current;
    if (!ws || !ready || currentTimeMs === undefined) return;
    if (updatingFromRegion.current) return;

    // Throttle: only sync if at least 200ms since last sync
    const now = Date.now();
    if (now - lastSyncTimeRef.current < 200) return;
    lastSyncTimeRef.current = now;

    const duration = ws.getDuration();
    if (duration > 0) {
      const progress = Math.min(
        1,
        Math.max(0, currentTimeMs / 1000 / duration),
      );
      ws.seekTo(progress);
    }
  }, [currentTimeMs, ready]);

  const handleZoomIn = useCallback(
    () => setZoom((z) => Math.min(500, z + 25)),
    [],
  );
  const handleZoomOut = useCallback(
    () => setZoom((z) => Math.max(10, z - 25)),
    [],
  );

  if (!peaksUrl) {
    return (
      <div
        style={{
          ...containerStyle,
          height: 40,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <span style={{ fontSize: 11, color: "var(--bz-text-disabled)" }}>
          No media loaded for waveform
        </span>
      </div>
    );
  }

  return (
    <div style={containerStyle}>
      <div
        ref={waveformRef}
        style={{
          minHeight: 110,
          cursor: "pointer",
        }}
      />
      {loading && (
        <div
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            height: 110,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: 8,
            background: "rgba(12, 11, 26, 0.92)",
            zIndex: 5,
          }}
        >
          <div
            style={{
              width: 24,
              height: 24,
              border: "3px solid rgba(230, 138, 0, 0.3)",
              borderTop: "3px solid #e68a00",
              borderRadius: "50%",
              animation: "waveform-spin 0.8s linear infinite",
            }}
          />
          <div style={{ color: "#e68a00", fontSize: 13, fontWeight: 600 }}>
            {peaksJobId != null
              ? `Generating waveform${
                  peaksJob && peaksJob.progress_max > 0
                    ? ` (${Math.round((peaksJob.progress_value / peaksJob.progress_max) * 100)}%)`
                    : ""
                }...`
              : "Loading waveform, please wait..."}
          </div>
          <style>{`@keyframes waveform-spin { to { transform: rotate(360deg); } }`}</style>
        </div>
      )}
      {waveformError && !loading && (
        <div
          role="status"
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            height: 110,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "var(--bz-text-secondary)",
            fontSize: 12,
            zIndex: 5,
          }}
        >
          {waveformError}
        </div>
      )}
      <div style={controlsStyle}>
        <span
          style={{
            fontFamily: "'JetBrains Mono', monospace",
            fontSize: 11,
            color: "var(--bz-stat-processing)",
            fontWeight: 600,
            minWidth: 60,
          }}
        >
          {formatMs(currentTimeMs ?? 0)}
        </span>
        <div
          style={{
            width: 1,
            height: 14,
            background: "var(--bz-border-interactive)",
          }}
        />
        <button
          type="button"
          style={zoomBtnStyle}
          onClick={handleZoomOut}
          title="Zoom out"
        >
          -
        </button>
        <span
          style={{
            fontFamily: "'JetBrains Mono', monospace",
            fontSize: 10,
            color: "var(--bz-text-tertiary)",
          }}
        >
          {zoom}px/s
        </span>
        <button
          type="button"
          style={zoomBtnStyle}
          onClick={handleZoomIn}
          title="Zoom in"
        >
          +
        </button>
        <span
          style={{
            marginLeft: "auto",
            fontSize: 10,
            color: "var(--bz-text-disabled)",
          }}
        >
          {cues.length} cues
        </span>
      </div>
    </div>
  );
}
