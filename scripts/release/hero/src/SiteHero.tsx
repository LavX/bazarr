/**
 * The public site's hero background.
 *
 * A multiplane night: ridge silhouettes receding into layered mist, with the
 * one warm window in the middle distance. Depth is carried the way a multiplane
 * camera carries it, by rate: nearer bands are larger, faster and softer.
 *
 * Everything except the mist is painted once and blitted, which is the same
 * trade the release hero makes. Identical background pixels on every frame are
 * what let the WebP encoder store almost nothing per frame, so the drifting air
 * is the only thing paying for bytes.
 *
 * LOOP CONTRACT
 * Every band is a horizontally repeating field of period `P`, advanced by
 * `cycles * P` across the whole loop. At the last frame the offset is an exact
 * multiple of `P`, so it is pixel-identical to frame 0. `cycles` must therefore
 * be a whole number, and the four values are coprime-ish (1, 2, 3, 5) so the
 * combined field does not visibly repeat inside a single pass.
 */

import React, { useMemo } from "react";
import { useCurrentFrame, useVideoConfig } from "remotion";

import { Ctx, Geometry, paintGrade, paintGrain, withAlpha } from "./brand";
import { AMBER, NAVY } from "./theme";

export type SiteHeroProps = {
  seed: number;
  loopFrames?: number;
};

interface Band {
  /** vertical centre, as a fraction of height */
  y: number;
  /** band height, as a fraction of height */
  h: number;
  /** whole cycles travelled across the loop; must be an integer */
  cycles: number;
  alpha: number;
  blur: number;
  /** horizontal period as a fraction of width */
  period: number;
}

const BANDS: Band[] = [
  { y: 0.30, h: 0.15, cycles: 1, alpha: 0.10, blur: 26, period: 0.9 },
  { y: 0.45, h: 0.19, cycles: 2, alpha: 0.09, blur: 34, period: 0.75 },
  { y: 0.60, h: 0.25, cycles: 3, alpha: 0.075, blur: 46, period: 0.62 },
  { y: 0.76, h: 0.32, cycles: 5, alpha: 0.06, blur: 62, period: 0.5 },
];

/** The ridges. Static, so they cost one paint and then nothing. */
function paintRidges(c: Ctx, geo: Geometry) {
  const { W, H } = geo;

  const ridge = (baseY: number, amp: number, shade: string, phase: number) => {
    c.beginPath();
    c.moveTo(0, H);
    c.lineTo(0, baseY);
    for (let x = 0; x <= W; x += 8) {
      const t = x / W;
      const y =
        baseY -
        Math.sin(t * Math.PI * 2.2 + phase) * amp -
        Math.sin(t * Math.PI * 5.7 + phase * 1.7) * amp * 0.35;
      c.lineTo(x, y);
    }
    c.lineTo(W, H);
    c.closePath();
    c.fillStyle = shade;
    c.fill();
  };

  ridge(H * 0.52, H * 0.05, "#2a2952", 0.0);
  ridge(H * 0.63, H * 0.045, "#20204a", 1.4);
  ridge(H * 0.76, H * 0.055, "#171634", 2.9);
  ridge(H * 0.94, H * 0.04, "#0e0d20", 4.1);
}

/**
 * The one warm light in the frame, and the only saturated thing in it.
 *
 * The release hero puts a small hard-edged window at the centre of this halo.
 * This one deliberately does not. The site crops the loop to whatever aspect
 * the visitor's viewport happens to be and then lays its own crisp UI over the
 * right-hand half, so a hard bright rectangle lands in an unpredictable place
 * and reads as a rendering fault rather than as a lit window. Only the halo
 * survives here, pooled low and right so it warms the ground under the panel
 * instead of shining through it.
 */
function paintLamp(c: Ctx, geo: Geometry, intensity: number) {
  const { W, H } = geo;
  const x = W * 0.74;
  const y = H * 0.7;

  /* The page lays a navy grade over this whole plate to keep cream text
     legible. The lamp is the one warm thing the direction promises, so it has
     to be painted strong enough to survive that grade: a first pass at 0.26
     left no warm pixel at all in the encoded asset. */
  const halo = c.createRadialGradient(x, y, 0, x, y, Math.min(W, H) * 0.66);
  halo.addColorStop(0, withAlpha(AMBER, 0.62 * intensity));
  halo.addColorStop(0.28, withAlpha(AMBER, 0.30 * intensity));
  halo.addColorStop(0.6, withAlpha(AMBER, 0.11 * intensity));
  halo.addColorStop(1, withAlpha(AMBER, 0));
  c.fillStyle = halo;
  c.fillRect(0, 0, W, H);

  /* The source of it. Soft-edged, unlike the release hero's hard window: this
     plate is cropped to the visitor's aspect ratio and has UI laid over it, so
     a crisp rectangle lands somewhere different on every screen. */
  const core = c.createRadialGradient(x, y, 0, x, y, Math.min(W, H) * 0.055);
  core.addColorStop(0, withAlpha("#ffd489", 0.5 * intensity));
  core.addColorStop(1, withAlpha("#ffd489", 0));
  c.fillStyle = core;
  c.fillRect(0, 0, W, H);
}

function buildStatic(geo: Geometry, seed: number): HTMLCanvasElement {
  const cv = document.createElement("canvas");
  cv.width = geo.W;
  cv.height = geo.H;
  const c = cv.getContext("2d") as Ctx;

  // sky
  const sky = c.createLinearGradient(0, 0, 0, geo.H);
  sky.addColorStop(0, "#14132c");
  sky.addColorStop(0.42, "#22214a");
  sky.addColorStop(1, "#100f22");
  c.fillStyle = sky;
  c.fillRect(0, 0, geo.W, geo.H);

  paintRidges(c, geo);
  return cv;
}

export const SiteHero: React.FC<SiteHeroProps> = ({ seed, loopFrames }) => {
  const frame = useCurrentFrame();
  const { width, height, durationInFrames } = useVideoConfig();
  const period = loopFrames ?? durationInFrames;

  const geo: Geometry = useMemo(
    () => ({ W: width, H: height, S: Math.min(width, height) }),
    [width, height],
  );

  const still = useMemo(() => buildStatic(geo, seed), [geo, seed]);

  const draw = React.useCallback(
    (c: Ctx) => {
      const { W, H } = geo;
      const t = (frame % period) / period; // 0..1 across exactly one loop

      c.clearRect(0, 0, W, H);
      c.drawImage(still, 0, 0);

      // The lamp breathes once per loop, so its phase returns exactly.
      const breath = 0.86 + 0.14 * Math.sin(t * Math.PI * 2);
      paintLamp(c, geo, breath);

      for (const band of BANDS) {
        const P = band.period * W;
        const offset = (t * band.cycles * P) % P;
        const y = band.y * H;
        const h = band.h * H;

        c.save();
        c.filter = `blur(${band.blur}px)`;

        const g = c.createLinearGradient(0, y - h / 2, 0, y + h / 2);
        g.addColorStop(0, "rgba(196, 212, 230, 0)");
        g.addColorStop(0.5, `rgba(196, 212, 230, ${band.alpha})`);
        g.addColorStop(1, "rgba(196, 212, 230, 0)");
        c.fillStyle = g;

        // One extra tile on each side so a wrapping lozenge is never clipped.
        for (let i = -1; i <= Math.ceil(W / P) + 1; i++) {
          c.beginPath();
          c.ellipse(i * P + offset, y, P * 0.42, h * 0.5, 0, 0, Math.PI * 2);
          c.fill();
        }
        c.restore();
      }

      paintGrade(c, geo);
      paintGrain(c, geo, seed);
    },
    [frame, geo, period, seed, still],
  );

  const ref = React.useRef<HTMLCanvasElement>(null);
  React.useLayoutEffect(() => {
    const cv = ref.current;
    if (!cv) return;
    const c = cv.getContext("2d");
    if (c) draw(c as Ctx);
  }, [draw]);

  return (
    <canvas
      ref={ref}
      width={width}
      height={height}
      style={{ width: "100%", height: "100%", display: "block", background: NAVY }}
    />
  );
};
