#!/usr/bin/env bash
# family-chrome — bring a vhs-rendered beat gif into the asset family's chrome.
#
# Why this exists: the 0.1.x beat gifs (honest-recall.gif, falsifies.gif) carry
# three FILLED window dots in the brand coppers — #D9612F, #C87832, #A74726 —
# at 10cs per frame. vhs 0.11.0 cannot produce either from a tape: on
# `WindowBar Colorful` it hardcodes the macOS traffic lights (the theme's
# red/yellow/green never reach them and `WindowBarColor` is rejected by its
# parser), and `Set Framerate` has no effect on its GIF output (always 4cs).
# So the family look was ALWAYS a post-render step; this script is that step,
# committed, so the gifs regenerate instead of rotting.
#
# Architecture note, learned the observed way: ffmpeg is the ONLY compositor
# here. ImageMagick's -coalesce disagrees with ffmpeg's GIF disposal/
# transparency semantics badly enough that a pure magick read-write round trip
# of an ffmpeg-encoded GIF "changes" half the pixels of a composited frame.
# So the GIF is decomposed to flat PNG frames by ffmpeg, magick paints those
# flat frames (no GIF semantics anywhere near it), and ffmpeg reassembles.
#
#   1. decompose at the family's 10 fps into full RGB frames;
#   2. repaint the window CHROME on each frame: cover vhs's traffic lights
#      with the bar's own background, draw the three copper dots at the
#      family's measured centres (x=57/82/107, y=56, r=6), antialias off so
#      every painted pixel is one of four colours the image already carries;
#   3. reassemble at 10 fps with a palette built FROM the painted frames,
#      dither off;
#   4. fail closed unless: the painted frames differ from the raw frames
#      NOWHERE below the chrome floor (terminal output is evidence, and
#      repainting evidence would be forgery); the reassembled GIF decodes
#      byte-identical to the painted frames everywhere; the dots sample as
#      the coppers; and the frame delay is a uniform 10cs.
#
# usage: family-chrome.sh <gif> [more.gif ...]   (edits in place)
set -euo pipefail

die() { printf 'family-chrome: %s\n' "$*" >&2; exit 1; }
for c in magick ffmpeg; do command -v "$c" >/dev/null 2>&1 || die "missing required command: $c"; done
[ "$#" -ge 1 ] || die "usage: family-chrome.sh <gif> [more.gif ...]"

# The family's chrome, measured from honest-recall.gif's composited final
# frame (centres and radius from a pixel scan of the bar row; the colours are
# the brand coppers those pixels hold).
readonly DOT_Y=56 DOT_R=6
readonly DOT1_X=57  DOT1_COLOR='#D9612F'
readonly DOT2_X=82  DOT2_COLOR='#C87832'
readonly DOT3_X=107 DOT3_COLOR='#A74726'
# Everything painted lives above this line; everything below it is terminal
# output and MUST come out untouched.
readonly CHROME_FLOOR=90

for gif in "$@"; do
  [ -f "$gif" ] || die "not a file: $gif"
  work="$(mktemp -d)"
  trap 'rm -rf -- "$work"' EXIT

  # 1 — decompose. ffmpeg composites disposal/transparency correctly and
  # emits full frames; everything downstream is plain RGB PNG.
  mkdir -p "$work/raw" "$work/painted" "$work/check"
  ffmpeg -v error -y -i "$gif" -vf 'fps=10' "$work/raw/f_%04d.png"
  frame_count="$(find "$work/raw" -name 'f_*.png' | wc -l | tr -d ' ')"
  [ "$frame_count" -gt 0 ] || die "$gif: decomposition produced no frames"

  # 2 — paint the chrome on every flat frame.
  bar_bg="$(magick "$work/raw/f_0001.png" -format '%[pixel:p{300,56}]' info:)"
  for f in "$work"/raw/f_*.png; do
    magick "$f" +antialias \
      -fill "$bar_bg" -draw "rectangle 36,40 130,72" \
      -fill "$DOT1_COLOR" -draw "circle $DOT1_X,$DOT_Y $DOT1_X,$((DOT_Y + DOT_R))" \
      -fill "$DOT2_COLOR" -draw "circle $DOT2_X,$DOT_Y $DOT2_X,$((DOT_Y + DOT_R))" \
      -fill "$DOT3_COLOR" -draw "circle $DOT3_X,$DOT_Y $DOT3_X,$((DOT_Y + DOT_R))" \
      "$work/painted/$(basename "$f")"
  done

  # 4a — the evidence check, on the flat frames where it is unambiguous:
  # below the chrome floor the paint changed NOTHING, on ANY frame.
  for f in "$work"/raw/f_*.png; do
    p="$work/painted/$(basename "$f")"
    magick "$f" -crop "960x450+0+${CHROME_FLOOR}" +repage "$work/check/a.png"
    magick "$p" -crop "960x450+0+${CHROME_FLOOR}" +repage "$work/check/b.png"
    ae="$(magick compare -metric AE "$work/check/a.png" "$work/check/b.png" null: 2>&1 || true)"
    [ "${ae%% *}" = "0" ] || die "$gif: ${ae%% *} pixel(s) differ below the chrome floor in $(basename "$f") — refusing to touch terminal output"
  done

  # 4b — the reassembly must be lossless, which needs every colour to fit one
  # palette. Count before encoding; refuse instead of quantizing silently.
  colors="$(magick "$work"/painted/f_*.png -append -format '%k' info:)"
  [ "$colors" -le 256 ] || die "$gif: painted frames hold ${colors} colours; a 256-colour GIF would requantize terminal output"

  # 3 — reassemble at 10 fps, palette built from the painted frames, no dither.
  ffmpeg -v error -y -framerate 10 -i "$work/painted/f_%04d.png" -vf 'palettegen=max_colors=256' "$work/palette.png"
  ffmpeg -v error -y -framerate 10 -i "$work/painted/f_%04d.png" -i "$work/palette.png" \
    -lavfi 'paletteuse=dither=none' -loop 0 "$work/final.gif"

  # 4c — decode what was written and prove it equals the painted frames.
  ffmpeg -v error -y -i "$work/final.gif" "$work/check/out_%04d.png"
  out_count="$(find "$work/check" -name 'out_*.png' | wc -l | tr -d ' ')"
  [ "$out_count" = "$frame_count" ] || die "$gif: frame count changed in reassembly ($frame_count -> $out_count)"
  for f in "$work"/painted/f_*.png; do
    n="${f##*f_}"; n="${n%.png}"
    ae="$(magick compare -metric AE "$f" "$work/check/out_${n}.png" null: 2>&1 || true)"
    [ "${ae%% *}" = "0" ] || die "$gif: reassembly altered frame ${n} (${ae%% *} pixel(s)) — the encode was not lossless"
  done

  # 4d — the dots are the coppers, and the delay is the family's 10cs.
  for spec in "$DOT1_X $DOT1_COLOR" "$DOT2_X $DOT2_COLOR" "$DOT3_X $DOT3_COLOR"; do
    x="${spec%% *}"; want="${spec##* }"
    got="$(magick "$work/check/out_0001.png" -alpha off -format "%[pixel:p{$x,$DOT_Y}]" info:)"
    want_rgb="$(magick -size 1x1 "xc:$want" -alpha off -format '%[pixel:p{0,0}]' info:)"
    [ "$got" = "$want_rgb" ] || die "$gif: dot at x=$x decoded as $got, wanted $want ($want_rgb)"
  done
  delay="$(magick identify -format '%T\n' "$work/final.gif" | sort -u | tr -d '\n ')"
  [ "$delay" = "10" ] || die "$gif: frame delay is '${delay}', wanted uniform 10cs"

  mv -- "$work/final.gif" "$gif"
  rm -rf -- "$work"; trap - EXIT
  printf 'family-chrome: %s — %s frames at 10cs, dots %s/%s/%s, output below y=%s untouched, encode lossless\n' \
    "$gif" "$frame_count" "$DOT1_COLOR" "$DOT2_COLOR" "$DOT3_COLOR" "$CHROME_FLOOR"
done
