/**
 * v2.7.0 "Atlas" motif: the world, turning, and the light finding every place.
 *
 * The codename is the brief. Atlas is the book of maps, and the thing this
 * release does is stop caring what is on your own shelf: Discover reaches the
 * whole catalogue, every media server is one kind of thing, and the app works
 * with no library at all. So the picture is the world itself, drawn from real
 * geography, turning under a fixed lamp, and a light that touches one real
 * city after another as the rotation brings each of them round to the front.
 *
 * Like Clockwork, it has to actually BE the thing. The coastlines are Natural
 * Earth at 1:110m, the projection is orthographic, the axis leans the 23.4
 * degrees a library globe leans, the day side is where the lamp says it is, and
 * every hop between cities is a great circle. Nothing here is a shape that
 * merely resembles a globe.
 *
 * WHAT MOVES
 * The globe turns one full revolution per loop at constant angular velocity,
 * eastward, the way the Earth turns. That is the one un-eased motion in the
 * composition and it is the correct physics: a planet does not ease. Under a
 * fixed lamp that means the continents cross the terminator as they go, from
 * cyan night through cream dawn into amber day, which is what makes a turning
 * sphere of lines legible as a turning sphere.
 *
 * THE BEAT
 * Twenty-five beats per loop, one every six frames, the same escapement cadence
 * as Clockwork and for the same reason: six divides by both the GIF's and the
 * WebP's frame decimation, so the pulse survives the formats people actually
 * see. On each beat a city blooms and a thread of great circle arrives at it
 * from the city before. The bloom is the only stepped event; the world keeps
 * turning through it.
 *
 * WHY THE LIGHT STAYS IN FRONT
 * One revolution over twenty-five beats is 14.4 degrees of longitude per beat.
 * So the meridian facing the viewer at beat k is 14.4 k degrees west of the one
 * facing at beat 0. The cities are chosen to sit on those meridians, within a
 * few degrees, working westward from Auckland: city k is the real place that
 * has just arrived at centre stage when beat k fires. The globe turns; the
 * light does not travel round it, the world brings each place to the light.
 * That is the Clockwork idea again, where only the light travelled, inverted.
 *
 * LOOP CONTRACT
 * The rotation is (frame / dur) * 2 PI from the raw frame, so frame dur is one
 * whole turn from frame 0 and lands on the identity. The beat schedule is
 * periodic by construction: BEATS_PER_LOOP beats of dur / BEATS_PER_LOOP frames
 * fill the loop exactly, so a bloom's age at frame dur is its age at frame 0.
 * The wagon-wheel check: the graticule repeats every 15 degrees, a beat turns
 * the globe 14.4, so no beat lands the pattern back on itself. Coastlines have
 * no period at all, which is the real reason a turning Earth never stutters.
 */

import { feature } from "topojson-client";
import type { MultiPolygon, Polygon } from "geojson";
import type { Topology } from "topojson-specification";
import land110 from "world-atlas/land-110m.json";

import { AMBER_BRIGHT, BEATS_PER_LOOP, BRAND_RAMP, CREAM, CYAN_PULSE, EASE } from "./theme";
import { clamp01, Ctx, gGlow, Geometry, smooth, withAlpha } from "./brand";

const TAU = Math.PI * 2;
const DEG = Math.PI / 180;

export interface AtlasFrame {
  frame: number;
  dur: number;
}

// --- the instrument ------------------------------------------------------------

/** Globe radius as a fraction of S. Every other size derives from it. */
const GLOBE_R = 0.35;

/** Where the globe sits: right of centre, the left third kept for the words. */
const CENTRE_X = 0.655;
const CENTRE_Y = 0.53;

/** Axial tilt, north pole leaning to the viewer's left. */
const AXIS_TILT = 23.4 * DEG;

/** The viewer looks slightly down onto the northern hemisphere. */
const VIEW_ELEVATION = 14 * DEG;

/** One whole turn per loop, eastward. */
const REVS_PER_LOOP = 1;

/**
 * The lamp, in view space: from the viewer's upper left, in front of the globe.
 * Fixed for the whole loop, so the terminator holds still while the world turns
 * through it.
 */
const LAMP = normalise([-0.62, 0.42, 0.66]);

/** Meridian ring radius relative to the globe, and its graduations. */
const RING_R = 1.075;
const RING_TICKS = 72; // every 5 degrees, a heavier one every 15

// --- the itinerary -------------------------------------------------------------

/**
 * Twenty-five real cities, one per beat, working westward from Auckland. The
 * slot column is the longitude facing the viewer when that beat fires; the
 * error is how far the city sits from it. Nothing is further than a dozen
 * degrees off centre stage, which keeps every bloom on the front of the globe
 * with room to spare, and the latitudes are deliberately spread so the threads
 * between neighbours curve rather than all running along the equator.
 *
 *   k  city           lat     lon     slot    error
 *   0  Auckland      -36.8   174.8   174.8    0.0
 *   1  Brisbane      -27.5   153.0   160.4   -7.4
 *   2  Tokyo          35.7   139.7   146.0   -6.3
 *   3  Seoul          37.6   127.0   131.6   -4.6
 *   4  Hong Kong      22.3   114.2   117.2   -3.0
 *   5  Singapore       1.4   103.8   102.8    1.0
 *   6  Kolkata        22.6    88.4    88.4    0.0
 *   7  Mumbai         19.1    72.9    74.0   -1.1
 *   8  Dubai          25.2    55.3    59.6   -4.3
 *   9  Tbilisi        41.7    44.8    45.2   -0.4
 *  10  Cairo          30.0    31.2    30.8    0.4
 *  11  Budapest       47.5    19.0    16.4    2.6
 *  12  Paris          48.9     2.4     2.0    0.4
 *  13  Lisbon         38.7    -9.1   -12.4    3.3
 *  14  Reykjavik      64.1   -21.9   -26.8    4.9
 *  15  Rio de Janeiro -22.9  -43.2   -41.2   -2.0
 *  16  Buenos Aires  -34.6   -58.4   -55.6   -2.8
 *  17  New York       40.7   -74.0   -70.0   -4.0
 *  18  Chicago        41.9   -87.6   -84.4   -3.2
 *  19  Mexico City    19.4   -99.1   -98.8   -0.3
 *  20  Los Angeles    34.1  -118.2  -113.2   -5.0
 *  21  Vancouver      49.3  -123.1  -127.6    4.5
 *  22  Anchorage      61.2  -149.9  -142.0   -7.9
 *  23  Honolulu       21.3  -157.9  -156.4   -1.5
 *  24  Apia          -13.8  -171.8  -170.8   -1.0
 */
const CITIES: readonly [number, number][] = [
  [-36.8, 174.8],
  [-27.5, 153.0],
  [35.7, 139.7],
  [37.6, 127.0],
  [22.3, 114.2],
  [1.4, 103.8],
  [22.6, 88.4],
  [19.1, 72.9],
  [25.2, 55.3],
  [41.7, 44.8],
  [30.0, 31.2],
  [47.5, 19.0],
  [48.9, 2.4],
  [38.7, -9.1],
  [64.1, -21.9],
  [-22.9, -43.2],
  [-34.6, -58.4],
  [40.7, -74.0],
  [41.9, -87.6],
  [19.4, -99.1],
  [34.1, -118.2],
  [49.3, -123.1],
  [61.2, -149.9],
  [21.3, -157.9],
  [-13.8, -171.8],
];

/** The longitude facing the viewer at frame 0: city 0 at centre stage. */
const FRONT_LON_AT_ZERO = CITIES[0][1] * DEG;

/** A bloom rises inside two frames and is gone within about a beat and a half. */
const BLOOM_ATTACK = 2;
const BLOOM_DECAY = 9;

/** A thread takes most of a beat to arrive and lingers for two more. */
const THREAD_TRAVEL = 5;
const THREAD_DECAY = 14;

// --- geometry ------------------------------------------------------------------

type Vec3 = [number, number, number];

function normalise(v: Vec3): Vec3 {
  const n = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / n, v[1] / n, v[2] / n];
}

/**
 * A point on the unit sphere in the globe's own frame: y north, lon 0 on +x,
 * and longitude increasing as a positive (counter-clockwise from the north)
 * rotation about y, which is what makes east come out on the right once the
 * viewer looks in along +z. Get that sign wrong and the Earth renders mirrored,
 * with Madagascar west of Africa; it did, once.
 */
function unit(latDeg: number, lonDeg: number): Vec3 {
  const lat = latDeg * DEG;
  const lon = lonDeg * DEG;
  const c = Math.cos(lat);
  return [c * Math.cos(lon), Math.sin(lat), -c * Math.sin(lon)];
}

/**
 * The view transform for one frame: spin about the polar axis, lean the axis,
 * tip the view. Returns screen x, screen y and depth toward the viewer, the
 * last on the unit sphere so it doubles as the surface normal's z.
 */
class View {
  private readonly cs: number;
  private readonly sn: number;
  private readonly ct = Math.cos(AXIS_TILT);
  private readonly st = Math.sin(AXIS_TILT);
  private readonly ce = Math.cos(VIEW_ELEVATION);
  private readonly se = Math.sin(VIEW_ELEVATION);

  constructor(
    spin: number,
    readonly cx: number,
    readonly cy: number,
    readonly R: number,
  ) {
    this.cs = Math.cos(spin);
    this.sn = Math.sin(spin);
  }

  /** Unit-sphere point to view space: x right, y up, z toward the viewer. */
  toView(p: Vec3): Vec3 {
    const x1 = p[0] * this.cs + p[2] * this.sn;
    const z1 = -p[0] * this.sn + p[2] * this.cs;
    const y1 = p[1];
    const x2 = x1 * this.ct - y1 * this.st;
    const y2 = x1 * this.st + y1 * this.ct;
    const y3 = y2 * this.ce - z1 * this.se;
    const z3 = y2 * this.se + z1 * this.ce;
    return [x2, y3, z3];
  }

  sx(v: Vec3): number {
    return this.cx + v[0] * this.R;
  }

  sy(v: Vec3): number {
    return this.cy - v[1] * this.R;
  }
}

/**
 * The spin that puts a given longitude at centre stage. A point at longitude L
 * is R_y(L) applied to (1, 0, 0); after a further R_y(spin) it sits at
 * (cos(L + spin), 0, -sin(L + spin)), which faces the viewer, +z, when
 * L + spin = -90 degrees. The lean and the tip that follow move the front
 * meridian up and sideways a little, never off the face.
 */
function spinFor(frontLon: number): number {
  return -Math.PI / 2 - frontLon;
}

/** Spherical interpolation between two unit vectors, the great-circle route. */
function slerp(a: Vec3, b: Vec3, t: number): Vec3 {
  const d = clamp01(a[0] * b[0] + a[1] * b[1] + a[2] * b[2]);
  const omega = Math.acos(d);
  if (omega < 1e-6) return a;
  const so = Math.sin(omega);
  const wa = Math.sin((1 - t) * omega) / so;
  const wb = Math.sin(t * omega) / so;
  return [a[0] * wa + b[0] * wb, a[1] * wa + b[1] * wb, a[2] * wa + b[2] * wb];
}

// --- the map -------------------------------------------------------------------

/**
 * Natural Earth land at 1:110m, decoded once at module load into unit vectors.
 * Every ring is a coastline, holes included: an inland sea's shore is a shore.
 * 126 rings, 5,123 points, which is coarse enough to read at 800 pixels wide
 * and fine enough that Italy still has a boot.
 */
const COAST: Vec3[][] = (() => {
  const topo = land110 as unknown as Topology;
  const decoded = feature(topo, topo.objects.land as never) as unknown as {
    type: string;
    features?: { geometry: MultiPolygon | Polygon }[];
    geometry?: MultiPolygon | Polygon;
  };
  const geoms: (MultiPolygon | Polygon)[] = decoded.features
    ? decoded.features.map((f) => f.geometry)
    : decoded.geometry
      ? [decoded.geometry]
      : [];
  const rings: Vec3[][] = [];
  for (const g of geoms) {
    const polys = g.type === "Polygon" ? [g.coordinates] : g.coordinates;
    for (const poly of polys) {
      for (const ring of poly) {
        rings.push(ring.map(([lon, lat]) => unit(lat, lon)));
      }
    }
  }
  return rings;
})();

/** Meridians and parallels every 15 degrees, sampled every 2. */
const GRATICULE: Vec3[][] = (() => {
  const lines: Vec3[][] = [];
  for (let lon = -180; lon < 180; lon += 15) {
    const line: Vec3[] = [];
    for (let lat = -90; lat <= 90; lat += 2) line.push(unit(lat, lon));
    lines.push(line);
  }
  for (let lat = -75; lat <= 75; lat += 15) {
    const line: Vec3[] = [];
    for (let lon = -180; lon <= 180; lon += 2) line.push(unit(lat, lon));
    lines.push(line);
  }
  return lines;
})();

const CITY_UNITS: Vec3[] = CITIES.map(([lat, lon]) => unit(lat, lon));

// --- light ---------------------------------------------------------------------

function rampAt(t: number): [number, number, number] {
  const n = BRAND_RAMP.length - 1;
  const x = clamp01(t) * n;
  const i = Math.min(n - 1, Math.floor(x));
  const f = x - i;
  const a = BRAND_RAMP[i];
  const b = BRAND_RAMP[i + 1];
  return [
    Math.round(a[0] + (b[0] - a[0]) * f),
    Math.round(a[1] + (b[1] - a[1]) * f),
    Math.round(a[2] + (b[2] - a[2]) * f),
  ];
}

/**
 * How lit a surface point is, 0 in full night and 1 under the lamp. The
 * terminator is softened over a band rather than cut, because a hard line of
 * colour change on a wireframe reads as a rendering seam, not as dusk.
 */
function daylight(v: Vec3): number {
  const lit = v[0] * LAMP[0] + v[1] * LAMP[1] + v[2] * LAMP[2];
  return smooth(clamp01((lit + 0.42) / 1.25));
}

/** Colour and strength for a stroke at a surface point. */
function shade(v: Vec3): { rgb: [number, number, number]; k: number } {
  const day = daylight(v);
  // Cool at night, cream by day, amber only under the lamp itself.
  const rgb = rampAt(0.08 + 0.92 * day);
  // Night strokes are dim but never gone: the dark side of a globe is still there.
  // The limb term keeps the rim from going flat, which the eye reads as depth.
  const limb = 0.55 + 0.45 * clamp01(v[2]);
  return { rgb, k: (0.28 + 0.72 * day) * limb };
}

// --- drawing -------------------------------------------------------------------

/**
 * A polyline on the sphere, drawn one segment at a time so every segment can
 * carry its own daylight. Segments with either end behind the globe are simply
 * not drawn: the orthographic limb is the true horizon.
 */
function strokeLine(
  c: Ctx,
  view: View,
  line: Vec3[],
  width: number,
  alpha: number,
  halo = 0,
) {
  const pts = line.map((p) => view.toView(p));
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1];
    const b = pts[i];
    if (a[2] <= 0.005 || b[2] <= 0.005) continue;
    const mid: Vec3 = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2];
    const { rgb, k } = shade(mid);
    const A = alpha * k;
    if (A < 0.006) continue;
    const x0 = view.sx(a);
    const y0 = view.sy(a);
    const x1 = view.sx(b);
    const y1 = view.sy(b);
    if (halo > 0) {
      c.strokeStyle = `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${A * 0.16})`;
      c.lineWidth = halo;
      c.beginPath();
      c.moveTo(x0, y0);
      c.lineTo(x1, y1);
      c.stroke();
    }
    c.strokeStyle = `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${A})`;
    c.lineWidth = width;
    c.beginPath();
    c.moveTo(x0, y0);
    c.lineTo(x1, y1);
    c.stroke();
  }
}

function drawAtmosphere(c: Ctx, view: View) {
  const { cx, cy, R } = view;
  c.save();
  c.globalCompositeOperation = "screen";
  // The night side's cool haze, and the lamp's warmth pooled where it falls.
  gGlow(c, cx, cy, R * 1.42, CYAN_PULSE, 0.075);
  gGlow(c, cx + LAMP[0] * R * 0.55, cy - LAMP[1] * R * 0.55, R * 1.05, AMBER_BRIGHT, 0.11);
  // The disc itself, barely: enough that the back of the graticule is not sky.
  gGlow(c, cx, cy, R, "#2a2a55", 0.5);
  c.restore();
}

function drawLimb(c: Ctx, view: View) {
  const { cx, cy, R } = view;
  c.save();
  c.globalCompositeOperation = "screen";
  c.lineWidth = Math.max(1, R * 0.006);
  c.strokeStyle = withAlpha(CYAN_PULSE, 0.28);
  c.beginPath();
  c.arc(cx, cy, R, 0, TAU);
  c.stroke();
  c.restore();
}

/**
 * The stand: a graduated meridian ring in the picture plane, leaning with the
 * axis, and a horizon ring seen edge-on from the viewing elevation. The front
 * half of the horizon passes in front of the globe and the back half behind it,
 * which is what tells the eye the sphere has a far side.
 */
function drawStand(c: Ctx, view: View) {
  const { cx, cy, R } = view;
  const r = R * RING_R;
  c.save();
  c.globalCompositeOperation = "screen";

  // Horizon ring, back half first so the globe's own strokes overdraw it.
  const ry = r * Math.sin(VIEW_ELEVATION);
  c.lineWidth = Math.max(1, R * 0.004);
  c.strokeStyle = withAlpha(CREAM, 0.06);
  c.beginPath();
  c.ellipse(cx, cy, r, ry, 0, Math.PI, TAU);
  c.stroke();

  // Meridian ring with its degree scale, zero at the north pole.
  c.lineWidth = Math.max(1.2, R * 0.007);
  c.strokeStyle = withAlpha(CREAM, 0.42);
  c.beginPath();
  c.arc(cx, cy, r, 0, TAU);
  c.stroke();
  c.lineWidth = Math.max(1, R * 0.004);
  c.strokeStyle = withAlpha(CREAM, 0.2);
  c.beginPath();
  c.arc(cx, cy, r * 1.022, 0, TAU);
  c.stroke();

  // Zero of the scale and both pins sit where the polar axle actually meets
  // the ring, read off the projected pole rather than assumed from the tilt.
  const pole = view.toView([0, 1, 0]);
  const northUp = Math.atan2(-pole[1], pole[0]);
  for (let i = 0; i < RING_TICKS; i++) {
    const a = northUp + (i / RING_TICKS) * TAU;
    const major = i % 3 === 0;
    const pole = i % (RING_TICKS / 2) === 0;
    const len = pole ? R * 0.045 : major ? R * 0.026 : R * 0.014;
    const r0 = r * 1.022;
    const r1 = r0 + len;
    c.lineWidth = Math.max(1, major ? R * 0.005 : R * 0.003);
    c.strokeStyle = withAlpha(pole ? AMBER_BRIGHT : CREAM, pole ? 0.85 : major ? 0.42 : 0.22);
    c.beginPath();
    c.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
    c.lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
    c.stroke();
  }

  // The axis pins, where the polar axle meets the ring.
  for (const s of [1, -1]) {
    const a = northUp + (s < 0 ? Math.PI : 0);
    const px = cx + Math.cos(a) * r;
    const py = cy + Math.sin(a) * r;
    gGlow(c, px, py, R * 0.05, AMBER_BRIGHT, 0.55);
    c.fillStyle = withAlpha("#ffffff", 0.9);
    c.beginPath();
    c.arc(px, py, R * 0.011, 0, TAU);
    c.fill();
  }
  c.restore();
}

function drawHorizonFront(c: Ctx, view: View) {
  const { cx, cy, R } = view;
  const r = R * RING_R;
  const ry = r * Math.sin(VIEW_ELEVATION);
  c.save();
  c.globalCompositeOperation = "screen";
  c.lineWidth = Math.max(1, R * 0.0045);
  c.strokeStyle = withAlpha(CREAM, 0.22);
  c.beginPath();
  c.ellipse(cx, cy, r, ry, 0, 0, Math.PI);
  c.stroke();
  c.restore();
}

function drawGlobe(c: Ctx, view: View, R: number) {
  c.save();
  c.globalCompositeOperation = "screen";
  c.lineCap = "round";
  c.lineJoin = "round";
  for (const line of GRATICULE) strokeLine(c, view, line, Math.max(1, R * 0.0032), 0.13);
  for (const ring of COAST) strokeLine(c, view, ring, Math.max(1.6, R * 0.0072), 0.95, R * 0.026);
  c.restore();
}

/**
 * Age of a periodic event in frames, wrapping through the loop so the tail of
 * the last beat is what is showing at frame 0. The schedule is periodic by
 * construction (BEATS_PER_LOOP beats fill dur exactly), so this is the honest
 * reading of the calendar, not a fold that would hide a rate error: the
 * rotation itself is never wrapped.
 */
function ageOf(frame: number, at: number, dur: number): number {
  return (((frame - at) % dur) + dur) % dur;
}

function drawItinerary(c: Ctx, view: View, frame: number, dur: number) {
  const { R } = view;
  const framesPerBeat = dur / BEATS_PER_LOOP;
  const n = CITY_UNITS.length;

  c.save();
  c.globalCompositeOperation = "screen";
  c.lineCap = "round";

  for (let k = 0; k < n; k++) {
    const at = k * framesPerBeat;
    const age = ageOf(frame, at, dur);

    // The thread from the previous city, arriving as this beat fires.
    const from = CITY_UNITS[(k - 1 + n) % n];
    const to = CITY_UNITS[k];
    const travelAge = ageOf(frame, at - THREAD_TRAVEL, dur);
    const progress = EASE.inOut(clamp01(travelAge / THREAD_TRAVEL));
    const linger = travelAge <= THREAD_TRAVEL ? 1 : Math.exp(-(travelAge - THREAD_TRAVEL) / THREAD_DECAY);
    if (progress > 0 && linger > 0.02 && travelAge < THREAD_TRAVEL + THREAD_DECAY * 4) {
      const steps = 40;
      const head = Math.max(1, Math.round(steps * progress));
      for (let i = 1; i <= head; i++) {
        const a = view.toView(slerp(from, to, (i - 1) / steps));
        const b = view.toView(slerp(from, to, i / steps));
        if (a[2] <= 0.01 || b[2] <= 0.01) continue;
        // Brightest at the head, thinning toward the tail.
        const s = i / head;
        const A = linger * (0.32 + 0.68 * s * s) * (0.6 + 0.4 * b[2]);
        c.strokeStyle = withAlpha(s > 0.85 ? "#ffffff" : CREAM, A);
        c.lineWidth = Math.max(1.6, R * (0.006 + 0.008 * s));
        c.beginPath();
        c.moveTo(view.sx(a), view.sy(a));
        c.lineTo(view.sx(b), view.sy(b));
        c.stroke();
      }
      // The travelling head.
      if (progress < 1) {
        const h = view.toView(slerp(from, to, progress));
        if (h[2] > 0.01) gGlow(c, view.sx(h), view.sy(h), R * 0.07, "#ffffff", 0.85);
      }
    }

    // The bloom.
    const rise = EASE.out(clamp01(age / BLOOM_ATTACK));
    const fall = Math.exp(-age / BLOOM_DECAY);
    const bloom = rise * fall;
    const v = view.toView(to);
    if (v[2] > 0.01) {
      const x = view.sx(v);
      const y = view.sy(v);
      const depth = 0.6 + 0.4 * v[2];
      // A resting mark at every city, so the itinerary is visible as a map of
      // places even where the light has not been for a while.
      c.fillStyle = withAlpha(CREAM, 0.55 * depth);
      c.beginPath();
      c.arc(x, y, Math.max(1.5, R * 0.007), 0, TAU);
      c.fill();
      if (bloom > 0.01) {
        gGlow(c, x, y, R * (0.08 + 0.22 * bloom), AMBER_BRIGHT, 0.95 * bloom * depth);
        gGlow(c, x, y, R * (0.03 + 0.07 * bloom), "#ffffff", 0.95 * bloom * depth);
        // A ring that expands as the bloom fades: the ping.
        const ringR = R * (0.02 + 0.22 * (1 - fall));
        c.strokeStyle = withAlpha(CREAM, 0.95 * fall * depth);
        c.lineWidth = Math.max(1.2, R * 0.006 * (0.3 + fall));
        c.beginPath();
        c.arc(x, y, ringR, 0, TAU);
        c.stroke();
      }
    }
  }
  c.restore();
}

// --- entry ---------------------------------------------------------------------

export function drawAtlasGlobe(c: Ctx, geo: Geometry, { frame, dur }: AtlasFrame) {
  const { W, H, S } = geo;
  const R = S * GLOBE_R;
  const cx = W * CENTRE_X;
  const cy = H * CENTRE_Y;

  // Eastward: the meridian at centre stage moves west as the frame advances.
  const frontLon = FRONT_LON_AT_ZERO - (frame / dur) * TAU * REVS_PER_LOOP;
  const view = new View(spinFor(frontLon), cx, cy, R);

  drawAtmosphere(c, view);
  drawStand(c, view);
  drawGlobe(c, view, R);
  drawLimb(c, view);
  drawItinerary(c, view, frame, dur);
  drawHorizonFront(c, view);
}
