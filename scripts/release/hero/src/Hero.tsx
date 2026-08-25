/**
 * The release hero shell: the part that does not change between releases.
 *
 * Layer stack, bottom to top:
 *   1. backdrop   navy, dawn gradient, three radial glows, fog, starfield  (static, cached)
 *   2. motif      the release's mechanism/flock/etc                        (animated)
 *   3. type       the title block                                          (near-static)
 *   4. grade      warm key into cool shadow                                (static)
 *   5. grain + vignette                                                    (static)
 *
 * Only layer 2 and the codename shimmer in layer 3 change between frames. That
 * is deliberate: it keeps the animation legible, and it keeps the GIF small.
 */

import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  continueRender,
  delayRender,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

import { drawClockwork } from "./Clockwork";
import { Ctx, getBackdrop, Geometry, paintGrade, paintGrain, paintText, paintVignette } from "./brand";
import { FONT_FAMILY } from "./theme";

// A type alias rather than an interface: Remotion's Composition constrains props
// to Record<string, unknown>, and only type aliases get the implicit index
// signature that satisfies it.
export type HeroProps = {
  version: string;
  codename: string;
  seed: number;
  /**
   * Period of the loop in frames. Defaults to the composition length, which is
   * what a real render uses. It is overridable purely so the loop seam can be
   * proven: render frame `loopFrames` from a composition one frame longer and
   * diff it against frame 0. They must be pixel-identical.
   */
  loopFrames?: number;
};

// Loaded once for the whole render rather than per frame, so seeking between
// frames never re-races the font against the first paint.
let fontPromise: Promise<void> | null = null;

function ensureFont(): Promise<void> {
  if (!fontPromise) {
    fontPromise = (async () => {
      const face = new FontFace(
        "Geist",
        `url(${staticFile("Geist-Variable.woff2")}) format("woff2")`,
        { weight: "100 900" },
      );
      const loaded = await face.load();
      // FontFaceSet.add is missing from the default DOM lib but is standard.
      (document.fonts as FontFaceSet & { add(f: FontFace): void }).add(loaded);
      await document.fonts.ready;
    })();
  }
  return fontPromise;
}

export const Hero: React.FC<HeroProps> = ({ version, codename, seed, loopFrames }) => {
  const frame = useCurrentFrame();
  const { width, height, durationInFrames } = useVideoConfig();
  const dur = loopFrames ?? durationInFrames;
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  const [fontReady, setFontReady] = useState(false);
  const [handle] = useState(() => delayRender("Loading Geist"));

  useEffect(() => {
    let live = true;
    const done = () => {
      if (!live) return;
      setFontReady(true);
      continueRender(handle);
    };
    ensureFont().then(done, done);
    return () => {
      live = false;
    };
  }, [handle]);

  const draw = useCallback(() => {
    const el = canvasRef.current;
    if (!el) return;
    const c = el.getContext("2d") as Ctx | null;
    if (!c) return;

    const geo: Geometry = { W: width, H: height, S: Math.min(width, height) };
    c.imageSmoothingEnabled = true;
    c.imageSmoothingQuality = "high";
    c.clearRect(0, 0, geo.W, geo.H);

    // 1. atmosphere
    c.drawImage(getBackdrop(geo, seed), 0, 0);

    // 2. motif
    drawClockwork(c, geo, { frame, dur });

    // 3. type. The shimmer crosses the codename once per loop, entering and
    // leaving fully outside the glyphs so the loop point shows no band.
    paintText(c, geo, FONT_FAMILY, {
      version,
      codename,
      shimmer: (frame / dur) % 1,
    });

    // 4. grade
    paintGrade(c, geo);

    // 5. grain and vignette
    paintVignette(c, geo);
    paintGrain(c, geo, seed);
  }, [frame, width, height, dur, version, codename, seed]);

  // Layout effect runs synchronously after commit and before the browser paints,
  // so the canvas is complete by the time Remotion captures the frame.
  useLayoutEffect(() => {
    if (!fontReady) return;
    draw();
  }, [draw, fontReady]);

  return (
    <canvas
      ref={canvasRef}
      width={width}
      height={height}
      style={{ width, height, display: "block" }}
    />
  );
};
