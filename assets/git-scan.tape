# git-scan.gif — nmemory git-scan: stored facts checked against real commits.
#
# Regenerate:  cd capabilities/nmemory/assets && vhs git-scan.tape
#
# Every line below drives the real `nmemory` 0.2.0 binary against a THROWAWAY
# store and a THROWAWAY git repository, both created by `mktemp -d`. No
# existing store is read or written: NMEMORY_DB is exported to the temp store
# before the binary is ever called. The git repo is hermetic (no user or
# system config, no hooks, fixed identity and commit dates), so the commit
# shas on screen are the same on every run.

Output "git-scan.gif"
Require jq
Require git

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
Set WindowBar Rings
Set WindowBarSize 56
Set Framerate 10
Set TypingSpeed 1ms
Set Theme { "background": "#F1E7D9", "foreground": "#252422", "cursor": "#A74726", "selection": "#E3D3BE", "black": "#252422", "red": "#A74726", "green": "#8A7A63", "yellow": "#C87832", "blue": "#7A6A55", "magenta": "#A74726", "cyan": "#8A7A63", "white": "#252422", "brightBlack": "#8A7A63", "brightRed": "#D9612F", "brightGreen": "#8A7A63", "brightYellow": "#C87832", "brightBlue": "#7A6A55", "brightMagenta": "#A74726", "brightCyan": "#8A7A63", "brightWhite": "#252422" }

Hide

# --- throwaway store + repo; the real 0.2.0 binary on PATH ---
Type `export TMPDIR=/var/tmp; D=$(mktemp -d); export NMEMORY_DB=$D/memory.sqlite3; PATH=$HOME/.local/bin:$PATH`
Enter

# --- hermetic git: no user/system config, no hooks, fixed identity + dates ---
Type `export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null`
Enter
Type `export GIT_AUTHOR_NAME=demo GIT_AUTHOR_EMAIL=demo@example.com GIT_COMMITTER_NAME=demo GIT_COMMITTER_EMAIL=demo@example.com`
Enter
Type `export GIT_AUTHOR_DATE=2026-01-05T09:00:00Z GIT_COMMITTER_DATE=2026-01-05T09:00:00Z`
Enter
Type `mkdir -p $D/repo/src && cd $D/repo && git init -q -b main --template= .`
Enter
Type `printf 'pub struct Retry;\n\n\n\n\n\n\n\n\n\n\n// budget: 3 attempts\npub const BUDGET: u32 = 3;\n' > src/retry.rs`
Enter
Type `printf 'pub struct Pool;\n\npub const MAX: u32 = 20;\n' > src/pool.rs`
Enter
Type `printf 'pub struct Legacy;\n\n\n\n\n\n// v1 tokens still land here\npub fn accept_v1() {}\n' > src/legacy.rs`
Enter
Type `git add -A && git commit -q -m "initial commit"`
Enter

# --- capture: anchors resolve against this repo ---
Type `export NMEMORY_ANCHOR_ROOT=$D/repo`
Enter
Type `nm(){ jq -nc --arg t "$1" --argjson a "$2" '{jsonrpc:"2.0",id:1,method:"initialize",params:{protocolVersion:"2025-06-18",capabilities:{},clientInfo:{name:"demo",version:"0"}}},{jsonrpc:"2.0",method:"notifications/initialized"},{jsonrpc:"2.0",id:2,method:"tools/call",params:{name:$t,arguments:$a}}' | nmemory --db "$NMEMORY_DB" --project demo 2>/dev/null | jq -M -r 'select(.id==2)|.result.content[0].text'; }`
Enter
Type `ingest(){ nm memory_ingest "$(jq -nc --arg c "$1" --arg s "${2%%:*}" --arg a "$2" '{content:$c,source:$s,anchor:$a}')" >/dev/null; }`
Enter
Type `ingest "Retry budget is 3 attempts." src/retry.rs:12`
Enter
Type `ingest "Pool max is 20 connections." src/pool.rs:3`
Enter
Type `ingest "Legacy path still accepts v1 tokens." src/legacy.rs:7`
Enter

# --- the code moves on, for real: one file edited, one file deleted ---
Type `export GIT_AUTHOR_DATE=2026-02-11T11:30:00Z GIT_COMMITTER_DATE=2026-02-11T11:30:00Z`
Enter
Type `printf 'pub struct Retry;\n\n\n\n\n\n\n\n\n\n\n// budget: 5 attempts\npub const BUDGET: u32 = 5;\n' > src/retry.rs`
Enter
Type `git commit -q -am "retry: raise the budget to 5 (cap-1)"`
Enter
Type `export GIT_AUTHOR_DATE=2026-03-02T15:45:00Z GIT_COMMITTER_DATE=2026-03-02T15:45:00Z`
Enter
Type `git rm -q src/legacy.rs && git commit -q -m "drop the legacy v1 token path"`
Enter

# --- presenter: the tool's own JSON, projected (jq -M so output is flat) ---
Type `memory_recall(){ nm memory_retrieve "$(jq -nc --args '{terms:$ARGS.positional}' "$@")" | jq -M -c '.results[]|{id,anchor:.provenance.anchor,corroboration:(.corroboration|{path,content,mentions}|with_entries(select(.value!=null)))}'; }`
Enter

# --- brand prompt: U+203A as UTF-8 bytes (vhs cannot type non-ASCII) ---
Type `E=$(printf "\033"); A=$(printf "\xe2\x80\xba")`
Enter
Type `PS1="\[$E[38;2;200;120;50m\]$A \[$E[0m\]"; PS2="> "; clear`
Enter

Show
Sleep 300ms

Type `# three facts, each anchored to a real file:line in this repo`
Enter
Sleep 250ms

Type `git log --oneline`
Enter
Sleep 1100ms

Type `nmemory git-scan --repo . --project demo`
Enter
Sleep 1900ms

Type `# what the scan recorded, capsule by capsule:`
Enter
Sleep 250ms

Type `memory_recall retry pool legacy`
Enter
Sleep 1900ms

Type `# cap-1 drifted. cap-3 is gone. never a verdict it cannot back.`
Enter
Sleep 1800ms
