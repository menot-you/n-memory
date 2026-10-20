#!/usr/bin/env bash
# LD-0..LD-4: the five live nSPAWN observations, run against the worktree venv
# binary (or the binary that the first argument names), the REAL provider roster
# (~/.nott/spawn.toml) and, once L7 lands, the REAL Claude child. It writes
# nothing outside this worktree and the probe's temp directory. See
# mcps/nspawn/RULEBOOK.md for the interface contract and
# mcps/nspawn/tests/live/matrix_probe.py for the per-observation logic and
# exact refusal text.
#
# Exit code follows this repository's D18 convention, worst across the five
# rows: 0 green, 3 findings, 2 refusal/BLOCKED, 1 operational failure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && python3 -c 'from workspace_root import WORKSPACE_ROOT; print(WORKSPACE_ROOT)')"
VENV_BIN="${REPO_ROOT}/.venv/bin"
VENV_NSPAWN="${VENV_BIN}/nspawn"
NSPAWN_BIN="${1:-${VENV_NSPAWN}}"
PYTHON_BIN="${VENV_BIN}/python3"

# Only the default venv binary is synced: a binary that the caller names is
# used as it is, and the shared venv is not written.
# --inexact is mandatory: without it `uv sync --package` PRUNES the workspace
# venv down to this member’s dependencies alone, uninstalling the other
# thirty-nine packages. A repository sweep running in parallel
# would then fail with ImportError, and the live judge would have broken the gate
# it exists to feed. Measured: 39 packages removed on 2026-08-28.
if [[ $# -eq 0 ]]; then
  echo "== syncing the worktree venv (uv sync --inexact --package nspawn) ==" >&2
  if ! (cd "${REPO_ROOT}" && uv sync --inexact --package nspawn); then
    echo "OPERATIONAL: 'uv sync --inexact --package nspawn' failed; see the output above" >&2
    exit 1
  fi
else
  PYTHON_BIN="$(command -v python3)"
fi

if [[ ! -x "${NSPAWN_BIN}" ]]; then
  echo "OPERATIONAL: ${NSPAWN_BIN} does not exist or is not executable" >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "OPERATIONAL: ${PYTHON_BIN} does not exist" >&2
  exit 1
fi

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/matrix_probe.py" \
  --binary "${NSPAWN_BIN}" \
  --repo-root "${REPO_ROOT}"
