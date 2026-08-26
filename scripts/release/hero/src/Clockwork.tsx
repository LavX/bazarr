/**
 * v2.6.0 "Clockwork" motif: a real epicyclic gear train, drawn in the same
 * luminous mark vocabulary the Murmuration hero uses for its flock, so the two
 * releases read as one body of work.
 *
 * The codename is the brief. v2.6.0 is the fix batch, so the image is a
 * mechanism running true, and it has to actually BE a mechanism: wheels that
 * touch share a pitch, mesh tooth-into-gap, and turn at the ratio their tooth
 * counts dictate. Concentric rings spinning at unrelated rates are a mandala,
 * not a machine.
 *
 * THE TRAIN  (sun / six planets / internal ring, carrier fixed)
 *   ring    96 teeth, internal    pitch radius 0.410 S
 *   planet  24 teeth, six of them pitch radius 0.103 S, centres at 0.308 S
 *   sun     48 teeth              pitch radius 0.205 S
 *   Standing constraint: N_ring = N_sun + 2*N_planet (96 = 48 + 48), which is
 *   what makes the three radii close exactly. Equal planet spacing additionally
 *   needs (N_sun + N_ring) / planets to be a whole number: 144 / 6 = 24.
 *
 * WHAT MOVES
 * The ring is the fixed member, so the entire outer assembly, ring and bolts and
 * bearing races and dial, is one stationary plate. The sun drives, the planets
 * ride a carrier that swings them round it, and each planet also spins about its
 * own centre. The only thing that travels around the outside is the light.
 *
 * Nothing here is a free rate: with the ring fixed, the tooth counts alone fix
 * the carrier at a third of the sun and the planet spin at minus the sun. See
 * SUN_REVS for why half a turn of the sun per loop is the slowest that closes.
 *
 * THE ESCAPEMENT DRIVES EVERYTHING
 * Every beat steps the whole moving assembly: snap, overshoot, recoil onto the
 * lock, then hold dead still until the next beat. In a real mechanical clock
 * every wheel in the train ticks; only the hand is obvious about it. The step
 * works out at 0.96 of a tooth rather than a whole one, on purpose: a whole
 * tooth per beat is the wagon-wheel trap described in theme.ts.
 *
 * LOOP CONTRACT
 * The frame is folded into one period before anything is computed, so the frame
 * at the loop point is bit-identical to frame 0 for free. What that does not buy
 * is a seamless WRAP: the last rendered frame is followed by frame 0, so a full
 * turn's worth of motion has to leave every wheel on a pose it already held. The
 * rates in SUN_REVS are chosen for exactly that, and each wheel's decoration
 * count is part of the condition rather than a free choice.
 */

import { BEATS_PER_LOOP, BRAND_RAMP, EASE } from "./theme";
import { clamp01, Ctx, gGlow, Geometry, smooth } from "./brand";

const TAU = Math.PI * 2;

// --- the train ---------------------------------------------------------------

const N_RING = 96;
const N_PLANET = 24;
const N_SUN = N_RING - 2 * N_PLANET; // 48, by the standing constraint
const PLANETS = 6;

/**
 * How many teeth each wheel must turn before it looks like itself again.
 *
 * A bare wheel repeats every single tooth. A wheel carrying decoration repeats
 * only on the decoration's period, which is the whole point of putting it there:
 * without it a symmetric wheel gives the eye nothing to track and its rotation
 * is invisible. Each of these divides TEETH_PER_LOOP, so the decoration returns
 * to its start with the wheel and the loop stays exact.
 */
const SUN_HOLES = 12;
const PLANET_SPOKES = 6;
const RING_BOLTS = 12;

/**
 * Rates, in revolutions per loop.
 *
 * The ring gear is the FIXED member, which is the ordinary way a planetary set is
 * arranged and the reason the whole outer assembly here holds still: the ring,
 * its bolts, the bearing races and the dial are one stationary plate. The sun
 * drives, and the carrier the planets ride on turns at a rate the tooth counts
 * fix, ω_carrier = ω_sun * N_sun / (N_sun + N_ring) = a third of the sun. Each
 * planet also spins about its own centre, at ω_planet = -ω_sun.
 *
 * A half turn of the sun per loop is the slowest setting that still closes:
 *   sun      0.500 rev = 180 deg, a whole number of its 30 deg hole period
 *   planet  -0.500 rev = 180 deg, a whole number of its 60 deg crossing period
 *   carrier  0.167 rev =  60 deg, exactly one planet spacing
 * The carrier moving by one spacing means planet j finishes where planet j+1
 * began, so their poses have to agree too: j ends 180 deg back while j+1 started
 * 60 deg round, a difference of 240 deg, which is 4 crossing periods. That last
 * condition is why the planets carry SIX crossings and not four.
 */
const SUN_REVS = 0.5;
const CARRIER_REVS = SUN_REVS * (N_SUN / (N_SUN + N_RING)); // a third of the sun
const PLANET_REVS = -SUN_REVS;

/**
 * Ring pitch radius as a fraction of S. Every other size derives from the pitch,
 * so this is the one scale knob. Sized so the dial AND the beat marker riding it
 * clear the frame: the marker's glow is what sets the real outer bound, and
 * sizing to the dial alone clips it at the top of every lap.
 */
const RING_R = 0.41;

/** Circular pitch, shared by every meshing wheel. */
const pitchOf = (S: number) => (RING_R * S * TAU) / N_RING;

/** Pitch radius of an N-tooth wheel. */
const radiusOf = (N: number, p: number) => (p * N) / TAU;

/** Tooth height, a little over half the pitch, which is ordinary gear proportion. */
const toothHeight = (p: number) => p * 0.55;

/** The fixed dial the hand reads against, outside the train. */
const DIAL_R = 0.46;
const DIAL_MARKS = BEATS_PER_LOOP * 4; // every fourth mark is a beat

/** Outermost radius used for the colour ramp. */
const R_MAX = 0.48;

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
 * Depth into the movement measured from the composition centre, 1 at the core
 * and 0 at the rim, gamma-shaped so cream carries most of the mechanism and cyan
 * is kept for the outer dial. Colour is keyed to the whole composition rather
 * than to each wheel, so the palette reads as one radial gradient across the
 * machine instead of repeating inside every planet.
 */
function depthAt(distFromCentre: number, S: number): number {
  return Math.pow(clamp01(1 - distFromCentre / (S * R_MAX)), 0.8);
}

/**
 * How far through the loop the movement has stepped, 0 to 1, advancing in
 * BEATS_PER_LOOP eased beats. Every rate above is scaled by this, so the whole
 * machine steps as one: snap, overshoot, recoil onto the lock, then stillness.
 */
function turnProgress(frame: number, dur: number): number {
  const framesPerBeat = dur / BEATS_PER_LOOP;
  const index = Math.floor(frame / framesPerBeat);
  const u = (frame % framesPerBeat) / framesPerBeat;
  // The jump occupies the first 55% of the beat; the rest is the lock, a held
  // beat of stillness. Fast move, then complete stillness, then fast move.
  const s = Math.min(1, u / 0.55);
  return (index + EASE.outBack(s)) / BEATS_PER_LOOP;
}

/** Shortest angular distance between two angles, in [0, PI]. */
function angDist(a: number, b: number): number {
  let d = Math.abs((((a - b) % TAU) + TAU) % TAU);
  if (d > Math.PI) d = TAU - d;
  return d;
}

/** Marks thin out near the title block, the same soft elliptical falloff the flock uses. */
function textFade(x: number, y: number, geo: Geometry): number {
  const ax = geo.W * 0.17;
  const ay = geo.H * 0.225;
  const R = geo.S * 0.3;
  const dx = (x - ax) / 1.3;
  const dy = y - ay;
  return smooth(clamp01(Math.hypot(dx, dy) / R));
}

interface Scene {
  geo: Geometry;
  /** composition centre, which the colour ramp and the highlight are keyed to */
  ox: number;
  oy: number;
  sweep: number;
}

const gleamAt = (a: number, sweep: number) => Math.exp(-((angDist(a, sweep) / 0.45) ** 2));

// --- drawing -----------------------------------------------------------------

/**
 * A circle drawn as many short arcs so it can honour the title falloff and the
 * travelling highlight per segment. A single `arc` stroke cannot fade.
 */
function drawArcBand(
  c: Ctx,
  scene: Scene,
  cx: number,
  cy: number,
  radius: number,
  lineWidth: number,
  alpha: number,
) {
  if (radius <= 0 || alpha <= 0) return;
  const { geo, ox, oy, sweep } = scene;
  const { S } = geo;
  const segments = 160;
  const step = TAU / segments;

  c.save();
  c.globalCompositeOperation = "screen";
  c.lineWidth = lineWidth;
  for (let i = 0; i < segments; i++) {
    const a = i * step;
    const x = cx + Math.cos(a) * radius;
    const y = cy + Math.sin(a) * radius;
    const fade = textFade(x, y, geo);
    if (fade < 0.02) continue;
    const [r, g, b] = rampAt(depthAt(Math.hypot(x - ox, y - oy), S));
    const gl = gleamAt(Math.atan2(y - oy, x - ox), sweep);
    const A = alpha * fade * (0.7 + 0.8 * gl);
    if (A < 0.008) continue;
    c.strokeStyle = `rgba(${r}, ${g}, ${b}, ${A})`;
    c.beginPath();
    c.arc(cx, cy, radius, a - step * 0.02, a + step * 1.02);
    c.stroke();
  }
  c.restore();
}

interface GearSpec {
  cx: number;
  cy: number;
  N: number;
  /** circular pitch, shared across the train */
  p: number;
  /** teeth face inward, i.e. a ring gear */
  internal?: boolean;
  /** angle of the wheel's zeroth tooth at rest */
  phase: number;
  /** the wheel's absolute rotation, in radians */
  rot: number;
  alpha: number;
  /** lightening holes drilled through the wheel's web */
  holes?: { count: number; at: number; r: number };
  /** crossings from hub to rim, the classic clock-wheel spoke */
  spokes?: number;
}

function drawGear(c: Ctx, scene: Scene, g: GearSpec) {
  const { geo, ox, oy, sweep } = scene;
  const { S } = geo;
  const R = radiusOf(g.N, g.p);
  const h = toothHeight(g.p);
  const step = TAU / g.N;
  const rot = g.rot;

  // The rim the teeth stand on. Teeth floating in space read as rays; teeth
  // attached to a wheel read as a gear.
  const rimR = g.internal ? R + h * 0.78 : R - h * 0.78;
  drawArcBand(c, scene, g.cx, g.cy, rimR, h * 0.5, g.alpha * 0.32);

  if (g.spokes) {
    const inner = R * 0.26;
    const outer = R * 0.74;
    c.save();
    c.globalCompositeOperation = "screen";
    for (let i = 0; i < g.spokes; i++) {
      const a = rot + (i / g.spokes) * TAU;
      const ux = Math.cos(a);
      const uy = Math.sin(a);
      const px = -uy;
      const py = ux;
      const mx = g.cx + ux * ((inner + outer) / 2);
      const my = g.cy + uy * ((inner + outer) / 2);
      const fade = textFade(mx, my, geo);
      if (fade < 0.02) continue;
      const [r, gg, b] = rampAt(depthAt(Math.hypot(mx - ox, my - oy), S));
      const gl = gleamAt(Math.atan2(my - oy, mx - ox), sweep);
      const A = clamp01(g.alpha * 0.5 * fade * (0.7 + 0.8 * gl));
      const wIn = R * 0.075;
      const wOut = R * 0.045;
      c.fillStyle = `rgba(${r}, ${gg}, ${b}, ${A})`;
      c.beginPath();
      c.moveTo(g.cx + ux * inner - px * wIn, g.cy + uy * inner - py * wIn);
      c.lineTo(g.cx + ux * outer - px * wOut, g.cy + uy * outer - py * wOut);
      c.lineTo(g.cx + ux * outer + px * wOut, g.cy + uy * outer + py * wOut);
      c.lineTo(g.cx + ux * inner + px * wIn, g.cy + uy * inner + py * wIn);
      c.closePath();
      c.fill();
    }
    c.restore();
  }

  if (g.holes) {
    for (let i = 0; i < g.holes.count; i++) {
      const a = rot + (i / g.holes.count) * TAU;
      drawArcBand(
        c,
        scene,
        g.cx + Math.cos(a) * R * g.holes.at,
        g.cy + Math.sin(a) * R * g.holes.at,
        R * g.holes.r,
        R * 0.014,
        g.alpha * 0.38,
      );
    }
  }

  c.save();
  c.globalCompositeOperation = "screen";

  const baseR = g.internal ? R + h * 0.45 : R - h * 0.45;
  const tipR = g.internal ? R - h * 0.55 : R + h * 0.55;
  const wb = g.p * 0.3;
  const wt = g.p * 0.17;

  for (let i = 0; i < g.N; i++) {
    const a = g.phase + rot + i * step;
    const ux = Math.cos(a);
    const uy = Math.sin(a);
    const x = g.cx + ux * R;
    const y = g.cy + uy * R;

    const fade = textFade(x, y, geo);
    if (fade < 0.02) continue;

    const [r, gg, b] = rampAt(depthAt(Math.hypot(x - ox, y - oy), S));
    const gl = gleamAt(Math.atan2(y - oy, x - ox), sweep);
    const alpha = clamp01(g.alpha * fade * (0.62 + 0.85 * gl));
    if (alpha < 0.012) continue;

    // A tapered trapezoid, wide at the root and narrow at the tip, which is the
    // silhouette of real gear cutting. Filled rather than stroked so it never
    // reads as a glyph, and proportioned from the pitch so it never reads as a ray.
    const px = -uy;
    const py = ux;
    c.fillStyle = `rgba(${r}, ${gg}, ${b}, ${alpha})`;
    c.beginPath();
    c.moveTo(g.cx + ux * baseR - px * wb, g.cy + uy * baseR - py * wb);
    c.lineTo(g.cx + ux * tipR - px * wt, g.cy + uy * tipR - py * wt);
    c.lineTo(g.cx + ux * tipR + px * wt, g.cy + uy * tipR + py * wt);
    c.lineTo(g.cx + ux * baseR + px * wb, g.cy + uy * baseR + py * wb);
    c.closePath();
    c.fill();
  }
  c.restore();
}

/** The fixed chapter ring the hand reads against. A dial does not turn. */
function drawDial(c: Ctx, scene: Scene) {
  const { geo, ox, oy, sweep } = scene;
  const { S } = geo;
  const R = S * DIAL_R;
  const [r, g, b] = rampAt(depthAt(R, S));

  c.save();
  c.globalCompositeOperation = "screen";
  c.lineCap = "butt";
  for (let i = 0; i < DIAL_MARKS; i++) {
    const a = -Math.PI / 2 + (i / DIAL_MARKS) * TAU;
    const beat = i % 4 === 0;
    const len = S * (beat ? 0.019 : 0.008);
    const ux = Math.cos(a);
    const uy = Math.sin(a);
    const x = ox + ux * R;
    const y = oy + uy * R;
    const fade = textFade(x, y, geo);
    if (fade < 0.02) continue;
    const alpha = clamp01((beat ? 0.5 : 0.26) * fade * (0.6 + 0.9 * gleamAt(a, sweep)));
    c.strokeStyle = `rgba(${r}, ${g}, ${b}, ${alpha})`;
    c.lineWidth = S * (beat ? 0.0022 : 0.0012);
    c.beginPath();
    c.moveTo(ox + ux * (R - len), oy + uy * (R - len));
    c.lineTo(ox + ux * (R + len), oy + uy * (R + len));
    c.stroke();
  }
  c.restore();
}

/** Hub and bore: plain circles, so the wheel stays symmetric at one tooth pitch. */
function drawHub(c: Ctx, scene: Scene, cx: number, cy: number, R: number, alpha: number) {
  drawArcBand(c, scene, cx, cy, R * 0.3, R * 0.055, alpha * 0.42);
  drawArcBand(c, scene, cx, cy, R * 0.1, R * 0.09, alpha * 0.5);
}

/**
 * The beat marker: a jewel riding the chapter ring, jumping exactly one division
 * per beat and completing one lap per loop.
 *
 * This replaced a centre-pivoted hand. A hand long enough to reach its own dial
 * draws a line right across the movement, and at the top of the loop it bisects
 * the whole composition; a marker on the ring says the same thing about the beat
 * without cutting the picture in half.
 */
function drawBeatMarker(c: Ctx, scene: Scene, beats: number) {
  const { geo, ox, oy } = scene;
  const { S } = geo;
  const a = -Math.PI / 2 + ((beats % BEATS_PER_LOOP) / BEATS_PER_LOOP) * TAU;
  const R = S * DIAL_R;
  const x = ox + Math.cos(a) * R;
  const y = oy + Math.sin(a) * R;

  c.save();
  c.globalCompositeOperation = "screen";
  gGlow(c, x, y, S * 0.034, "#ffb347", 0.26);
  gGlow(c, x, y, S * 0.012, "#fff3d0", 0.5);
  gGlow(c, x, y, S * 0.0038, "#ffffff", 0.85);
  c.restore();
}

/** Soft luminous body, so the movement sits in the frame instead of floating on it. */
function drawBodyGlow(c: Ctx, geo: Geometry, cx: number, cy: number, pulse: number) {
  const { S } = geo;
  c.save();
  c.globalCompositeOperation = "screen";
  gGlow(c, cx, cy, S * 0.44, "#8a6bff", 0.05);
  gGlow(c, cx - S * 0.2, cy + S * 0.16, S * 0.3, "#7fe9ff", 0.038);
  gGlow(c, cx + S * 0.14, cy - S * 0.12, S * 0.26, "#ffb347", 0.045);
  gGlow(c, cx, cy, S * 0.16, "#ffce92", 0.05 + 0.05 * pulse);
  c.restore();
}

/**
 * The jewel at the sun's centre. It is the only element in the frame that glows,
 * and it flares on every beat, tying the light to the rhythm of the mechanism.
 */
function drawCore(c: Ctx, geo: Geometry, cx: number, cy: number, pulse: number, breath: number) {
  const { S } = geo;
  c.save();
  c.globalCompositeOperation = "screen";
  const swell = 1 + 0.14 * pulse + 0.05 * breath;
  gGlow(c, cx, cy, S * 0.075 * swell, "#ffb347", 0.3 + 0.26 * pulse);
  gGlow(c, cx, cy, S * 0.03 * swell, "#fff3d0", 0.45 + 0.3 * pulse);
  gGlow(c, cx, cy, S * 0.011 * swell, "#ffffff", 0.8 + 0.2 * pulse);
  c.restore();
}

export interface ClockworkFrame {
  frame: number;
  dur: number;
}

export function drawClockwork(c: Ctx, geo: Geometry, { frame: rawFrame, dur }: ClockworkFrame) {
  const { W, H, S } = geo;
  // The MECHANISM is driven by the raw frame, deliberately unfolded. Folding it
  // into one period would make the loop check tautological: frame `dur` would
  // render as frame 0 by construction and prove nothing about whether the train
  // actually returns to its starting pose. Left raw, rendering frame `dur` is a
  // real test of the rates in SUN_REVS.
  const frame = rawFrame;
  // The lighting and the breath are phase functions of position in the loop, not
  // parts of the train, so those do fold.
  const phase = ((rawFrame % dur) + dur) % dur;

  const ox = W * 0.65;
  const oy = H * 0.5;

  const turn = turnProgress(frame, dur);
  const sunAng = turn * SUN_REVS * TAU;
  const planetAng = turn * PLANET_REVS * TAU;
  const carrierAng = turn * CARRIER_REVS * TAU;

  const framesPerBeat = dur / BEATS_PER_LOOP;
  const beatPhase = (frame % framesPerBeat) / framesPerBeat;
  const pulse = Math.exp(-4 * beatPhase); // sharp attack on the beat, decay through the lock
  const beats = turn * BEATS_PER_LOOP;
  const breath = Math.sin(TAU * 2 * (phase / dur)); // two whole breaths per loop
  const sweep = -Math.PI / 2 + TAU * (phase / dur); // light, not a part: one smooth revolution

  const scene: Scene = { geo, ox, oy, sweep };

  const p = pitchOf(S);
  const rSun = radiusOf(N_SUN, p);
  const rPlanet = radiusOf(N_PLANET, p);
  const carrier = rSun + rPlanet; // planet centres sit exactly the pitch-radius sum out

  drawBodyGlow(c, geo, ox, oy, pulse);
  drawDial(c, scene);

  // Bearing races: concentric and symmetric, so they cost the loop nothing.
  drawArcBand(c, scene, ox, oy, S * 0.434, S * 0.0016, 0.13);
  drawArcBand(c, scene, ox, oy, S * 0.1, S * 0.0012, 0.12);

  // Ring gear. Internal mesh, so it turns the same way as the planets it holds.
  drawGear(c, scene, {
    cx: ox,
    cy: oy,
    N: N_RING,
    p,
    internal: true,
    phase: 0,
    // The fixed member. Its bolts belong to the stationary plate, so nothing out
    // here turns; the only thing that travels around the rim is the light.
    rot: 0,
    holes: { count: RING_BOLTS, at: 1.075, r: 0.028 },
    alpha: 0.5,
  });

  // Planets. Each sits on a spoke of the carrier, and carries a half-pitch offset
  // so its teeth fall into the sun's gaps rather than colliding with its teeth.
  for (let k = 0; k < PLANETS; k++) {
    const rest = (k / PLANETS) * TAU; // where this planet sits at rest
    const at = rest + carrierAng; // where the carrier has swung it to
    const px = ox + Math.cos(at) * carrier;
    const py = oy + Math.sin(at) * carrier;
    drawGear(c, scene, {
      cx: px,
      cy: py,
      N: N_PLANET,
      p,
      // The mesh offset is fixed at the wheel's rest position; the carrier moves
      // where the planet IS, while its own spin is what turns its teeth. Folding
      // the carrier into the phase instead would double-count the orbit and tear
      // the mesh apart.
      phase: rest + Math.PI + Math.PI / N_PLANET,
      rot: planetAng,
      spokes: PLANET_SPOKES,
      alpha: 0.62,
    });
    drawHub(c, scene, px, py, rPlanet, 0.5);
  }

  // Sun. External mesh with the planets, so it turns against them.
  drawGear(c, scene, {
    cx: ox,
    cy: oy,
    N: N_SUN,
    p,
    phase: 0,
    rot: sunAng,
    holes: { count: SUN_HOLES, at: 0.62, r: 0.12 },
    alpha: 0.72,
  });
  drawHub(c, scene, ox, oy, rSun, 0.45);

  drawBeatMarker(c, scene, beats);
  drawCore(c, geo, ox, oy, pulse, breath);
}
