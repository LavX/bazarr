#!/usr/bin/env bash
# Render the release hero: MP4 source of truth, plus the GIF and animated WebP
# derivatives that actually ship.
#
#   ./render.sh <version> <codename> [seed]
#   ./render.sh 2.6.0 Clockwork 260
#
# Outputs land in out/ as hero-<codename>-v<version>.{mp4,gif,webp}. The GIF is
# what goes into screenshot/ for the GitHub release page, because GIF is the only
# animated format release markdown renders everywhere. The WebP is for the site.
set -euo pipefail

cd "$(dirname "$0")"

VERSION="${1:?usage: render.sh <version> <codename> [seed]}"
CODENAME="${2:?usage: render.sh <version> <codename> [seed]}"
SEED="${3:-260}"

SLUG="$(echo "$CODENAME" | tr '[:upper:]' '[:lower:]')"
STEM="hero-${SLUG}-v${VERSION}"
mkdir -p out

# Remotion needs a Chromium. Prefer a headless shell: full Chrome refuses to run
# in old-headless mode, which is what Remotion asks for.
find_browser() {
  if [[ -n "${REMOTION_BROWSER:-}" ]]; then echo "$REMOTION_BROWSER"; return; fi
  local c
  for c in \
    "$HOME"/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell \
    "$HOME"/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell \
    /opt/pw-browsers/chromium_headless_shell-*/chrome-linux/headless_shell; do
    [[ -x "$c" ]] && { echo "$c"; return; }
  done
  echo ""
}

BROWSER="$(find_browser)"
BROWSER_ARG=()
if [[ -n "$BROWSER" ]]; then
  BROWSER_ARG=(--browser-executable="$BROWSER")
  echo "==> chromium: $BROWSER"
else
  echo "==> chromium: letting Remotion resolve its own"
fi

PROPS="$(printf '{"version":"%s","codename":"%s","seed":%s}' "$VERSION" "$CODENAME" "$SEED")"

# The brand face lives once, in site/fonts/, and Remotion can only load it from
# public/. Copy it in rather than committing a second copy: a tracked duplicate
# drifts silently the day the site font is updated, and the hero would keep
# rendering the old face with nothing to show for it.
FONT_SRC="../../../site/fonts/Geist-Variable.woff2"
if [[ ! -f "$FONT_SRC" ]]; then
  echo "error: brand font not found at $FONT_SRC (run from a full checkout)" >&2
  exit 1
fi
mkdir -p public
cp -f "$FONT_SRC" public/

echo "==> 1/4 MP4 (source of truth, 1920x1080 30fps)"
npx remotion render src/index.ts Hero "out/${STEM}.mp4" \
  --codec h264 --crf 16 --props="$PROPS" "${BROWSER_ARG[@]}" --overwrite

# The background is identical on every frame by design, so both encoders below
# are told to exploit inter-frame similarity rather than re-encode the field.

# 800px at 10fps with a 96-colour adaptive palette and no dithering. Dithering
# looks better on a still but its noise pattern defeats inter-frame compression,
# and on this artwork it roughly doubled the file for no visible gain once the
# grain is already there. 10fps keeps every 3rd frame, which divides the 6-frame
# escapement period exactly, so the tick stays crisp instead of stuttering.
echo "==> 2/4 GIF (release page, 800px @ 10fps)"
ffmpeg -v error -y -i "out/${STEM}.mp4" \
  -vf "fps=10,scale=800:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=96:stats_mode=diff[p];[s1][p]paletteuse=dither=none:diff_mode=rectangle" \
  -loop 0 "out/${STEM}.gif"

# Quality 80 at 15fps, and both halves of that matter for the LOOP rather than
# for the picture. libwebp builds an animation by blending each frame over the
# last, so lossy error accumulates in a fixed direction; on the fine dial strokes
# it brightened them steadily across the loop and then snapped back at the wrap,
# which is exactly where a seamless loop gets caught out. Measured on the dial
# band, first-to-last drift runs 0.59 at q60 and 0.004 at q80. Halving the frame
# count halves the accumulation as well, and 15fps still divides the 6-frame beat.
# The result is smaller than the drifting q60/30fps encode it replaces.
echo "==> 3/4 WebP (site, 1280x720 @ 15fps)"
ffmpeg -v error -y -i "out/${STEM}.mp4" \
  -vf "fps=15,scale=1280:-1:flags=lanczos" \
  -vcodec libwebp_anim -lossless 0 -quality 80 -compression_level 6 -preset picture \
  -loop 0 "out/${STEM}.webp"

# Poster fallback for anywhere animation is stripped. Rendered to PNG first
# because that is what Remotion emits losslessly, then converted to WebP, which
# is a fraction of the size at the same visible quality and matches the format
# the animated site asset already uses.
echo "==> 4/4 Poster still (WebP)"
npx remotion still src/index.ts Hero "out/${STEM}-poster.png" \
  --frame=0 --props="$PROPS" "${BROWSER_ARG[@]}" --overwrite
ffmpeg -v error -y -i "out/${STEM}-poster.png" -vcodec libwebp -lossless 0 -q:v 88 \
  -compression_level 6 -preset picture "out/${STEM}-poster.webp"
rm -f "out/${STEM}-poster.png"

echo
echo "==> done"
ls -lh "out/${STEM}".{mp4,gif,webp} "out/${STEM}-poster.webp" | awk '{printf "    %-46s %s\n", $9, $5}'
