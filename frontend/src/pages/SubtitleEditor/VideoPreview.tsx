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

const videoWrapperStyle: CSSProperties = {
  position: "relative",
  width: "100%",
  background: "#000",
  lineHeight: 0,
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
    const retryCountRef = useRef(0);

    const [playing, setPlaying] = useState(false);
    const [buffering, setBuffering] = useState(false);
    const [volume, setVolume] = useState(0.5);
    const [playbackRate, setPlaybackRate] = useState(1);
    const [currentMs, setCurrentMs] = useState(0);
    const [duration, setDuration] = useState(0);
    const [videoError, setVideoError] = useState(false);
    const [streamKey, setStreamKey] = useState(0);

    const hasMedia = mediaType != null && mediaId != null;
    const apiKey = Environment.apiKey ?? "";

    // Check if the browser can play this video natively (no transcode needed)
    const [directPlay, setDirectPlay] = useState<boolean | null>(null);
    const [remuxMode, setRemuxMode] = useState(false);
    const [videoCodecInfo, setVideoCodecInfo] = useState("");
    const [browserCodecs, setBrowserCodecs] = useState("");
    useEffect(() => {
      if (!hasMedia) return;
      // Reset playback-mode state before the new probe completes, otherwise
      // videoSrc gets computed with the previous directPlay value against the
      // new baseVideoUrl during the in-flight fetch.
      setDirectPlay(null);
      setRemuxMode(false);
      const infoUrl = `${Environment.baseUrl}/api/editor/info?mediaType=${mediaType}&mediaId=${mediaId}&apikey=${encodeURIComponent(apiKey)}`;
      fetch(infoUrl)
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (!data) {
            setDirectPlay(false);
            return;
          }
          if (data.duration) setDuration(Math.round(data.duration * 1000));
          if (data.audioTracks && onAudioTracksLoaded) {
            onAudioTracksLoaded(data.audioTracks);
          }

          // Ask the browser if it can play this codec/container combo
          const testVideo = document.createElement("video");
          const vCodec = (data.videoCodec || "").toLowerCase();
          const aCodec = (data.audioCodec || "").toLowerCase();
          const ext = (data.container || "").toLowerCase();

          // Build MIME type with codecs for canPlayType check
          const codecMap: Record<string, string> = {
            h264: "avc1.64001E",
            avc: "avc1.64001E",
            hevc: "hvc1.1.6.L93.B0",
            h265: "hvc1.1.6.L93.B0",
            vp9: "vp09.00.10.08",
            vp8: "vp8",
            av1: "av01.0.04M.08",
          };
          const audioCodecMap: Record<string, string> = {
            aac: "mp4a.40.2",
            opus: "opus",
            vorbis: "vorbis",
            ac3: "ac-3",
            eac3: "ec-3",
            mp3: "mp4a.69",
          };

          const vCodecStr = codecMap[vCodec] || "";
          const aCodecStr = audioCodecMap[aCodec] || "";
          const container = ext === "webm" ? "webm" : "mp4";
          // MKV is excluded: browsers can't efficiently seek MKV over HTTP ranges
          const servableContainer = [".mp4", ".m4v", ".webm"].includes(
            `.${ext}`,
          );

          // Test video codec independently (audio will be transcoded to AAC by backend if needed)
          const canPlayVideo = vCodecStr
            ? !!testVideo.canPlayType(
                `video/${container}; codecs="${vCodecStr}"`,
              )
            : false;
          const canPlayAudio = aCodecStr
            ? !!testVideo.canPlayType(
                `video/${container}; codecs="${aCodecStr}"`,
              )
            : false;
          const canPlayBoth = canPlayVideo && canPlayAudio;

          // Direct play: browser handles both codecs AND file container is directly servable.
          // Remux: browser handles video codec but not audio. Copy video, transcode audio to AAC.
          // Direct play also requires the default audio track. For audioTrack > 0 we always
          // remux so ffmpeg can map the requested track into the served stream, which keeps
          // the video element single (no parallel <audio> element to drift against).
          const isDirect = canPlayBoth && servableContainer && audioTrack === 0;
          const isRemux = canPlayVideo && !isDirect;
          setDirectPlay(isDirect);
          setRemuxMode(isRemux);

          const mode = isDirect
            ? "direct play"
            : isRemux
              ? "remux (audio transcode)"
              : "transcoding";
          const parts = [vCodec, aCodec, data.resolution].filter(Boolean);
          setVideoCodecInfo(`${parts.join(" / ")} (${mode})`);

          // Detect what this browser can play
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
        .catch(() => setDirectPlay(false));
    }, [hasMedia, mediaType, mediaId, apiKey, onAudioTracksLoaded, audioTrack]);

    // Reset retry state when media changes
    useEffect(() => {
      retryCountRef.current = 0;
    }, [mediaType, mediaId]);

    // For transcoded mode, track the seek offset in the ffmpeg ?t= param
    const [seekOffsetSec, setSeekOffsetSec] = useState(0);
    const seekOffsetSecRef = useRef(0);
    useEffect(() => {
      seekOffsetSecRef.current = seekOffsetSec;
    }, [seekOffsetSec]);

    const baseVideoUrl = hasMedia
      ? `${Environment.baseUrl}/api/editor/video?mediaType=${encodeURIComponent(mediaType)}&mediaId=${mediaId}&audioTrack=${audioTrack}&apikey=${encodeURIComponent(apiKey)}`
      : "";
    // Only direct-play supports native seeking, it's served as a file with HTTP Range support.
    // Remux is a live ffmpeg pipe, setting video.currentTime on a non-seekable stream doesn't
    // work. Remux seeks by reloading the stream with ?t=, same as transcode mode.
    const nativeSeek = directPlay === true;
    // Don't set src until directPlay is resolved.
    const videoSrc =
      !baseVideoUrl || directPlay === null
        ? ""
        : directPlay
          ? `${baseVideoUrl}&direct=1`
          : remuxMode
            ? `${baseVideoUrl}&remux=1${seekOffsetSec > 0 ? `&t=${seekOffsetSec.toFixed(1)}` : ""}`
            : `${baseVideoUrl}${seekOffsetSec > 0 ? `&t=${seekOffsetSec.toFixed(1)}` : ""}`;

    // Time reporting: directPlay uses video.currentTime directly, transcode adds offset
    const startTimeReporting = useCallback(() => {
      if (timerRef.current) return;
      timerRef.current = setInterval(() => {
        const video = videoRef.current;
        if (!video) return;
        const absoluteMs = nativeSeek
          ? Math.round(video.currentTime * 1000)
          : Math.round((seekOffsetSecRef.current + video.currentTime) * 1000);
        setCurrentMs(absoluteMs);
        if (
          onTimeUpdate &&
          Math.abs(absoluteMs - lastReportedMsRef.current) >= 50
        ) {
          lastReportedMsRef.current = absoluteMs;
          onTimeUpdate(absoluteMs);
        }
      }, 100);
    }, [onTimeUpdate, nativeSeek]);

    const stopTimeReporting = useCallback(() => {
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    }, []);

    useEffect(() => {
      return () => stopTimeReporting();
    }, [stopTimeReporting]);

    // Seek handling: native vs transcode
    const autoPlayAfterSeekRef = useRef(false);
    useEffect(() => {
      if (!hasMedia || directPlay === null) return;
      const targetSec = currentTimeMs / 1000;

      if (nativeSeek) {
        const video = videoRef.current;
        if (video) {
          video.currentTime = targetSec;
          setCurrentMs(currentTimeMs);
        }
      } else {
        // Remux/transcode: reload the video stream with ?t= parameter
        if (Math.abs(targetSec - seekOffsetSec) > 1) {
          autoPlayAfterSeekRef.current = isPlayingRef.current;
          const video = videoRef.current;
          if (video && isPlayingRef.current) {
            video.pause();
          }
          seekOffsetSecRef.current = targetSec;
          setSeekOffsetSec(targetSec);
          setCurrentMs(currentTimeMs);
          // User-initiated seek: re-arm the retry budget so a previous burst
          // of errors doesn't make the new stream fall straight to the
          // "Video unavailable" overlay.
          retryCountRef.current = 0;
        }
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [currentTimeMs, seekId, hasMedia, directPlay]);

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

    const seekRelative = useCallback(
      (deltaMs: number) => {
        const video = videoRef.current;
        if (!video) return;
        const absoluteMs = nativeSeek
          ? Math.round(video.currentTime * 1000)
          : Math.round((seekOffsetSecRef.current + video.currentTime) * 1000);
        const targetMs = Math.max(0, Math.min(duration, absoluteMs + deltaMs));
        const targetSec = targetMs / 1000;
        if (nativeSeek) {
          video.currentTime = targetSec;
          setCurrentMs(targetMs);
          onTimeUpdate?.(targetMs);
        } else {
          const wasPlaying = isPlayingRef.current;
          if (wasPlaying) video.pause();
          seekOffsetSecRef.current = targetSec;
          setSeekOffsetSec(targetSec);
          setCurrentMs(targetMs);
          onTimeUpdate?.(targetMs);
          if (wasPlaying) autoPlayAfterSeekRef.current = true;
          retryCountRef.current = 0;
        }
      },
      [nativeSeek, duration, onTimeUpdate],
    );

    const seekTo = useCallback(
      (ms: number) => {
        const targetMs = Math.max(0, Math.min(duration, ms));
        const targetSec = targetMs / 1000;
        const video = videoRef.current;
        if (!video) return;
        if (nativeSeek) {
          video.currentTime = targetSec;
          setCurrentMs(targetMs);
          onTimeUpdate?.(targetMs);
        } else {
          const wasPlaying = isPlayingRef.current;
          if (wasPlaying) video.pause();
          seekOffsetSecRef.current = targetSec;
          setSeekOffsetSec(targetSec);
          setCurrentMs(targetMs);
          onTimeUpdate?.(targetMs);
          if (wasPlaying) autoPlayAfterSeekRef.current = true;
          retryCountRef.current = 0;
        }
      },
      [nativeSeek, duration, onTimeUpdate],
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
      // Don't reset retryCountRef here: a stream that plays for a few ms,
      // errors, retries, plays again briefly, errors again, would loop forever
      // and respawn ffmpeg every time. The counter only resets on a user
      // intervention (seek or media change).
      isPlayingRef.current = true;
      setPlaying(true);
      onPlayStateChange?.(true);
      startTimeReporting();
    }, [onPlayStateChange, startTimeReporting]);

    const handleVideoError = useCallback(() => {
      // Some browsers fire `error` for `<video src="">`. We can hit that
      // empty-src window during the initial /info probe (directPlay === null)
      // or during a media-change fetch. Don't burn retries or escalate to the
      // error overlay just because the src isn't ready yet.
      if (!videoSrc) return;
      if (!nativeSeek && retryCountRef.current < 3) {
        retryCountRef.current++;
        const video = videoRef.current;
        if (video) {
          const absoluteSec = seekOffsetSecRef.current + video.currentTime;
          seekOffsetSecRef.current = absoluteSec;
          setSeekOffsetSec(absoluteSec);
        }
        // Only resume play after retry if the user was actually playing.
        // A buffer-drop error while paused shouldn't auto-start playback.
        autoPlayAfterSeekRef.current = isPlayingRef.current;
        setStreamKey((k) => k + 1);
      } else {
        setVideoError(true);
      }
    }, [nativeSeek, videoSrc]);

    const handlePause = useCallback(() => {
      isPlayingRef.current = false;
      setPlaying(false);
      onPlayStateChange?.(false);
      stopTimeReporting();
      const video = videoRef.current;
      if (video && onTimeUpdate) {
        const absoluteMs = nativeSeek
          ? Math.round(video.currentTime * 1000)
          : Math.round((seekOffsetSecRef.current + video.currentTime) * 1000);
        setCurrentMs(absoluteMs);
        onTimeUpdate(absoluteMs);
      }
    }, [onPlayStateChange, stopTimeReporting, onTimeUpdate, nativeSeek]);

    const handleLoadedMetadata = useCallback(() => {
      const video = videoRef.current;
      if (video) {
        if (nativeSeek) {
          setDuration(Math.round(video.duration * 1000));
        }
        // Remux/transcode: duration was already set from /api/editor/info.
        setVideoError(false);
        video.volume = volume;
        // After a stream-key remount the new <video> DOM node mounts at default
        // playbackRate=1.0 even though the UI selector still shows e.g. 1.5x.
        // Reapply alongside volume so the rate survives error retries.
        video.playbackRate = playbackRate;
        // Auto-play if user was playing before the seek (transcode/remux mode only).
        if (autoPlayAfterSeekRef.current) {
          autoPlayAfterSeekRef.current = false;
          video.play().catch(() => {
            /* ignored */
          });
        }
      }
    }, [volume, nativeSeek, playbackRate]);

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
        {/* Video area */}
        <div
          style={videoWrapperStyle}
          tabIndex={0}
          onKeyDown={handleKeyDown}
          role="region"
          aria-label="Video preview"
        >
          <video
            key={streamKey}
            ref={videoRef}
            src={videoSrc}
            style={{ width: "100%", display: "block" }}
            preload="metadata"
            onPlay={handlePlay}
            onPause={handlePause}
            onLoadedMetadata={handleLoadedMetadata}
            onWaiting={() => setBuffering(true)}
            onCanPlay={() => setBuffering(false)}
            onError={handleVideoError}
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

          {directPlay !== null && (
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
                background: directPlay
                  ? "rgba(105, 219, 124, 0.15)"
                  : remuxMode
                    ? "rgba(77, 171, 247, 0.15)"
                    : "rgba(255, 202, 40, 0.15)",
                color: directPlay
                  ? "#69db7c"
                  : remuxMode
                    ? "#4dabf7"
                    : "#ffca28",
                whiteSpace: "nowrap",
              }}
            >
              {directPlay ? "Direct" : remuxMode ? "Remux" : "Transcode"}
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
