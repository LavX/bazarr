/**
 * v2.7.0 "Atlas" motif: a globe built from its own printed pages.
 *
 * The codename is the brief. An atlas is a book of maps. A globe is made by
 * printing those maps as gores, pointed strips, and wrapping them onto a
 * sphere. v2.7.0 is the release that stops being limited to the media already
 * on the shelf: the catalogue is the whole sheet, the local library is one
 * strip. So the picture is not a globe with a map painted on it. It is a globe
 * visibly made of twelve gores, and the sheet is always being read.
 *
 * THE CARTOGRAPHY
 * Twelve gores, thirty degrees of longitude each. A gore is the lune between
 * two meridians, flattened with the classical sinusoidal (Sanson-Flamsteed)
 * equal-area strip projection. For a lune of half-width `dlon` about its
 * central meridian, a point at latitude `lat` sits at
 *
 *   x = dlon * cos(lat) * R
 *   y = lat * R
 *
 * on the printed page. Wrapped, the same (lat, lon) sits on the sphere of
 * radius R. The page uses that projection at a reading scale chosen so the
 * whole leaf stays inside the gimbal ring. The aspect ratio is the real gore's,
 * π / (2 dlon) = 6. Every graticule line is the image of the same line in both
 * states. The outline is that projection, not an eyeballed leaf.
 *
 * Each wrapped gore is its own leaf: fill inset from the meridians, seams
 * drawn on the true edges. When a gore is in transit it is not drawn on the
 * sphere, so a thirty-degree slot stays empty. Peel phase and spin are locked
 * so that slot sits at the left limb, next to the open page.
 *
 * WHAT MOVES
 * Each gore cycles continuously between wrapped and flat. Flatten is a period-1
 * pulse of (frame / dur + k / 12): unwrap, hold as a readable strip, re-wrap.
 * The pulse is just under two gore-slots wide, so one page is off the sphere
 * and its neighbours only overlap at the tails. The open strip sits against
 * the globe's left limb, fully inside the gimbal.
 *
 * The sphere turns one full revolution per loop, the same rate as the existing
 * Atlas motif. Each gore carries its peel as it rides around. Because peel and
 * spin are both period 1 in the raw frame, the gore that is open is always the
 * one at the left limb: lon(k, t) = k τ/12 - π/2 + t τ, and k peaks when
 * t = -k/12, which is exactly lon = -π/2.
 *
 * The east edge of a gore leads the west by a fraction of the pulse, decaying
 * with cos(lat) so the poles stay a single point. Intermediate positions
 * interpolate the two projections of the same (lat, lon). Travel is t^2, so a
 * gore stays on the sphere until late in the pulse. A parabolic lift toward
 * the camera keeps the page from tunneling through the sphere.
 *
 * WHAT FIXES EACH RATE
 *   gores           12, because a 30 degree gore is the classical globe-making
 *                   width and because 12 divides the 150-frame loop.
 *   peel cycles     one full unwrap-hold-wrap per gore per loop. Phase offset
 *                   k/12 puts the peel on the next gore every 12.5 frames.
 *   sphere spin     one turn per loop, (frame / dur) * τ, from the raw frame.
 *                   A 30 degree step is the wagon-wheel of twelve meridians.
 *                   A whole revolution is not: it passes through every phase
 *                   and lands on the identity.
 *   breath          two cycles per loop, a lighting function, not a rate of
 *                   the cartography.
 *
 * LOOP CONTRACT
 * The motif is driven from the raw frame. Flatten is period-1 in
 * (frame / dur + k / 12). Spin is period-1 in (frame / dur) * τ. Frame `dur`
 * is one full turn and one full peel from frame 0, so the pose matches.
 * Lighting folds into the period the way Clockwork's breath does: it is not
 * the train.
 */

import { AMBER_BRIGHT, BRAND_RAMP, CREAM, CYAN_PULSE } from "./theme";
import { clamp01, Ctx, gGlow, Geometry, smooth, withAlpha } from "./brand";

const TAU = Math.PI * 2;

export interface AtlasGoresFrame {
  frame: number;
  dur: number;
}

interface Vec3 {
  x: number;
  y: number;
  z: number;
}

const N_GORES = 12;
const DLON = TAU / N_GORES / 2;
/** Fill stops short of the true meridians so adjacent leaves show a seam. */
const SEAM = DLON * 0.16;

/** Earth-like axial tilt, static. The view does not spin. */
const AXIAL_TILT = (-23.4 * Math.PI) / 180;
const VIEW_ELEVATION = (18 * Math.PI) / 180;

const COS_TILT = Math.cos(AXIAL_TILT);
const SIN_TILT = Math.sin(AXIAL_TILT);
const COS_VIEW = Math.cos(VIEW_ELEVATION);
const SIN_VIEW = Math.sin(VIEW_ELEVATION);

/**
 * Fraction of a gore's cycle spent off the sphere. 0.155 is just under two
 * gore-slots, so the hold-flat of one gore meets its neighbours only at the
 * tails, where flatten is near 0. One page off the sphere at a time.
 */
const PEEL_WIDTH = 0.155;

/** East meridian leads the west, as a fraction of the cycle. */
const EDGE_LEAD = 0.028;

const LAT_STEPS = 52;
const PAR_STEPS = 12;

/**
 * Parallels printed on every gore, the same latitudes in both states.
 * Weight and a polar-to-core tint: poles lean cyan, equator leans cream.
 */
const PARALLELS: readonly { lat: number; weight: number; tint: number }[] = [
  { lat: 0, weight: 2.0, tint: 0.72 },
  { lat: (23.44 * Math.PI) / 180, weight: 1.35, tint: 0.58 },
  { lat: (-23.44 * Math.PI) / 180, weight: 1.35, tint: 0.58 },
  { lat: (30 * Math.PI) / 180, weight: 1.05, tint: 0.48 },
  { lat: (-30 * Math.PI) / 180, weight: 1.05, tint: 0.48 },
  { lat: (60 * Math.PI) / 180, weight: 1.0, tint: 0.28 },
  { lat: (-60 * Math.PI) / 180, weight: 1.0, tint: 0.28 },
  { lat: (66.56 * Math.PI) / 180, weight: 1.1, tint: 0.16 },
  { lat: (-66.56 * Math.PI) / 180, weight: 1.1, tint: 0.16 },
];

/** Local meridians drawn on a gore, as a fraction of dlon. Edges plus axis. */
const MERIDIANS: readonly { lonFrac: number; weight: number }[] = [
  { lonFrac: -1, weight: 1.7 },
  { lonFrac: -0.5, weight: 0.9 },
  { lonFrac: 0, weight: 1.15 },
  { lonFrac: 0.5, weight: 0.9 },
  { lonFrac: 1, weight: 1.7 },
];

/**
 * Beacons ride the gore they belong to. lonFrac is local longitude as a
 * fraction of dlon, in (-1, 1). Latitudes are radians. At least two per gore
 * so a page in transit is never an empty leaf.
 */
const BEACONS: readonly { gore: number; lat: number; lonFrac: number; size: number }[] = [
  { gore: 0, lat: 0.1, lonFrac: 0.22, size: 1.25 },
  { gore: 0, lat: -0.52, lonFrac: -0.34, size: 0.95 },
  { gore: 0, lat: 0.48, lonFrac: -0.18, size: 0.85 },
  { gore: 1, lat: 0.42, lonFrac: -0.12, size: 1.05 },
  { gore: 1, lat: -0.28, lonFrac: 0.36, size: 0.9 },
  { gore: 2, lat: -0.18, lonFrac: 0.4, size: 1.15 },
  { gore: 2, lat: 0.55, lonFrac: -0.22, size: 0.85 },
  { gore: 3, lat: 0.62, lonFrac: 0.16, size: 0.95 },
  { gore: 3, lat: -0.4, lonFrac: -0.3, size: 1.05 },
  { gore: 4, lat: -0.38, lonFrac: -0.26, size: 1.1 },
  { gore: 4, lat: 0.2, lonFrac: 0.32, size: 0.9 },
  { gore: 5, lat: 0.22, lonFrac: 0.3, size: 1.0 },
  { gore: 5, lat: -0.06, lonFrac: -0.44, size: 0.95 },
  { gore: 6, lat: -0.68, lonFrac: 0.06, size: 0.85 },
  { gore: 6, lat: 0.35, lonFrac: -0.28, size: 1.1 },
  { gore: 7, lat: 0.48, lonFrac: -0.4, size: 1.2 },
  { gore: 7, lat: -0.22, lonFrac: 0.18, size: 0.9 },
  { gore: 8, lat: -0.12, lonFrac: 0.2, size: 1.05 },
  { gore: 8, lat: 0.33, lonFrac: -0.28, size: 1.1 },
  { gore: 9, lat: 0.7, lonFrac: -0.14, size: 0.9 },
  { gore: 9, lat: -0.45, lonFrac: 0.3, size: 1.0 },
  { gore: 10, lat: -0.48, lonFrac: 0.36, size: 1.0 },
  { gore: 10, lat: 0.18, lonFrac: -0.2, size: 1.15 },
  { gore: 11, lat: 0.16, lonFrac: -0.2, size: 1.25 },
  { gore: 11, lat: 0.55, lonFrac: 0.38, size: 0.85 },
];

function fract(x: number): number {
  return x - Math.floor(x);
}

function rampRgb(t: number): [number, number, number] {
  const n = BRAND_RAMP.length - 1;
  const x = clamp01(t) * n;
  const i0 = Math.floor(x);
  const i1 = Math.min(i0 + 1, n);
  const f = x - i0;
  const c0 = BRAND_RAMP[i0];
  const c1 = BRAND_RAMP[i1];
  return [
    Math.round(c0[0] + (c1[0] - c0[0]) * f),
    Math.round(c0[1] + (c1[1] - c0[1]) * f),
    Math.round(c0[2] + (c1[2] - c0[2]) * f),
  ];
}

function rampColor(t: number): string {
  const [r, g, b] = rampRgb(t);
  return `rgb(${r}, ${g}, ${b})`;
}

/**
 * Brand ramp keyed to the composition: cyan at the sparse limb, cream through
 * the middle, amber at the core. A gore in transit is shifted warm.
 */
function goreTint(distFromCentre: number, R: number, flatten: number, polar = 0, z = 0): number {
  const rim = Math.pow(clamp01(1 - distFromCentre / (R * 1.08)), 0.85);
  const facing = clamp01(z / R);
  return clamp01(rim * 0.35 + facing * 0.4 + flatten * 0.55 - polar * 0.22);
}

/** Marks thin out near the title block, the same elliptical falloff Clockwork uses. */
function textFade(x: number, y: number, geo: Geometry): number {
  const ax = geo.W * 0.17;
  const ay = geo.H * 0.225;
  const R = geo.S * 0.28;
  const dx = (x - ax) / 1.3;
  const dy = y - ay;
  return smooth(clamp01(Math.hypot(dx, dy) / R));
}

function spherePoint(lat: number, lon: number, r: number): Vec3 {
  const cl = Math.cos(lat);
  const sl = Math.sin(lat);
  return {
    x: r * cl * Math.sin(lon),
    y: r * sl,
    z: r * cl * Math.cos(lon),
  };
}

function viewTransform(p: Vec3): Vec3 {
  const tx = p.x * COS_TILT - p.y * SIN_TILT;
  const ty = p.x * SIN_TILT + p.y * COS_TILT;
  const tz = p.z;
  return {
    x: tx,
    y: ty * COS_VIEW - tz * SIN_VIEW,
    z: ty * SIN_VIEW + tz * COS_VIEW,
  };
}

function toScreen(p: Vec3, cx: number, cy: number): Vec3 {
  return { x: cx + p.x, y: cy - p.y, z: p.z };
}

/**
 * Flatten amount, period 1 in `cycle`. 0 is wrapped, 1 is the printed strip.
 * Smoothstep on the way out and back, a hold in the middle so the page can be
 * read rather than only glanced at.
 */
function flattenAt(cycle: number): number {
  const s = fract(cycle);
  if (s <= 0 || s >= PEEL_WIDTH) return 0;
  const t = s / PEEL_WIDTH;
  const unwrapEnd = 0.28;
  const wrapStart = 0.72;
  if (t < unwrapEnd) return smooth(t / unwrapEnd);
  if (t > wrapStart) return 1 - smooth((t - wrapStart) / (1 - wrapStart));
  return 1;
}

interface Layout {
  geo: Geometry;
  cx: number;
  cy: number;
  R: number;
  RFlat: number;
  RGimbal: number;
  fx: number;
  fy: number;
  spin: number;
}

function goreLon0(k: number): number {
  // Gore 0 starts at the left limb (-pi/2). Locked to peel phase, the open
  // slot stays there for the whole loop.
  return (k / N_GORES) * TAU - Math.PI / 2;
}

function wrappedPoint(lat: number, lon: number, layout: Layout): Vec3 {
  return toScreen(viewTransform(spherePoint(lat, lon + layout.spin, layout.R)), layout.cx, layout.cy);
}

/** Slight clockwise lean, so the open page still belongs to the globe. */
const PAGE_LEAN = (4 * Math.PI) / 180;
const COS_LEAN = Math.cos(PAGE_LEAN);
const SIN_LEAN = Math.sin(PAGE_LEAN);

function flatPoint(lat: number, localLon: number, layout: Layout): Vec3 {
  const { R, RFlat, fx, fy } = layout;
  const x0 = RFlat * localLon * Math.cos(lat);
  const y0 = -RFlat * lat;
  return {
    x: fx + COS_LEAN * x0 - SIN_LEAN * y0,
    y: fy + SIN_LEAN * x0 + COS_LEAN * y0,
    z: R * 1.22,
  };
}

/**
 * Same (lat, localLon) in both states. `t` is the flatten amount in [0, 1].
 * Travel is t^2 so the gore stays on the sphere through most of the pulse and
 * only occupies the limb stand at the hold. The parabolic lift is 0 at both
 * endpoints, so the wrap and the page are exact.
 */
function morph(wrap: Vec3, flat: Vec3, t: number, R: number): Vec3 {
  if (t <= 0) return wrap;
  if (t >= 1) return flat;
  const u = t * t;
  const lift = 4 * u * (1 - u);
  return {
    x: wrap.x + (flat.x - wrap.x) * u,
    y: wrap.y + (flat.y - wrap.y) * u,
    z: wrap.z + (flat.z - wrap.z) * u + lift * R * 0.45,
  };
}

function goreCycle(frame: number, dur: number, k: number): number {
  // Peak flatten of gore 0 sits at frame 0 / frame dur. Gore 0 is parked at
  // the left limb, so the loop point is the open page beside its empty slot.
  return frame / dur + k / N_GORES + PEEL_WIDTH / 2;
}

function pointOnGore(lat: number, localLon: number, k: number, cycle: number, layout: Layout): Vec3 {
  const wrap = wrappedPoint(lat, goreLon0(k) + localLon, layout);
  const flat = flatPoint(lat, localLon, layout);
  // Lead decays with cos(lat) so the poles stay a single point. At ±90 degrees
  // there is no east edge to unroll from.
  const t = flattenAt(cycle - EDGE_LEAD * (localLon / DLON) * Math.cos(lat));
  return morph(wrap, flat, t, layout.R);
}

function goreFlatten(_k: number, cycle: number): number {
  return flattenAt(cycle);
}

function depthAlpha(z: number, R: number, flatten: number): number {
  const facing = clamp01(0.35 + 0.65 * (z / R));
  const wrapped = 0.18 + 0.82 * facing;
  return wrapped + (1 - wrapped) * flatten;
}

function drawGlows(c: Ctx, geo: Geometry, cx: number, cy: number, pulse: number) {
  const { S } = geo;
  c.save();
  c.globalCompositeOperation = "screen";
  gGlow(c, cx, cy, S * 0.46, "#8a6bff", 0.052);
  gGlow(c, cx - S * 0.22, cy + S * 0.16, S * 0.32, "#7fe9ff", 0.042);
  gGlow(c, cx + S * 0.16, cy - S * 0.14, S * 0.28, "#ffb347", 0.048);
  gGlow(c, cx, cy, S * 0.2, "#ffce92", 0.06 + 0.05 * pulse);
  c.restore();
}

function drawGlobeInterior(c: Ctx, layout: Layout) {
  const { cx, cy, R } = layout;
  c.save();
  c.globalCompositeOperation = "screen";
  const g = c.createRadialGradient(cx - R * 0.2, cy - R * 0.26, R * 0.05, cx, cy, R);
  g.addColorStop(0, "rgba(255, 206, 146, 0.07)");
  g.addColorStop(0.55, "rgba(127, 233, 255, 0.03)");
  g.addColorStop(1, "rgba(12, 10, 28, 0)");
  c.fillStyle = g;
  c.beginPath();
  c.arc(cx, cy, R, 0, TAU);
  c.fill();
  c.restore();
}

/** Outer meridian gimbal and degree ticks, ported from the Atlas instrument. */
function drawOuterGimbal(c: Ctx, layout: Layout) {
  const { cx, cy, RGimbal, geo } = layout;
  const { S } = geo;
  const radius = RGimbal;
  c.save();

  c.beginPath();
  c.arc(cx, cy, radius, 0, TAU);
  c.lineWidth = Math.max(1.5, S * 0.0028);
  c.strokeStyle = withAlpha(CREAM, 0.48);
  c.stroke();

  const innerR = radius - S * 0.012;
  c.beginPath();
  c.arc(cx, cy, innerR, 0, TAU);
  c.lineWidth = Math.max(1, S * 0.0016);
  c.strokeStyle = withAlpha(CREAM, 0.24);
  c.stroke();

  const ticks = 72;
  for (let i = 0; i < ticks; i++) {
    const a = (i / ticks) * TAU;
    const isMajor = i % 2 === 0;
    const isQuadrant = i % 18 === 0;
    const len = isQuadrant ? S * 0.014 : isMajor ? S * 0.009 : S * 0.005;
    const r1 = innerR;
    const r2 = innerR - len;
    const cosA = Math.cos(a);
    const sinA = Math.sin(a);

    c.beginPath();
    c.moveTo(cx + cosA * r1, cy + sinA * r1);
    c.lineTo(cx + cosA * r2, cy + sinA * r2);
    c.lineWidth = Math.max(1, isMajor ? S * 0.0018 : S * 0.001);
    c.strokeStyle = withAlpha(isQuadrant ? AMBER_BRIGHT : CREAM, isQuadrant ? 0.75 : isMajor ? 0.45 : 0.25);
    c.stroke();
  }

  const poleDist = radius + S * 0.016;
  const pTop = {
    x: cx + Math.sin(-AXIAL_TILT) * poleDist,
    y: cy - Math.cos(-AXIAL_TILT) * poleDist,
  };
  const pBot = {
    x: cx - Math.sin(-AXIAL_TILT) * poleDist,
    y: cy + Math.cos(-AXIAL_TILT) * poleDist,
  };

  c.globalCompositeOperation = "screen";
  gGlow(c, pTop.x, pTop.y, S * 0.024, AMBER_BRIGHT, 0.5);
  gGlow(c, pBot.x, pBot.y, S * 0.024, AMBER_BRIGHT, 0.5);

  c.beginPath();
  c.arc(pTop.x, pTop.y, S * 0.004, 0, TAU);
  c.arc(pBot.x, pBot.y, S * 0.004, 0, TAU);
  c.fillStyle = "#ffffff";
  c.fill();

  c.restore();
}

function drawCore(c: Ctx, geo: Geometry, cx: number, cy: number, pulse: number, breath: number) {
  const { S } = geo;
  c.save();
  c.globalCompositeOperation = "screen";
  const swell = 1 + 0.12 * pulse + 0.04 * breath;
  gGlow(c, cx, cy, S * 0.082 * swell, "#ffb347", 0.32 + 0.22 * pulse);
  gGlow(c, cx, cy, S * 0.034 * swell, "#fff3d0", 0.5 + 0.25 * pulse);
  gGlow(c, cx, cy, S * 0.012 * swell, "#ffffff", 0.85 + 0.15 * pulse);
  c.beginPath();
  c.arc(cx, cy, S * 0.0055 * swell, 0, TAU);
  c.fillStyle = "#ffffff";
  c.shadowColor = AMBER_BRIGHT;
  c.shadowBlur = S * 0.015;
  c.fill();
  c.restore();
}

function strokePoly(c: Ctx, pts: Vec3[], color: string, alpha: number, width: number) {
  if (pts.length < 2 || alpha < 0.018) return;
  c.save();
  c.globalCompositeOperation = "screen";
  c.strokeStyle = color.startsWith("rgba") ? color : withAlpha(color, alpha);
  c.lineWidth = width;
  c.lineJoin = "round";
  c.lineCap = "round";
  c.beginPath();
  c.moveTo(pts[0].x, pts[0].y);
  for (let i = 1; i < pts.length; i++) c.lineTo(pts[i].x, pts[i].y);
  c.stroke();
  c.restore();
}

function sampleMeridian(k: number, cycle: number, layout: Layout, localLon: number): Vec3[] {
  const pts: Vec3[] = [];
  for (let i = 0; i <= LAT_STEPS; i++) {
    const lat = -Math.PI / 2 + (i / LAT_STEPS) * Math.PI;
    pts.push(pointOnGore(lat, localLon, k, cycle, layout));
  }
  return pts;
}

function sampleParallel(k: number, cycle: number, layout: Layout, lat: number): Vec3[] {
  const pts: Vec3[] = [];
  for (let i = 0; i <= PAR_STEPS; i++) {
    const localLon = -DLON + (i / PAR_STEPS) * 2 * DLON;
    pts.push(pointOnGore(lat, localLon, k, cycle, layout));
  }
  return pts;
}

function drawGoreFill(
  c: Ctx,
  k: number,
  cycle: number,
  layout: Layout,
  flatten: number,
  vis: number,
) {
  const { geo, R, cx, cy } = layout;
  const inset = DLON - SEAM;
  const outline: Vec3[] = [];
  for (let i = 0; i <= LAT_STEPS; i++) {
    const lat = -Math.PI / 2 + (i / LAT_STEPS) * Math.PI;
    outline.push(pointOnGore(lat, -inset, k, cycle, layout));
  }
  for (let i = 1; i <= LAT_STEPS; i++) {
    const lat = Math.PI / 2 - (i / LAT_STEPS) * Math.PI;
    outline.push(pointOnGore(lat, inset, k, cycle, layout));
  }

  const mid = pointOnGore(0, 0, k, cycle, layout);
  const fade = textFade(mid.x, mid.y, geo);
  if (fade < 0.03) return;

  const dist = Math.hypot(mid.x - cx, mid.y - cy);
  const tint = goreTint(dist, R, flatten, 0, mid.z);
  const facing = clamp01(0.25 + 0.75 * (mid.z / R));
  const paper = (0.045 + 0.07 * facing + 0.08 * flatten) * vis * fade;
  c.save();
  c.globalCompositeOperation = "screen";
  c.beginPath();
  c.moveTo(outline[0].x, outline[0].y);
  for (let i = 1; i < outline.length; i++) c.lineTo(outline[i].x, outline[i].y);
  c.closePath();
  c.fillStyle = withAlpha(rampColor(tint), paper);
  c.fill();
  c.restore();

  if (flatten > 0.4) {
    c.save();
    c.globalCompositeOperation = "screen";
    gGlow(c, mid.x, mid.y, R * 0.2, AMBER_BRIGHT, 0.07 * flatten * vis);
    c.restore();
  }
}

function drawGoreSeams(
  c: Ctx,
  k: number,
  cycle: number,
  layout: Layout,
  flatten: number,
  vis: number,
) {
  const { geo, R, cx, cy } = layout;
  const { S } = geo;
  const mid = pointOnGore(0, 0, k, cycle, layout);
  const fade = textFade(mid.x, mid.y, geo);
  if (fade < 0.03) return;
  const dist = Math.hypot(mid.x - cx, mid.y - cy);
  const tint = goreTint(dist, R, flatten, 0, mid.z);
  const a = (0.42 + 0.4 * flatten) * vis * fade;
  const w = Math.max(1.15, S * (0.0018 + 0.001 * flatten));
  const [r, g, b] = rampRgb(tint);
  const col = `rgba(${r}, ${g}, ${b}, ${a})`;
  strokePoly(c, sampleMeridian(k, cycle, layout, -DLON), col, a, w);
  strokePoly(c, sampleMeridian(k, cycle, layout, DLON), col, a, w);
}

function drawGoreGraticule(
  c: Ctx,
  k: number,
  cycle: number,
  layout: Layout,
  flatten: number,
  vis: number,
) {
  const { geo, R, cx, cy } = layout;
  const { S } = geo;
  const mid = pointOnGore(0, 0, k, cycle, layout);
  const fade = textFade(mid.x, mid.y, geo);
  if (fade < 0.03) return;
  const dist = Math.hypot(mid.x - cx, mid.y - cy);
  const a = vis * fade;
  const print = 0.42 + 0.58 * flatten;

  for (const p of PARALLELS) {
    const tint = goreTint(dist, R, flatten, 1 - p.tint, mid.z);
    const [r, g, b] = rampRgb(tint);
    const alpha = (0.28 + 0.5 * flatten) * p.weight * 0.45 * a * print;
    strokePoly(
      c,
      sampleParallel(k, cycle, layout, p.lat),
      `rgba(${r}, ${g}, ${b}, ${alpha})`,
      alpha,
      Math.max(1, S * 0.0011 * p.weight),
    );
  }

  for (const m of MERIDIANS) {
    if (Math.abs(m.lonFrac) === 1) continue;
    const tint = goreTint(dist, R, flatten, 0, mid.z);
    const [r, g, b] = rampRgb(tint);
    const alpha = 0.22 * m.weight * a * print;
    strokePoly(
      c,
      sampleMeridian(k, cycle, layout, m.lonFrac * DLON),
      `rgba(${r}, ${g}, ${b}, ${alpha})`,
      alpha,
      Math.max(0.9, S * 0.001 * m.weight),
    );
  }
}

function drawBeaconsOnGore(
  c: Ctx,
  k: number,
  cycle: number,
  layout: Layout,
  flatten: number,
  vis: number,
  pulse: number,
) {
  const { geo, R } = layout;
  const { S } = geo;
  c.save();
  c.globalCompositeOperation = "screen";
  for (const b of BEACONS) {
    if (b.gore !== k) continue;
    const pt = pointOnGore(b.lat, b.lonFrac * DLON, k, cycle, layout);
    const fade = textFade(pt.x, pt.y, geo);
    if (fade < 0.04) continue;
    const depth = depthAlpha(pt.z, R, flatten);
    const alpha = (0.62 + 0.38 * flatten) * vis * depth * fade;
    const polar = Math.abs(b.lat) / (Math.PI / 2);
    const tRamp = goreTint(Math.hypot(pt.x - layout.cx, pt.y - layout.cy), R, flatten, polar, pt.z);
    const color = rampColor(tRamp);
    const nodeR = S * 0.0044 * b.size * (0.75 + 0.35 * depth) * (1 + 0.15 * flatten);

    gGlow(c, pt.x, pt.y, S * 0.02 * b.size * (0.75 + 0.35 * depth), color, alpha * 0.55);

    c.beginPath();
    c.arc(pt.x, pt.y, Math.max(1.15, nodeR), 0, TAU);
    c.fillStyle = withAlpha("#ffffff", alpha);
    c.fill();

    if (b.size >= 1.05) {
      const spike = S * 0.012 * b.size * (1 + 0.12 * pulse) * (0.55 + 0.45 * flatten);
      c.beginPath();
      c.moveTo(pt.x - spike, pt.y);
      c.lineTo(pt.x + spike, pt.y);
      c.moveTo(pt.x, pt.y - spike);
      c.lineTo(pt.x, pt.y + spike);
      c.lineWidth = Math.max(0.8, S * 0.00085);
      c.strokeStyle = withAlpha(color, alpha * 0.62);
      c.stroke();
    }
  }
  c.restore();
}

function sampleWrappedMeridian(k: number, localLon: number, layout: Layout): Vec3[] {
  const pts: Vec3[] = [];
  const lon = goreLon0(k) + localLon;
  for (let i = 0; i <= LAT_STEPS; i++) {
    const lat = -Math.PI / 2 + (i / LAT_STEPS) * Math.PI;
    pts.push(wrappedPoint(lat, lon, layout));
  }
  return pts;
}

function strokeFront(c: Ctx, pts: Vec3[], color: string, alpha: number, width: number, R: number) {
  const runs: Vec3[][] = [];
  let run: Vec3[] = [];
  for (const p of pts) {
    if (p.z >= -0.04 * R) {
      run.push(p);
    } else if (run.length) {
      runs.push(run);
      run = [];
    }
  }
  if (run.length) runs.push(run);
  for (const r of runs) strokePoly(c, r, color, alpha, width);
}

/** The missing lune, outlined on the sphere so the gap is the story. */
function drawEmptySlot(c: Ctx, k: number, layout: Layout) {
  const { geo, R } = layout;
  const { S } = geo;
  const west = sampleWrappedMeridian(k, -DLON, layout);
  const east = sampleWrappedMeridian(k, DLON, layout);
  const mid = wrappedPoint(0, goreLon0(k), layout);
  c.save();
  c.globalCompositeOperation = "screen";
  gGlow(c, mid.x, mid.y, R * 0.14, CYAN_PULSE, 0.14);
  c.restore();
  const w = Math.max(1.6, S * 0.0026);
  strokeFront(c, west, CYAN_PULSE, 0.72, w, R);
  strokeFront(c, east, CREAM, 0.78, w, R);
  strokeFront(c, west, CREAM, 0.32, Math.max(1, S * 0.0012), R);
  strokeFront(c, east, AMBER_BRIGHT, 0.28, Math.max(1, S * 0.0012), R);
}

function drawGore(c: Ctx, k: number, cycle: number, layout: Layout, pulse: number) {
  const flatten = goreFlatten(k, cycle);
  const mid = pointOnGore(0, 0, k, cycle, layout);
  const vis = depthAlpha(mid.z, layout.R, flatten);
  drawGoreFill(c, k, cycle, layout, flatten, vis);
  drawGoreSeams(c, k, cycle, layout, flatten, vis);
  drawGoreGraticule(c, k, cycle, layout, flatten, vis);
  drawBeaconsOnGore(c, k, cycle, layout, flatten, vis, pulse);
}

export function drawAtlasGores(c: Ctx, geo: Geometry, { frame, dur }: AtlasGoresFrame) {
  const { W, H, S } = geo;

  const cx = W * 0.65;
  const cy = H * 0.5;
  const R = S * 0.355;
  const RGimbal = S * 0.42;
  // Reading scale: the whole leaf must sit inside the gimbal circle.
  const RFlat = S * 0.175;
  const fx = cx - R * 0.78;
  const fy = cy;

  const spin = (frame / dur) * TAU;
  const layout: Layout = { geo, cx, cy, R, RFlat, RGimbal, fx, fy, spin };

  const phase = ((frame % dur) + dur) % dur;
  const pulse = 0.5 + 0.5 * Math.sin((phase / dur) * TAU);
  const breath = Math.sin(TAU * 2 * (phase / dur));

  drawGlows(c, geo, cx, cy, pulse);
  drawOuterGimbal(c, layout);

  const meta = Array.from({ length: N_GORES }, (_, k) => {
    const cycle = goreCycle(frame, dur, k);
    const wrapped = wrappedPoint(0, goreLon0(k), layout);
    return { k, cycle, z: wrapped.z, flatten: goreFlatten(k, cycle) };
  });

  const peeling = meta.filter((g) => g.flatten > 0.16);
  const resting = meta.filter((g) => g.flatten <= 0.16);
  const back = resting.filter((g) => g.z < 0).sort((a, b) => a.z - b.z);
  const front = resting.filter((g) => g.z >= 0).sort((a, b) => a.z - b.z);

  for (const g of back) drawGore(c, g.k, g.cycle, layout, pulse);
  drawGlobeInterior(c, layout);
  drawCore(c, geo, cx, cy, pulse, breath);
  for (const g of front) drawGore(c, g.k, g.cycle, layout, pulse);
  for (const g of peeling) drawEmptySlot(c, g.k, layout);
  peeling.sort((a, b) => a.flatten - b.flatten);
  for (const g of peeling) drawGore(c, g.k, g.cycle, layout, pulse);
}
