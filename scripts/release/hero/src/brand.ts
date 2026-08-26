/**
 * The canonical Bazarr+ hero atmosphere, ported from the static generators in
 * `site/hero/` (most recently `murmuration-v2.5.0.html`).
 *
 * `paintBackdrop`, `paintVignette` and `paintGrain` are deliberate ports rather
 * than reinventions: the navy base, the three radial glows, the fog, the dusk
 * starfield, the vignette falloff and the 0.046 overlay grain are what make a
 * Bazarr+ hero recognisable. A new release changes its motif, never this.
 *
 * The one change the animated pipeline makes is that all of it is STATIC across
 * the loop, and the expensive parts are rendered once and blitted. That is not
 * only a speed decision: identical background pixels on every frame are what let
 * the GIF and WebP encoders store almost nothing per frame, so the moving
 * mechanism is the only thing paying for bytes.
 */

import { AMBER, CREAM, NAVY } from "./theme";

export type Ctx = CanvasRenderingContext2D;

export interface Geometry {
  W: number;
  H: number;
  /** min(W, H). Features scale off this so extreme aspect ratios do not blow out. */
  S: number;
}

// ---------------------------------------------------------------------------
// helpers, ported verbatim so the atmosphere matches the static heroes exactly

export const clamp01 = (v: number) => Math.max(0, Math.min(1, v));
export const smooth = (t: number) => t * t * (3 - 2 * t);

export function mulberry32(seed: number) {
  return function next() {
    let t = (seed += 0x6d2b79f5);
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function withAlpha(color: string, alpha: number): string {
  if (color[0] === "#") {
    const h = color.replace("#", "");
    const r = parseInt(h.substring(0, 2), 16);
    const g = parseInt(h.substring(2, 4), 16);
    const b = parseInt(h.substring(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  const nums = color.replace(/rgba?\(|\)/g, "");
  return `rgba(${nums}, ${alpha})`;
}

export function gGlow(c: Ctx, x: number, y: number, r: number, color: string, alpha: number) {
  const grad = c.createRadialGradient(x, y, 0, x, y, r);
  grad.addColorStop(0, withAlpha(color, alpha));
  grad.addColorStop(1, withAlpha(color, 0));
  c.fillStyle = grad;
  c.beginPath();
  c.arc(x, y, r, 0, Math.PI * 2);
  c.fill();
}

function radialFill(
  c: Ctx,
  W: number,
  H: number,
  x: number,
  y: number,
  r: number,
  stops: [string, number][],
) {
  const grad = c.createRadialGradient(x, y, 0, x, y, r);
  for (const [color, stop] of stops) grad.addColorStop(stop, color);
  c.fillStyle = grad;
  c.fillRect(0, 0, W, H);
}

// ---------------------------------------------------------------------------

/**
 * Navy base, dawn gradient, three radial glows (amber TL, teal BR, purple
 * centre), screened fog and a dusk starfield. Entirely static.
 */
function paintBackdropInto(c: Ctx, geo: Geometry, seed: number) {
  const { W, H, S } = geo;
  const rng = mulberry32(seed);
  const rand = (min: number, max: number) => min + rng() * (max - min);
  const bpx = (k: number) => (S * k).toFixed(2) + "px";

  c.fillStyle = NAVY;
  c.fillRect(0, 0, W, H);

  const base = c.createLinearGradient(0, 0, W, H);
  base.addColorStop(0, "#0b0a1a");
  base.addColorStop(0.5, NAVY);
  base.addColorStop(1, "#181640");
  c.fillStyle = base;
  c.fillRect(0, 0, W, H);

  radialFill(c, W, H, W * 0.15, H * 0.17, S * 0.7, [
    ["rgba(230, 138, 0, 0.16)", 0],
    ["rgba(255, 179, 71, 0.045)", 0.45],
    ["rgba(230, 138, 0, 0)", 1],
  ]);
  radialFill(c, W, H, W * 0.9, H * 0.86, S * 0.8, [
    ["rgba(127, 233, 255, 0.11)", 0],
    ["rgba(90, 169, 255, 0.04)", 0.42],
    ["rgba(127, 233, 255, 0)", 1],
  ]);
  radialFill(c, W, H, W * 0.55, H * 0.52, S * 0.92, [
    ["rgba(124, 92, 255, 0.11)", 0],
    ["rgba(124, 92, 255, 0.035)", 0.5],
    ["rgba(124, 92, 255, 0)", 1],
  ]);

  c.save();
  c.globalCompositeOperation = "screen";
  const fog: [number, number, number, string, number][] = [
    [W * 0.22, H * 0.3, S * 0.32, "#ffb347", 0.038],
    [W * 0.72, H * 0.62, S * 0.38, "#7fe9ff", 0.034],
    [W * 0.5, H * 0.84, S * 0.44, "#8a6bff", 0.038],
  ];
  for (const [x, y, r, col, a] of fog) {
    c.filter = `blur(${bpx(0.05)})`;
    gGlow(c, x, y, r, col, a);
  }
  c.filter = "none";
  c.restore();

  // dusk starfield
  c.save();
  c.globalCompositeOperation = "screen";
  for (let i = 0; i < 260; i++) {
    const x = rand(0, W);
    const y = rand(0, H);
    const r = rng() < 0.92 ? rand(0.35, 1.05) : rand(1.3, 1.9);
    const a = rng() < 0.86 ? rand(0.05, 0.28) : rand(0.36, 0.62);
    c.fillStyle = `rgba(255, 248, 225, ${a})`;
    c.beginPath();
    c.arc(x, y, r, 0, Math.PI * 2);
    c.fill();
  }
  c.restore();

  // ground reflection
  c.save();
  const g = c.createLinearGradient(0, H * 0.8, 0, H);
  g.addColorStop(0, "rgba(124, 92, 255, 0)");
  g.addColorStop(0.55, "rgba(120, 150, 200, 0.04)");
  g.addColorStop(1, "rgba(150, 180, 220, 0.08)");
  c.fillStyle = g;
  c.fillRect(0, H * 0.8, W, H * 0.2);
  c.restore();
}

// The backdrop and the grain tile never change across the loop, so they are
// built once per (size, seed) and reused for all 150 frames.
const backdropCache = new Map<string, HTMLCanvasElement>();
const grainCache = new Map<string, HTMLCanvasElement>();

function makeCanvas(w: number, h: number): HTMLCanvasElement {
  const el = document.createElement("canvas");
  el.width = w;
  el.height = h;
  return el;
}

export function getBackdrop(geo: Geometry, seed: number): HTMLCanvasElement {
  const key = `${geo.W}x${geo.H}:${seed}`;
  const hit = backdropCache.get(key);
  if (hit) return hit;
  const el = makeCanvas(geo.W, geo.H);
  const c = el.getContext("2d") as Ctx;
  c.imageSmoothingEnabled = true;
  c.imageSmoothingQuality = "high";
  paintBackdropInto(c, geo, seed);
  backdropCache.set(key, el);
  return el;
}

function getGrainTile(seed: number): HTMLCanvasElement {
  const key = String(seed);
  const hit = grainCache.get(key);
  if (hit) return hit;
  const size = 256;
  const el = makeCanvas(size, size);
  const c = el.getContext("2d") as Ctx;
  const img = c.createImageData(size, size);
  const rng = mulberry32(seed ^ 0x9e3779b9);
  for (let i = 0; i < size * size; i++) {
    const v = Math.floor(rng() * 255);
    const o = i * 4;
    img.data[o] = v;
    img.data[o + 1] = v;
    img.data[o + 2] = v;
    img.data[o + 3] = 255;
  }
  c.putImageData(img, 0, 0);
  grainCache.set(key, el);
  return el;
}

export function paintVignette(c: Ctx, geo: Geometry) {
  const { W, H, S } = geo;
  c.save();
  const v = c.createRadialGradient(W * 0.5, H * 0.48, S * 0.33, W * 0.5, H * 0.5, S * 1.02);
  v.addColorStop(0, "rgba(0, 0, 0, 0)");
  v.addColorStop(0.7, "rgba(4, 3, 12, 0.16)");
  v.addColorStop(1, "rgba(4, 3, 12, 0.56)");
  c.fillStyle = v;
  c.fillRect(0, 0, W, H);
  c.restore();
}

/**
 * Colour grade: warm lift into the top-left where the amber key sits, cool push
 * into the bottom-right shadows. Soft-light keeps it a grade rather than a wash,
 * so it shapes the existing colour instead of tinting flat over it.
 */
export function paintGrade(c: Ctx, geo: Geometry) {
  const { W, H } = geo;
  c.save();
  c.globalCompositeOperation = "soft-light";
  const g = c.createLinearGradient(0, 0, W, H);
  g.addColorStop(0, "rgba(255, 179, 71, 0.20)");
  g.addColorStop(0.5, "rgba(255, 248, 225, 0.03)");
  g.addColorStop(1, "rgba(90, 169, 255, 0.18)");
  c.fillStyle = g;
  c.fillRect(0, 0, W, H);
  c.restore();
}

/**
 * Film grain, held still for the whole loop. Animated grain is more filmic but
 * it changes every pixel on every frame, which is ruinous for a looping GIF.
 */
export function paintGrain(c: Ctx, geo: Geometry, seed: number) {
  const tile = getGrainTile(seed);
  c.save();
  c.globalAlpha = 0.046;
  c.globalCompositeOperation = "overlay";
  const pattern = c.createPattern(tile, "repeat");
  if (pattern) {
    c.fillStyle = pattern;
    c.fillRect(0, 0, geo.W, geo.H);
  }
  c.restore();
}

// ---------------------------------------------------------------------------

export interface TextBlock {
  /** e.g. "2.6.0" */
  version: string;
  /** e.g. "Clockwork" */
  codename: string;
  /** 0..1 position of the shimmer band travelling through the codename. */
  shimmer: number;
}

/**
 * Top-left title block: "Bazarr" in cream with a bold amber "+" superscript,
 * the released version, and the codename in amber. Ported from the static
 * heroes; the only addition is a highlight band that travels through the
 * codename once per loop.
 */
export function paintText(c: Ctx, geo: Geometry, font: string, block: TextBlock) {
  const { W, H } = geo;
  const padX = W * 0.085;
  const baseY = H * 0.165;
  const titleSize = Math.round(H * 0.086);

  c.save();
  c.fillStyle = CREAM;
  c.shadowColor = "rgba(0, 0, 0, 0.72)";
  c.shadowBlur = Math.round(H * 0.016);
  c.textBaseline = "top";
  c.font = `800 ${titleSize}px ${font}`;
  c.fillText("Bazarr", padX, baseY);

  const baseW = c.measureText("Bazarr").width;
  const plusSize = Math.round(titleSize * 0.72);
  c.font = `900 ${plusSize}px ${font}`;
  c.fillStyle = AMBER;
  c.fillText(
    "+",
    padX + baseW + Math.round(titleSize * 0.035),
    baseY - Math.round(titleSize * 0.045),
  );
  c.restore();

  c.save();
  c.shadowColor = "rgba(0, 0, 0, 0.74)";
  c.shadowBlur = Math.round(H * 0.014);
  c.textBaseline = "top";
  const subSize = Math.round(H * 0.037);
  c.font = `500 ${subSize}px ${font}`;
  c.fillStyle = "rgba(255, 248, 225, 0.94)";
  const subY = baseY + Math.round(H * 0.115);
  c.fillText(`V${block.version} Released`, padX, subY);

  const codeY = subY + Math.round(subSize * 1.48);
  c.fillText("Codename: ", padX, codeY);
  const cwidth = c.measureText("Codename: ").width;

  c.font = `700 ${subSize}px ${font}`;
  const nameX = padX + cwidth;
  const nameW = c.measureText(block.codename).width;
  c.fillStyle = AMBER;
  c.fillText(block.codename, nameX, codeY);

  // Highlight band: a narrow bright window slid across the word. It starts and
  // ends fully outside the glyphs, so the loop point shows no band at all.
  const travel = nameW * 2.2;
  const head = nameX - nameW * 0.6 + travel * block.shimmer;
  const bandW = nameW * 0.42;
  const grad = c.createLinearGradient(head - bandW, 0, head + bandW, 0);
  grad.addColorStop(0, withAlpha(AMBER, 0));
  // Warm highlight rather than white: the codename has to stay amber even at the
  // peak of the sweep, or the brand colour drops out of the frame for a beat.
  grad.addColorStop(0.5, withAlpha("#ffd89a", 0.62));
  grad.addColorStop(1, withAlpha(AMBER, 0));
  c.fillStyle = grad;
  c.fillText(block.codename, nameX, codeY);
  c.restore();
}
