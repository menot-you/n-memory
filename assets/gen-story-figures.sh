#!/usr/bin/env bash
# gen-story-figures.sh — the two "get it in five seconds" figures for nMEMORY.
#
# These are the plain-language companions to one-rule.svg and architecture.svg.
# Those two are correct and stay; they speak in the product's own vocabulary
# (grounded / missing_evidence / abstain, MCP, stdio, sqlite3). A reader deciding
# whether to try this gives the screen five seconds and needs the story, not the
# schema. So: same brand, same font, no jargon on the frame.
#
#   how-it-works.svg  — one question, the only three answers it can give.
#   self-check.svg    — it re-reads your commits and grades its own notes.
#
# Usage: gen-story-figures.sh [OUTDIR]   (default: this script's directory)
# PNGs are rendered with rsvg-convert at 1x.
set -euo pipefail

OUT=${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}
mkdir -p "$OUT"

# Brand identity (owner-locked, byte-identical to the rest of the family).
PAPER=#F4E8DB; INK=#252422; COPPER=#A74726; COPPER2=#C87832
MUTED=#62594F; CHROME=#F8EFE5; FAINT=#9B887A; CARD_LINE=#CEB9A6
FONT='DejaVu Sans Mono, Noto Sans Mono, monospace'

esc() { printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }

txt() { # x y size weight fill content
  printf '<text fill="%s" font-family="%s" font-size="%s" font-weight="%s" x="%s" y="%s">%s</text>' \
    "$5" "$FONT" "$3" "$4" "$1" "$2" "$(esc "$6")"
}

panel() { # x y w h stroke [stroke-width]
  printf '<rect fill="%s" height="%s" rx="18" ry="18" stroke="%s" stroke-width="%s" width="%s" x="%s" y="%s"/>' \
    "$CHROME" "$4" "$5" "${6:-1.5}" "$3" "$1" "$2"
}

spine() { # x y h color
  printf '<rect fill="%s" height="%s" rx="5" ry="5" width="10" x="%s" y="%s"/>' "$4" "$3" "$1" "$2"
}

arrow() { # x1 x2 y
  printf '<line stroke="%s" stroke-linecap="round" stroke-width="2.2" x1="%s" x2="%s" y1="%s" y2="%s"/>' \
    "$MUTED" "$1" "$2" "$3" "$3"
  printf '<polygon fill="%s" points="%s,%s %s,%s %s,%s"/>' \
    "$MUTED" "$2" "$3" "$(($2 - 11))" "$(($3 + 7))" "$(($2 - 11))" "$(($3 - 7))"
}

# ---------------------------------------------------------------- figure one --
# One question. The only three answers it can give. The third card is the point:
# an honest "I don't know" is the feature, not a gap.
emit_how_it_works() {
  { printf '<?xml version="1.0" encoding="utf-8"?><svg viewBox="0 0 1600 900" width="1600" height="900" xmlns="http://www.w3.org/2000/svg">'
    printf '<rect fill="%s" width="1600" height="900"/>' "$PAPER"

    txt 70 84 44 bold "$INK" "ASK IT ANYTHING. IT ANSWERS "
    txt 812 84 44 bold "$COPPER" "ONE OF THREE WAYS."
    txt 70 132 22 normal "$MUTED" "a memory for coding agents that would rather say nothing than make something up"

    # The question, in a human's words.
    txt 70 375 19 normal "$MUTED" "your agent, halfway through a task"
    panel 70 400 690 140 "$CARD_LINE"
    txt 104 480 30 normal "$INK" "“wait — why did we drop Redis?”"
    txt 70 600 19 normal "$MUTED" "it does not answer from vibes. it answers from what it can show you."

    # Fan-out from the question to the three answers.
    arrow 760 810 470
    printf '<line stroke="%s" stroke-width="2.2" x1="810" x2="810" y1="255" y2="685"/>' "$MUTED"

    _answer() { # y color label answer tag
      arrow 810 870 "$(($1 + 85))"
      panel 880 "$1" 660 170 "$2" 2.3
      spine 880 "$1" 170 "$2"
      txt 920 "$(($1 + 48))" 27 bold "$2" "$3"
      txt 920 "$(($1 + 96))" 24 normal "$INK" "$4"
      txt 920 "$(($1 + 136))" 17.5 normal "$MUTED" "$5"
    }
    _answer 170 "$COPPER"  "IT KNOWS" \
      "“The failover bug. You called it 3 Mar.”" \
      "attached: the commit it read that from"
    _answer 385 "$COPPER2" "IT KNEW, AND IT WENT STALE" \
      "“I have a note. You overruled it since.”" \
      "so it shows you the note is dead, and why"
    _answer 600 "$FAINT"   "IT DOESN'T KNOW" \
      "“No idea. I am not going to guess.”" \
      "silence, on purpose — never an invented answer"

    printf '<line stroke="%s" stroke-width="1.5" x1="0" x2="1600" y1="800" y2="800"/>' "$CARD_LINE"
    txt 70 855 25 normal "$INK" "The first answer is easy. The other two are why this exists."
    printf '</svg>'; } > "$OUT/how-it-works.svg"
}

# ---------------------------------------------------------------- figure two --
# It re-reads your commits and grades every note it kept. Left column is what it
# believed; right column is what your repo says today.
emit_self_check() {
  { printf '<?xml version="1.0" encoding="utf-8"?><svg viewBox="0 0 1600 900" width="1600" height="900" xmlns="http://www.w3.org/2000/svg">'
    printf '<rect fill="%s" width="1600" height="900"/>' "$PAPER"

    txt 70 84 44 bold "$INK" "IT GOES BACK AND "
    txt 521 84 44 bold "$COPPER" "CHECKS WHAT IT TOLD YOU."
    txt 70 132 22 normal "$MUTED" "point it at your repo and it re-reads your commits, then grades every note it kept"

    txt 70 205 18 bold "$MUTED" "WHAT IT WROTE DOWN"
    txt 830 205 18 bold "$MUTED" "WHAT YOUR REPO SAYS TODAY"

    _row() { # cy color note where label detail
      panel 70 "$(($1 - 60))" 620 120 "$CARD_LINE"
      txt 106 "$(($1 - 8))" 22 normal "$INK" "$3"
      txt 106 "$(($1 + 26))" 17 normal "$MUTED" "$4"
      arrow 710 800 "$1"
      panel 830 "$(($1 - 60))" 690 120 "$2" 2.3
      spine 830 "$(($1 - 60))" 120 "$2"
      txt 872 "$(($1 - 6))" 26 bold "$2" "$5"
      txt 872 "$(($1 + 28))" 18 normal "$MUTED" "$6"
    }
    _row 300 "$COPPER"  "the auth check lives in login.rs:42" "learned six weeks ago, from your commit" \
      "STILL TRUE" "the line is exactly where the note said it would be"
    _row 460 "$COPPER2" "the retry cap is 3" "learned from src/client.rs:88" \
      "IT MOVED" "the code shifted under the note — flagged, not deleted"
    _row 620 "$FAINT"   "config loads from env.toml" "learned in March" \
      "NOTHING BACKS IT" "your repo no longer says this anywhere"

    printf '<line stroke="%s" stroke-width="1.5" x1="0" x2="1600" y1="730" y2="730"/>' "$CARD_LINE"
    txt 70 776 17 normal "$MUTED" "this scan:"
    printf '<rect fill="#F1E0D0" height="54" rx="14" ry="14" stroke="#E8D7C6" stroke-width="1" width="450" x="70" y="796"/>'
    printf '<text fill="%s" font-family="%s" font-size="24" font-weight="bold" text-anchor="middle" x="295" y="830">%s</text>' \
      "$INK" "$FONT" "1 still true · 1 moved · 1 gone"
    txt 570 830 23 normal "$INK" "It grades its own memory. What it cannot check, it does not grade."
    printf '</svg>'; } > "$OUT/self-check.svg"
}

emit_how_it_works
emit_self_check

for f in how-it-works self-check; do
  rsvg-convert -w 1600 -h 900 "$OUT/$f.svg" -o "$OUT/$f.png"
  printf '%s\n' "$(cd "$OUT" && ls -l "$f.svg" "$f.png" | awk '{print $9, $5"B"}')"
done
