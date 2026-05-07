import {
  type CSSProperties,
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import {
  faBackward,
  faForward,
  faPause,
  faPlay,
  faVideoSlash,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import Hls, { type ErrorData } from "hls.js";
import { Environment } from "@/utilities/env";

export interface VideoPreviewHandle {
  togglePlay(): void;
  seekRelative(deltaMs: number): void;
}

export interface AudioTrack {
  index: number;
  codec: string;
  language: string;
  title: string;
  channels: number;
  label: string;
}

interface VideoPreviewProps {
  mediaType?: string;
  mediaId?: number;
  currentTimeMs: number;
  seekId?: number;
  currentSubtitleText?: string;
  onTimeUpdate?: (ms: number) => void;
  onPlayStateChange?: (playing: boolean) => void;
  onAudioTracksLoaded?: (tracks: AudioTrack[]) => void;
  audioTrack?: number;
}

export function formatTime(ms: number): string {
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) {
    return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  return `${m}:${String(s).padStart(2, "0")}`;
}

const containerStyle: CSSProperties = {
  background: "var(--bz-surface-ground)",
  border: "1px solid var(--bz-border-card)",
  borderRadius: "var(--bz-radius-xs)",
  overflow: "hidden",
  display: "flex",
  flexDirection: "column",
  width: "100%",
  flexShrink: 0,
};

// Convert subtitle text with tags into safe display HTML
export function renderSubtitleHtml(text: string): string {
  // Strip ASS override tags like {\i1}, {\b1}, {\an8}, {\pos(x,y)}, etc.
  let html = text.replace(/\{\\[^}]*\}/g, "");
  // Allow basic HTML formatting tags, escape everything else
  // First escape HTML entities
  html = html
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  // Restore safe tags
  html = html
    .replace(/&lt;i&gt;/gi, "<i>")
    .replace(/&lt;\/i&gt;/gi, "</i>")
    .replace(/&lt;b&gt;/gi, "<b>")
    .replace(/&lt;\/b&gt;/gi, "</b>")
    .replace(/&lt;u&gt;/gi, "<u>")
    .replace(/&lt;\/u&gt;/gi, "</u>")
    .replace(/&lt;s&gt;/gi, "<s>")
    .replace(/&lt;\/s&gt;/gi, "</s>");
  // Strip font tags (render as plain text)
  html = html
    .replace(/&lt;font[^&]*&gt;/gi, "")
    .replace(/&lt;\/font&gt;/gi, "");
  // Convert newlines to <br>
  html = html.replace(/\n/g, "<br>");
  return html;
}

const subtitleOverlayStyle: CSSProperties = {
  position: "absolute",
  bottom: 8,
  left: 8,
  right: 8,
  textAlign: "center",
  fontSize: 14,
  fontFamily: "sans-serif",
  color: "#fff",
  textShadow:
    "-1px -1px 0 #000, 1px -1px 0 #000, -1px 1px 0 #000, 1px 1px 0 #000, 0 0 4px rgba(0,0,0,0.8)",
  pointerEvents: "none",
  whiteSpace: "pre-wrap",
  lineHeight: 1.3,
};

const controlsBarStyle: CSSProperties = {
  height: 28,
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "0 8px",
  background: "var(--bz-surface-base)",
  borderTop: "1px solid var(--bz-border-card)",
  flexShrink: 0,
};

const timeDisplayStyle: CSSProperties = {
  fontFamily: "'JetBrains Mono', monospace",
  fontSize: 11,
  color: "var(--bz-stat-processing)",
  whiteSpace: "nowrap",
  userSelect: "none",
};

const playBtnStyle: CSSProperties = {
  background: "none",
  border: "none",
  cursor: "pointer",
  color: "var(--bz-text-secondary)",
  padding: 2,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 12,
  lineHeight: 1,
};

const volumeSliderStyle: CSSProperties = {
  width: 50,
  height: 3,
  accentColor: "#e68a00",
  cursor: "pointer",
};

const spinnerStyle: CSSProperties = {
  position: "absolute",
  top: "50%",
  left: "50%",
  transform: "translate(-50%, -50%)",
  color: "var(--mantine-color-brand-5)",
  fontSize: 20,
  pointerEvents: "none",
};

const placeholderStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  gap: 8,
  padding: "24px 12px",
  color: "var(--bz-text-disabled)",
  fontSize: 13,
  background: "var(--bz-surface-ground)",
  border: "1px solid var(--bz-border-card)",
  borderRadius: "var(--bz-radius-xs)",
  minHeight: 120,
};

const VideoPreview = forwardRef<VideoPreviewHandle, VideoPreviewProps>(
  function VideoPreview(
    {
      mediaType,
      mediaId,
      currentTimeMs,
      seekId,
      currentSubtitleText,
      onTimeUpdate,
      onPlayStateChange,
      onAudioTracksLoaded,
      audioTrack = 0,
    },
    ref,
  ) {
    const videoRef = useRef<HTMLVideoElement>(null);
    const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const isPlayingRef = useRef(false);
    const lastReportedMsRef = useRef(-1);
    // Set when we tear down hls.js to start a new session at a different
    // start time (track switch or seek-before-current-session-start) so the
    // new stream resumes playback automatically once it loads.
    const shouldAutoplayAfterAttachRef = useRef(false);

    const [playing, setPlaying] = useState(false);
    const [buffering, setBuffering] = useState(false);
    const [volume, setVolume] = useState(0.5);
    const [playbackRate, setPlaybackRate] = useState(1);
    const [currentMs, setCurrentMs] = useState(0);
    const [duration, setDuration] = useState(0);
    const [videoError, setVideoError] = useState(false);

    // HLS session: which audio track ffmpeg is encoding and what source-time
    // offset it began at. Updated atomically (one setState) so the URL never
    // ends up momentarily mismatched between track and start-time. The video
    // element's currentTime is *local* to this session; the user-facing
    // position is `startSec + video.currentTime`.
    const [hlsSession, setHlsSession] = useState({
      audioTrack: 0,
      startSec: 0,
    });
    const hlsSessionRef = useRef(hlsSession);
    useEffect(() => {
      hlsSessionRef.current = hlsSession;
    }, [hlsSession]);

    const hasMedia = mediaType != null && mediaId != null;
    const apiKey = Environment.apiKey ?? "";

    // Codec info popup data is purely informational now; the HLS encoder
    // server-side picks copy vs transcode based on its own probe. Aspect ratio
    // is pinned on the wrapper so the video element doesn't collapse to zero
    // height while hls.js is attaching, which avoids layout shift on first
    // load and on every audio-track switch.
    const [videoCodecInfo, setVideoCodecInfo] = useState("");
    const [browserCodecs, setBrowserCodecs] = useState("");
    const [aspectRatio, setAspectRatio] = useState<string | undefined>(
      undefined,
    );
    useEffect(() => {
      if (!hasMedia) return;
      let cancelled = false;
      const infoUrl = `${Environment.baseUrl}/api/editor/info?mediaType=${mediaType}&mediaId=${mediaId}&apikey=${encodeURIComponent(apiKey)}`;
      fetch(infoUrl)
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (cancelled || !data) return;
          if (data.duration) setDuration(Math.round(data.duration * 1000));
          if (data.audioTracks && onAudioTracksLoaded) {
            onAudioTracksLoaded(data.audioTracks);
          }
          if (data.resolution && typeof data.resolution === "string") {
            const dims = data.resolution
              .split("x")
              .map((s: string) => parseInt(s, 10));
            const w = dims[0];
            const h = dims[1];
            if (Number.isFinite(w) && Number.isFinite(h) && w > 0 && h > 0) {
              setAspectRatio(`${w} / ${h}`);
            }
          }
          const vCodec = (data.videoCodec || "").toLowerCase();
          const aCodec = (data.audioCodec || "").toLowerCase();
          const parts = [vCodec, aCodec, data.resolution].filter(Boolean);
          setVideoCodecInfo(parts.join(" / "));

          // Browser codec capability table for the popup tooltip. Informational
          // only; hls.js + the server-side HLS encoder make the actual playback
          // decisions.
          const testVideo = document.createElement("video");
          const testCodecs: Array<[string, string, string]> = [
            ["H.264", "video/mp4", "avc1.64001E"],
            ["HEVC", "video/mp4", "hvc1.1.6.L93.B0"],
            ["VP9", "video/webm", "vp09.00.10.08"],
            ["AV1", "video/mp4", "av01.0.04M.08"],
            ["AAC", "audio/mp4", "mp4a.40.2"],
            ["AC3", "audio/mp4", "ac-3"],
            ["EAC3", "audio/mp4", "ec-3"],
            ["Opus", "audio/webm", "opus"],
            ["MP3", "audio/mpeg", ""],
          ];
          const supported: string[] = [];
          const unsupported: string[] = [];
          for (const [name, mime, codec] of testCodecs) {
            const full = codec ? `${mime}; codecs="${codec}"` : mime;
            const result = testVideo.canPlayType(full);
            if (result) {
              supported.push(name);
            } else {
              unsupported.push(name);
            }
          }
          setBrowserCodecs(
            `Browser supports: ${supported.join(", ") || "none"}\nNot supported: ${unsupported.join(", ") || "none"}`,
          );
        })
        .catch(() => {
          // /info failure is non-fatal; HLS playback still attempts.
        });
      return () => {
        cancelled = true;
      };
    }, [hasMedia, mediaType, mediaId, apiKey, onAudioTracksLoaded]);

    // Media-change reset: fresh session at start of file. Bail out if the
    // session is already in the right state (initial mount with default props).
    useEffect(() => {
      const prev = hlsSessionRef.current;
      if (prev.audioTrack === audioTrack && prev.startSec === 0) return;
      shouldAutoplayAfterAttachRef.current = false;
      setHlsSession({ audioTrack, startSec: 0 });
      // audioTrack is intentionally outside the deps; we only reset on media
      // swap. Audio-track changes are handled by their own effect below so
      // they can capture the current playback position.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [mediaType, mediaId]);

    // Audio-track change: spawn a new HLS session that begins at the user's
    // current position so they don't have to wait for ffmpeg to encode from
    // t=0 to where they were.
    useEffect(() => {
      setHlsSession((prev) => {
        if (prev.audioTrack === audioTrack) return prev;
        const video = videoRef.current;
        const currentLocalSec = video ? video.currentTime : 0;
        shouldAutoplayAfterAttachRef.current = isPlayingRef.current;
        return {
          audioTrack,
          startSec: prev.startSec + currentLocalSec,
        };
      });
    }, [audioTrack]);

    // HLS playlist URL is path-based so segments resolve correctly via relative
    // URLs in the manifest. startSec is part of the path so segment URLs inherit
    // the same session.
    const hlsUrl = hasMedia
      ? `${Environment.baseUrl}/api/editor/hls/${encodeURIComponent(mediaType)}/${mediaId}/${hlsSession.audioTrack}/${Math.floor(hlsSession.startSec)}/playlist.m3u8`
      : "";

    // Set up hls.js (or native HLS on Safari) on the video element. Re-runs when
    // the playlist URL changes (media swap or audio-track change). hls.js owns
    // segment fetching, buffering, and seek-back within the cached buffer.
    useEffect(() => {
      const video = videoRef.current;
      if (!video || !hlsUrl) return;

      setVideoError(false);
      let hls: Hls | null = null;

      if (Hls.isSupported()) {
        hls = new Hls({
          xhrSetup: (xhr) => {
            if (apiKey) xhr.setRequestHeader("X-API-KEY", apiKey);
          },
          // Generous back-buffer so seek-back within the recently played window
          // is instant (no segment refetch).
          backBufferLength: 90,
          maxBufferLength: 30,
        });
        hls.loadSource(hlsUrl);
        hls.attachMedia(video);
        hls.on(Hls.Events.ERROR, (_event, data: ErrorData) => {
          if (!data.fatal) return;
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
            // Most often a transient segment 404 because ffmpeg hasn't written
            // it yet. hls.js retries internally on non-fatal; on fatal network
            // we give it one explicit kick.
            hls?.startLoad();
          } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
            hls?.recoverMediaError();
          } else {
            setVideoError(true);
          }
        });
      } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        // Safari / iOS native HLS: can't inject custom headers on segment
        // fetches, so the API key has to ride in the URL.
        video.src = `${hlsUrl}?apikey=${encodeURIComponent(apiKey)}`;
      } else {
        setVideoError(true);
      }

      return () => {
        hls?.destroy();
        if (video) {
          video.removeAttribute("src");
          video.load();
        }
      };
    }, [hlsUrl, apiKey]);

    // Time reporting: video.currentTime is local to the current HLS session;
    // the user-facing absolute position is startSec + local.
    const startTimeReporting = useCallback(() => {
      if (timerRef.current) return;
      timerRef.current = setInterval(() => {
        const video = videoRef.current;
        if (!video) return;
        const absoluteMs = Math.round(
          (hlsSessionRef.current.startSec + video.currentTime) * 1000,
        );
        setCurrentMs(absoluteMs);
        if (
          onTimeUpdate &&
          Math.abs(absoluteMs - lastReportedMsRef.current) >= 50
        ) {
          lastReportedMsRef.current = absoluteMs;
          onTimeUpdate(absoluteMs);
        }
      }, 100);
    }, [onTimeUpdate]);

    const stopTimeReporting = useCallback(() => {
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    }, []);

    useEffect(() => {
      return () => stopTimeReporting();
    }, [stopTimeReporting]);

    // External seek (parent passes new absolute currentTimeMs). Goes through
    // the same three-case logic as the user-initiated seek helper: spawn a new
    // session for before-startSec or far-forward-beyond-buffered targets, do
    // a native seek otherwise.
    useEffect(() => {
      if (!hasMedia) return;
      const video = videoRef.current;
      if (!video) return;
      const targetAbsoluteSec = currentTimeMs / 1000;
      const startSec = hlsSessionRef.current.startSec;
      const localTargetSec = targetAbsoluteSec - startSec;
      const NEAR_BUFFER_SLACK_SEC = 10;

      let needsNewSession;
      if (targetAbsoluteSec < startSec) {
        needsNewSession = true;
      } else if (video.buffered.length === 0) {
        needsNewSession = localTargetSec > NEAR_BUFFER_SLACK_SEC;
      } else {
        let withinReach = false;
        for (let i = 0; i < video.buffered.length; i++) {
          if (
            localTargetSec >= video.buffered.start(i) - NEAR_BUFFER_SLACK_SEC &&
            localTargetSec <= video.buffered.end(i) + NEAR_BUFFER_SLACK_SEC
          ) {
            withinReach = true;
            break;
          }
        }
        needsNewSession = !withinReach;
      }

      if (needsNewSession) {
        shouldAutoplayAfterAttachRef.current = isPlayingRef.current;
        setHlsSession((prev) => ({ ...prev, startSec: targetAbsoluteSec }));
        setCurrentMs(currentTimeMs);
      } else if (Math.abs(localTargetSec - video.currentTime) > 0.25) {
        video.currentTime = localTargetSec;
        setCurrentMs(currentTimeMs);
      }
    }, [currentTimeMs, seekId, hasMedia]);

    // Sync volume and playback rate
    useEffect(() => {
      const video = videoRef.current;
      if (video) {
        video.volume = volume;
        video.playbackRate = playbackRate;
      }
    }, [volume, playbackRate]);

    const togglePlayPause = useCallback(() => {
      const video = videoRef.current;
      if (!video) return;
      if (video.paused) {
        video.play().catch(() => {
          /* ignored */
        });
      } else {
        video.pause();
      }
    }, []);

    // Internal seek helper. Shared by seekRelative, seekTo, and seek bar onChange.
    // Three cases:
    //   1. Target before current session's startSec: spawn new session at target.
    //   2. Target far past what hls.js currently has buffered: spawn new session
    //      at target, because ffmpeg likely hasn't encoded that far yet and the
    //      segment isn't in the playlist (hls.js would just play the latest
    //      available segment instead of seeking).
    //   3. Target within / near the buffered range: native video.currentTime;
    //      hls.js fetches nearby segments quickly.
    const seekToAbsoluteMs = useCallback(
      (targetMs: number) => {
        const video = videoRef.current;
        if (!video) return;
        const clampedMs = Math.max(0, Math.min(duration, targetMs));
        const targetAbsoluteSec = clampedMs / 1000;
        const startSec = hlsSessionRef.current.startSec;
        const localTargetSec = targetAbsoluteSec - startSec;

        // Slack around buffered ranges, in seconds. Inside this window hls.js
        // can fetch the missing segment quickly; outside it, ffmpeg almost
        // certainly hasn't encoded that far and a fresh session is faster.
        const NEAR_BUFFER_SLACK_SEC = 10;

        let needsNewSession;
        if (targetAbsoluteSec < startSec) {
          needsNewSession = true;
        } else if (video.buffered.length === 0) {
          // Just-loaded session with no buffer yet. Native seek only if target
          // is within slack of the session start; otherwise ffmpeg won't reach
          // that far for a long time.
          needsNewSession = localTargetSec > NEAR_BUFFER_SLACK_SEC;
        } else {
          let withinReach = false;
          for (let i = 0; i < video.buffered.length; i++) {
            if (
              localTargetSec >=
                video.buffered.start(i) - NEAR_BUFFER_SLACK_SEC &&
              localTargetSec <= video.buffered.end(i) + NEAR_BUFFER_SLACK_SEC
            ) {
              withinReach = true;
              break;
            }
          }
          needsNewSession = !withinReach;
        }

        if (needsNewSession) {
          shouldAutoplayAfterAttachRef.current = isPlayingRef.current;
          setHlsSession((prev) => ({ ...prev, startSec: targetAbsoluteSec }));
        } else {
          video.currentTime = localTargetSec;
        }
        setCurrentMs(clampedMs);
        onTimeUpdate?.(clampedMs);
      },
      [duration, onTimeUpdate],
    );

    const seekRelative = useCallback(
      (deltaMs: number) => {
        seekToAbsoluteMs(currentMs + deltaMs);
      },
      [currentMs, seekToAbsoluteMs],
    );

    const seekTo = useCallback(
      (ms: number) => {
        seekToAbsoluteMs(ms);
      },
      [seekToAbsoluteMs],
    );

    useImperativeHandle(
      ref,
      () => ({
        togglePlay: togglePlayPause,
        seekRelative,
      }),
      [togglePlayPause, seekRelative],
    );

    const handlePlay = useCallback(() => {
      isPlayingRef.current = true;
      setPlaying(true);
      onPlayStateChange?.(true);
      startTimeReporting();
    }, [onPlayStateChange, startTimeReporting]);

    const handlePause = useCallback(() => {
      isPlayingRef.current = false;
      setPlaying(false);
      onPlayStateChange?.(false);
      stopTimeReporting();
      const video = videoRef.current;
      if (video && onTimeUpdate) {
        const absoluteMs = Math.round(
          (hlsSessionRef.current.startSec + video.currentTime) * 1000,
        );
        setCurrentMs(absoluteMs);
        onTimeUpdate(absoluteMs);
      }
    }, [onPlayStateChange, stopTimeReporting, onTimeUpdate]);

    const handleLoadedMetadata = useCallback(() => {
      const video = videoRef.current;
      if (!video) return;
      // Duration is the *full source* duration, only known from /info. The
      // video element's duration is the remaining stream from session start
      // and is only useful as a fallback when /info hasn't resolved.
      if (!duration && Number.isFinite(video.duration)) {
        setDuration(
          Math.round((hlsSessionRef.current.startSec + video.duration) * 1000),
        );
      }
      // Latch aspect ratio from the loaded video if /info didn't have it.
      if (!aspectRatio && video.videoWidth > 0 && video.videoHeight > 0) {
        setAspectRatio(`${video.videoWidth} / ${video.videoHeight}`);
      }
      setVideoError(false);
      video.volume = volume;
      video.playbackRate = playbackRate;
      // Resume playback after a session change (track switch or seek-before-
      // start) if the user was playing prior to the tear-down. hls.js attach
      // resets the video to paused, so we explicitly call play() here.
      if (shouldAutoplayAfterAttachRef.current) {
        shouldAutoplayAfterAttachRef.current = false;
        video.play().catch(() => {
          /* user-gesture restrictions or transient: ignored */
        });
      }
    }, [duration, aspectRatio, volume, playbackRate]);

    const handleKeyDown = useCallback(
      (e: React.KeyboardEvent) => {
        if (e.key === " " || e.key === "Spacebar") {
          e.preventDefault();
          togglePlayPause();
        }
        if (e.key === "ArrowLeft" && e.altKey) {
          e.preventDefault();
          seekRelative(-5000);
        }
        if (e.key === "ArrowRight" && e.altKey) {
          e.preventDefault();
          seekRelative(5000);
        }
      },
      [togglePlayPause, seekRelative],
    );

    // No media placeholder
    if (!hasMedia) {
      return (
        <div style={placeholderStyle}>
          <FontAwesomeIcon
            icon={faVideoSlash}
            style={{ fontSize: 24, color: "var(--bz-text-disabled)" }}
          />
          <span>No video</span>
        </div>
      );
    }

    // Error state
    if (videoError) {
      return (
        <div style={placeholderStyle}>
          <FontAwesomeIcon
            icon={faVideoSlash}
            style={{ fontSize: 24, color: "#EF4444" }}
          />
          <span style={{ color: "#EF4444" }}>Video unavailable</span>
        </div>
      );
    }

    return (
      <div style={containerStyle}>
        {/* Video area. aspectRatio is pinned (when known) so the wrapper
            doesn't collapse to zero height while hls.js is attaching, which
            avoids layout shift on first load and on every track switch. */}
        <div
          style={{
            position: "relative",
            width: "100%",
            background: "#000",
            lineHeight: 0,
            aspectRatio,
          }}
          tabIndex={0}
          onKeyDown={handleKeyDown}
          role="region"
          aria-label="Video preview"
        >
          <video
            ref={videoRef}
            style={{ width: "100%", height: "100%", display: "block" }}
            preload="metadata"
            onPlay={handlePlay}
            onPause={handlePause}
            onLoadedMetadata={handleLoadedMetadata}
            onWaiting={() => setBuffering(true)}
            onCanPlay={() => setBuffering(false)}
          />

          {/* Loading spinner */}
          {buffering && (
            <div style={spinnerStyle}>
              <FontAwesomeIcon icon={faPlay} spin />
            </div>
          )}

          {/* Subtitle overlay */}
          {currentSubtitleText && (
            <div
              style={subtitleOverlayStyle}
              dangerouslySetInnerHTML={{
                __html: renderSubtitleHtml(currentSubtitleText),
              }}
            />
          )}
        </div>

        {/* Seek bar */}
        {duration > 0 && (
          <div style={{ padding: "0 8px", flexShrink: 0 }}>
            <input
              type="range"
              min={0}
              max={duration}
              step={100}
              value={currentMs}
              onChange={(e) => seekTo(parseInt(e.target.value, 10))}
              style={{
                width: "100%",
                height: 4,
                cursor: "pointer",
                accentColor: "var(--mantine-color-brand-5)",
              }}
              aria-label="Seek"
            />
          </div>
        )}

        {/* Controls bar */}
        <div style={controlsBarStyle}>
          <button
            type="button"
            style={playBtnStyle}
            onClick={() => seekRelative(-5000)}
            aria-label="Back 5s"
            title="Back 5s (Alt+Left)"
          >
            <FontAwesomeIcon icon={faBackward} style={{ fontSize: 9 }} />
          </button>

          <button
            type="button"
            style={playBtnStyle}
            onClick={togglePlayPause}
            aria-label={playing ? "Pause" : "Play"}
            title={playing ? "Pause" : "Play"}
          >
            <FontAwesomeIcon icon={playing ? faPause : faPlay} />
          </button>

          <button
            type="button"
            style={playBtnStyle}
            onClick={() => seekRelative(5000)}
            aria-label="Forward 5s"
            title="Forward 5s (Alt+Right)"
          >
            <FontAwesomeIcon icon={faForward} style={{ fontSize: 9 }} />
          </button>

          <span style={timeDisplayStyle}>
            {formatTime(currentMs)}
            {duration > 0 && (
              <span style={{ color: "var(--bz-text-disabled)" }}>
                {" "}
                / {formatTime(duration)}
              </span>
            )}
          </span>

          {videoCodecInfo && (
            <span
              title={
                [videoCodecInfo, browserCodecs].filter(Boolean).join("\n\n") ||
                undefined
              }
              style={{
                fontSize: 9,
                fontFamily: "'JetBrains Mono', monospace",
                padding: "1px 5px",
                borderRadius: "var(--bz-radius-xs)",
                background: "rgba(77, 171, 247, 0.15)",
                color: "#4dabf7",
                whiteSpace: "nowrap",
              }}
            >
              HLS
            </span>
          )}
          <span style={{ flex: 1 }} />

          <select
            value={playbackRate}
            onChange={(e) => setPlaybackRate(parseFloat(e.target.value))}
            style={{
              background: "var(--bz-surface-raised)",
              border: "1px solid var(--bz-border-interactive)",
              borderRadius: "var(--bz-radius-xs)",
              color:
                playbackRate !== 1
                  ? "var(--mantine-color-brand-5)"
                  : "var(--bz-text-tertiary)",
              fontSize: 9,
              fontFamily: "'JetBrains Mono', monospace",
              padding: "0 2px",
              height: 18,
              cursor: "pointer",
            }}
            title="Playback speed"
          >
            <option value={0.25}>0.25x</option>
            <option value={0.5}>0.5x</option>
            <option value={0.75}>0.75x</option>
            <option value={1}>1x</option>
            <option value={1.25}>1.25x</option>
            <option value={1.5}>1.5x</option>
            <option value={2}>2x</option>
          </select>

          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={volume}
            onChange={(e) => setVolume(parseFloat(e.target.value))}
            style={volumeSliderStyle}
            aria-label="Volume"
            title={`Volume: ${Math.round(volume * 100)}%`}
          />
        </div>
      </div>
    );
  },
);

export default VideoPreview;
