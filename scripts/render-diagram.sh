#!/usr/bin/env bash
# Render docs/architecture.d2 to docs/img/architecture.svg and docs/img/architecture.png.
# The PNG targets TARGET_WIDTH px and must land in [MIN_WIDTH, MAX_WIDTH].
# Renderers, first that works: d2 (native rasteriser), rsvg-convert, qlmanage (macOS).
set -euo pipefail
cd "$(dirname "$0")/.."

SRC=docs/architecture.d2
SVG=docs/img/architecture.svg
PNG=docs/img/architecture.png
D2_VERSION=v0.9.0
TARGET_WIDTH=3000
MIN_WIDTH=2400
MAX_WIDTH=4000
D2_FLAGS=(--layout elk --pad 60 --omit-version)

# Pinned .bin/d2 first, then $D2 from the caller, then PATH.
if [ -x .bin/d2 ]; then
  D2=.bin/d2
else
  D2="${D2:-$(command -v d2 || true)}"
fi
if [ -z "$D2" ] || ! command -v "$D2" >/dev/null 2>&1; then
  echo "render-diagram: d2 not found; run 'make tools'" >&2
  exit 1
fi
if ! "$D2" --version | grep -qx "$D2_VERSION"; then
  echo "render-diagram: $D2 is $("$D2" --version), need $D2_VERSION; run 'make tools'" >&2
  exit 1
fi

errlog=$(mktemp)
trap 'rm -f "$errlog"' EXIT

mkdir -p docs/img

"$D2" validate "$SRC"
"$D2" "${D2_FLAGS[@]}" "$SRC" "$SVG"

png_size() { python3 scripts/png_width.py "$PNG"; }

# Width of the SVG viewBox (first viewBox attribute in the file).
svg_width() {
  grep -o -m1 'viewBox="[^"]*"' "$SVG" | head -n1 | awk -F'[" ]' '{print $4}'
}

# d2 v0.9.0: PNG px = 2 x scale x viewBox width.
scale_for() { python3 -c "print(round($TARGET_WIDTH / (2 * $1), 3))"; }

# Render the PNG with d2 at scale $1. On a raster work limit error, retry at a
# lower scale (up to 3 times). Returns non-zero for any other failure.
d2_png() {
  local scale="$1" try
  for try in 1 2 3 4; do
    rm -f "$PNG"
    if "$D2" "${D2_FLAGS[@]}" --scale "$scale" "$SRC" "$PNG" 2>"$errlog"; then
      used_scale="$scale"
      return 0
    fi
    cat "$errlog" >&2
    if [ "$try" -lt 4 ] && grep -q 'exceeds limit' "$errlog"; then
      scale=$(python3 -c "print(round($scale * 0.7, 3))")
      echo "render-diagram: raster limit hit, retrying with --scale $scale" >&2
      continue
    fi
    rm -f "$PNG"
    return 1
  done
}

render_png_d2() {
  local vbw scale width
  vbw=$(svg_width)
  [ -n "$vbw" ] || { echo "render-diagram: no viewBox in $SVG" >&2; return 1; }
  scale=$(scale_for "$vbw")
  d2_png "$scale" || return 1
  read -r width _ _ < <(png_size) || return 1
  [ -n "$width" ] || return 1
  if [ "$width" -lt "$MIN_WIDTH" ] || [ "$width" -gt "$MAX_WIDTH" ]; then
    scale=$(python3 -c "print(round($used_scale * $TARGET_WIDTH / $width, 3))")
    echo "render-diagram: width $width outside [$MIN_WIDTH, $MAX_WIDTH]; re-rendering with --scale $scale"
    d2_png "$scale" || return 1
  fi
}

render_png_fallback() {
  rm -f "$PNG"
  if ! command -v rsvg-convert >/dev/null 2>&1 && [ "${CI:-}" = "true" ] && command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq librsvg2-bin
  fi
  if command -v rsvg-convert >/dev/null 2>&1; then
    rsvg-convert -w "$TARGET_WIDTH" "$SVG" -o "$PNG"
    renderer=rsvg-convert
  elif [ "$(uname -s)" = Darwin ] && command -v qlmanage >/dev/null 2>&1; then
    qlmanage -t -s "$TARGET_WIDTH" -o docs/img "$SVG" >/dev/null
    mv docs/img/architecture.svg.png "$PNG"
    renderer=qlmanage
  else
    echo "render-diagram: no PNG renderer available (d2, rsvg-convert, qlmanage)" >&2
    exit 1
  fi
}

if render_png_d2; then
  renderer=d2
else
  echo "render-diagram: d2 PNG export failed; trying SVG converters" >&2
  render_png_fallback
fi

read -r width height ratio < <(png_size)
echo "renderer=$renderer width=$width height=$height ratio=$ratio"
python3 scripts/png_width.py "$PNG" --min "$MIN_WIDTH" --max "$MAX_WIDTH" >/dev/null
