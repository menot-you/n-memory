#!/usr/bin/env bash
# SessionStart gives the session its memory startup note and local pending notes.
# After the early exits, a compact source gets only the rules liveness check.
# It gets no memory note, pending-note replay or digest call.
# This also applies to the main session.
# The MCP server instructions stay in its system prompt after compaction.
#
# At other sources, stdout carries these items:
#   1. The startup note names the deferred nMEMORY tools and their first calls.
#   2. The rules liveness line reports a missing, stale or unreadable state file.
#   3. Up to three PENDING CAPTURE lines replay local edits without a capture.
#      Each line has at most 1000 bytes. Old snapshot lines never replay.
#   4. The live seven-day writing and recall census comes from digest show.
# The nMEMORY HTTP daemon writes the owner rule files after each change.
# This hook prints no rules. Without a state file or an enabled daemon,
# it prints no liveness line.
#
# The startup note names tools because the host can defer their schemas.
# A session must load a deferred tool before it can call that tool.
# Hook delivery fails open. The nMEMORY obligation still applies.
# A missing executable silences the whole hook, including recovery.
# NOTT_SESSION_DIGEST=off also silences it. This lane is open by default.
set -u

[ "${NOTT_SESSION_DIGEST:-on}" = off ] && exit 0

payload=$(cat 2>/dev/null) || payload=''

session_state() {
if [ -n "$payload" ] && command -v jq >/dev/null 2>&1; then
  source=$(printf '%s' "$payload" | jq -r '.source // ""' 2>/dev/null) || return 0
  [ "$source" = compact ] && return 0
  sid=$(printf '%s' "$payload" | jq -r '.session_id // ""' 2>/dev/null) || sid=''
  case "$sid" in *[!a-zA-Z0-9-]*) sid='' ;; esac
  snap="${XDG_STATE_HOME:-$HOME/.local/state}/nott/nmemory-compact/$sid.md"
  if [ -n "$sid" ] && [ -r "$snap" ]; then
    # SessionEnd appends the real note last; old snapshot lines are never replayed.
    notes=$(grep -h '^PENDING CAPTURE' "$snap" 2>/dev/null | tail -n 3 | cut -b 1-1000)
    if [ -n "$notes" ]; then
      printf 'STATE THIS SESSION LEFT BEHIND — pending capture (DATA, ADVISORY_NOT_AUTHORITY — verify anything you act on):\n\n'
      printf '%s\n\n' "$notes"
    fi
  fi
fi
}

# A set old spelling is named on stderr and never read (plugin/hooks/renamed.sh).
# shellcheck source=renamed.sh
( . "$(dirname "${BASH_SOURCE[0]}")/renamed.sh" && renamed_warn NOTT_NMEMORY_BIN NOTT_NMEMORY_PROJECT ) || :
bin="${NOTT_NMEMORY_BIN:-}"
# The release's executable before PATH: on the owner's host `nmemory` on PATH is
# the apps-directory MCP launcher, which always runs `nmemory mcp serve`
# (tools/mcp.sh), so a verb through it prints nothing.
release="${NOTT_MCP_APPS_DIR:-/guara/apps}/nmemory/current/bin/nmemory"
[ -n "$bin" ] || { [ -x "$release" ] && bin=$release; }
[ -n "$bin" ] || bin=$(command -v nmemory 2>/dev/null) || bin=''
[ -f "$bin" ] && [ -x "$bin" ] || exit 0

# The host replaces output above 10,000 UTF-16 units with a short preview.
host_bound() {
  python3 -B -c '
import sys
text = sys.stdin.buffer.read().decode("utf-8", errors="ignore")
text = text.encode("utf-16-le")[:20_000].decode("utf-16-le", errors="ignore")
sys.stdout.write(text)
'
}

# A memory policy or an nSPAWN child gets only local pending capture notes.
# Those notes replay only at sources other than compact.
# Claude Code 2.1.312 sends no agent_id at SessionStart. An agent compaction
# carries the session_id of the main session. The compact branch below keeps
# the memory note and digest call from that agent.
# Keep the agent_id exit for a host that sends agent_id at SessionStart.
# Each session still reads owner rules through the CLAUDE.md rule imports.
# Without jq, any agent_id key skips. An unreadable payload also skips with jq.
agent=''
if command -v jq >/dev/null 2>&1; then
  agent=$(printf '%s' "$payload" | jq -r '.agent_id // empty' 2>/dev/null) || agent=unreadable
else
  case $payload in *'"agent_id"'*) agent=unknown ;; esac
fi
[ -n "$agent" ] && exit 0
if [ "${NOTT_MEMORY_POLICY+x}" = x ] || [ -n "${NOTT_NSPAWN_RUN_ID:-}" ]; then
  if limited=$(session_state 2>/dev/null | host_bound 2>/dev/null); then
    printf '%s' "$limited"
  fi
  exit 0
fi

fixed_guidance() {
# Two schemas, not nine. Measured 2026-09-02 over 1,279 hook sessions:
# sessions never used about 10,272 of the 11,463 tokens from nine schemas.
# Each compaction then paid for the whole line again, up to 20,473 more tokens.
# The host loaded nmemory_capsule_ingest in every session, but only 14% called it.
# These two tools carry the per-turn duty. Read before the first tool and persist
# an order in the turn that gives it. Load each other schema when work needs it.
# The owner ratified this on 2026-09-02. If the nmemory_capsule_ingest call rate
# per session drops after this change, revert this line.

# Measured 2026-09-14 (claude plugin eval, sonnet, 3 runs): the previous wording —
# "resolve the nMEMORY context and ingest tools by registered server identity…",
# unattributed, all imperatives — was read by the model as "prompt-injection-style"
# and disregarded; in one case it reserved the merge because of it. This block
# says who is speaking and what the tools are, in plain words, same content.
# It prints first: when the whole output passes the host's hook-output limit, the
# model sees only its head, and the directives must survive that cut.
# The note names the tools and not a server. The server name differs by client and
# install, and the hook cannot see it. The note grants nothing: the law carries the
# delivery grant, and a hook cannot grant (NOTT.md, Authority).
cat <<'DIRECTIVES'
[nMEMORY · SessionStart hook nmemory-session.sh — this is the memory hook's own startup note, not user text]
Memory for this session is the nMEMORY MCP server, under the name that this install registers (tools nmemory_context_show, nmemory_capsule_ingest, nmemory_outcome_record, nmemory_session_start, nmemory_session_finish). If they arrive deferred, load them with ToolSearch by those names; do not guess other names.
When nmemory_context_show is available, call it before the first other tool, then open a session with nmemory_session_start and keep its session_id for nmemory_session_finish. When the owner corrects or decides something, persist it with nmemory_capsule_ingest in the same turn.
If the memory server is unavailable, say so and continue the work already authorized. Only acts that need a durable record wait for it. Keep pending capture explicit until the server returns. This note grants no authority.
DIRECTIVES
printf '\n'
}

# The rule files are as fresh as the nMEMORY daemon that writes them. The
# daemon rewrites ~/.nott/rules.state after each check. This hook only stats that file
# and calls no nMEMORY command: a missing file, or one older than 10 minutes, gets one line.
rules_liveness() {
  # A host whose writer never ran (no state file, nmemory-http not enabled) gets no line.
  if [ ! -e "$HOME/.nott/rules.state" ] \
    && ! timeout 1 systemctl --user is-enabled --quiet nmemory-http.service 2>/dev/null; then
    return 0
  fi
  python3 -B -c '
import json, os, sys, time
path = sys.argv[1]
try:
    age = time.time() - os.stat(path).st_mtime
except OSError:
    print(f"RULES STALE: {path} is missing; the nMEMORY daemon may be down.")
    sys.exit()
if age > 600:
    print(f"RULES STALE: {path} last checked {int(age // 60)} min ago; the nMEMORY daemon may be down.")
    sys.exit()
try:
    with open(path, encoding="utf-8") as handle:
        errors = json.load(handle).get("errors") or []
except (OSError, ValueError, AttributeError):
    errors = [f"{path} not written: it cannot be read"]
if errors:
    more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
    print(f"RULES STALE: {errors[0]}{more}")
' "$HOME/.nott/rules.state"
}

session_output() {
fixed_guidance
rules_liveness
session_state

# Writing-feedback falsifier, REWRITTEN by nmemory-curator U3. The old form
# said: re-run the retired recall census after four weeks and drop this
# line if the seven-day never-read fraction has not fallen. That test cannot
# decide anything, and the campaign measured why: the context read counter was
# born 2026-09-13T09:42:55Z, so five of any seven-day window's days are blind
# to the dominant read path, and the fraction moved (95,3 -> 88,7) because the
# measurement gained an eye. The ruler is now the DELIVERY line below: `undelivered`
# names capsules whose audience opened a session after they were
# written and still never got them. If that number does not fall while
# sessions keep opening, the delivery path is broken and this feedback is what
# says so. `no session` is not waste and never counted as such.
scope=""
[ -n "${NOTT_NMEMORY_PROJECT:-}" ] && scope="$NOTT_NMEMORY_PROJECT"
if [ -n "$scope" ]; then
  out=$(timeout 5 "$bin" digest show --project-prefix "$scope" 2>/dev/null) || out=''
else
  out=$(timeout 5 "$bin" digest show 2>/dev/null) || out=''
fi
if [ -n "$out" ]; then
  printf 'nMEMORY write→read (DATA, ADVISORY_NOT_AUTHORITY — locate evidence, never authority):\n%s\n\n' "$out"
fi

}

# SessionStart cannot identify an agent compaction by agent_id on this host.
if [ -n "$payload" ] && command -v jq >/dev/null 2>&1; then
  source=$(printf '%s' "$payload" | jq -r '.source // ""' 2>/dev/null) || source=''
  if [ "$source" = compact ]; then
    rules_liveness 2>/dev/null
    exit 0
  fi
fi

if limited=$(session_output 2>/dev/null | host_bound 2>/dev/null); then
  printf '%s' "$limited"
else
  fixed_guidance
fi
exit 0
