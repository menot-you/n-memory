#!/bin/sh
# nMEMORY installer — the file served at https://no.tt/install
# Source of truth: install.sh at the repo root (menot-you/n-memory).
#
# What it does, in order:
#   1. Detect OS/arch.
#   2. Try the latest GitHub release binary for that platform.
#   3. Fall back to a source build (requires Rust; the toolchain is pinned).
#   4. Install to ~/.local/bin/nmemory and print the MCP registration line.
#
# It never touches anything outside ~/.local/bin and a temp dir it cleans up.
set -eu

REPO="menot-you/n-memory"
BIN_DIR="${NMEMORY_BIN_DIR:-$HOME/.local/bin}"
BIN="$BIN_DIR/nmemory"

say() { printf '%s\n' "$*" >&2; }
die() { say "nmemory install: $*"; exit 1; }

command -v curl >/dev/null 2>&1 || die "curl is required"

OS=$(uname -s) ARCH=$(uname -m)
case "$OS" in
  Linux)  TARGET_OS="linux" ;;
  Darwin) TARGET_OS="macos" ;;
  *) die "unsupported OS: $OS (Linux and macOS today; Windows via WSL)" ;;
esac
case "$ARCH" in
  x86_64|amd64)  TARGET_ARCH="x86_64" ;;
  arm64|aarch64) TARGET_ARCH="aarch64" ;;
  *) die "unsupported arch: $ARCH" ;;
esac

TMP=$(mktemp -d) || die "mktemp failed"
trap 'rm -rf "$TMP"' EXIT INT TERM

ASSET="nmemory-${TARGET_OS}-${TARGET_ARCH}.tar.gz"
URL="https://github.com/$REPO/releases/latest/download/$ASSET"

install_bin() {
  mkdir -p "$BIN_DIR"
  install -m 0755 "$1" "$BIN"
  say ""
  say "installed: $BIN"
  "$BIN" --version >&2 2>/dev/null || true
  say ""
  say "register it with your agent (scope your captures with your own project name):"
  say "  claude mcp add nmemory -- \"$BIN\" --project my-project"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say "note: $BIN_DIR is not on your PATH" ;;
  esac
}

say "nmemory install: trying release binary ($ASSET)…"
if curl -fsSL -o "$TMP/$ASSET" "$URL" 2>/dev/null; then
  tar -xzf "$TMP/$ASSET" -C "$TMP" || die "release asset unpack failed"
  [ -f "$TMP/nmemory" ] || die "release asset did not contain the nmemory binary"
  install_bin "$TMP/nmemory"
  exit 0
fi

say "no release binary for ${TARGET_OS}-${TARGET_ARCH}; building from source."
say "(first build needs the network for crates; the RUNTIME is hermetic — zero sockets)"
command -v git   >/dev/null 2>&1 || die "git is required for a source build"
command -v cargo >/dev/null 2>&1 || die "Rust is required for a source build — https://rustup.rs"

git clone --depth 1 "https://github.com/$REPO" "$TMP/src" || die "clone failed"
( cd "$TMP/src" && cargo build --release ) || die "cargo build failed"
install_bin "$TMP/src/target/release/nmemory"
