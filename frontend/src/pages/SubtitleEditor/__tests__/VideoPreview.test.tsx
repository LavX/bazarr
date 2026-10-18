import { ComponentProps } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import VideoPreview from "@/pages/SubtitleEditor/VideoPreview";

// A native player (Safari, iOS) cannot add headers to the playlist and
// segment requests the browser makes itself, so the native path plays from
// a media-scoped stream token minted over a header-authenticated call. These
// tests force the native path by turning hls.js support off everywhere.
vi.mock("hls.js", () => ({
  default: { isSupported: () => false },
}));

const API_KEY = "editor key/+&=";
const DEFAULT_LIFETIME = 3600;
const MINUTE = 60 * 1000;

interface FetchRecord {
  url: URL;
  init?: RequestInit;
}

let fetchLog: FetchRecord[];
let tokenSeq: number;
let infoDuration: number | undefined;
let defaultLifetime: number;
let defaultMintFailureStatus: number | null;
let defaultMintHangs: boolean;
let playlistStatus: number;
let playlistNetworkError: boolean;
let holdPlaylistResponses: boolean;
let fetchIgnoresAbort: boolean;
let heldPlaylistResolves: Array<(response: Response) => void>;
let mintResponders: Array<(call: FetchRecord) => Response | Promise<Response>>;

function tokenBody(lifetime: number) {
  tokenSeq += 1;
  return { token: `st-token-${tokenSeq}`, lifetime };
}

function jsonResponse(
  body: unknown,
  status = 200,
  headers: Record<string, string> = {},
) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function playlistResponse(status: number) {
  return new Response("#EXTM3U\n", { status });
}

function defaultRoute(url: string): Response | Promise<Response> {
  const parsed = new URL(url, "http://bazarr.test");
  if (parsed.pathname === "/api/editor/info") {
    return jsonResponse({
      duration: infoDuration,
      videoCodec: "h264",
      audioCodec: "aac",
      resolution: "1920x1080",
      container: "mkv",
      audioTracks: [],
    });
  }
  if (parsed.pathname === "/api/editor/stream-token") {
    const respond = mintResponders.shift();
    if (respond) {
      return respond({ url: parsed, init: undefined });
    }
    if (defaultMintHangs) {
      return new Promise<Response>(() => {});
    }
    if (defaultMintFailureStatus !== null) {
      return jsonResponse({ message: "upstream" }, defaultMintFailureStatus);
    }
    return jsonResponse(tokenBody(defaultLifetime));
  }
  if (parsed.pathname.endsWith("/playlist.m3u8")) {
    if (playlistNetworkError) {
      return Promise.reject(new TypeError("network error"));
    }
    if (holdPlaylistResponses) {
      return new Promise<Response>((resolve) => {
        heldPlaylistResolves.push(resolve);
      });
    }
    return playlistResponse(playlistStatus);
  }
  return jsonResponse({}, 404);
}

function mintCalls(): FetchRecord[] {
  return fetchLog.filter(
    (call) => call.url.pathname === "/api/editor/stream-token",
  );
}

function probeCalls(): FetchRecord[] {
  return fetchLog.filter((call) =>
    call.url.pathname.endsWith("/playlist.m3u8"),
  );
}

/** A mint answer that stays pending until the test settles it. */
function pendingMint() {
  let resolve!: (response: Response) => void;
  const deferred = new Promise<Response>((res) => {
    resolve = res;
  });
  mintResponders.push(() => deferred);
  return {
    resolveToken(lifetime = DEFAULT_LIFETIME) {
      resolve(jsonResponse(tokenBody(lifetime)));
    },
    resolveRefused(status: number, marker: boolean) {
      resolve(
        new Response(JSON.stringify({ message: "refused" }), {
          status,
          headers: marker ? { "X-Bazarr-Stream-Token-Route": "1" } : {},
        }),
      );
    },
  };
}

function refusedMint(status: number, marker: boolean) {
  mintResponders.push(
    () =>
      new Response(JSON.stringify({ message: "refused" }), {
        status,
        headers: marker ? { "X-Bazarr-Stream-Token-Route": "1" } : {},
      }),
  );
}

function failedMint(status: number) {
  mintResponders.push(() => jsonResponse({ message: "upstream" }, status));
}

function neverSettlingMint() {
  mintResponders.push(() => new Promise<Response>(() => {}));
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("Bazarr", { apiKey: API_KEY, baseUrl: "" });
  vi.spyOn(HTMLMediaElement.prototype, "canPlayType").mockReturnValue("maybe");
  fetchLog = [];
  tokenSeq = 0;
  infoDuration = undefined;
  defaultLifetime = DEFAULT_LIFETIME;
  defaultMintFailureStatus = null;
  defaultMintHangs = false;
  playlistStatus = 200;
  playlistNetworkError = false;
  holdPlaylistResponses = false;
  fetchIgnoresAbort = false;
  heldPlaylistResolves = [];
  mintResponders = [];
  vi.stubGlobal("fetch", (url: RequestInfo | URL, init?: RequestInit) => {
    const urlText = typeof url === "string" ? url : url.toString();
    fetchLog.push({ url: new URL(urlText, "http://bazarr.test"), init });
    return new Promise<Response>((resolve, reject) => {
      Promise.resolve(defaultRoute(urlText)).then(resolve, reject);
      if (!fetchIgnoresAbort) {
        (init?.signal as AbortSignal | undefined)?.addEventListener(
          "abort",
          () => {
            reject(new TypeError("aborted"));
          },
        );
      }
    });
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const BASE_PROPS = {
  mediaType: "episode",
  mediaId: 7,
  arrInstanceId: 2,
  currentTimeMs: 0,
};

type PreviewProps = ComponentProps<typeof VideoPreview>;

function mountPreview(props: Partial<PreviewProps> = {}) {
  const view = render(<VideoPreview {...BASE_PROPS} {...props} />);
  // The video element is the player's own root concern; no role or text
  // query reaches it, so the container is read directly.
  // eslint-disable-next-line testing-library/no-container, testing-library/no-node-access
  const video = view.container.querySelector("video") as HTMLMediaElement;
  vi.spyOn(video, "play").mockResolvedValue(undefined);
  vi.spyOn(video, "pause").mockImplementation(() => undefined);
  return { ...view, video };
}

async function flush(ticks = 8) {
  for (let i = 0; i < ticks; i += 1) {
    // Not empty to the rule: each round flushes the React updates and
    // fetch answers the previous one produced.
    // eslint-disable-next-line testing-library/no-unnecessary-act
    await act(async () => {});
  }
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function emit(video: HTMLMediaElement, type: string) {
  act(() => {
    video.dispatchEvent(new Event(type));
  });
}

/** Mount, let the first token mint land, and reach loadedmetadata. */
async function mounted() {
  const view = mountPreview();
  await flush();
  emit(view.video, "loadedmetadata");
  await flush();
  return view;
}

/** Mount, then move to a start-time session beginning at startMs. */
async function mountedAtSessionStart(
  startMs: number,
  props: Partial<PreviewProps> = {},
) {
  const view = mountPreview();
  await flush();
  view.rerender(
    <VideoPreview
      {...BASE_PROPS}
      {...props}
      currentTimeMs={startMs}
      seekId={1}
    />,
  );
  await flush();
  emit(view.video, "loadedmetadata");
  await flush();
  return view;
}

function seekBar(container: HTMLElement) {
  return container.querySelector(
    'input[type="range"][aria-label="Seek"]',
  ) as HTMLInputElement;
}

function expectErrorState(container: HTMLElement) {
  expect(screen.getByText("Video unavailable")).toBeTruthy();
  // eslint-disable-next-line testing-library/no-node-access
  expect(container.querySelector("video")).toBeNull();
}

function expectNoErrorState(container: HTMLElement) {
  expect(screen.queryByText("Video unavailable")).toBeNull();
  // eslint-disable-next-line testing-library/no-node-access
  expect(container.querySelector("video")).not.toBeNull();
}

/** The element a source change mounted after the error placeholder. */
function freshVideo(container: HTMLElement) {
  return container.querySelector("video") as HTMLMediaElement;
}

describe("the native HLS source", () => {
  it("mints a stream token with the header and plays from st, never the API key", async () => {
    const view = mountPreview();
    await flush();

    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toBe(
      "/api/editor/hls/episode/7/0/0.000/playlist.m3u8",
    );
    expect(src.searchParams.get("st")).toBe("st-token-1");
    expect(src.searchParams.get("player")).not.toBeNull();
    expect(src.searchParams.get("arr_instance_id")).toBe("2");
    expect(src.searchParams.has("apikey")).toBe(false);

    const [mint] = mintCalls();
    expect(mint.url.pathname).toBe("/api/editor/stream-token");
    expect(mint.url.searchParams.get("mediaType")).toBe("episode");
    expect(mint.url.searchParams.get("mediaId")).toBe("7");
    expect(mint.url.searchParams.get("audioTrack")).toBe("0");
    expect(mint.url.searchParams.get("arr_instance_id")).toBe("2");
    expect(mint.url.searchParams.has("apikey")).toBe(false);
    expect((mint.init?.headers as Record<string, string>)["X-API-KEY"]).toBe(
      API_KEY,
    );
  });

  it("does not set the native source until the token has answered", async () => {
    pendingMint();
    const view = mountPreview();
    await flush();
    expect(view.video.getAttribute("src")).toBeNull();
  });
});

describe("native token renewal", () => {
  it("renews on play with a token older than the floor, restoring position, rate and play state", async () => {
    const view = await mounted();
    view.video.currentTime = 30;
    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "0.5" },
    });

    await advance(21 * MINUTE);
    emit(view.video, "play");
    await flush();

    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st=st-token-2");

    // A browser resets currentTime when src changes; the window between src
    // and loadedmetadata holds the saved position.
    view.video.currentTime = 0;
    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.currentTime).toBe(30);
    expect(view.video.playbackRate).toBe(0.5);
    expect(view.video.play).toHaveBeenCalled();
  });

  it("ends at the speed picked during a pending reset, not the saved one", async () => {
    const view = await mounted();
    view.video.currentTime = 30;
    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "0.5" },
    });
    await advance(21 * MINUTE);
    emit(view.video, "play"); // the renewal resets the source
    await flush();
    expect(view.video.src).toContain("st=st-token-2");

    // 1x is picked after the reset began, while loadedmetadata is still
    // pending: the user's current choice wins over the rate the window
    // was opened at.
    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "1" },
    });
    await flush();

    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.playbackRate).toBe(1);
    expect(view.video.currentTime).toBe(30);
  });

  it("leaves a token younger than the floor alone on play", async () => {
    const view = await mounted();
    await advance(10 * MINUTE);
    emit(view.video, "play");
    await flush();
    expect(mintCalls()).toHaveLength(1);
    expect(view.video.src).toContain("st=st-token-1");
  });

  it("renews on seeking with an old token", async () => {
    const view = await mounted();
    await advance(21 * MINUTE);
    emit(view.video, "seeking");
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("renews on timeupdate when the token cannot cover the rest of a slow playback", async () => {
    infoDuration = 2700; // a 45 minute episode
    defaultLifetime = 4500; // a 75 minute token
    const view = await mounted();
    // 61 minutes old: 14 minutes left. At 0.25x the remaining 35 minutes of
    // media need 150 wall minutes.
    view.video.currentTime = 600;
    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "0.25" },
    });
    await advance(61 * MINUTE);

    emit(view.video, "timeupdate");
    await flush();

    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("leaves a token with more than 15 minutes left alone", async () => {
    infoDuration = 2700;
    defaultLifetime = 4500;
    const view = await mounted();
    view.video.currentTime = 600;
    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "0.25" },
    });
    await advance(25 * MINUTE); // 50 minutes left

    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(1);
  });

  it("leaves a token that outlasts the rest of a fast playback alone", async () => {
    infoDuration = 2700;
    defaultLifetime = 4500;
    const view = await mounted();
    view.video.currentTime = 2400; // 5 minutes left at 2x: 12.5 minutes needed
    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "2" },
    });
    await advance(61 * MINUTE); // 14 minutes left

    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(1);
  });

  it("applies the same remaining-life rule on ratechange", async () => {
    infoDuration = 2700;
    defaultLifetime = 4500;
    const view = await mounted();
    view.video.currentTime = 600;
    await advance(61 * MINUTE);

    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "0.25" },
    });
    emit(view.video, "ratechange");
    await flush();
    expect(mintCalls()).toHaveLength(2);
  });

  it("with no duration from /info renews only on the 15 minute threshold", async () => {
    defaultLifetime = 5000;
    const view = await mounted();
    view.video.currentTime = 16200; // 4h30
    await advance(1400 * 1000); // age 23.3 minutes, 1 hour left

    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(1);

    await advance(2760 * 1000); // age 69.3 minutes, 14 minutes left
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(2);
  });

  it("scales the old-token floor to a third of a shortened lifetime", async () => {
    const view = await mounted();
    mintResponders.push(() => jsonResponse(tokenBody(360)));
    await advance(21 * MINUTE);
    emit(view.video, "play");
    await flush(); // renews into the 360 second token
    expect(mintCalls()).toHaveLength(2);
    emit(view.video, "pause");

    await advance(2.5 * MINUTE); // older than the 2 minute scaled floor
    emit(view.video, "play");
    await flush();
    expect(mintCalls()).toHaveLength(3);
  });

  it("leaves a token under the scaled floor alone even when the remaining-life rule would renew", async () => {
    infoDuration = 2700; // a 45 minute episode
    defaultLifetime = 900; // a 15 minute token: the floor is a third, 5 minutes
    const view = await mounted();
    view.video.currentTime = 600;
    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "0.25" },
    });

    await advance(4 * MINUTE); // under the 5 minute floor, 11 minutes left
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(1);

    await advance(2 * MINUTE); // past the floor, 9 minutes left
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(2);
  });

  it("renews twice through a full 180 minute 0.25x playback without an expired-token request", async () => {
    infoDuration = 2700; // 45 minutes of media: 180 wall minutes at 0.25x
    defaultLifetime = 4500; // 75 minute tokens
    const view = await mounted();
    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "0.25" },
    });

    // Every token's mint time, read from the src the element carries: with
    // the lifetime fixed at 75 minutes, the test can prove at every step
    // that the applied token is still inside its own life.
    const mintedAt = new Map<string, number>();
    const appliedToken = () =>
      new URL(view.video.src, "http://bazarr.test").searchParams.get("st") ??
      "";
    mintedAt.set(appliedToken(), Date.now());

    // A timeupdate every 15 wall seconds from 0 to 180 minutes, with the
    // media position advancing at 0.25x: a full quarter-speed playback,
    // driven by the event the player reports on.
    const renewals: number[] = [];
    let restoredToken = appliedToken();
    for (let step = 0; step < 720; step += 1) {
      emit(view.video, "timeupdate");
      await advance(15 * 1000);
      await flush(1);
      view.video.currentTime += 3.75; // 15 wall seconds at 0.25x
      if (mintCalls().length > renewals.length + 1) {
        renewals.push(step * 15);
      }
      const token = appliedToken();
      if (token !== restoredToken) {
        // A renewal reset the element and opened the window holding the
        // position. The metadata the browser loads afterwards restores
        // it, as in a real playback, so no window stays open and hides
        // where the playback stands.
        restoredToken = token;
        mintedAt.set(token, Date.now());
        emit(view.video, "loadedmetadata");
        await flush();
      }
      // The token the element actually plays is checked at every step:
      // holding a renewed token back in memory would leave the element on
      // the old one, and the old one past its expiry fails here.
      const minted = mintedAt.get(token) as number;
      expect(Date.now() - minted).toBeLessThan(defaultLifetime * 1000);
    }
    // The first token is renewed at 60 minutes, the second at 120. Each
    // renewal lands with 15 minutes of its predecessor to live, and the
    // per-step check above held: no step of the playback ran on a token
    // past its expiry.
    expect(renewals).toEqual([3600, 7200]);
    expect(mintCalls()).toHaveLength(3);
    expect(probeCalls()).toHaveLength(0);
    expectNoErrorState(view.container);
    expect(new URL(view.video.src, "http://bazarr.test").pathname).toContain(
      "/0/0.000/playlist.m3u8",
    );
    expect(view.video.src).toContain("st=st-token-3");
  });
});

describe("the reset window between src and loadedmetadata", () => {
  it("restores the saved position after a renewal in a start-time session", async () => {
    const onTimeUpdate = vi.fn();
    const view = await mountedAtSessionStart(600000, { onTimeUpdate });
    view.video.currentTime = 30;
    await advance(21 * MINUTE);
    onTimeUpdate.mockClear();
    emit(view.video, "play");
    await flush();
    expect(view.video.src).toContain("st=st-token-2");

    view.video.currentTime = 0; // as a browser does on a src change
    // Every report through the window reads the spec's 10:30, the saved
    // position, never the session start or the element's reset zero.
    await advance(300);
    const reported = onTimeUpdate.mock.calls.map(([ms]) => ms);
    expect(reported.length).toBeGreaterThan(0);
    for (const ms of reported) {
      expect(ms).toBe(630000);
    }

    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.currentTime).toBe(30);
    expect(view.video.play).toHaveBeenCalled();
  });

  it("reports the saved position throughout the window and on a pause inside it", async () => {
    const onTimeUpdate = vi.fn();
    const view = await mountedAtSessionStart(600000);
    view.rerender(
      <VideoPreview
        {...BASE_PROPS}
        currentTimeMs={600000}
        seekId={1}
        onTimeUpdate={onTimeUpdate}
      />,
    );
    view.video.currentTime = 30;
    await advance(21 * MINUTE);
    emit(view.video, "play");
    await flush();
    expect(view.video.src).toContain("st=st-token-2");
    view.video.currentTime = 0;

    await advance(300);
    // Every report through the window reads the saved 10 minutes 30
    // seconds, never the session start.
    const reported = onTimeUpdate.mock.calls.map(([ms]) => ms);
    expect(reported.length).toBeGreaterThan(0);
    for (const ms of reported) {
      expect(ms).toBe(630000);
    }

    fireEvent.click(screen.getByTitle("Pause"));
    expect(onTimeUpdate).toHaveBeenLastCalledWith(630000);
    expect(screen.getByTitle("Play")).toBeTruthy();

    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.currentTime).toBe(30);
    expect(view.video.play).not.toHaveBeenCalled();
  });

  it("carries an audio switch in the window at the saved position, playing", async () => {
    const view = await mountedAtSessionStart(600000);
    view.video.currentTime = 30;
    await advance(21 * MINUTE);
    emit(view.video, "play");
    await flush();
    view.video.currentTime = 0;

    view.rerender(
      <VideoPreview
        {...BASE_PROPS}
        audioTrack={1}
        currentTimeMs={600000}
        seekId={1}
      />,
    );
    await flush();
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/1/630.000/playlist.m3u8");
    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.play).toHaveBeenCalled();
    expect(view.video.currentTime).toBe(0);
  });

  it("starts a changed media item at its start, paused, and the old restore does not run", async () => {
    const view = await mountedAtSessionStart(600000);
    view.video.currentTime = 30;
    await advance(21 * MINUTE);
    emit(view.video, "play");
    await flush();
    view.video.currentTime = 0;

    view.rerender(
      <VideoPreview
        {...BASE_PROPS}
        mediaId={8}
        currentTimeMs={600000}
        seekId={1}
      />,
    );
    await flush();
    emit(view.video, "loadedmetadata");
    await flush();

    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/episode/8/0/0.000/playlist.m3u8");
    expect(view.video.currentTime).toBe(0);
    expect(view.video.play).not.toHaveBeenCalled();
  });

  it("resets the playing state of a changed item so the first click plays, and carries the new instance", async () => {
    const onPlayStateChange = vi.fn();
    const view = await mountedAtSessionStart(600000);
    view.video.currentTime = 30;
    emit(view.video, "play");
    await flush();
    expect(screen.getByTitle("Pause")).toBeTruthy();

    view.rerender(
      <VideoPreview
        {...BASE_PROPS}
        mediaId={8}
        arrInstanceId={3}
        onPlayStateChange={onPlayStateChange}
      />,
    );
    await flush();
    emit(view.video, "loadedmetadata");
    await flush();

    // The controls report the new item paused, the parent is told, and the
    // mint carries the new instance's scope.
    expect(screen.getByTitle("Play")).toBeTruthy();
    expect(onPlayStateChange).toHaveBeenLastCalledWith(false);
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/episode/8/0/0.000/playlist.m3u8");
    expect(src.searchParams.get("arr_instance_id")).toBe("3");
    expect(view.video.play).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTitle("Play"));
    await flush();
    expect(view.video.play).toHaveBeenCalled();
  });

  it("plays on the first click after a media change from a zero-start session", async () => {
    const view = await mounted();
    emit(view.video, "play");
    await flush();
    expect(screen.getByTitle("Pause")).toBeTruthy();

    // The old session began at zero on the same track, which used to skip
    // the reset: the old item's state must not survive even so.
    view.rerender(<VideoPreview {...BASE_PROPS} mediaId={8} />);
    await flush();
    emit(view.video, "loadedmetadata");
    await flush();

    expect(screen.getByTitle("Play")).toBeTruthy();
    fireEvent.click(screen.getByTitle("Play"));
    await flush();
    expect(view.video.play).toHaveBeenCalled();
  });

  it("plays on the first click after an arr instance change from a zero-start session", async () => {
    const onPlayStateChange = vi.fn();
    const view = await mounted();
    emit(view.video, "play");
    await flush();
    expect(screen.getByTitle("Pause")).toBeTruthy();

    view.rerender(
      <VideoPreview
        {...BASE_PROPS}
        arrInstanceId={3}
        onPlayStateChange={onPlayStateChange}
      />,
    );
    await flush();
    emit(view.video, "loadedmetadata");
    await flush();

    expect(screen.getByTitle("Play")).toBeTruthy();
    expect(onPlayStateChange).toHaveBeenLastCalledWith(false);
    fireEvent.click(screen.getByTitle("Play"));
    await flush();
    expect(view.video.play).toHaveBeenCalled();
  });

  it("lands a same-source seek in the window on the seek target", async () => {
    infoDuration = 2700;
    const view = await mountedAtSessionStart(600000);
    view.video.currentTime = 30;
    await advance(21 * MINUTE);
    emit(view.video, "seeking");
    await flush();
    view.video.currentTime = 0;

    fireEvent.change(seekBar(view.container), { target: { value: "605000" } });
    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.currentTime).toBe(5);
  });

  it("keeps an open window's rate and play state through a same-source seek, reporting the target", async () => {
    infoDuration = 2700;
    const onTimeUpdate = vi.fn();
    const view = await mountedAtSessionStart(600000);
    view.rerender(
      <VideoPreview
        {...BASE_PROPS}
        currentTimeMs={600000}
        seekId={1}
        onTimeUpdate={onTimeUpdate}
      />,
    );
    view.video.currentTime = 30;
    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "0.75" },
    });
    await advance(21 * MINUTE);
    emit(view.video, "play");
    await flush(); // the renewal opens the window
    view.video.currentTime = 0;
    onTimeUpdate.mockClear();

    fireEvent.change(seekBar(view.container), { target: { value: "605000" } });
    // The open window now names the seek target, so the reports through the
    // window read the target, never the session start.
    await advance(300);
    expect(onTimeUpdate).toHaveBeenLastCalledWith(605000);

    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.currentTime).toBe(5);
    expect(view.video.playbackRate).toBe(0.75);
    expect(view.video.play).toHaveBeenCalled();
  });

  it("does not restore a seek that starts a new session before the current start", async () => {
    infoDuration = 2700;
    const onTimeUpdate = vi.fn();
    const view = await mountedAtSessionStart(600000, { onTimeUpdate });
    view.video.currentTime = 30;
    await advance(21 * MINUTE);
    emit(view.video, "seeking");
    await flush();
    view.video.currentTime = 0;

    fireEvent.change(seekBar(view.container), { target: { value: "300000" } });
    await flush();
    // The new session begins at the spec's 5:00, and that is the position
    // the seek reports, not the 10:30 the replaced source left behind.
    expect(onTimeUpdate).toHaveBeenLastCalledWith(300000);
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/0/300.000/playlist.m3u8");
    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.currentTime).toBe(0);
  });

  it("lands an external seek to the session start inside a reset window on the target", async () => {
    infoDuration = 2700;
    const view = await mountedAtSessionStart(600000);
    view.video.currentTime = 30;
    await advance(21 * MINUTE);
    emit(view.video, "play"); // the renewal opens the window at 10:30
    await flush();
    expect(view.video.src).toContain("st=st-token-2");
    view.video.currentTime = 0; // as a browser does on the src change

    // The parent seeks back to the session start through a new seek id:
    // the open window must name the target, so the pending restore lands
    // on it and not on the position the window was opened at.
    view.rerender(
      <VideoPreview {...BASE_PROPS} currentTimeMs={600000} seekId={2} />,
    );
    await flush();
    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.currentTime).toBe(0);
  });

  it("reports the new position while a replacement mint is held, never the old window", async () => {
    infoDuration = 2700;
    const onTimeUpdate = vi.fn();
    const view = await mountedAtSessionStart(600000, { onTimeUpdate });
    view.video.currentTime = 30;
    await advance(21 * MINUTE);
    emit(view.video, "play"); // the renewal opens the window at 10:30
    await flush();
    view.video.currentTime = 0; // as a browser does on a src change
    onTimeUpdate.mockClear();

    // An audio switch needs a new token, and its mint is held: the window
    // of the replaced source must not outlive the source change.
    const held = pendingMint();
    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    await flush();
    expect(view.video.getAttribute("src")).toBeNull();

    // A seek to the spec's 5:00, before the new session's 10:30 start:
    // every report from here reads the seek target, not the position the
    // replaced source left behind.
    fireEvent.change(seekBar(view.container), { target: { value: "300000" } });
    await flush();
    onTimeUpdate.mockClear();
    await advance(300);
    const reported = onTimeUpdate.mock.calls.map(([ms]) => ms);
    expect(reported.length).toBeGreaterThan(0);
    for (const ms of reported) {
      expect(ms).toBe(300000);
    }

    held.resolveToken();
    await flush();
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/1/300.000/playlist.m3u8");
  });
});

describe("waiting probes", () => {
  it("probes once in 20 seconds of waiting with a valid token, and neither mints nor errors", async () => {
    const view = await mounted();
    emit(view.video, "waiting");
    await advance(20 * 1000);
    await flush();

    const probes = probeCalls();
    expect(probes).toHaveLength(1);
    expect(probes[0].url.searchParams.get("st")).toBe("st-token-1");
    expect(probes[0].init?.headers).toBeUndefined();
    expect(probes[0].init?.cache as string | undefined).toBe("no-store");
    expect(mintCalls()).toHaveLength(1);
    expectNoErrorState(view.container);
  });

  it("probes every 15 seconds while the element waits", async () => {
    const view = await mounted();
    emit(view.video, "waiting");
    await advance(50 * 1000);
    expect(probeCalls()).toHaveLength(3);
  });

  it("stops probing at canplay and at a pause", async () => {
    const view = await mounted();
    emit(view.video, "waiting");
    await advance(15 * 1000);
    expect(probeCalls()).toHaveLength(1);
    emit(view.video, "canplay");
    await advance(45 * 1000);
    expect(probeCalls()).toHaveLength(1);

    emit(view.video, "waiting");
    await advance(15 * 1000);
    expect(probeCalls()).toHaveLength(2);
    emit(view.video, "pause");
    await advance(45 * 1000);
    expect(probeCalls()).toHaveLength(2);
  });

  it("aborts a probe still pending when the next one falls due, and drops its late answer", async () => {
    // fetchIgnoresAbort makes the aborted probe's answer arrive anyway:
    // only the generation check can retire it.
    fetchIgnoresAbort = true;
    holdPlaylistResponses = true;
    const view = await mounted();
    emit(view.video, "waiting");
    await advance(15 * 1000);
    expect(probeCalls()).toHaveLength(1);
    await advance(15 * 1000);
    expect(probeCalls()).toHaveLength(2);
    expect(heldPlaylistResolves).toHaveLength(2);

    // The next probe answers 401 and mints; the reset it starts ends the
    // episode the held probe belongs to.
    heldPlaylistResolves[1](playlistResponse(401));
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st=st-token-2");

    // The aborted probe's late 401 arrives after that reset: it starts
    // no mint.
    heldPlaylistResolves[0](playlistResponse(401));
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expectNoErrorState(view.container);
  });

  it("drops a probe 401 landing in the error retry gap, and the retry recovers", async () => {
    fetchIgnoresAbort = true;
    holdPlaylistResponses = true;
    const view = await mounted();
    emit(view.video, "waiting");
    await advance(15 * 1000);
    expect(probeCalls()).toHaveLength(1);

    failedMint(502);
    emit(view.video, "error"); // aborts the held probe and starts the error mint
    await flush();
    expect(mintCalls()).toHaveLength(2);

    // The probe's late 401 lands inside the 10 second retry gap: it
    // starts no mint of its own.
    heldPlaylistResolves[0](playlistResponse(401));
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expectNoErrorState(view.container);

    await advance(10 * 1000); // the error retry recovers
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expect(view.video.src).toContain("st-token-2");
    expectNoErrorState(view.container);
  });

  it("sends no probe while a probe-driven mint is pending", async () => {
    playlistStatus = 401;
    const view = await mounted();
    const held = pendingMint();
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush(); // the probe's 401 starts a mint
    expect(mintCalls()).toHaveLength(2);

    await advance(15 * 1000);
    expect(probeCalls()).toHaveLength(1);

    held.resolveToken();
    await flush();
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("sends no probe while the error chain retries, and its recovery resets the source", async () => {
    const view = await mounted();
    defaultMintFailureStatus = 502;
    emit(view.video, "error");
    await flush();
    emit(view.video, "waiting");
    await advance(60 * 1000);
    await flush();

    // The error chain owns the recovery: the wait's every 15 second tick
    // fetches nothing beside it, and eight mints answer 502, the mount
    // one, the error one and its six retries.
    expect(probeCalls()).toHaveLength(0);
    expect(mintCalls()).toHaveLength(8);

    defaultMintFailureStatus = null;
    await advance(10 * 1000); // the retry at seventy seconds recovers
    await flush();
    expect(view.video.src).toContain("st=st-token-2");
    expectNoErrorState(view.container);
  });

  it("ignores a probe answer that arrives after a reset ended its episode", async () => {
    fetchIgnoresAbort = true;
    holdPlaylistResponses = true;
    const view = await mounted();
    emit(view.video, "waiting");
    await advance(15 * 1000);
    expect(probeCalls()).toHaveLength(1);

    emit(view.video, "error"); // an error recovery resets the source, ending the episode
    await flush();
    expect(view.video.src).toContain("st=st-token-2");

    heldPlaylistResolves[0](playlistResponse(401));
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expectNoErrorState(view.container);
  });

  it("mints once and resets at the saved position when a probe answers 401", async () => {
    playlistStatus = 401;
    const view = await mounted();
    view.video.currentTime = 42;
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush();

    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st=st-token-2");
    view.video.currentTime = 0;
    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.currentTime).toBe(42);
    expect(view.video.play).not.toHaveBeenCalled();
  });

  it("neither mints nor errors on a 503 probe or a network error", async () => {
    playlistStatus = 503;
    const view = await mounted();
    emit(view.video, "waiting");
    await advance(35 * 1000);
    expect(probeCalls()).toHaveLength(2);
    expect(mintCalls()).toHaveLength(1);
    expectNoErrorState(view.container);

    playlistNetworkError = true;
    emit(view.video, "canplay");
    emit(view.video, "waiting");
    await advance(15 * 1000);
    expect(probeCalls()).toHaveLength(3);
    expect(mintCalls()).toHaveLength(1);
    expectNoErrorState(view.container);
  });

  it("shows the error state when the new token of a probe reset is refused within a minute", async () => {
    playlistStatus = 401;
    const view = await mounted();
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush(); // probe 401 -> mint -> reset, which ends the episode
    expect(view.video.src).toContain("st=st-token-2");

    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expectErrorState(view.container);
  });

  it("attempts recovery for a new scope refused within the minute of another scope's recovery", async () => {
    playlistStatus = 401;
    const view = await mounted();
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush(); // the first scope's probe 401 recovers with its own token
    expect(view.video.src).toContain("st=st-token-2");

    // An audio switch mints a token for a new scope, and its own refusal
    // within the minute still attempts recovery: the recovery budget
    // belongs to the token it protects, not to whichever source last
    // recovered.
    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    await flush();
    expect(view.video.src).toContain("st=st-token-3");

    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush();
    expect(mintCalls()).toHaveLength(4);
    expectNoErrorState(view.container);
    expect(view.video.src).toContain("st=st-token-4");
  });

  it("attempts recovery for a replacement token of a recovered scope refused within the minute", async () => {
    playlistStatus = 401;
    const view = await mounted();
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush(); // the first scope's probe 401 recovers with its own token
    expect(view.video.src).toContain("st=st-token-2");

    // An audio switch away and back within the minute mints a replacement
    // token for the original scope: the recovery minute protects the token
    // it minted, and the replacement's own refusal still attempts
    // recovery.
    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    await flush();
    expect(view.video.src).toContain("st=st-token-3");

    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={0} />);
    await flush();
    expect(view.video.src).toContain("st=st-token-4");

    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush();
    expect(mintCalls()).toHaveLength(5);
    expectNoErrorState(view.container);
    expect(view.video.src).toContain("st=st-token-5");
  });

  it("mints for an audio switch after a terminal answer latched the error state", async () => {
    playlistStatus = 401;
    const view = await mounted();
    refusedMint(400, true);
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush(); // the probe's 401 mints, and the terminal answer latches
    expectErrorState(view.container);

    // A new track is a new source: the terminal verdict of the old one
    // ends with it, and the switch mints and goes on the air instead of
    // standing in the error state.
    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    await flush();
    expect(mintCalls()).toHaveLength(3);
    const video = freshVideo(view.container);
    const src = new URL(video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/1/0.000/playlist.m3u8");
    expect(video.src).toContain("st=st-token-2");
    expectNoErrorState(view.container);
  });

  it("mints once and resets once after a probe fails to connect and the next answers 401", async () => {
    playlistNetworkError = true;
    const view = await mounted();
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush();
    expect(mintCalls()).toHaveLength(1);

    playlistNetworkError = false;
    playlistStatus = 401;
    await advance(15 * 1000);
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("waits for the next probe when a probe mint fails, and mints again on its 401", async () => {
    playlistStatus = 401;
    const view = await mounted();
    failedMint(502);
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush();
    expect(mintCalls()).toHaveLength(2); // the failed one
    expect(view.video.src).toContain("st=st-token-1");

    await advance(15 * 1000);
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("gives up two minutes after the first failed probe mint, and no mint follows the error state", async () => {
    playlistStatus = 401;
    const view = await mounted();
    defaultMintFailureStatus = 502;
    emit(view.video, "waiting");
    for (let i = 0; i < 9; i += 1) {
      await advance(15 * 1000);
      await flush();
    }
    // Mints at 15, 30, ... seconds, every one failing. The give-up timer
    // armed at the first failure preempts the probe due at 135 seconds,
    // so the error state shows there and the next mint never starts.
    expect(mintCalls()).toHaveLength(9);
    expectErrorState(view.container);

    await advance(60 * 1000);
    expect(mintCalls()).toHaveLength(9);
  });

  it("shows the error state two minutes after the first failed probe mint without another mint", async () => {
    playlistStatus = 401;
    const view = await mounted();
    failedMint(502);
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush(); // the probe's 401 mints, and the mint fails
    expect(mintCalls()).toHaveLength(2);

    // The server then answers nothing but 503s: no probe mints again, and
    // the give-up is a real timer, not a later mint's failure.
    playlistStatus = 503;
    for (let i = 0; i < 8; i += 1) {
      await advance(15 * 1000);
      await flush();
    }
    expectErrorState(view.container);
    expect(mintCalls()).toHaveLength(2);

    await advance(60 * 1000);
    expect(mintCalls()).toHaveLength(2);
  });

  it("ignores a probe mint failure that lands after a pause ended the wait", async () => {
    playlistStatus = 401;
    const view = await mounted();
    const held = pendingMint();
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush(); // the probe's 401 starts the held mint
    expect(mintCalls()).toHaveLength(2);

    emit(view.video, "pause"); // the wait ends while the mint is in flight
    held.resolveRefused(502, false);
    await flush();

    // The wait is over: its late failure arms no give-up timer for a
    // fresh episode, so the paused player keeps its source well past the
    // two minute mark.
    await advance(125 * 1000);
    expectNoErrorState(view.container);
    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st=st-token-1");
  });

  it("ignores a probe mint failure that lands after a canplay ended the wait", async () => {
    playlistStatus = 401;
    const view = await mounted();
    const held = pendingMint();
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush(); // the probe's 401 starts the held mint
    expect(mintCalls()).toHaveLength(2);

    emit(view.video, "canplay"); // the stream moves again; the wait ends
    held.resolveRefused(502, false);
    await flush();

    await advance(125 * 1000);
    expectNoErrorState(view.container);
    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st=st-token-1");
  });

  it("puts a new session on the air when a pause button press lands while it awaits a probe mint", async () => {
    playlistStatus = 401;
    const view = await mounted();
    emit(view.video, "play");
    await flush();
    const held = pendingMint();
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush(); // the probe's 401 starts the held mint
    expect(mintCalls()).toHaveLength(2);

    // A new start-time session on the same track waits for that mint.
    view.rerender(
      <VideoPreview {...BASE_PROPS} currentTimeMs={650000} seekId={2} />,
    );
    await flush();
    expect(view.video.getAttribute("src")).toBeNull();

    // The button ends the probe wait, but the mint the new session waits
    // on is not unowned: its answer still puts the new session on the air.
    fireEvent.click(screen.getByTitle("Pause"));
    await flush();
    held.resolveToken();
    await flush();
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/650.000/playlist.m3u8");
    expect(view.video.src).toContain("st=st-token-2");
    expectNoErrorState(view.container);

    // The pause the user pressed while the session waited on its mint is
    // still in force when the source goes on the air: loading it must not
    // start playback on its own.
    (view.video.play as unknown as ReturnType<typeof vi.fn>).mockClear();
    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.play).not.toHaveBeenCalled();

    // The new session stays on the air past the two minute give-up its
    // source chain armed while it waited.
    await advance(125 * 1000);
    expectNoErrorState(view.container);
    expect(mintCalls()).toHaveLength(2);
  });

  it("keeps a paused player paused when its audio switch ends a held source mint", async () => {
    const view = await mounted();
    emit(view.video, "play");
    await flush();

    // An audio switch needs a new token, and its mint is held: the new
    // source waits on it.
    const held = pendingMint();
    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    await flush();
    expect(view.video.getAttribute("src")).toBeNull();

    // The user pauses while the mint is still in flight, then the source
    // goes on the air: loading it must not start playback on its own.
    fireEvent.click(screen.getByTitle("Pause"));
    await flush();
    held.resolveToken();
    await flush();
    (view.video.play as unknown as ReturnType<typeof vi.fn>).mockClear();
    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.play).not.toHaveBeenCalled();
    expect(screen.getByTitle("Play")).toBeTruthy();
  });

  it("puts a new session on the air when an element pause event lands while it awaits an expiry mint", async () => {
    mintResponders.push(() => jsonResponse(tokenBody(180)));
    const view = await mounted();
    await advance(125 * 1000); // the first token has 55 seconds left
    const held = pendingMint();
    emit(view.video, "waiting"); // the expiry mint starts and is held
    await flush();
    expect(mintCalls()).toHaveLength(2);

    // A new start-time session on the same track waits for that mint.
    view.rerender(
      <VideoPreview {...BASE_PROPS} currentTimeMs={650000} seekId={2} />,
    );
    await flush();
    expect(view.video.getAttribute("src")).toBeNull();

    // The element fires pause while already paused (a src reset can do
    // that in the browser): the mint the new session waits on is not
    // unowned, and its answer still puts the new session on the air.
    emit(view.video, "pause");
    await flush();
    held.resolveToken();
    await flush();
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/650.000/playlist.m3u8");
    expect(view.video.src).toContain("st=st-token-2");
    expectNoErrorState(view.container);

    await advance(125 * 1000);
    expectNoErrorState(view.container);
    expect(mintCalls()).toHaveLength(2);
  });

  it("lets the error path's own deadline govern after it takes over a failing probe", async () => {
    playlistStatus = 401;
    const view = await mounted();
    failedMint(502); // the probe's mint fails at 15 seconds
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush();
    expect(mintCalls()).toHaveLength(2);

    // Later probes reach the server but stop minting, so the probe path's
    // only live business is its give-up timer.
    playlistStatus = 503;
    await advance(45 * 1000); // one minute in

    // The media error takes over at one minute, and its own two minutes
    // start there. Eight failing answers keep the error path retrying
    // past the moment the probe path's give-up would have fired.
    for (let i = 0; i < 8; i += 1) {
      failedMint(502);
    }
    emit(view.video, "error");
    await flush();
    for (let i = 0; i < 8; i += 1) {
      await advance(10 * 1000);
      await flush();
    }

    // Two minutes twenty after the probe failed, well past the probe
    // path's would-be give-up: the ninth attempt recovers, and the error
    // state never showed.
    expectNoErrorState(view.container);
    expect(mintCalls()).toHaveLength(11);
    expect(probeCalls()).toHaveLength(4); // 15, 30, 45 and 60 seconds
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("shows the error state at once when the probe mint gets a terminal answer", async () => {
    playlistStatus = 401;
    const view = await mounted();
    refusedMint(400, true);
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush();

    expectErrorState(view.container);
    await advance(60 * 1000);
    expect(mintCalls()).toHaveLength(2);
    expect(probeCalls()).toHaveLength(1);
  });

  it("treats a bare 404 from a proxy as transient and mints again on the next 401", async () => {
    playlistStatus = 401;
    const view = await mounted();
    refusedMint(404, false);
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush();
    expectNoErrorState(view.container);
    expect(view.video.src).toContain("st=st-token-1");

    await advance(15 * 1000);
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("mints on waiting without a probe when the token is within a minute of expiry", async () => {
    mintResponders.push(() => jsonResponse(tokenBody(180)));
    const view = await mounted();
    expect(view.video.src).toContain("st-token-1");

    await advance(125 * 1000); // 55 seconds left
    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush();
    expect(probeCalls()).toHaveLength(0);
    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("treats a media error after an expiry reset as a first failure", async () => {
    mintResponders.push(() => jsonResponse(tokenBody(180)));
    const view = await mounted();
    await advance(125 * 1000);
    emit(view.video, "waiting");
    await advance(1000);
    await flush();
    expect(view.video.src).toContain("st=st-token-2");

    emit(view.video, "error");
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expectNoErrorState(view.container);
  });

  it("ignores a probe answer that arrives after a seek renewal reset the source", async () => {
    fetchIgnoresAbort = true;
    holdPlaylistResponses = true;
    const view = await mounted();
    await advance(21 * MINUTE);
    emit(view.video, "waiting");
    await advance(15 * 1000);
    expect(probeCalls()).toHaveLength(1);

    emit(view.video, "seeking"); // an old token renews while the probe is out
    await flush();
    expect(view.video.src).toContain("st=st-token-2");

    heldPlaylistResolves[0](playlistResponse(401));
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expectNoErrorState(view.container);
  });

  it("ignores a probe answer that arrives after a source change began", async () => {
    fetchIgnoresAbort = true;
    holdPlaylistResponses = true;
    const view = await mounted();
    // An error recovery resets the source; its recovery stamp is the error
    // path's business and must not make a probe answer fatal.
    emit(view.video, "error");
    await flush();
    expect(view.video.src).toContain("st=st-token-2");

    emit(view.video, "waiting");
    await advance(15 * 1000);
    expect(probeCalls()).toHaveLength(1);

    // An audio switch takes the old session's probe with it.
    const held = pendingMint();
    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expect(view.video.getAttribute("src")).toBeNull(); // off the air

    heldPlaylistResolves[0](playlistResponse(401)); // the stale probe answers
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expectNoErrorState(view.container);

    held.resolveToken(); // the switch's own mint lands
    await flush();
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/1/");
    expect(view.video.src).toContain("st=st-token-3");
  });

  it("lets a probe reset follow an error recovery within the minute", async () => {
    playlistStatus = 401;
    const view = await mountedAtSessionStart(600000);
    view.video.currentTime = 30;
    await advance(21 * MINUTE);
    emit(view.video, "play");
    await flush(); // the renewal resets the source and opens the window
    emit(view.video, "error"); // the second reset: an error recovery
    await flush();
    expect(mintCalls()).toHaveLength(3);
    view.video.currentTime = 0; // as a browser does on each src change

    emit(view.video, "waiting");
    await advance(15 * 1000);
    await flush(); // the probe's 401 resets the source a third time
    expect(mintCalls()).toHaveLength(4);
    expectNoErrorState(view.container);
    expect(view.video.src).toContain("st=st-token-4");

    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.currentTime).toBe(30);
    expect(view.video.play).toHaveBeenCalled();
  });
});

describe("one mint at a time", () => {
  it("holds every path off while a mint is pending, then resets once", async () => {
    const view = await mounted();
    view.video.currentTime = 30;
    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "0.5" },
    });
    await advance(21 * MINUTE);

    const held = pendingMint();
    emit(view.video, "play"); // starts the pending renewal mint
    await flush();
    expect(mintCalls()).toHaveLength(2);

    emit(view.video, "timeupdate");
    emit(view.video, "ratechange");
    emit(view.video, "play");
    emit(view.video, "waiting");
    emit(view.video, "error");
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st=st-token-1");

    held.resolveToken();
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st=st-token-2");

    view.video.currentTime = 0;
    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.currentTime).toBe(30);
    expect(view.video.playbackRate).toBe(0.5);
    expect(view.video.play).toHaveBeenCalled();
  });

  it("waits 60 seconds after a failed renewal before the event rules mint again", async () => {
    infoDuration = 2700;
    defaultLifetime = 4500;
    const view = await mounted();
    view.video.currentTime = 600;
    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "0.25" },
    });
    await advance(61 * MINUTE);

    failedMint(502);
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st=st-token-1"); // resets nothing

    await advance(30 * 1000);
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(2);

    await advance(31 * 1000);
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("recovers once when the error path mint fails twice and then succeeds", async () => {
    const view = await mounted();
    await advance(21 * MINUTE); // an old token: the event rules would renew
    failedMint(502);
    failedMint(502);
    emit(view.video, "error");
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expectNoErrorState(view.container);

    // In the retry gap the error chain occupies the one-mint guard: a play
    // on the old token mints nothing while the chain is still retrying.
    emit(view.video, "play");
    await flush();
    expect(mintCalls()).toHaveLength(2);

    await advance(10 * 1000);
    await flush();
    expect(mintCalls()).toHaveLength(3);

    await advance(10 * 1000);
    await flush();
    expect(mintCalls()).toHaveLength(4); // the third attempt succeeds
    expect(view.video.src).toContain("st=st-token-2");
    expectNoErrorState(view.container);
  });

  it("shows the error state two minutes after an error whose mints keep failing", async () => {
    const view = await mounted();
    defaultMintFailureStatus = 502;
    emit(view.video, "error");
    for (let i = 0; i < 12; i += 1) {
      await advance(10 * 1000);
      await flush();
    }
    expect(mintCalls()).toHaveLength(13); // the mount mint plus twelve attempts
    expectErrorState(view.container);

    await advance(30 * 1000);
    expect(mintCalls()).toHaveLength(13);
  });

  it("stops the error retries for a source change and mints for the new source", async () => {
    const view = await mounted();
    failedMint(502);
    emit(view.video, "error");
    await flush();
    await advance(10 * 1000);
    await flush();
    expect(mintCalls()).toHaveLength(3);

    defaultMintFailureStatus = null;
    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    await flush();
    expect(mintCalls()).toHaveLength(4); // the switch's own mint
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/1/");
    // The mount mint, the failed error mint, its successful retry and the
    // switch's own mint: the switch carries the fourth token.
    expect(view.video.src).toContain("st=st-token-3");

    await advance(30 * 1000);
    expect(mintCalls()).toHaveLength(4); // the old retries stopped
    expectNoErrorState(view.container);
  });

  it("lets the error path take over a pending renewal mint that fails", async () => {
    const view = await mounted();
    await advance(21 * MINUTE);
    const held = pendingMint();
    emit(view.video, "play");
    await flush();
    emit(view.video, "error"); // adopts the pending mint

    held.resolveRefused(502, false);
    await flush();
    expect(view.video.src).toContain("st=st-token-1"); // no reset yet

    await advance(10 * 1000); // the error path retries
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expect(view.video.src).toContain("st=st-token-2");
    expectNoErrorState(view.container);
  });

  it("shows the error state two minutes after an error when an adopted mint and its retries fail", async () => {
    const view = await mounted();
    await advance(21 * MINUTE);
    const held = pendingMint();
    emit(view.video, "play");
    await flush();
    emit(view.video, "error");

    held.resolveRefused(502, false);
    defaultMintFailureStatus = 502;
    for (let i = 0; i < 12; i += 1) {
      await advance(10 * 1000);
      await flush();
    }
    expectErrorState(view.container);
  });
});

describe("the mint deadline and terminal answers", () => {
  it("aborts a renewal mint that never settles after 45 seconds and blocks the rules for 60 more", async () => {
    infoDuration = 2700;
    defaultLifetime = 4500;
    const view = await mounted();
    view.video.currentTime = 600;
    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "0.25" },
    });
    await advance(61 * MINUTE);

    neverSettlingMint();
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(2);

    await advance(45 * 1000); // the deadline aborts the mint
    await flush();
    expect(view.video.src).toContain("st=st-token-1");

    await advance(15 * 1000);
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(2);

    await advance(46 * 1000);
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("releases the guard at the deadline when a mint body stalls, then blocks the rules for 60 more", async () => {
    infoDuration = 2700;
    defaultLifetime = 4500;
    const view = await mounted();
    view.video.currentTime = 600;
    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "0.25" },
    });
    await advance(61 * MINUTE);

    // The headers arrive and the body never closes: the deadline spans the
    // body parse, so aborting the request releases the guard.
    mintResponders.push(() => {
      const response = jsonResponse({
        token: "st-token-2",
        lifetime: DEFAULT_LIFETIME,
      });
      // The body never closes: a promise whose executor never settles.
      vi.spyOn(response, "json").mockReturnValue(
        new Promise<never>(() => undefined),
      );
      return response;
    });
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st-token-1");

    await advance(45 * 1000); // the deadline aborts the stalled mint
    await flush();
    expect(view.video.src).toContain("st-token-1");

    await advance(15 * 1000); // still inside the 60 second block
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(2);

    await advance(46 * 1000);
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("treats a 200 body without a finite lifetime as a failed mint and keeps the retry rules", async () => {
    const view = await mounted();
    await advance(21 * MINUTE);
    mintResponders.push(() => jsonResponse({ token: "st-token-2" }));
    emit(view.video, "play");
    await flush();
    expect(mintCalls()).toHaveLength(2);
    // The unusable body never reaches the source.
    expect(view.video.src).toContain("st-token-1");
    expectNoErrorState(view.container);

    // A failed renewal blocks the event rules for a minute.
    await advance(30 * 1000);
    emit(view.video, "play");
    await flush();
    expect(mintCalls()).toHaveLength(2);

    await advance(31 * 1000);
    emit(view.video, "play");
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("hands a never-settling renewal mint to the error path at its deadline, recovering once", async () => {
    const view = await mounted();
    await advance(21 * MINUTE);
    neverSettlingMint();
    emit(view.video, "play");
    await flush();
    emit(view.video, "error");

    await advance(45 * 1000); // deadline abort -> the error path takes over
    await advance(10 * 1000); // the error path's retry
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expect(view.video.src).toContain("st=st-token-2");
    expectNoErrorState(view.container);
  });

  it("shows the error state two minutes after an error when every attempt hangs", async () => {
    const view = await mounted();
    await advance(21 * MINUTE);
    defaultMintHangs = true;
    emit(view.video, "play");
    await flush();
    emit(view.video, "error");

    for (let i = 0; i < 14; i += 1) {
      await advance(10 * 1000);
      await flush();
    }
    expectErrorState(view.container);
  });

  it("aborts an unusable pending renewal mint at an audio switch, and ignores its late answer", async () => {
    fetchIgnoresAbort = true;
    const view = await mounted();
    await advance(21 * MINUTE);
    const held = pendingMint();
    emit(view.video, "play");
    await flush();

    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    await flush();
    expect(mintCalls()).toHaveLength(3);
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/1/");
    expect(view.video.src).toContain("st=st-token-2");

    held.resolveToken(); // the aborted mint's answer arrives anyway
    await flush();
    expect(view.video.src).toContain("st=st-token-2");
    expect(mintCalls()).toHaveLength(3);
  });

  it("waits for a pending mint binding a new session's scope, then retries after its deadline", async () => {
    const view = await mounted();
    await advance(21 * MINUTE);
    pendingMint();
    emit(view.video, "play");
    await flush();

    // A new start-time session on the same track waits for the pending mint.
    view.rerender(
      <VideoPreview {...BASE_PROPS} currentTimeMs={650000} seekId={2} />,
    );
    await flush();
    expect(mintCalls()).toHaveLength(2);

    await advance(45 * 1000); // the pending mint hits its deadline
    await advance(10 * 1000); // the new source's retry
    await flush();
    expect(mintCalls()).toHaveLength(3);
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/650.000/playlist.m3u8");
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("shows the error state at once for a terminal answer on the error path", async () => {
    const view = await mounted();
    refusedMint(400, true);
    emit(view.video, "error");
    await flush();
    expectErrorState(view.container);

    await advance(60 * 1000);
    expect(mintCalls()).toHaveLength(2);
  });

  it("shows the error state for an audio switch whose mint gets a terminal answer", async () => {
    const view = await mounted();
    refusedMint(403, true);
    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    await flush();
    expectErrorState(view.container);
    expect(mintCalls()).toHaveLength(2);
    expect(view.video.getAttribute("src")).toBeNull();
  });

  it("shows the error state when a renewal an error adopted is refused terminally", async () => {
    const view = await mounted();
    await advance(21 * MINUTE);
    refusedMint(400, true);
    emit(view.video, "play"); // starts the renewal mint
    emit(view.video, "error"); // the error adopts it before it settles
    await flush();
    expectErrorState(view.container);
    expect(mintCalls()).toHaveLength(2);
  });

  it("shows the error state when a renewal a new session waits on is refused terminally", async () => {
    const view = await mounted();
    await advance(21 * MINUTE);
    const held = pendingMint();
    emit(view.video, "play");
    await flush();
    expect(mintCalls()).toHaveLength(2);

    // A new start-time session on the same track waits for the pending mint.
    view.rerender(
      <VideoPreview {...BASE_PROPS} currentTimeMs={650000} seekId={2} />,
    );
    await flush();
    expect(view.video.getAttribute("src")).toBeNull();

    held.resolveRefused(404, true);
    await flush();
    expectErrorState(view.container);
    expect(mintCalls()).toHaveLength(2);
  });

  it("waits 60 seconds after a terminal renewal answer and never shows the error state", async () => {
    infoDuration = 2700;
    defaultLifetime = 4500;
    const view = await mounted();
    view.video.currentTime = 600;
    fireEvent.change(screen.getByTitle("Playback speed"), {
      target: { value: "0.25" },
    });
    await advance(61 * MINUTE);

    refusedMint(404, true);
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expectNoErrorState(view.container);
    expect(view.video.src).toContain("st=st-token-1");

    await advance(30 * 1000);
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(2);

    await advance(31 * 1000);
    emit(view.video, "timeupdate");
    await flush();
    expect(mintCalls()).toHaveLength(3);
  });

  it("treats a bare proxy 404 as transient on the error path", async () => {
    const view = await mounted();
    refusedMint(404, false);
    refusedMint(404, false);
    emit(view.video, "error");
    await flush();
    expectNoErrorState(view.container);

    await advance(10 * 1000);
    await flush();
    expectNoErrorState(view.container);
    await advance(10 * 1000);
    await flush();
    expect(mintCalls()).toHaveLength(4);
    expectNoErrorState(view.container);
  });

  it("recovers an audio switch whose first two mints get a bare proxy 404", async () => {
    const view = await mounted();
    refusedMint(404, false);
    refusedMint(404, false);
    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    await flush();
    expect(view.video.getAttribute("src")).toBeNull();
    await advance(10 * 1000);
    await flush();
    await advance(10 * 1000);
    await flush();
    expect(mintCalls()).toHaveLength(4);
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/1/");
    expect(view.video.src).toContain("st=st-token-2");
    expectNoErrorState(view.container);
  });
});

describe("new native sources", () => {
  it("mints for an audio switch before setting the source", async () => {
    const view = await mounted();
    expect(new URL(view.video.src, "http://bazarr.test").pathname).toContain(
      "/0/0.000/",
    );

    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    await flush();
    expect(mintCalls()).toHaveLength(2);
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/1/0.000/playlist.m3u8");
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("reuses a young token for a new start-time session on the same track", async () => {
    const view = await mounted();
    view.rerender(
      <VideoPreview {...BASE_PROPS} currentTimeMs={600000} seekId={1} />,
    );
    await flush();
    expect(mintCalls()).toHaveLength(1);
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/0/600.000/playlist.m3u8");
    expect(view.video.src).toContain("st=st-token-1");
  });

  it("mints first for a new session on an old token", async () => {
    const view = await mounted();
    await advance(21 * MINUTE);
    view.rerender(
      <VideoPreview {...BASE_PROPS} currentTimeMs={600000} seekId={1} />,
    );
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("mints for a changed media item during a delayed renewal mint", async () => {
    fetchIgnoresAbort = true;
    const view = await mounted();
    await advance(21 * MINUTE);
    const held = pendingMint();
    emit(view.video, "play");
    await flush();

    view.rerender(<VideoPreview {...BASE_PROPS} mediaId={8} />);
    await flush();
    expect(mintCalls()).toHaveLength(3);
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/episode/8/");
    expect(view.video.src).toContain("st=st-token-2");

    held.resolveToken(); // late answer of the old media's mint: dropped
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("mints for a changed arr instance during a delayed renewal mint", async () => {
    fetchIgnoresAbort = true;
    const view = await mounted();
    await advance(21 * MINUTE);
    const held = pendingMint();
    emit(view.video, "play");
    await flush();

    view.rerender(
      <VideoPreview {...BASE_PROPS} mediaId={8} arrInstanceId={3} />,
    );
    await flush();
    expect(mintCalls()).toHaveLength(3);
    const call = mintCalls()[2];
    expect(call.url.searchParams.get("arr_instance_id")).toBe("3");
    expect(view.video.src).toContain("st=st-token-2");

    held.resolveToken(); // late answer of the old instance's mint: dropped
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expect(view.video.src).toContain("st=st-token-2");
  });

  it("waits for a pending error mint before a new session goes on the air", async () => {
    const view = await mounted();
    const held = pendingMint();
    emit(view.video, "error"); // the error path's mint is held
    await flush();
    expect(mintCalls()).toHaveLength(2);

    // A new start-time session on the same track: the young token must
    // not go on the air while the error mint is in flight, and the mint
    // is not orphaned by the wait.
    view.rerender(
      <VideoPreview {...BASE_PROPS} currentTimeMs={600000} seekId={1} />,
    );
    await flush();
    expect(view.video.getAttribute("src")).toBeNull();
    expectNoErrorState(view.container);

    held.resolveToken();
    await flush();
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/0/600.000/playlist.m3u8");
    expect(view.video.src).toContain("st=st-token-2");
    expectNoErrorState(view.container);
  });

  it("aborts a pending mint of another scope before reusing the young token", async () => {
    fetchIgnoresAbort = true;
    const view = await mounted();
    const held = pendingMint();
    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    await flush();
    expect(mintCalls()).toHaveLength(2); // the track 1 mint is held
    expect(view.video.getAttribute("src")).toBeNull();

    // Back to track 0: its young token serves this scope again, and the
    // held mint of the track 1 scope must be aborted, not answered into
    // this source.
    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={0} />);
    await flush();
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/0/0.000/playlist.m3u8");
    expect(view.video.src).toContain("st=st-token-1");
    expect(mintCalls()).toHaveLength(2);

    held.resolveToken(); // the aborted mint's late answer: dropped
    await flush();
    expect(view.video.src).toContain("st=st-token-1");
    expect(mintCalls()).toHaveLength(2);
  });

  it("mints for a changed arr instance alone during a delayed renewal mint", async () => {
    fetchIgnoresAbort = true;
    const view = await mounted();
    await advance(21 * MINUTE);
    const held = pendingMint();
    emit(view.video, "play");
    await flush();

    // The media item stays: only the instance changes, and the new mint
    // binds it while the old instance's mint is aborted.
    view.rerender(<VideoPreview {...BASE_PROPS} arrInstanceId={3} />);
    await flush();
    expect(mintCalls()).toHaveLength(3);
    const call = mintCalls()[2];
    expect(call.url.searchParams.get("arr_instance_id")).toBe("3");
    expect(call.url.searchParams.get("mediaId")).toBe("7");
    const src = new URL(view.video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/episode/7/0/0.000/playlist.m3u8");
    expect(src.searchParams.get("arr_instance_id")).toBe("3");
    expect(view.video.src).toContain("st=st-token-2");

    held.resolveToken(); // the late answer of the old instance's mint
    await flush();
    expect(mintCalls()).toHaveLength(3);
    expect(view.video.src).toContain("st-token-2");
  });

  it("keeps an audio switch off the air until its mint succeeds, giving up after two minutes", async () => {
    const view = await mounted();
    defaultMintFailureStatus = 502;
    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    for (let i = 0; i < 12; i += 1) {
      await advance(10 * 1000);
      await flush();
    }
    expect(view.video.getAttribute("src")).toBeNull();
    expectErrorState(view.container);
  });
});

describe("media errors", () => {
  it("recovers once at the saved position and keeps the playing state", async () => {
    const view = await mounted();
    view.video.currentTime = 42;
    emit(view.video, "play");
    emit(view.video, "error");
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expect(view.video.src).toContain("st=st-token-2");

    view.video.currentTime = 0;
    emit(view.video, "loadedmetadata");
    await flush();
    expect(view.video.currentTime).toBe(42);
    expect(view.video.play).toHaveBeenCalled();
  });

  it("shows the error state for a second error within a minute of a recovery", async () => {
    const view = await mounted();
    emit(view.video, "error");
    await flush();
    expect(mintCalls()).toHaveLength(2);
    expectNoErrorState(view.container);

    emit(view.video, "error");
    await flush();
    expectErrorState(view.container);
    expect(mintCalls()).toHaveLength(2);
  });

  it("mints for a media swap after a terminal error latched the error state", async () => {
    const view = await mountedAtSessionStart(600000);
    emit(view.video, "error");
    await flush(); // the first error recovers with its own token
    emit(view.video, "error"); // the second within the minute is terminal
    await flush();
    expectErrorState(view.container);

    // The new media is a new playback: the terminal verdict of the old
    // source ends with it, and the swap mints and goes on the air instead
    // of standing in the error state.
    view.rerender(<VideoPreview {...BASE_PROPS} mediaId={8} />);
    await flush();
    expect(mintCalls()).toHaveLength(3);
    const video = freshVideo(view.container);
    const src = new URL(video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/episode/8/0/0.000/playlist.m3u8");
    expect(video.src).toContain("st=st-token-3");
    expectNoErrorState(view.container);
  });

  it("mints for a media swap from a zero-start session after a terminal error", async () => {
    const view = await mounted();
    emit(view.video, "error");
    await flush(); // the first error recovers with its own token
    emit(view.video, "error"); // the second within the minute is terminal
    await flush();
    expectErrorState(view.container);

    // The pre-swap session is the zero-start one of a fresh open: the
    // swap resets it to the same values, so the playlist URL does not
    // change with the media. The remounted element still goes on the
    // air with a mint of its own, which the URL alone never triggers.
    view.rerender(<VideoPreview {...BASE_PROPS} mediaId={8} />);
    await flush();
    expect(mintCalls()).toHaveLength(3);
    const video = freshVideo(view.container);
    const src = new URL(video.src, "http://bazarr.test");
    expect(src.pathname).toContain("/episode/8/0/0.000/playlist.m3u8");
    expect(video.src).toContain("st=st-token-3");
    expectNoErrorState(view.container);
  });

  it("attempts recovery for a new scope's error within the minute of another scope's recovery", async () => {
    const view = await mounted();
    emit(view.video, "error");
    await flush(); // the first scope's error recovery mints its own token
    expect(mintCalls()).toHaveLength(2);
    expectNoErrorState(view.container);

    // An audio switch mints a token for a new scope, and its own media
    // error within the minute still attempts recovery: the recovery
    // budget belongs to the token it protects, not to whichever source
    // last recovered.
    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    await flush();
    expect(view.video.src).toContain("st=st-token-3");

    emit(view.video, "error");
    await flush();
    expect(mintCalls()).toHaveLength(4);
    expectNoErrorState(view.container);
    expect(view.video.src).toContain("st=st-token-4");
  });

  it("attempts recovery for a replacement token of a recovered scope failing with a media error within the minute", async () => {
    const view = await mounted();
    emit(view.video, "error");
    await flush(); // the first scope's error recovery mints its own token
    expect(mintCalls()).toHaveLength(2);
    expectNoErrorState(view.container);

    // An audio switch away and back within the minute mints a replacement
    // token for the original scope: the recovery minute protects the token
    // it minted, and the replacement's own media error still attempts
    // recovery.
    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={1} />);
    await flush();
    expect(view.video.src).toContain("st=st-token-3");

    view.rerender(<VideoPreview {...BASE_PROPS} audioTrack={0} />);
    await flush();
    expect(view.video.src).toContain("st=st-token-4");

    emit(view.video, "error");
    await flush();
    expect(mintCalls()).toHaveLength(5);
    expectNoErrorState(view.container);
    expect(view.video.src).toContain("st=st-token-5");
  });
});
