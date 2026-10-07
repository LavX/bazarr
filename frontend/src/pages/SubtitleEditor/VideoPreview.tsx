import { renderSubtitleHtml } from "@/utilities/subtitleText";
export { renderSubtitleHtml } from "@/utilities/subtitleText";
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
  faCompress,
  faExpand,
  faForward,
  faPause,
  faPlay,
  faVideoSlash,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import Hls, { type ErrorData } from "hls.js";
import { Environment } from "@/utilities/env";
import { appendArrInstanceParam } from "./editorScope";

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
  arrInstanceId?: number;
  currentTimeMs: number;
  seekId?: number;
  currentSubtitleText?: string;
  onTimeUpdate?: (ms: number) => void;
  onPlayStateChange?: (playing: boolean) => void;
  onAudioTracksLoaded?: (tracks: AudioTrack[]) => void;
  audioTrack?: number;
}

// Native HLS playback (Safari, iOS) cannot send the X-API-KEY header on the
// playlist and segment requests the browser makes itself, so those URLs
// carry a media-scoped stream token instead of the API key. The token is
// minted over a header-authenticated call, renewed while playback runs, and
// is bound to one media item, one audio track and one arr instance.
interface StreamTokenScope {
  mediaType: string;
  mediaId: number;
  arrInstanceId: number | null;
  audioTrack: number;
}

interface StreamTokenRecord {
  value: string;
  lifetime: number;
  mintedAt: number;
  scope: StreamTokenScope;
}

// One mint request is in flight at a time. `adopted` marks a mint that an
// error event claimed: its answer, whatever it is, now belongs to the error
// recovery path.
type StreamTokenMintPurpose =
  | "source"
  | "renewal"
  | "expiry"
  | "probe"
  | "error";

interface StreamTokenMint {
  id: number;
  purpose: StreamTokenMintPurpose;
  scope: StreamTokenScope;
  adopted: boolean;
  controller: AbortController;
}

// A completed recovery, bound to the token its mint produced and to that
// token's scope: only a failure of that very token reads it, so a source
// or track change, or a replacement token for the same scope, starts its
// own recovery budget.
interface StreamTokenRecovery {
  tokenValue: string;
  scope: StreamTokenScope;
  at: number;
}

// A src change resets the video element, so between src and loadedmetadata
// the user's position and play state are held here. The rate is not: the
// load algorithm resets the element's rate, and loadedmetadata re-applies
// the speed the user has chosen by then.
interface NativeRestoreWindow {
  sessionKey: string;
  localSec: number;
  absoluteMs: number;
  playing: boolean;
}

const STREAM_TOKEN_MINT_DEADLINE_MS = 45 * 1000;
const STREAM_TOKEN_RETRY_MS = 10 * 1000;
// After a failed renewal the play, seek and progress rules stay quiet for
// a minute before they ask again.
const STREAM_TOKEN_EVENT_BLOCK_MS = 60 * 1000;
const STREAM_TOKEN_GIVE_UP_MS = 120 * 1000;
// A fresh recovery that fails again within a minute means retrying the same
// mint will keep answering the same way, so the error state shows at once.
const STREAM_TOKEN_RECOVERY_WINDOW_MS = 60 * 1000;
const STREAM_TOKEN_PROBE_INTERVAL_MS = 15 * 1000;
// An old token is renewed on user interaction: a third of its lifetime old,
// and never more eagerly than the 20 minute floor of a full token. A token
// with 15 minutes left is renewed when the rest of the playback, at the
// current rate, plus a 10 minute margin, would outlast it.
const STREAM_TOKEN_RENEWAL_FLOOR_MS = 20 * 60 * 1000;
const STREAM_TOKEN_RENEWAL_THRESHOLD_MS = 15 * 60 * 1000;
const STREAM_TOKEN_RENEWAL_MARGIN_MS = 10 * 60 * 1000;
const STREAM_TOKEN_EXPIRY_MINT_MS = 60 * 1000;
// Bazarr marks its own answers on the token route. A 404 without the marker
// is a reverse proxy answering while Bazarr is down or restarting: a
// transient failure worth retrying, not a refusal.
const TERMINAL_STREAM_TOKEN_STATUSES = [400, 401, 403, 404];

function streamTokenRenewalFloorSec(lifetimeSec: number) {
  return Math.min(STREAM_TOKEN_RENEWAL_FLOOR_MS / 1000, lifetimeSec / 3);
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
      arrInstanceId,
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
    const containerRef = useRef<HTMLDivElement>(null);
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
    const [isFullscreen, setIsFullscreen] = useState(false);

    // HLS session: which audio track ffmpeg is encoding and what source-time
    // offset it began at. Updated atomically (one setState) so the URL never
    // ends up momentarily mismatched between track and start-time. The video
    // element's currentTime is *local* to this session; the user-facing
    // position is `startSec + video.currentTime`.
    //
    // Non-zero sessions are transcoded by the backend so startSec can be the
    // user's exact requested source time without a client-side keyframe seek.
    const [hlsSession, setHlsSession] = useState({
      audioTrack: 0,
      startSec: 0,
    });
    const hlsSessionRef = useRef(hlsSession);
    useEffect(() => {
      hlsSessionRef.current = hlsSession;
    }, [hlsSession]);

    // The scope the media-change reset last saw, so a real change of media
    // or arr instance is told apart from the initial mount.
    const prevScopeRef = useRef<{
      mediaType?: string;
      mediaId?: number;
      arrInstanceId?: number;
    } | null>(null);

    const hasMedia = mediaType != null && mediaId != null;
    const apiKey = Environment.apiKey ?? "";

    // Full source duration, readable from the memoized play/pause handlers.
    // The /info call fills the state; this ref always carries its latest
    // value so a stale closure never renews against an unknown duration.
    const durationRef = useRef(0);
    durationRef.current = duration;

    // Native HLS path: the stream token and its bookkeeping. Everything the
    // async paths touch lives in refs, because mint answers and timers land
    // long after the render that started them.
    const isNativeRef = useRef(false);
    const hlsUrlRef = useRef("");
    const scopeRef = useRef<StreamTokenScope | null>(null);
    const streamTokenRef = useRef<StreamTokenRecord | null>(null);
    const mintPendingRef = useRef<StreamTokenMint | null>(null);
    const mintIdRef = useRef(0);
    const renewalFailedAtRef = useRef<number | null>(null);
    // Two recovery stamps, one per recovery path, each bound to the token
    // its recovery minted. The error handler reads only the error stamp
    // and the probe's 401 check only the probe stamp, and both honor a
    // stamp only when the request failed for the token it recovered: an
    // error recovery is no reason to refuse a probe reset, a probe
    // recovery is no reason to give up on the next media error, and
    // neither is carried across a source or track change or a replacement
    // token, each of which starts its own budget.
    const lastErrorRecoveryRef = useRef<StreamTokenRecovery | null>(null);
    const lastProbeRecoveryRef = useRef<StreamTokenRecovery | null>(null);
    const firstFailedProbeMintRef = useRef<number | null>(null);
    // The probe path's give-up: a timer armed at the first failed probe or
    // expiry mint, cleared together with the budget it belongs to.
    const probeGiveUpTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
      null,
    );
    const errorStateRef = useRef(false);
    const appliedSrcRef = useRef<string | null>(null);
    const appliedSessionRef = useRef<string | null>(null);
    const restoreWindowRef = useRef<NativeRestoreWindow | null>(null);
    const sourceChainRef = useRef<{
      retryTimer: ReturnType<typeof setTimeout> | null;
      giveUpTimer: ReturnType<typeof setTimeout> | null;
    } | null>(null);
    const errorChainRef = useRef<{
      retryTimer: ReturnType<typeof setTimeout> | null;
      hardTimer: ReturnType<typeof setTimeout> | null;
    } | null>(null);
    const probeChainRef = useRef<{
      active: boolean;
      timer: ReturnType<typeof setTimeout> | null;
      generation: number;
      controller: AbortController | null;
    }>({ active: false, timer: null, generation: 0, controller: null });

    const nativeSessionKey = useCallback(
      () =>
        `${hlsSessionRef.current.audioTrack}@${hlsSessionRef.current.startSec}`,
      [],
    );

    const scopeMatches = useCallback(
      (a: StreamTokenScope, b: StreamTokenScope) =>
        a.mediaType === b.mediaType &&
        a.mediaId === b.mediaId &&
        a.arrInstanceId === b.arrInstanceId &&
        a.audioTrack === b.audioTrack,
      [],
    );

    // The user-facing position. While a reset window is open the element has
    // been rewound by the src change, so the saved position is the truth.
    const currentAbsoluteMs = useCallback(() => {
      const win = restoreWindowRef.current;
      if (win) return win.absoluteMs;
      return Math.round(
        (hlsSessionRef.current.startSec +
          (videoRef.current?.currentTime ?? 0)) *
          1000,
      );
    }, []);

    const stopProbeChain = useCallback(() => {
      probeChainRef.current.active = false;
      if (probeChainRef.current.timer) {
        clearTimeout(probeChainRef.current.timer);
        probeChainRef.current.timer = null;
      }
      // Late answers of an aborted or stale probe must not drive anything.
      probeChainRef.current.generation += 1;
      probeChainRef.current.controller?.abort();
      probeChainRef.current.controller = null;
      // The wait this chain belonged to is over: the next one is a new
      // episode with a fresh probe budget.
      firstFailedProbeMintRef.current = null;
      if (probeGiveUpTimerRef.current) {
        clearTimeout(probeGiveUpTimerRef.current);
        probeGiveUpTimerRef.current = null;
      }
    }, []);

    // Retires a pending probe or expiry mint when the wait it belonged to
    // ends with no owner left: an answer that lands after the wait is over
    // must settle nothing, not even a fresh token onto a stopped player.
    // A mint the error path adopted keeps running for its new owner, and so
    // does one a source chain is waiting on: while the chain exists, every
    // pending probe or expiry mint is that one, since none can start
    // beside it.
    const retireUnownedProbeMint = useCallback(() => {
      const pending = mintPendingRef.current;
      if (
        pending &&
        !pending.adopted &&
        !sourceChainRef.current &&
        (pending.purpose === "probe" || pending.purpose === "expiry")
      ) {
        mintPendingRef.current = null;
        pending.controller.abort();
      }
    }, []);

    const clearErrorChain = useCallback(() => {
      const chain = errorChainRef.current;
      if (!chain) return;
      if (chain.retryTimer) clearTimeout(chain.retryTimer);
      if (chain.hardTimer) clearTimeout(chain.hardTimer);
      errorChainRef.current = null;
    }, []);

    const cancelSourceChain = useCallback(() => {
      const chain = sourceChainRef.current;
      if (!chain) return;
      if (chain.retryTimer) clearTimeout(chain.retryTimer);
      if (chain.giveUpTimer) clearTimeout(chain.giveUpTimer);
      sourceChainRef.current = null;
    }, []);

    function showVideoErrorState() {
      errorStateRef.current = true;
      stopProbeChain();
      clearErrorChain();
      cancelSourceChain();
      const pending = mintPendingRef.current;
      if (pending) {
        mintPendingRef.current = null;
        pending.controller.abort();
      }
      setVideoError(true);
    }

    function scheduleSourceRetry() {
      const chain = sourceChainRef.current;
      if (!chain || chain.retryTimer) return;
      chain.retryTimer = setTimeout(() => {
        if (sourceChainRef.current) {
          sourceChainRef.current.retryTimer = null;
        }
        if (errorStateRef.current || !scopeRef.current) return;
        startMint("source", scopeRef.current);
      }, STREAM_TOKEN_RETRY_MS);
    }

    function scheduleErrorRetry() {
      const chain = errorChainRef.current;
      if (!chain || chain.retryTimer) return;
      chain.retryTimer = setTimeout(() => {
        if (errorChainRef.current) {
          errorChainRef.current.retryTimer = null;
        }
        if (errorStateRef.current || !scopeRef.current) return;
        startMint("error", scopeRef.current);
      }, STREAM_TOKEN_RETRY_MS);
    }

    function settleMintSuccess(
      mint: StreamTokenMint,
      data: { token: string; lifetime: number },
    ) {
      // The body is usable only when its lifetime is a finite number: the
      // renewal rules floor and divide by it, and anything else would
      // renew on every event. A body without one is a failed mint, so the
      // retry chain of the path that asked stays intact.
      if (
        typeof data.lifetime !== "number" ||
        !Number.isFinite(data.lifetime)
      ) {
        settleMintFailure(mint, false);
        return;
      }
      mintPendingRef.current = null;
      streamTokenRef.current = {
        value: data.token,
        lifetime: data.lifetime,
        mintedAt: Date.now(),
        scope: mint.scope,
      };
      // The source this mint puts on the air ends every episode of the
      // older source: the error chain stops retrying, and the probe
      // budget of the episode it belonged to is spent.
      clearErrorChain();
      firstFailedProbeMintRef.current = null;
      if (probeGiveUpTimerRef.current) {
        clearTimeout(probeGiveUpTimerRef.current);
        probeGiveUpTimerRef.current = null;
      }
      if (mint.purpose === "error" || mint.adopted) {
        // The source is back after a media error: another error within a
        // minute of now is a fresh recovery failing, and the error state
        // shows instead of another reset. The stamp binds to the token
        // this recovery minted, so only a failure of that token reads it.
        lastErrorRecoveryRef.current = {
          tokenValue: data.token,
          scope: mint.scope,
          at: Date.now(),
        };
      }
      if (mint.purpose === "probe") {
        // The source is back after a refused playlist: a new refusal
        // within a minute of now shows the error state, not another
        // reset, and only for the token minted here.
        lastProbeRecoveryRef.current = {
          tokenValue: data.token,
          scope: mint.scope,
          at: Date.now(),
        };
      }
      applyNativeSource();
    }

    function settleMintFailure(mint: StreamTokenMint, terminal: boolean) {
      if (mintPendingRef.current !== mint) return;
      mintPendingRef.current = null;
      const now = Date.now();
      if (terminal) {
        if (
          mint.purpose === "renewal" &&
          !mint.adopted &&
          !sourceChainRef.current
        ) {
          // A plain refused renewal keeps the current source playing; the
          // event rules wait a minute before asking again. A renewal an
          // error adopted, or one a new source is already waiting on,
          // leaves nothing to play: its refusal shows the error state.
          renewalFailedAtRef.current = now;
        } else {
          showVideoErrorState();
        }
        return;
      }
      if (sourceChainRef.current) {
        // A source is waiting for a token: keep it off the air and retry,
        // giving up two minutes after the request began.
        scheduleSourceRetry();
        return;
      }
      if (mint.purpose === "error" || mint.adopted) {
        scheduleErrorRetry();
        return;
      }
      if (mint.purpose === "probe" || mint.purpose === "expiry") {
        // The wait this mint belonged to has already ended, at a pause, a
        // canplay or an error takeover: its late failure arms nothing, or
        // a fresh timer for a wait that is over would eventually replace
        // the recovered player with the error state.
        if (!probeChainRef.current.active) return;
        // No retry timer: the next 15 second probe answers again. The
        // two-minute budget is a real timer from the first failed mint:
        // a path whose probes answer nothing but 503s cannot wait
        // forever, and the error state shows even when no later mint
        // fails. The timer is cleared with the budget it belongs to, on
        // a stop, a reset or any success, and its expiry aborts whatever
        // attempt is still pending.
        if (firstFailedProbeMintRef.current === null) {
          firstFailedProbeMintRef.current = now;
          probeGiveUpTimerRef.current = setTimeout(() => {
            showVideoErrorState();
          }, STREAM_TOKEN_GIVE_UP_MS);
        }
        return;
      }
      renewalFailedAtRef.current = now;
    }

    function startMint(
      purpose: StreamTokenMintPurpose,
      scope: StreamTokenScope,
    ) {
      if (errorStateRef.current) return;
      // One mint at a time, whatever path asked for it.
      if (mintPendingRef.current) return;
      const mint: StreamTokenMint = {
        id: (mintIdRef.current += 1),
        purpose,
        scope,
        adopted: false,
        controller: new AbortController(),
      };
      mintPendingRef.current = mint;
      // A mint that never answers must not hold every path hostage: at the
      // deadline the request is aborted and treated as a failure. The
      // deadline spans the body parse too, so a stalled stream after the
      // headers cannot hold the guard past it.
      const deadline = setTimeout(() => {
        if (mintPendingRef.current !== mint) return;
        mint.controller.abort();
        settleMintFailure(mint, false);
      }, STREAM_TOKEN_MINT_DEADLINE_MS);
      const url = appendArrInstanceParam(
        `${Environment.baseUrl}/api/editor/stream-token?mediaType=${encodeURIComponent(scope.mediaType)}&mediaId=${scope.mediaId}&audioTrack=${scope.audioTrack}`,
        scope.arrInstanceId ?? undefined,
      );
      fetch(url, {
        headers: { "X-API-KEY": apiKey },
        cache: "no-store",
        signal: mint.controller.signal,
      })
        .then(async (response) => {
          try {
            if (mintPendingRef.current !== mint || errorStateRef.current) {
              return;
            }
            if (!response.ok) {
              const terminal =
                TERMINAL_STREAM_TOKEN_STATUSES.includes(response.status) &&
                response.headers.get("X-Bazarr-Stream-Token-Route") === "1";
              settleMintFailure(mint, terminal);
              return;
            }
            const data = await response.json();
            if (mintPendingRef.current !== mint || errorStateRef.current) {
              return;
            }
            settleMintSuccess(mint, data);
          } finally {
            clearTimeout(deadline);
          }
        })
        .catch(() => {
          clearTimeout(deadline);
          if (mintPendingRef.current !== mint) return;
          settleMintFailure(mint, false);
        });
    }

    function applyNativeSource() {
      const video = videoRef.current;
      const token = streamTokenRef.current;
      if (!video || !token || !isNativeRef.current || !hlsUrlRef.current) {
        return;
      }
      const sessionKey = nativeSessionKey();
      const previous = restoreWindowRef.current;
      if (appliedSessionRef.current === sessionKey && appliedSrcRef.current) {
        // Same session, new token: a browser resets the element on a src
        // change, so the user's position and play state are held until
        // loadedmetadata restores them.
        restoreWindowRef.current = {
          sessionKey,
          localSec: previous ? previous.localSec : video.currentTime,
          absoluteMs: currentAbsoluteMs(),
          playing: previous ? previous.playing : isPlayingRef.current,
        };
      } else {
        // A new session begins at its own start time; nothing to restore.
        restoreWindowRef.current = null;
      }
      const separator = hlsUrlRef.current.includes("?") ? "&" : "?";
      const src = `${hlsUrlRef.current}${separator}st=${encodeURIComponent(token.value)}`;
      video.src = src;
      appliedSrcRef.current = src;
      appliedSessionRef.current = sessionKey;
      // The reset ends the current wait and makes any probe in flight
      // stale: its answer must not mint against the session it left.
      stopProbeChain();
      cancelSourceChain();
    }

    function clearNativeSrc() {
      const video = videoRef.current;
      if (video && appliedSrcRef.current) {
        video.removeAttribute("src");
        video.load();
      }
      appliedSrcRef.current = null;
      appliedSessionRef.current = null;
    }

    // Runs whenever the native source changes: mount, media change, audio
    // switch or a new start-time session. Puts the element on the air only
    // once a token for the current scope is at hand.
    function nativeSourceForSession() {
      const scope = scopeRef.current;
      if (!scope) return;
      // A source change begins here: the probes of the source being
      // replaced are stale from this point, whatever they answer, and the
      // position of the source being replaced stops being the truth as
      // soon as this one needs its own token. The audio-switch effect has
      // read the window by the time it lands here.
      stopProbeChain();
      restoreWindowRef.current = null;
      // A pending mint is settled before any stored token is reused: a
      // young token cannot put this source on the air while a mint is
      // still in flight, and a mint of another scope can never serve it.
      const pending = mintPendingRef.current;
      if (pending && scopeMatches(pending.scope, scope)) {
        // A mint binding this scope is already in flight; its answer will
        // put the new session on the air. The chain of an error-owned
        // mint stays: its failure still drives the error retries, so a
        // pending error mint is never orphaned by the wait.
        if (pending.purpose !== "error" && !pending.adopted) {
          clearErrorChain();
        }
        cancelSourceChain();
        sourceChainRef.current = {
          retryTimer: null,
          giveUpTimer: setTimeout(
            () => showVideoErrorState(),
            STREAM_TOKEN_GIVE_UP_MS,
          ),
        };
        clearNativeSrc();
        return;
      }
      if (pending) {
        // The pending mint binds another scope and can never serve this
        // source: abort it and ignore its late answer.
        mintPendingRef.current = null;
        pending.controller.abort();
      }
      const token = streamTokenRef.current;
      if (
        token &&
        scopeMatches(token.scope, scope) &&
        (Date.now() - token.mintedAt) / 1000 <
          streamTokenRenewalFloorSec(token.lifetime)
      ) {
        // A young token already binds this scope: reuse it.
        clearErrorChain();
        applyNativeSource();
        return;
      }
      clearErrorChain();
      cancelSourceChain();
      clearNativeSrc();
      sourceChainRef.current = {
        retryTimer: null,
        giveUpTimer: setTimeout(
          () => showVideoErrorState(),
          STREAM_TOKEN_GIVE_UP_MS,
        ),
      };
      startMint("source", scope);
    }

    function probeTick() {
      if (!probeChainRef.current.active) return;
      probeChainRef.current.timer = setTimeout(
        probeTick,
        STREAM_TOKEN_PROBE_INTERVAL_MS,
      );
      if (errorStateRef.current || mintPendingRef.current) return;
      const src = appliedSrcRef.current;
      if (!src) return;
      if (errorChainRef.current || sourceChainRef.current) {
        // An error or source change is in flight and will reset the element
        // itself; the probes of the episode it ends are over.
        return;
      }
      const token = streamTokenRef.current;
      if (token) {
        const remainingSec =
          token.lifetime - (Date.now() - token.mintedAt) / 1000;
        if (remainingSec <= STREAM_TOKEN_EXPIRY_MINT_MS / 1000) {
          // The token expires before the next probe is due: mint now, on
          // the waiting path's rules rather than the renewal rules.
          if (scopeRef.current) startMint("expiry", scopeRef.current);
          return;
        }
      }
      probeChainRef.current.generation += 1;
      probeChainRef.current.controller?.abort();
      const generation = probeChainRef.current.generation;
      const controller = new AbortController();
      probeChainRef.current.controller = controller;
      fetch(src, { cache: "no-store", signal: controller.signal })
        .then((response) => {
          if (generation !== probeChainRef.current.generation) return;
          // A 503 or a network error means the server is transiently
          // unreachable: keep waiting, do not error, do not mint.
          if (response.status !== 401) return;
          // An error or source change took over while this probe was in
          // flight: it owns the reset now, and this answer belongs to the
          // episode it ended.
          if (errorChainRef.current || sourceChainRef.current) return;
          const recovery = lastProbeRecoveryRef.current;
          if (
            recovery !== null &&
            token !== null &&
            recovery.tokenValue === token.value &&
            scopeRef.current !== null &&
            scopeMatches(recovery.scope, scopeRef.current) &&
            Date.now() - recovery.at < STREAM_TOKEN_RECOVERY_WINDOW_MS
          ) {
            // The token this fresh recovery minted was refused again within
            // a minute: retrying will answer the same way. A replacement
            // token for the same scope is not that token, and its refusal
            // starts a fresh recovery instead.
            showVideoErrorState();
            return;
          }
          if (scopeRef.current) startMint("probe", scopeRef.current);
        })
        .catch(() => {
          // The probe was aborted or failed to connect; the next one is due
          // in 15 seconds.
        });
    }

    function startProbeChain() {
      if (probeChainRef.current.active) return;
      probeChainRef.current.active = true;
      if (!probeChainRef.current.timer) {
        probeChainRef.current.timer = setTimeout(
          probeTick,
          STREAM_TOKEN_PROBE_INTERVAL_MS,
        );
      }
    }

    // Renewal on playback events. `interactive` covers play and seeking: a
    // user interaction with an old token renews it outright. The progress
    // events renew only when the token cannot cover the rest of the
    // playback, at the current rate, plus the margin.
    function maybeRenewToken(interactive: boolean) {
      if (!isNativeRef.current) return;
      // An error or source chain in flight occupies the guard with it: it
      // will either reset the element or show the error state, and a
      // renewal minted beside it could never be applied anyway.
      if (
        errorStateRef.current ||
        mintPendingRef.current ||
        errorChainRef.current ||
        sourceChainRef.current
      ) {
        return;
      }
      const now = Date.now();
      const failedAt = renewalFailedAtRef.current;
      if (failedAt !== null && now - failedAt < STREAM_TOKEN_EVENT_BLOCK_MS) {
        return;
      }
      const token = streamTokenRef.current;
      const scope = scopeRef.current;
      if (!token || !scope) return;
      const ageSec = (now - token.mintedAt) / 1000;
      // Both rules share the old-token floor: neither of them renews a
      // token still young for its lifetime.
      if (ageSec <= streamTokenRenewalFloorSec(token.lifetime)) return;
      if (interactive) {
        startMint("renewal", scope);
        return;
      }
      const remainingSec = token.lifetime - ageSec;
      if (remainingSec > STREAM_TOKEN_RENEWAL_THRESHOLD_MS / 1000) return;
      if (durationRef.current > 0) {
        const video = videoRef.current;
        const rate = video?.playbackRate || 1;
        const remainingMediaSec =
          durationRef.current / 1000 - currentAbsoluteMs() / 1000;
        const neededSec =
          remainingMediaSec / rate + STREAM_TOKEN_RENEWAL_MARGIN_MS / 1000;
        if (neededSec <= remainingSec) return;
      }
      startMint("renewal", scope);
    }

    // While the element waits, the token is checked against the real server
    // every 15 seconds, because a stalled stream can mean an expired token
    // that the element swallowed as a network stall.
    function handleNativeWaiting() {
      if (!isNativeRef.current || errorStateRef.current) return;
      const token = streamTokenRef.current;
      const chainInFlight =
        mintPendingRef.current ||
        errorChainRef.current ||
        sourceChainRef.current;
      if (
        !chainInFlight &&
        token &&
        scopeRef.current &&
        token.lifetime - (Date.now() - token.mintedAt) / 1000 <=
          STREAM_TOKEN_EXPIRY_MINT_MS / 1000
      ) {
        // The token is about to expire; mint before anything else. The
        // expiry mint answers to the probe rules, not the renewal rules:
        // its refusal counts toward the probe budget, and its success
        // resets the source without opening a recovery window.
        startMint("expiry", scopeRef.current);
      }
      startProbeChain();
    }

    function handleNativeMediaError() {
      if (!isNativeRef.current || errorStateRef.current) return;
      const now = Date.now();
      const recovery = lastErrorRecoveryRef.current;
      const token = streamTokenRef.current;
      if (
        recovery !== null &&
        token !== null &&
        recovery.tokenValue === token.value &&
        scopeRef.current !== null &&
        scopeMatches(recovery.scope, scopeRef.current) &&
        now - recovery.at < STREAM_TOKEN_RECOVERY_WINDOW_MS
      ) {
        // The token this fresh recovery minted is failing again within a
        // minute: retrying will answer the same way. A replacement token
        // for the same scope is not that token, and its error starts a
        // fresh recovery instead.
        showVideoErrorState();
        return;
      }
      if (errorChainRef.current) return;
      errorChainRef.current = {
        retryTimer: null,
        hardTimer: setTimeout(
          () => showVideoErrorState(),
          STREAM_TOKEN_GIVE_UP_MS,
        ),
      };
      // A probe of the stalled source may still be in flight: the error
      // path owns the recovery now, so the probe chain ends here with its
      // budget and its timer, and the error path's own two minutes govern.
      // A pending mint is adopted below and its answer belongs to the
      // error path.
      stopProbeChain();
      const pending = mintPendingRef.current;
      if (pending) {
        // The error path takes over the pending mint: its failure now
        // drives the error retries, and its success is the recovery.
        pending.adopted = true;
        return;
      }
      if (scopeRef.current) startMint("error", scopeRef.current);
    }

    // Nothing may outlive the component: timers stop at unmount.
    useEffect(
      () => () => {
        stopProbeChain();
        retireUnownedProbeMint();
        clearErrorChain();
        cancelSourceChain();
      },
      [
        stopProbeChain,
        retireUnownedProbeMint,
        clearErrorChain,
        cancelSourceChain,
      ],
    );

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
      const infoUrl = appendArrInstanceParam(
        `${Environment.baseUrl}/api/editor/info?mediaType=${encodeURIComponent(mediaType)}&mediaId=${mediaId}`,
        arrInstanceId,
      );
      fetch(infoUrl, { headers: { "X-API-KEY": apiKey } })
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
    }, [
      hasMedia,
      mediaType,
      mediaId,
      arrInstanceId,
      apiKey,
      onAudioTracksLoaded,
    ]);

    // Media-change reset: fresh session at start of file. The initial mount
    // with default props bails out, but a real change of media or arr
    // instance always resets, even from a zero-start session: the old
    // item's playing state and position reports must not survive into the
    // new item, or its first click pauses instead of playing.
    useEffect(() => {
      const prevSession = hlsSessionRef.current;
      const prevScope = prevScopeRef.current;
      prevScopeRef.current = { mediaType, mediaId, arrInstanceId };
      const scopeChanged =
        prevScope !== null &&
        (prevScope.mediaType !== mediaType ||
          prevScope.mediaId !== mediaId ||
          prevScope.arrInstanceId !== arrInstanceId);
      if (
        !scopeChanged &&
        prevSession.audioTrack === audioTrack &&
        prevSession.startSec === 0
      ) {
        return;
      }
      shouldAutoplayAfterAttachRef.current = false;
      // A changed item is a new playback: the old item's playing state and
      // its position reports stop here, so the first click on the new item
      // plays instead of pausing a playback that no longer exists.
      if (isPlayingRef.current) {
        isPlayingRef.current = false;
        setPlaying(false);
        onPlayStateChange?.(false);
      }
      stopTimeReporting();
      // The new item is a new source: the terminal verdict of the old one
      // ends with it, so this source may mint and go on the air again.
      errorStateRef.current = false;
      setVideoError(false);
      setHlsSession({ audioTrack, startSec: 0 });
      // audioTrack is intentionally outside the deps; we only reset on media
      // swap. Audio-track changes are handled by their own effect below so
      // they can capture the current playback position.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [mediaType, mediaId, arrInstanceId]);

    // Audio-track change: spawn a new HLS session that begins at the user's
    // current position so they don't have to wait for ffmpeg to encode from
    // t=0 to where they were.
    useEffect(() => {
      const prev = hlsSessionRef.current;
      if (prev.audioTrack === audioTrack) return;
      const video = videoRef.current;
      // A reset window may be open: the element has been rewound by a src
      // change, so the saved position is where the user actually is.
      const win = restoreWindowRef.current;
      const userTarget = win
        ? win.absoluteMs / 1000
        : prev.startSec + (video?.currentTime ?? 0);
      shouldAutoplayAfterAttachRef.current = isPlayingRef.current;
      // The new track is a new source: the terminal verdict of the old one
      // ends with it, so this source may mint and go on the air again.
      errorStateRef.current = false;
      setVideoError(false);
      setHlsSession({
        audioTrack,
        startSec: userTarget,
      });
    }, [audioTrack]);

    // Identifies this player to the backend, so a new session replaces this
    // player's previous encoder and leaves other tabs on the same file alone.
    const [playerId] = useState(() => Math.random().toString(36).slice(2, 12));

    // HLS playlist URL is path-based so segments resolve correctly via relative
    // URLs in the manifest. startSec is part of the path so segment URLs inherit
    // the same session.
    const scopedHlsUrl = hasMedia
      ? appendArrInstanceParam(
          `${Environment.baseUrl}/api/editor/hls/${encodeURIComponent(mediaType)}/${mediaId}/${hlsSession.audioTrack}/${hlsSession.startSec.toFixed(3)}/playlist.m3u8`,
          arrInstanceId,
        )
      : "";
    const hlsUrl = scopedHlsUrl
      ? `${scopedHlsUrl}${scopedHlsUrl.includes("?") ? "&" : "?"}player=${playerId}`
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
        // fetches, so the playlist URL carries a media-scoped stream token
        // instead of the API key. The source goes on the air only once a
        // token minted for this exact media, track and instance is at hand.
        isNativeRef.current = true;
        hlsUrlRef.current = hlsUrl;
        scopeRef.current = {
          mediaType: mediaType as string,
          mediaId: mediaId as number,
          arrInstanceId: arrInstanceId ?? null,
          audioTrack: hlsSessionRef.current.audioTrack,
        };
        nativeSourceForSession();
      } else {
        setVideoError(true);
      }

      return () => {
        hls?.destroy();
        if (video) {
          video.removeAttribute("src");
          video.load();
        }
        appliedSrcRef.current = null;
        appliedSessionRef.current = null;
      };
      // nativeSourceForSession reads only refs and the scope values above,
      // which all change together with hlsUrl; listing the function itself
      // would re-run the effect every render.
      // The latch clear is a dep for the same reason: a terminal error
      // unmounted the element this effect last attached, and the remount
      // lands in the commit after the media change, where a zero-start
      // session reset leaves the URL unchanged. Only the clear re-runs
      // the attach the fresh element needs, and the URL never will.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [hlsUrl, apiKey, mediaType, mediaId, arrInstanceId, videoError]);

    // Time reporting: video.currentTime is local to the current HLS session;
    // the user-facing absolute position is startSec + local, except while a
    // reset window is open, when the saved position is the truth.
    const startTimeReporting = useCallback(() => {
      if (timerRef.current) return;
      timerRef.current = setInterval(() => {
        const video = videoRef.current;
        if (!video) return;
        const absoluteMs = currentAbsoluteMs();
        setCurrentMs(absoluteMs);
        if (
          onTimeUpdate &&
          Math.abs(absoluteMs - lastReportedMsRef.current) >= 50
        ) {
          lastReportedMsRef.current = absoluteMs;
          onTimeUpdate(absoluteMs);
        }
      }, 100);
    }, [onTimeUpdate, currentAbsoluteMs]);

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
        setCurrentMs(currentTimeMs);
        setHlsSession((prev) => ({
          ...prev,
          startSec: targetAbsoluteSec,
        }));
      } else {
        // A same-source seek lands on its target: the open window moves to
        // the target even when the reset element already sits there, so a
        // pending restore lands on the target and not on the position the
        // window was opened at. A window of a different session is stale
        // and stays dropped.
        const win = restoreWindowRef.current;
        if (win && win.sessionKey === nativeSessionKey()) {
          win.localSec = localTargetSec;
          win.absoluteMs = currentTimeMs;
        }
        if (Math.abs(localTargetSec - video.currentTime) > 0.25) {
          video.currentTime = localTargetSec;
        }
        setCurrentMs(currentTimeMs);
      }
    }, [currentTimeMs, seekId, hasMedia, nativeSessionKey]);

    // Sync volume and playback rate
    useEffect(() => {
      const video = videoRef.current;
      if (video) {
        video.volume = volume;
        video.playbackRate = playbackRate;
      }
    }, [volume, playbackRate]);

    // The latest handlePause, reachable from the stable togglePlayPause.
    // handlePause closes over the latest onTimeUpdate, so the button must
    // always find the current one.
    const handlePauseRef = useRef<(() => void) | null>(null);

    const togglePlayPause = useCallback(() => {
      const video = videoRef.current;
      if (!video) return;
      // The component's own flag decides, not video.paused: a play() call
      // whose play event has not landed yet (autoplay permission, native
      // HLS attach) must not read as paused.
      if (isPlayingRef.current) {
        video.pause();
        // The user's pause outranks a source change that asked for
        // autoplay: if a new source is still waiting on its token mint,
        // the loadedmetadata that lands once it arrives must not start
        // playback on its own. Cleared here and not in handlePause: the
        // element can fire pause during a src reset without the user
        // meaning it.
        shouldAutoplayAfterAttachRef.current = false;
        // The browser queues the pause event; the state moves now, and the
        // event handler is a no-op when the flag already says paused.
        handlePauseRef.current?.();
      } else {
        video.play().catch(() => {
          /* ignored */
        });
      }
    }, []);

    // Fullscreen the whole player container (video + subtitle overlay +
    // controls) so the rendered subtitle preview and the transport stay
    // visible, not just the bare <video> element. Falls back to the webkit-
    // prefixed API for Safari.
    const toggleFullscreen = useCallback(() => {
      const el = containerRef.current;
      if (!el) return;
      const doc = document as Document & {
        webkitFullscreenElement?: Element | null;
        webkitExitFullscreen?: () => void;
      };
      const fsEl = document.fullscreenElement ?? doc.webkitFullscreenElement;
      if (!fsEl) {
        const node = el as HTMLDivElement & {
          webkitRequestFullscreen?: () => void;
        };
        (node.requestFullscreen ?? node.webkitRequestFullscreen)?.call(node);
      } else {
        (document.exitFullscreen ?? doc.webkitExitFullscreen)?.call(document);
      }
    }, []);

    // Keep the flag in sync with the browser so Esc (or the OS chrome) updates
    // the button and the fullscreen-only styles.
    useEffect(() => {
      const onChange = () => {
        const doc = document as Document & {
          webkitFullscreenElement?: Element | null;
        };
        setIsFullscreen(
          Boolean(document.fullscreenElement ?? doc.webkitFullscreenElement),
        );
      };
      document.addEventListener("fullscreenchange", onChange);
      document.addEventListener("webkitfullscreenchange", onChange);
      return () => {
        document.removeEventListener("fullscreenchange", onChange);
        document.removeEventListener("webkitfullscreenchange", onChange);
      };
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
          setCurrentMs(clampedMs);
          onTimeUpdate?.(clampedMs);
          setHlsSession((prev) => ({
            ...prev,
            startSec: targetAbsoluteSec,
          }));
        } else {
          video.currentTime = localTargetSec;
          // A same-source seek lands on its target: the open window moves
          // to the target with it, keeping the rate and play state it
          // captured, so the pending restore still lands on the target.
          // A window of a different session is stale and stays dropped.
          const win = restoreWindowRef.current;
          if (win && win.sessionKey === nativeSessionKey()) {
            win.localSec = localTargetSec;
            win.absoluteMs = clampedMs;
          }
          setCurrentMs(clampedMs);
          onTimeUpdate?.(clampedMs);
        }
      },
      [duration, onTimeUpdate, nativeSessionKey],
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
      const win = restoreWindowRef.current;
      if (win) win.playing = true;
      setPlaying(true);
      onPlayStateChange?.(true);
      startTimeReporting();
      maybeRenewToken(true);
      // maybeRenewToken reads only refs, so the memoized handler may hold
      // the render it was created in.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [onPlayStateChange, startTimeReporting]);

    const handlePause = useCallback(() => {
      // The wait ends on any pause, even one the element fires while already
      // paused (a src reset can do that in the browser).
      stopProbeChain();
      retireUnownedProbeMint();
      // The pause event and the pause button both land here; only the first
      // of them moves the state.
      if (!isPlayingRef.current) return;
      isPlayingRef.current = false;
      const win = restoreWindowRef.current;
      if (win) win.playing = false;
      setPlaying(false);
      onPlayStateChange?.(false);
      stopTimeReporting();
      if (onTimeUpdate) {
        const absoluteMs = currentAbsoluteMs();
        setCurrentMs(absoluteMs);
        onTimeUpdate(absoluteMs);
      }
    }, [
      onPlayStateChange,
      stopTimeReporting,
      onTimeUpdate,
      stopProbeChain,
      retireUnownedProbeMint,
      currentAbsoluteMs,
    ]);

    // togglePlayPause is stable, so it must always reach this latest
    // handlePause and the onTimeUpdate it closes over.
    useEffect(() => {
      handlePauseRef.current = handlePause;
    }, [handlePause]);

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
      // A native token swap within one session left the element reset: put
      // the user back where they were, playing if they were playing. The
      // playbackRate assignment above already holds the speed the user has
      // chosen by now. A window left over from a session that no longer
      // exists restores nothing.
      const win = restoreWindowRef.current;
      restoreWindowRef.current = null;
      if (win && win.sessionKey === nativeSessionKey()) {
        if (Number.isFinite(win.localSec)) video.currentTime = win.localSec;
        if (win.playing || shouldAutoplayAfterAttachRef.current) {
          shouldAutoplayAfterAttachRef.current = false;
          video.play().catch(() => {
            /* user-gesture restrictions or transient: ignored */
          });
        }
        return;
      }
      // Resume playback after a session change (track switch or seek-before-
      // start) if the user was playing prior to the tear-down. hls.js attach
      // resets the video to paused, so we explicitly call play() here.
      if (shouldAutoplayAfterAttachRef.current) {
        shouldAutoplayAfterAttachRef.current = false;
        video.play().catch(() => {
          /* user-gesture restrictions or transient: ignored */
        });
      }
    }, [duration, aspectRatio, volume, playbackRate, nativeSessionKey]);

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
        if (e.key === "f" || e.key === "F") {
          e.preventDefault();
          toggleFullscreen();
        }
      },
      [togglePlayPause, seekRelative, toggleFullscreen],
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
      <div
        ref={containerRef}
        style={
          isFullscreen
            ? { ...containerStyle, height: "100%", background: "#000" }
            : containerStyle
        }
      >
        {/* Video area. aspectRatio is pinned (when known) so the wrapper
            doesn't collapse to zero height while hls.js is attaching, which
            avoids layout shift on first load and on every track switch. In
            fullscreen the wrapper grows to fill the remaining height and the
            video letterboxes inside it. */}
        <div
          style={{
            position: "relative",
            width: "100%",
            background: "#000",
            lineHeight: 0,
            ...(isFullscreen ? { flex: 1, minHeight: 0 } : { aspectRatio }),
          }}
          tabIndex={0}
          onKeyDown={handleKeyDown}
          role="region"
          aria-label="Video preview"
        >
          <video
            ref={videoRef}
            style={{
              width: "100%",
              height: "100%",
              display: "block",
              ...(isFullscreen ? { objectFit: "contain" as const } : null),
            }}
            preload="metadata"
            onPlay={handlePlay}
            onPause={handlePause}
            onLoadedMetadata={handleLoadedMetadata}
            onWaiting={() => {
              setBuffering(true);
              handleNativeWaiting();
            }}
            onCanPlay={() => {
              setBuffering(false);
              // The stream moves again; the wait is over, and a probe or
              // expiry mint still in flight settles nothing.
              stopProbeChain();
              retireUnownedProbeMint();
            }}
            onSeeking={() => maybeRenewToken(true)}
            onTimeUpdate={() => maybeRenewToken(false)}
            onRateChange={() => maybeRenewToken(false)}
            onError={handleNativeMediaError}
          />

          {/* Loading spinner */}
          {buffering && (
            <div style={spinnerStyle}>
              <FontAwesomeIcon icon={faPlay} spin />
            </div>
          )}

          {/* Subtitle overlay. Scaled up in fullscreen so it stays readable on
              a large screen. */}
          {currentSubtitleText && (
            <div
              style={
                isFullscreen
                  ? {
                      ...subtitleOverlayStyle,
                      fontSize: 28,
                      bottom: 40,
                      left: 24,
                      right: 24,
                    }
                  : subtitleOverlayStyle
              }
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

          <button
            type="button"
            style={playBtnStyle}
            onClick={toggleFullscreen}
            aria-label={isFullscreen ? "Exit fullscreen" : "Fullscreen"}
            title={isFullscreen ? "Exit fullscreen (f)" : "Fullscreen (f)"}
          >
            <FontAwesomeIcon icon={isFullscreen ? faCompress : faExpand} />
          </button>
        </div>
      </div>
    );
  },
);

export default VideoPreview;
