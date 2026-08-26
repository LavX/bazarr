/**
 * The Bazarr+ hero brand constants.
 *
 * These values are the brand, shared verbatim with the static hero generators in
 * `site/hero/`. Do not tweak them per release: the palette and the atmosphere are
 * what make five releases of hero art read as one product. Only the motif changes.
 */

export const NAVY = "#121125"; // --bz-surface-ground
export const AMBER = "#e68a00"; // brand-5
export const AMBER_BRIGHT = "#ffb347";
export const CREAM = "#fff8e1"; // brand-0
export const CYAN_PULSE = "#7fe9ff";

/**
 * Cool cyan at the sparse outer edge, through cream, to warm amber at the dense
 * core. The murmuration hero uses this exact ramp for its flock; the clockwork
 * motif reuses it radially so the two releases share a visual family.
 */
export const BRAND_RAMP: readonly [number, number, number][] = [
  [127, 233, 255], // cyan-pulse (edge)
  [255, 248, 225], // cream
  [255, 179, 71], // amber-bright (core)
];

export const FPS = 30;

/**
 * 5 seconds. Every periodic motion in the composition must complete a whole
 * number of cycles across exactly this many frames, or the loop seams.
 * 150 factors as 2 x 3 x 5^2, which is why the tick counts below divide it.
 */
export const DURATION = 150;

export const WIDTH = 1920;
export const HEIGHT = 1080;

/**
 * Escapement beats per loop: 25 beats, one every 6 frames, five per second.
 *
 * The count must divide DURATION, but 6 frames per beat is chosen for a second
 * reason. The GIF derivative is decimated to 10 or 15 fps, keeping every 3rd or
 * every 2nd frame, and 6 divides by both. A 5-frame beat (30 beats) samples at
 * a different phase on every beat once decimated, which turns the escapement's
 * crisp step into a stutter in the very format most people will see it in.
 */
export const BEATS_PER_LOOP = 25;

/* ---------------------------------------------------------------------------
 * A standing constraint on the rates chosen in Clockwork.tsx, recorded here
 * beside the beat count it depends on: a beat must NOT advance a wheel by a
 * whole number of teeth.
 *
 * When it does, every wheel lands back on its own symmetry period at the end of
 * every beat and returns to a pose indistinguishable from the one it left, so it
 * reads as vibrating in place rather than turning: the wagon-wheel effect. The
 * sun there advances 24 teeth across 25 beats, or 0.96 of a tooth each, so the
 * pose drifts and the rotation stays legible. This is physically ordinary too:
 * only the escape wheel itself advances a whole tooth per beat, and the wheels
 * drawn here are the train it drives, geared away from it.
 * ------------------------------------------------------------------------- */

export const FONT_FAMILY = "Geist, -apple-system, 'Segoe UI', system-ui, sans-serif";

/**
 * Easing curves. Nothing designed may move linearly.
 *
 * One deliberate exception runs through this composition: the gear rings turn at
 * constant angular velocity. That is not un-eased motion, it is the correct
 * physics for a mechanism, and a gear that accelerated into its rotation would
 * read as broken rather than as designed. Everything that is a *designed* move,
 * the escapement step, the core pulse, the light sweep, the shimmer and the
 * breathing, is eased below.
 */
export const EASE = {
  /** easeOutExpo. Entrances. */
  out: (t: number) => (t >= 1 ? 1 : 1 - Math.pow(2, -10 * t)),
  /** easeInOutQuint. Long moves. */
  inOut: (t: number) => (t < 0.5 ? 16 * t ** 5 : 1 - Math.pow(-2 * t + 2, 5) / 2),
  /**
   * easeOutBack. Overshoots past the target and settles back onto it, which is
   * exactly what a real escapement does when the pallet drops: the wheel jumps,
   * recoils slightly, and locks. Lands on exactly 1 at t = 1, which is what
   * keeps the 30 discrete ticks summing to a seamless whole revolution.
   */
  outBack: (t: number) => {
    const c1 = 1.70158;
    const c3 = c1 + 1;
    return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
  },
} as const;
