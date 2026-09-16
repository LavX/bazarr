/**
 * v2.7.0 "Atlas" motif: an armillary celestial sphere and coordinate globe,
 * drawn in the luminous mark vocabulary of the Bazarr+ brand.
 *
 * Codename: Atlas. The metaphor is reach: subtitles for media across the entire
 * celestial expanse of film, television and sport, whether in the local library
 * or discovery space.
 *
 * THE CELESTIAL MECHANISM
 * An armillary sphere with:
 *   1. Outer fixed meridian gimbal and horizon ring with degree graduations
 *   2. Tilted polar axis passing through celestial poles with pivot finials
 *   3. Rotating celestial coordinate sphere with 12 meridian great circles
 *   4. Parallels of latitude (Equator, Tropics of Cancer/Capricorn, Polar circles)
 *   5. Oblique ecliptic band tilted at the axial obliquity
 *   6. Constellation beacon nodes (the subtitle stars) traveling along their parallels
 *   7. Radiant inner amber jewel core with harmonic breathing pulse
 *
 * LOOP CONTRACT
 * Every coordinate and rotation is an exact function of frame:
 *   angle = (frame / dur) * 2 * PI
 * Across dur = 150 frames (5 seconds at 30 fps), the sphere completes exactly
 * one full 360-degree revolution (2*PI). At frame 150, every point returns
 * bit-identically to its frame 0 pose.
 */

import { AMBER, AMBER_BRIGHT, BRAND_RAMP, CREAM, CYAN_PULSE } from "./theme";
import { clamp01, Ctx, gGlow, Geometry, withAlpha } from "./brand";

const TAU = Math.PI * 2;

export interface AtlasFrame {
  frame: number;
  dur: number;
}

/** 3D vector */
interface Vec3 {
  x: number;
  y: number;
  z: number;
}

/** Fixed axial tilt of the celestial sphere (standard 23.4 degrees). */
const AXIAL_TILT = (-23.4 * Math.PI) / 180;

/** Viewing elevation angle: tilts the sphere slightly toward viewer. */
const VIEW_ELEVATION = (20.0 * Math.PI) / 180;

/** Ecliptic obliquity relative to celestial equator. */
const ECLIPTIC_TILT = (23.4 * Math.PI) / 180;

/** Sampled stars / coordinate beacon positions on unit sphere (lat, lon0). */
const BEACON_COORDS: readonly { lat: number; lon0: number; size: number; color: string }[] = [
  { lat: 0.0, lon0: 0.0, size: 1.2, color: CYAN_PULSE },
  { lat: 0.35, lon0: 0.75, size: 1.0, color: CREAM },
  { lat: -0.42, lon0: 1.3, size: 1.1, color: AMBER_BRIGHT },
  { lat: 0.65, lon0: 2.1, size: 0.9, color: CYAN_PULSE },
  { lat: -0.22, lon0: 2.8, size: 1.3, color: CREAM },
  { lat: 0.18, lon0: 3.5, size: 1.0, color: AMBER },
  { lat: -0.68, lon0: 4.1, size: 0.8, color: CYAN_PULSE },
  { lat: 0.48, lon0: 4.7, size: 1.2, color: CREAM },
  { lat: -0.15, lon0: 5.4, size: 1.1, color: AMBER_BRIGHT },
  { lat: 0.72, lon0: 5.9, size: 0.9, color: CYAN_PULSE },
  { lat: 0.28, lon0: 1.8, size: 1.1, color: AMBER_BRIGHT },
  { lat: -0.38, lon0: 0.3, size: 1.0, color: CYAN_PULSE },
  { lat: 0.52, lon0: 3.1, size: 1.2, color: CREAM },
  { lat: -0.58, lon0: 5.0, size: 0.9, color: AMBER },
  { lat: 0.1, lon0: 4.2, size: 1.3, color: CYAN_PULSE },
  { lat: -0.12, lon0: 2.3, size: 1.0, color: CREAM },
];

/** Transform a point on the sphere by rotation around polar axis, then axial tilt and view elevation. */
function projectSpherePoint(
  lat: number,
  lon: number,
  radius: number,
  cx: number,
  cy: number,
): { x: number; y: number; z: number } {
  // Spherical coords with polar axis along Y before tilt:
  const cosLat = Math.cos(lat);
  const sinLat = Math.sin(lat);
  const cosLon = Math.cos(lon);
  const sinLon = Math.sin(lon);

  // Un-tilted coordinates:
  let px = radius * cosLat * Math.sin(lon);
  let py = radius * sinLat;
  let pz = radius * cosLat * Math.cos(lon);

  // 1. Axial tilt in XY plane (around Z axis):
  const cosTilt = Math.cos(AXIAL_TILT);
  const sinTilt = Math.sin(AXIAL_TILT);
  const tx = px * cosTilt - py * sinTilt;
  const ty = px * sinTilt + py * cosTilt;
  const tz = pz;

  // 2. View elevation tilt around X axis:
  const cosView = Math.cos(VIEW_ELEVATION);
  const sinView = Math.sin(VIEW_ELEVATION);
  const vx = tx;
  const vy = ty * cosView - tz * sinView;
  const vz = ty * sinView + tz * cosView;

  return {
    x: cx + vx,
    y: cy - vy,
    z: vz,
  };
}

/** Sample color from the brand ramp. */
function rampColor(t: number): string {
  const n = BRAND_RAMP.length - 1;
  const x = clamp01(t) * n;
  const i0 = Math.floor(x);
  const i1 = Math.min(i0 + 1, n);
  const f = x - i0;
  const c0 = BRAND_RAMP[i0];
  const c1 = BRAND_RAMP[i1];
  const r = Math.round(c0[0] + (c1[0] - c0[0]) * f);
  const g = Math.round(c0[1] + (c1[1] - c0[1]) * f);
  const b = Math.round(c0[2] + (c1[2] - c0[2]) * f);
  return `rgb(${r}, ${g}, ${b})`;
}

/** Draws the ambient glows around the celestial globe. */
function drawCelestialGlows(c: Ctx, geo: Geometry, cx: number, cy: number, pulse: number) {
  const { S } = geo;
  c.save();
  c.globalCompositeOperation = "screen";
  gGlow(c, cx, cy, S * 0.46, "#8a6bff", 0.052);
  gGlow(c, cx - S * 0.22, cy + S * 0.16, S * 0.32, "#7fe9ff", 0.042);
  gGlow(c, cx + S * 0.16, cy - S * 0.14, S * 0.28, "#ffb347", 0.048);
  gGlow(c, cx, cy, S * 0.2, "#ffce92", 0.06 + 0.05 * pulse);
  c.restore();
}

/** Draws the central beating core of Atlas. */
function drawCore(c: Ctx, geo: Geometry, cx: number, cy: number, pulse: number, breath: number) {
  const { S } = geo;
  c.save();
  c.globalCompositeOperation = "screen";
  const swell = 1 + 0.12 * pulse + 0.04 * breath;

  // Soft luminous aureole
  gGlow(c, cx, cy, S * 0.082 * swell, "#ffb347", 0.32 + 0.22 * pulse);
  gGlow(c, cx, cy, S * 0.034 * swell, "#fff3d0", 0.5 + 0.25 * pulse);
  gGlow(c, cx, cy, S * 0.012 * swell, "#ffffff", 0.85 + 0.15 * pulse);

  // Small central jewel
  c.beginPath();
  c.arc(cx, cy, S * 0.0055 * swell, 0, TAU);
  c.fillStyle = "#ffffff";
  c.shadowColor = AMBER_BRIGHT;
  c.shadowBlur = S * 0.015;
  c.fill();

  c.restore();
}

/** Draws outer meridian gimbal ring and degree tick marks. */
function drawOuterGimbal(c: Ctx, cx: number, cy: number, radius: number, S: number) {
  c.save();

  // Outer ring path
  c.beginPath();
  c.arc(cx, cy, radius, 0, TAU);
  c.lineWidth = Math.max(1.5, S * 0.0028);
  c.strokeStyle = withAlpha(CREAM, 0.48);
  c.stroke();

  // Second concentric ring
  const innerR = radius - S * 0.012;
  c.beginPath();
  c.arc(cx, cy, innerR, 0, TAU);
  c.lineWidth = Math.max(1, S * 0.0016);
  c.strokeStyle = withAlpha(CREAM, 0.24);
  c.stroke();

  // Degree graduation marks (every 5 and 10 degrees)
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

  // Polar finials along the tilted axis
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

/** Draws latitude circles (parallels). */
function drawLatitudeParallels(
  c: Ctx,
  cx: number,
  cy: number,
  radius: number,
  rotAngle: number,
  S: number,
  isFront: boolean,
) {
  const latitudes = [
    { lat: 0.0, label: "equator", weight: 2.0, alpha: 0.65, color: CREAM },
    { lat: (23.4 * Math.PI) / 180, label: "tropic_n", weight: 1.4, alpha: 0.45, color: AMBER_BRIGHT },
    { lat: (-23.4 * Math.PI) / 180, label: "tropic_s", weight: 1.4, alpha: 0.45, color: AMBER_BRIGHT },
    { lat: (66.5 * Math.PI) / 180, label: "arctic", weight: 1.0, alpha: 0.32, color: CYAN_PULSE },
    { lat: (-66.5 * Math.PI) / 180, label: "antarctic", weight: 1.0, alpha: 0.32, color: CYAN_PULSE },
  ];

  const steps = 96;

  for (const p of latitudes) {
    c.save();
    c.beginPath();
    let started = false;

    for (let i = 0; i <= steps; i++) {
      const lon = rotAngle + (i / steps) * TAU;
      const pt = projectSpherePoint(p.lat, lon, radius, cx, cy);

      const satisfiesDepth = isFront ? pt.z >= 0 : pt.z < 0;
      if (satisfiesDepth) {
        if (!started) {
          c.moveTo(pt.x, pt.y);
          started = true;
        } else {
          c.lineTo(pt.x, pt.y);
        }
      } else {
        started = false;
      }
    }

    c.lineWidth = Math.max(1, S * 0.0012 * p.weight);
    const alpha = isFront ? p.alpha : p.alpha * 0.28;
    c.strokeStyle = withAlpha(p.color, alpha);
    c.stroke();
    c.restore();
  }
}

/** Draws the 12 longitude meridians rotating with the celestial globe. */
function drawMeridians(
  c: Ctx,
  cx: number,
  cy: number,
  radius: number,
  rotAngle: number,
  S: number,
  isFront: boolean,
) {
  const meridianCount = 12;
  const latSteps = 64;

  for (let m = 0; m < meridianCount; m++) {
    const baseLon = rotAngle + (m / meridianCount) * TAU;
    const isPrime = m % 3 === 0;

    c.save();
    c.beginPath();
    let started = false;

    for (let i = 0; i <= latSteps; i++) {
      const lat = -Math.PI / 2 + (i / latSteps) * Math.PI;
      const pt = projectSpherePoint(lat, baseLon, radius, cx, cy);

      const satisfiesDepth = isFront ? pt.z >= 0 : pt.z < 0;
      if (satisfiesDepth) {
        if (!started) {
          c.moveTo(pt.x, pt.y);
          started = true;
        } else {
          c.lineTo(pt.x, pt.y);
        }
      } else {
        started = false;
      }
    }

    const baseAlpha = isPrime ? 0.48 : 0.26;
    const alpha = isFront ? baseAlpha : baseAlpha * 0.25;
    const color = isPrime ? CREAM : CYAN_PULSE;
    c.lineWidth = Math.max(1, S * (isPrime ? 0.0016 : 0.0011));
    c.strokeStyle = withAlpha(color, alpha);
    c.stroke();
    c.restore();
  }
}

/** Draws the oblique ecliptic band tilted at 23.4 degrees to celestial equator. */
function drawEcliptic(
  c: Ctx,
  cx: number,
  cy: number,
  radius: number,
  rotAngle: number,
  S: number,
  isFront: boolean,
) {
  const steps = 96;
  c.save();
  c.beginPath();
  let started = false;

  for (let i = 0; i <= steps; i++) {
    const lambda = rotAngle + (i / steps) * TAU;
    // Ecliptic coordinates to equatorial latitude & longitude:
    // sin(lat) = sin(ecliptic_tilt) * sin(lambda)
    // tan(lon) = cos(ecliptic_tilt) * tan(lambda)
    const sinLat = Math.sin(ECLIPTIC_TILT) * Math.sin(lambda);
    const lat = Math.asin(sinLat);
    const lon = Math.atan2(Math.cos(ECLIPTIC_TILT) * Math.sin(lambda), Math.cos(lambda));

    const pt = projectSpherePoint(lat, lon, radius, cx, cy);
    const satisfiesDepth = isFront ? pt.z >= 0 : pt.z < 0;

    if (satisfiesDepth) {
      if (!started) {
        c.moveTo(pt.x, pt.y);
        started = true;
      } else {
        c.lineTo(pt.x, pt.y);
      }
    } else {
      started = false;
    }
  }

  const alpha = isFront ? 0.68 : 0.18;
  c.lineWidth = Math.max(1.2, S * 0.0022);
  c.strokeStyle = withAlpha(AMBER, alpha);
  c.stroke();
  c.restore();
}

/** Draws the constellation beacon nodes traveling across the sphere. */
function drawBeacons(
  c: Ctx,
  cx: number,
  cy: number,
  radius: number,
  rotAngle: number,
  S: number,
  pulse: number,
) {
  c.save();
  c.globalCompositeOperation = "screen";

  for (let i = 0; i < BEACON_COORDS.length; i++) {
    const b = BEACON_COORDS[i];
    const lon = rotAngle + b.lon0;
    const pt = projectSpherePoint(b.lat, lon, radius, cx, cy);

    // Fade when on back hemisphere
    const isFront = pt.z >= 0;
    const depthScale = isFront ? 0.7 + 0.3 * (pt.z / radius) : 0.25;
    const alpha = isFront ? 0.85 * depthScale : 0.2;
    const nodeR = S * 0.0042 * b.size * depthScale;

    // Glowing halo
    gGlow(c, pt.x, pt.y, S * 0.02 * b.size * depthScale, b.color, alpha * 0.5);

    // Bright star pip
    c.beginPath();
    c.arc(pt.x, pt.y, Math.max(1, nodeR), 0, TAU);
    c.fillStyle = withAlpha("#ffffff", alpha);
    c.fill();

    // Subtle cross spike on major front stars
    if (isFront && b.size >= 1.1) {
      const spikeLen = S * 0.012 * b.size * (1 + 0.15 * pulse);
      c.beginPath();
      c.moveTo(pt.x - spikeLen, pt.y);
      c.lineTo(pt.x + spikeLen, pt.y);
      c.moveTo(pt.x, pt.y - spikeLen);
      c.lineTo(pt.x, pt.y + spikeLen);
      c.lineWidth = Math.max(0.8, S * 0.0008);
      c.strokeStyle = withAlpha(b.color, alpha * 0.6);
      c.stroke();
    }
  }

  c.restore();
}

/**
 * Main draw function for the v2.7.0 Atlas hero motif.
 */
export function drawAtlas(c: Ctx, geo: Geometry, { frame, dur }: AtlasFrame) {
  const { W, H, S } = geo;

  // The center is placed at W*0.65, H*0.5 to balance the top-left title block.
  const ox = W * 0.65;
  const oy = H * 0.5;

  // Base radii
  const R_GIMBAL = S * 0.42;
  const R_SPHERE = S * 0.36;

  // The rotation angle completes exactly 1 revolution (TAU) over `dur` frames.
  // Using raw frame without modulo ensures LoopCheck can prove the seam closes.
  const turnProgress = frame / dur;
  const rotAngle = turnProgress * TAU;

  // Harmonic breath and pulse are phase functions of position in the loop:
  const phase = ((frame % dur) + dur) % dur;
  const pulse = Math.sin((phase / dur) * TAU);
  const breath = Math.cos((phase / dur) * TAU * 2);

  // 1. Celestial background glows
  drawCelestialGlows(c, geo, ox, oy, pulse);

  // 2. Fixed outer gimbal and meridian graduation ring
  drawOuterGimbal(c, ox, oy, R_GIMBAL, S);

  // 3. BACK hemisphere of the celestial sphere (drawn behind inner core)
  drawLatitudeParallels(c, ox, oy, R_SPHERE, rotAngle, S, false);
  drawMeridians(c, ox, oy, R_SPHERE, rotAngle, S, false);
  drawEcliptic(c, ox, oy, R_SPHERE, rotAngle, S, false);

  // 4. Central beating jewel core of Atlas
  drawCore(c, geo, ox, oy, pulse, breath);

  // 5. FRONT hemisphere of the celestial sphere (drawn in front of inner core)
  drawLatitudeParallels(c, ox, oy, R_SPHERE, rotAngle, S, true);
  drawMeridians(c, ox, oy, R_SPHERE, rotAngle, S, true);
  drawEcliptic(c, ox, oy, R_SPHERE, rotAngle, S, true);

  // 6. Constellation beacons / subtitle stars
  drawBeacons(c, ox, oy, R_SPHERE, rotAngle, S, pulse);
}
