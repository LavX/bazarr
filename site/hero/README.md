# Bazarr+ Release Hero Generators

p5.js sketches that render the cinematic hero image for each release. One
file per release, frozen alongside its tag so the artwork stays
reproducible.

## Inventory

| Version | Codename | Sketch |
|--|--|--|
| v2.4.0 | Prism | [`prism-v2.4.0.html`](prism-v2.4.0.html) |
| v2.3.1 | Keystone | [`keystone-v2.3.1.html`](keystone-v2.3.1.html) |
| v2.3.0 | Keystone | [`keystone.html`](keystone.html) |
| v2.2.0 | Synapse | [`synapse.html`](synapse.html) |

## Render workflow

The sketches draw once on load (`noLoop()`). Capture them with headless
Chrome at 2x supersample, then downsample with Lanczos for crispness.

> **Why oversize-then-crop?** Headless Chrome on Linux silently shaves
> ~80px off the bottom of `--window-size` (virtual-framebuffer quirk),
> leaving a navy band on the rendered hero. The fix is to render at
> `H + 120` and crop back to the exact canvas height before downsampling.

```bash
# 1. Oversized capture (2x of 1920x1080 + 120px slack at the bottom)
google-chrome --headless=new --disable-gpu --no-sandbox \
  --hide-scrollbars --window-size=3840,2280 \
  --virtual-time-budget=8000 \
  --default-background-color=121125ff \
  --screenshot=/tmp/hero_oversized.png \
  "file://$PWD/site/hero/<sketch>.html?w=3840&h=2160"

# 2. Crop to exactly 3840x2160 from origin (drops the bottom slack)
magick /tmp/hero_oversized.png -crop 3840x2160+0+0 +repage /tmp/hero_3840.png

# 3. Downsample to final 1920x1080. Commit alongside the
#    previous release heroes so every release hero lives in screenshot/.
magick /tmp/hero_3840.png -filter Lanczos -resize 1920x1080 \
  screenshot/hero-<codename>.png
```

The release-notes markdown then references the rendered PNG at
`https://raw.githubusercontent.com/LavX/bazarr/development/screenshot/hero-<codename>.png`.

## Authoring conventions for new releases

Copy the latest sketch as a starting point, rename it after the new
codename, and adjust:

- **Canvas params**: `?w=` and `?h=` URL query params control dimensions.
  Defaults are 1920x1080. Internal `S = min(W, H)` is used to scale
  features so they don't blow out at extreme aspect ratios.
- **Bazarr+ palette**: keep these constants verbatim: they match the
  atmospheric dark theme the app ships with.
  ```js
  const NAVY = '#121125';      // --bz-surface-ground
  const AMBER       = '#e68a00'; // brand-5
  const AMBER_BRIGHT= '#ffb347';
  const CREAM       = '#fff8e1'; // brand-0
  const CYAN_PULSE  = '#7fe9ff';
  ```
- **Background**: every hero re-uses the same atmospheric base
  (`#121125` + 3 radial glows: amber TL / teal BR / purple center) and
  the 0.07 film-grain overlay. Keep `drawBackground()` and `drawNoise()`
  intact so the brand reads consistently across versions.
- **Text block**: top-left, "Bazarr+" with the bold amber "+" superscript,
  "V<version> Released", "Codename: <Name>" with the codename in amber.
- **Motif**: each release gets a distinct central visual that matches its
  codename. Synapse used a multi-arm radial neuron with cyan synaptic
  pulses; future codenames should pick their own metaphor (Vortex, Aurora,
  Constellation, etc.).
- **Pipeline**: render the sketch via the headless-chrome workflow above,
  commit the rendered PNG to `screenshot/`, then update the release
  notes hero reference.

## Why p5.js

- Single-file, no build step.
- Renders deterministically with `randomSeed()` / `noiseSeed()` so the
  exact same PNG can be regenerated from the source.
- Works inside the `site/` folder which is already part of the GitHub
  Pages source.
