# pin.gif — memory_pin: a load-bearing fact stops fading, and is fenced anyway.
#
# Regenerate:  cd capabilities/nmemory/assets && vhs pin.tape && ./family-chrome.sh pin.gif
#
# The second step is NOT optional: vhs 0.11.0 hardcodes macOS traffic-light
# colours on `WindowBar Colorful` (the theme's red/yellow/green never reach it,
# and `WindowBarColor` is rejected by its parser), and `Set Framerate` has no
# effect on GIF output in this build. family-chrome.sh repaints exactly that
# window chrome to the brand coppers and resamples to the family's 10cs — it
# never touches a pixel of terminal output.
#
# Every line below drives the real `nmemory` 0.2.0 binary against a THROWAWAY
# store created by `mktemp -d`. No existing store is read or written. The
# helpers are transport plus a jq projection of the tool's own JSON — they
# format real bytes and invent nothing.

Output "pin.gif"
Require jq

Set Shell "bash"
Set FontFamily "JetBrains Mono"
Set FontSize 11
Set LineHeight 1.33
Set Width 960
Set Height 540
Set Padding 28
Set Margin 28
Set MarginFill "#F4E8DB"
Set BorderRadius 14
Set WindowBar Colorful
Set WindowBarSize 56
Set Framerate 10
Set TypingSpeed 1ms
Set Theme { "background": "#F1E7D9", "foreground": "#252422", "cursor": "#A74726", "selection": "#E3D3BE", "black": "#252422", "red": "#A74726", "green": "#8A7A63", "yellow": "#C87832", "blue": "#7A6A55", "magenta": "#A74726", "cyan": "#8A7A63", "white": "#252422", "brightBlack": "#8A7A63", "brightRed": "#D9612F", "brightGreen": "#8A7A63", "brightYellow": "#C87832", "brightBlue": "#7A6A55", "brightMagenta": "#A74726", "brightCyan": "#8A7A63", "brightWhite": "#252422" }

Hide

# --- throwaway store; the real 0.2.0 binary ---
# Fixed scratch path, not mktemp: a random suffix makes every regeneration
# differ byte-for-byte wherever the path is echoed, and macOS ignores an
# exported TMPDIR here anyway (observed: mktemp kept /var/folders/...).
Type `D=/var/tmp/nmem-demo-pin; rm -rf $D; mkdir -p $D; DB=$D/memory.sqlite3; NM=$HOME/.local/bin/nmemory`
Enter

# --- one process per tool call: serial by construction, never pipelined ---
Type `nm(){ jq -nc --arg t "$1" --argjson a "$2" '{jsonrpc:"2.0",id:1,method:"initialize",params:{protocolVersion:"2025-06-18",capabilities:{},clientInfo:{name:"demo",version:"0"}}},{jsonrpc:"2.0",method:"notifications/initialized"},{jsonrpc:"2.0",id:2,method:"tools/call",params:{name:$t,arguments:$a}}' | "$NM" --db "$DB" --project demo 2>/dev/null | jq -M -r 'select(.id==2)|.result.content[0].text'; }`
Enter

# --- presenters: the tool's own JSON, projected (jq -M so output is flat) ---
Type `ingest(){ nm memory_ingest "$(jq -nc --arg c "$1" --arg a "$2" '{content:$c,source:"runbook.md",anchor:$a,valid_from:"2025-10-02T09:00:00Z",confidence:0.9}')" >/dev/null; }`
Enter
Type `memory_pin(){ nm memory_pin "$(jq -nc --arg i "$1" --arg r "$2" '{id:$i,pinned:true,reason:$r}')" | jq -M -c .; }`
Enter
Type `memory_relate(){ nm memory_relate "$(jq -nc --arg k "$1" --arg f "$2" --arg t "$3" '{kind:$k,from:$f,to:$t}')" | jq -M -c .; }`
Enter
Type `memory_recall(){ nm memory_retrieve "$(jq -nc --args '{terms:$ARGS.positional}' "$@")" | jq -M --indent 2 '{outcome,results:[.results[]?|{id,headline,valid_from:.freshness.valid_from,confidence,decayed_weight}],excluded}|with_entries(select(.value!=null and .value!=[]))'; }`
Enter

# --- seed: two facts of identical age and confidence, plus the successor ---
Type `ingest "Postgres pool max is 20." runbook.md:14`
Enter
Type `ingest "Backup window is 03:00 UTC." runbook.md:31`
Enter
Type `ingest "Postgres ceiling raised to 50." runbook.md:14`
Enter

# --- brand prompt: U+203A as UTF-8 bytes (vhs cannot type non-ASCII) ---
Type `E=$(printf "\033"); A=$(printf "\xe2\x80\xba")`
Enter
Type `PS1="\[$E[38;2;200;120;50m\]$A \[$E[0m\]"; PS2="> "; clear`
Enter

Show
Sleep 300ms

Type `# cap-1 is load-bearing:`
Enter
Sleep 200ms

Type `memory_pin cap-1 "every incident triage reads it"`
Enter
Sleep 500ms

Type `memory_recall pool backup`
Enter
Sleep 2400ms

Type `# decay exempt. not eligibility though:`
Enter
Sleep 250ms

Type `memory_relate supersedes cap-3 cap-1`
Enter
Sleep 450ms

Type `memory_recall pool`
Enter
Sleep 1300ms

Type `# pinned, superseded, still excluded.`
Enter
Sleep 2100ms
