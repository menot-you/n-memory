#!/usr/bin/env node
// nmemory npm install hook — fetch the prebuilt binary for this platform from the
// GitHub release that matches this package's version, VERIFY it against that
// release's SHA256SUMS, and only then unpack it.
//
// The order is the whole point: download, hash, compare, and unpack ONLY on a
// match. Nothing from the network is executed, made executable, or unpacked
// before its digest matches the published checksum, and every failure path here
// exits non-zero with the cause named. An installer that unpacks first and
// checks later has already lost.
//
// What this verification DOES and DOES NOT buy (stated because the project's
// claim is that it never asserts what it cannot back):
//   - It DOES catch a truncated, corrupted, or cache-poisoned download, and a
//     release whose asset was replaced without its SHA256SUMS being replaced too.
//   - It does NOT buy authenticity. SHA256SUMS travels the same channel as the
//     archive, so an actor who can rewrite the release can rewrite both. Only a
//     signature over the checksums, verified against a key that does NOT travel
//     with them, would close that gap. This installer MUST NOT be described as
//     protecting against a compromised release.
//
// Zero-Python is absolute here (d12-zero-python-absolute): Node and the system
// `tar` only, no interpreter, no runtime npm dependency.

import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const PACKAGE_DIR = dirname(fileURLToPath(import.meta.url));
const REPO = 'menot-you/n-memory';

// A download is bounded in both bytes and time so a hostile or hung endpoint
// cannot fill the disk or wedge `npm install` forever. Release archives are
// single-digit megabytes; this ceiling is slack, not a target.
const MAX_BYTES = 64 * 1024 * 1024;
const TIMEOUT_MS = 120_000;

// Every asset the release workflow (.github/workflows/release.yml) actually
// builds, keyed by `${process.platform}-${process.arch}`. Deliberately absent:
//   - darwin-x64: the build matrix has no Intel macOS runner, so there is no
//     Intel asset to install. Serving the aarch64 archive there would install a
//     binary that cannot run.
//   - any musl target: the archives are built on glibc runners.
// An absent key is a hard, named failure, never a silent nearest match.
const TARGETS = {
  'linux-x64': { asset: 'nmemory-linux-x86_64.tar.gz', bin: 'nmemory' },
  'linux-arm64': { asset: 'nmemory-linux-aarch64.tar.gz', bin: 'nmemory' },
  'darwin-arm64': { asset: 'nmemory-macos-aarch64.tar.gz', bin: 'nmemory' },
  'win32-x64': { asset: 'nmemory-windows-x86_64.zip', bin: 'nmemory.exe' },
};

function fail(headline, detail) {
  process.stderr.write(`\nnmemory install: ${headline}\n`);
  if (detail) process.stderr.write(`${detail}\n`);
  process.stderr.write('\nNothing was installed.\n\n');
  process.exit(1);
}

// Advisory only, and deliberately fail-open: a glibc binary on musl fails at
// exec time with a message that names nothing useful, so naming it here is a
// kindness. When the runtime cannot tell, the install proceeds — the digest
// check is the security control, this is not.
function looksLikeMusl() {
  if (process.platform !== 'linux') return false;
  try {
    const header = process.report?.getReport?.()?.header;
    return typeof header === 'object' && header !== null && !header.glibcVersionRuntime;
  } catch {
    return false;
  }
}

async function download(url, what) {
  let response;
  try {
    response = await fetch(url, { redirect: 'follow', signal: AbortSignal.timeout(TIMEOUT_MS) });
  } catch (cause) {
    return fail(
      `cannot reach the release to fetch ${what}.`,
      `  url:   ${url}\n  cause: ${cause?.message ?? cause}\n\n` +
        'If this machine has no route to github.com, install the binary another way\n' +
        '(https://no.tt/install, or `cargo install nmemory`) and re-run this install with\n' +
        'NMEMORY_SKIP_DOWNLOAD=1 to accept a wrapper with no bundled binary.',
    );
  }
  if (!response.ok) {
    return fail(
      `the release does not serve ${what} (HTTP ${response.status}).`,
      `  url: ${url}\n\nThis package's version MUST match a published release tag.`,
    );
  }
  // GitHub redirects a release download to its object store. That hop MUST stay
  // on https — a downgrade would put the bytes on the wire in clear, where the
  // digest check below still passes for whatever an on-path actor served.
  if (new URL(response.url).protocol !== 'https:') {
    return fail(
      `the request for ${what} was redirected off https.`,
      `  final url: ${response.url}\n\nRefusing to read bytes over a downgraded transport.`,
    );
  }
  const chunks = [];
  let total = 0;
  for await (const chunk of response.body) {
    total += chunk.length;
    if (total > MAX_BYTES) {
      return fail(
        `${what} exceeded the ${MAX_BYTES} byte ceiling.`,
        `  url: ${url}\n\nRefusing to buffer an unbounded response.`,
      );
    }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

// `sha256sum <files> > SHA256SUMS` writes `<hex>  <name>`; the binary-mode form
// is `<hex> *<name>`. Both are accepted; anything else is ignored rather than
// guessed at.
function parseChecksums(text) {
  const sums = new Map();
  for (const line of text.split('\n')) {
    const match = /^([0-9a-fA-F]{64})[ \t]+\*?(.+?)[\r]*$/.exec(line);
    if (match) sums.set(match[2], match[1].toLowerCase());
  }
  return sums;
}

async function main() {
  const { version } = JSON.parse(readFileSync(join(PACKAGE_DIR, 'package.json'), 'utf8'));
  const tag = `v${version}`;
  const base = `https://github.com/${REPO}/releases/download/${tag}`;

  // The escape hatch exists so an air-gapped or download-blocked environment can
  // still complete `npm install`; the launcher then refuses with the exact fix
  // rather than exec'ing something that is not there. It NEVER weakens
  // verification — it only declines to download at all.
  if (process.env.NMEMORY_SKIP_DOWNLOAD) {
    process.stderr.write(
      '\nnmemory install: NMEMORY_SKIP_DOWNLOAD is set — no binary was downloaded.\n' +
        'The `nmemory` command will refuse to run until one is installed.\n\n',
    );
    return;
  }

  const key = `${process.platform}-${process.arch}`;
  const target = TARGETS[key];
  if (!target) {
    const extra =
      key === 'darwin-x64'
        ? '\nThe release builds no Intel macOS archive. On Apple Silicon this key also\n' +
          'appears when Node itself runs under Rosetta — install an arm64 Node and retry.\n'
        : '';
    return fail(
      `no prebuilt binary for ${key}.`,
      `  built targets: ${Object.keys(TARGETS).join(', ')}\n${extra}\n` +
        'Build from source instead: `cargo install nmemory`.',
    );
  }
  if (looksLikeMusl()) {
    return fail(
      'this looks like a musl system (Alpine), and the release archives are glibc-linked.',
      'The downloaded binary would fail to exec. Build from source: `cargo install nmemory`.',
    );
  }

  const sums = parseChecksums(
    (await download(`${base}/SHA256SUMS`, 'SHA256SUMS')).toString('utf8'),
  );
  const expected = sums.get(target.asset);
  if (!expected) {
    return fail(
      `release ${tag} publishes no checksum for ${target.asset}.`,
      'Refusing to install an unverifiable archive.',
    );
  }

  const archive = await download(`${base}/${target.asset}`, target.asset);
  const actual = createHash('sha256').update(archive).digest('hex');
  if (actual !== expected) {
    return fail(
      `checksum mismatch on ${target.asset} — REFUSING to unpack it.`,
      `  expected ${expected}\n  actual   ${actual}\n\n` +
        `The archive served does not match the SHA256SUMS published for ${tag}.\n` +
        'Treat this as hostile until proven otherwise: do not retry blindly, and\n' +
        `report it at https://github.com/${REPO}/security/advisories.`,
    );
  }

  const work = mkdtempSync(join(tmpdir(), 'nmemory-install-'));
  try {
    const archivePath = join(work, target.asset);
    writeFileSync(archivePath, archive);
    try {
      // `tar -xf` auto-detects gzip on GNU tar and reads a real zip on the bsdtar
      // that Windows 10+ ships as tar.exe, so one invocation covers every target.
      execFileSync('tar', ['-xf', archivePath, '-C', work], { stdio: ['ignore', 'ignore', 'pipe'] });
    } catch (cause) {
      return fail(
        `could not unpack the verified ${target.asset}.`,
        `  cause: ${cause?.message ?? cause}\n\n` +
          '`tar` MUST be on PATH (it ships with macOS, Linux, and Windows 10+).',
      );
    }

    const extracted = join(work, target.bin);
    if (!existsSync(extracted)) {
      return fail(
        `the verified archive did not contain \`${target.bin}\` at its root.`,
        'The release asset layout changed; this wrapper refuses to guess.',
      );
    }

    const vendorDir = join(PACKAGE_DIR, 'vendor');
    mkdirSync(vendorDir, { recursive: true });
    const installed = join(vendorDir, target.bin);
    rmSync(installed, { force: true });
    // Copy, never rename: the temp dir is routinely on a different filesystem
    // from node_modules, where rename fails EXDEV.
    copyFileSync(extracted, installed);
    chmodSync(installed, 0o755);

    process.stderr.write(
      `\nnmemory ${version} installed: ${installed}\n` +
        `  sha256 ${actual}\n` +
        `  verified against SHA256SUMS published for ${tag}\n\n` +
        'register it with your agent — point at the binary, not at this wrapper,\n' +
        'so no extra process sits in the MCP stdio path:\n' +
        `  claude mcp add nmemory -- "${installed}" --project my-project\n\n`,
    );
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}

main().catch((cause) => {
  fail('unexpected failure.', `  cause: ${cause?.stack ?? cause}`);
});
