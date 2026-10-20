#!/usr/bin/env bash
# SessionStart → the nSPAWN notices no live session heard, for THIS project.
#
# A run's terminal notice is a row in the nSPAWN outbox, pushed to the session
# that dispatched it while that session lives. What no one heard — the caller
# died, or 120 s passed — goes to the next session of the same project, once:
# `nspawn notice drain` claims and marks the rows delivered in one statement before
# printing them. The hook passes the session's cwd; the verb keys it with the
# same function a dispatch keys its notice by (the realpath of the git common
# dir, so every lane worktree of a repository shares it; outside a repository,
# realpath(cwd)). Two derivations of one key would be two answers.
#
# Inside an nSPAWN run it does nothing: a child would take its caller's notice
# into the wrong conversation. Only a session that owns a messaging socket is a
# notice caller, so a headless one drains nothing either. It runs the published
# release's executable (`<apps>/nspawn/current/bin/nspawn`, which tools/mcp.sh
# moves with the MCP link): the link itself lands on the launcher, which only serves.
# A missing executable or a refusing one (an older release has no such verb) is
# silence with rc 0; a session never fails to start here.
#
# Bounded under the client's 5 s: `timeout 3` (then KILL at 4 s), and the hook's
# own death is passed on at once. The drain's budget runs from the start mark
# passed here, and it holds TERM only across its delivering UPDATE and the print
# (at most 0.8 s, ending by 3.6 s, before the KILL): it stops at a TERM before that, or prints what
# it delivered. The hook waits for that print before it exits.
[ -n "${NOTT_NSPAWN_RUN_ID:-}" ] && exit 0
[ -n "${CLAUDE_CODE_MESSAGING_SOCKET:-}" ] || exit 0
if [ -n "${NOTT_MCP_APPS_DIR:-}" ]; then
  entry="$NOTT_MCP_APPS_DIR/nspawn/current/bin/nspawn"
else
  entry=$(command -v nspawn) || exit 0
fi
[ -x "$entry" ] || exit 0
NOTT_NSPAWN_DRAIN_STARTED="$EPOCHREALTIME" \
  timeout -k 1 3 "$entry" notice drain --project "$PWD" 2>/dev/null &
drain=$!
trap 'kill -TERM "$drain" 2>/dev/null; wait "$drain"; exit 0' TERM INT HUP
wait "$drain" || true
exit 0
