# Animated release hero

Renders the hero artwork for a feature release as a seamless five-second loop.

```bash
npm install
./render.sh 2.6.0 Clockwork 260      # version, codename, seed
```

Four files land in `out/`:

| File | What it is | Where it goes |
| --- | --- | --- |
| `hero-<codename>-v<version>.gif` | 800x450, 10 fps | the GitHub release page |
| `hero-<codename>-v<version>.webp` | 1280x720, 15 fps | the site |
| `hero-<codename>-v<version>-poster.webp` | 1920x1080 still | fallback where animation is stripped |
| `hero-<codename>-v<version>.mp4` | 1920x1080, 30 fps | source of truth, everything else derives from it |

GIF is the release-page format because it is the only animated format GitHub
release markdown renders everywhere. Commit the shipped files to `screenshot/`
alongside the previous heroes.

## Layout

- `src/theme.ts` brand constants and the loop's timing contract
- `src/brand.ts` the atmosphere, ported from `site/hero/`: backdrop, grade, grain, vignette, title block
- `src/Clockwork.tsx` the v2.6.0 motif. A new release replaces this file
- `src/Hero.tsx` the shell that stacks the layers. Shared across releases

Only the motif changes between releases. The palette, the navy atmosphere and the
top-left title block are the brand and are shared verbatim with the static
generators in `site/hero/`; see that README for the conventions.

The title is set in Geist, the site's own face. It is not committed here: the one
copy lives in `site/fonts/` and is copied into `public/` by `render.sh` and by
the `font` npm script, which `studio` and `still` both run first. A second
tracked copy would drift the day the site font changes.

## Writing a new motif

The codename is the brief. Beyond that, the loop is the hard part, and the
comments in `Clockwork.tsx` explain the traps in detail. In short:

1. **Everything is a pure function of the frame.** No accumulated state.
2. **The wrap has to close.** The last frame is followed by frame 0, so a whole
   loop of motion must leave every element on a pose it already held. Rotations
   need to land on whole multiples of each element's own symmetry period, and any
   decoration you add tightens that condition rather than being free.
3. **Never advance a repeating pattern by exactly one of its periods per step.**
   It returns to an identical pose and reads as vibrating in place instead of
   moving. This is the wagon-wheel effect and it is easy to ship by accident.
4. **Hold the background still.** The backdrop, starfield, grain and vignette are
   identical on every frame by design, which is most of why the files are small.

## Verifying

Two checks, both required, because this artwork can fail in ways a glance misses.

**The wrap.** The `LoopCheck` composition is one frame longer than the loop so
that frame `DURATION` can be rendered and diffed against frame 0. They must be
pixel-identical. Note that the motif is driven by the raw frame on purpose: fold
it into the period and this test passes by construction while proving nothing.

```bash
npx remotion still src/index.ts LoopCheck out/a.png --frame=0   --overwrite
npx remotion still src/index.ts LoopCheck out/b.png --frame=150 --overwrite
# compare a.png and b.png; any difference is a real seam
```

**The encode.** A seamless source is not a seamless file. libwebp builds an
animation by blending each frame over the last, so lossy error accumulates in one
direction, and on fine bright strokes it drifts visibly across the loop and then
snaps back at the wrap. Measured on the v2.6.0 dial, first-to-last drift ran 0.59
levels at quality 60 and 0.004 at quality 80, which is why the WebP is encoded at
80 and at half the frame rate. If you change the encode settings, re-measure:
sample a region the light passes over, across every decoded frame, and compare
the first frame against the last.

Remotion needs a Chromium. `render.sh` finds a Playwright `headless_shell`
automatically; override with `REMOTION_BROWSER=/path/to/binary` if needed. Full
Chrome does not work, it refuses the old-headless mode Remotion asks for.
