# nmemory (npm)

Installer for the [nMEMORY](https://github.com/menot-you/n-memory) binary: hermetic,
single-file, local memory for coding agents, spoken over MCP on stdio. Capture
carries mandatory provenance; recall is grounded, `missing_evidence`, or an honest
abstain — never a fabricated fourth answer.

This package ships **no binary of its own**. It is a thin wrapper: on install it
downloads the prebuilt archive for your platform from the GitHub release that
matches its own version, verifies it, and unpacks it.

```
npm install -g nmemory
```

## What the install actually does

1. Resolves `${platform}-${arch}` against the targets the release workflow builds.
   An unbuilt target is a named failure, never a nearest match.
2. Downloads `SHA256SUMS` from the release tagged `v<this package's version>` —
   the pinned tag, never `latest`, so a given npm version always installs the same
   bytes.
3. Downloads the platform archive, over https only; a redirect off https is refused.
4. Computes the SHA-256 of what arrived and compares it to the published checksum.
5. Unpacks **only** on a match. A mismatch installs nothing and exits non-zero.

### What that verification is worth

It catches a truncated, corrupted, or cache-poisoned download, and a release whose
asset was swapped without its `SHA256SUMS` being swapped too.

It is **not** authenticity. `SHA256SUMS` travels the same channel as the archive, so
an actor able to rewrite the release rewrites both. Closing that gap needs a
signature over the checksums verified against a key that does not travel with them.
This wrapper MUST NOT be read as protection against a compromised release.

## Platforms

| `platform-arch` | asset |
| --- | --- |
| `linux-x64` | `nmemory-linux-x86_64.tar.gz` |
| `linux-arm64` | `nmemory-linux-aarch64.tar.gz` |
| `darwin-arm64` | `nmemory-macos-aarch64.tar.gz` |
| `win32-x64` | `nmemory-windows-x86_64.zip` |

Intel macOS (`darwin-x64`) has no build and therefore no install; the same key
appears on Apple Silicon when Node itself runs under Rosetta, where an arm64 Node
fixes it. Alpine and other musl systems are detected and refused, because the
archives are glibc-linked. Both cases build from source with `cargo install nmemory`.

Requirements: Node >= 20, and `tar` on `PATH` (macOS, Linux, and Windows 10+ ship it).

## Registering it with an agent

The `nmemory` command this package installs is a Node launcher that execs the real
binary. Register the **binary** instead, so no extra process sits in the MCP stdio
path — the install prints its absolute path, and the line to paste:

```
claude mcp add nmemory -- /path/printed/by/the/install --project my-project
```

## Other ways in

- `curl -fsSL https://no.tt/install | sh` — the same release archives, no Node.
- `cargo install nmemory` — builds from source.

If you already have a `nmemory` on `PATH` from one of those, installing this package
globally puts a second one there; `PATH` order decides which wins.

## Escape hatch

`NMEMORY_SKIP_DOWNLOAD=1` completes the install without downloading anything, for
air-gapped or download-blocked environments. The launcher then refuses to run and
names the fix. It never relaxes verification — it only declines to download.

## License

AGPL-3.0-only. Full text: [LICENSE](https://github.com/menot-you/n-memory/blob/main/LICENSE).
