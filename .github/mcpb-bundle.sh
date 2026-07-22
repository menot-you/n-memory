#!/usr/bin/env bash
# Build an MCPB bundle (manifest.json + binary, zipped) for one release target.
# Usage: mcpb-bundle.sh <os: linux|macos> <arch>. Reads RELEASE_TAG (vX.Y.Z).
# MCPB spec: https://github.com/anthropics/mcpb/blob/main/MANIFEST.md (v0.3).
set -euo pipefail

os=${1:?usage: mcpb-bundle.sh <os> <arch>}
arch=${2:?usage: mcpb-bundle.sh <os> <arch>}
version=${RELEASE_TAG#v}

case "$os" in
  linux) platform=linux ;;
  macos) platform=darwin ;;
  *)
    printf 'mcpb-bundle: unsupported os: %s\n' "$os" >&2
    exit 64
    ;;
esac

bundle_dir=$(mktemp -d)
cp dist/nmemory "$bundle_dir/nmemory"
chmod +x "$bundle_dir/nmemory"

# Single quotes are deliberate: ${__dirname} is an MCPB placeholder the client
# expands at install time, never the shell.
cat > "$bundle_dir/manifest.json" <<EOF
{
  "manifest_version": "0.3",
  "name": "nmemory",
  "version": "$version",
  "description": "Hermetic local memory for coding agents: provenance-mandatory capture, honest recall (grounded/missing_evidence/abstain), falsifiable relations, hash-chained journal. One binary, one SQLite file, zero network sockets.",
  "author": { "name": "Tiago do Couto" },
  "license": "AGPL-3.0-only",
  "server": {
    "type": "binary",
    "entry_point": "nmemory",
    "mcp_config": {
      "command": "\${__dirname}/nmemory",
      "args": []
    }
  },
  "compatibility": { "platforms": ["$platform"] }
}
EOF

out="$PWD/nmemory-$os-$arch.mcpb"
(cd "$bundle_dir" && zip -q "$out" manifest.json nmemory)
rm -rf "$bundle_dir"
printf 'mcpb-bundle: wrote %s\n' "$out"
