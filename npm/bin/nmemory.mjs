#!/usr/bin/env node
// Launcher for the binary that install.mjs verified and unpacked into vendor/.
//
// It hands the real file descriptors straight to the child (`stdio: 'inherit'`)
// because nmemory speaks MCP over stdio: nothing in this process may buffer,
// reframe, or touch that stream. Exit status and terminating signal are
// propagated so a supervisor sees the child's outcome, not the wrapper's.

import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const packageDir = dirname(dirname(fileURLToPath(import.meta.url)));
const binary = join(packageDir, 'vendor', process.platform === 'win32' ? 'nmemory.exe' : 'nmemory');

if (!existsSync(binary)) {
  process.stderr.write(
    '\nnmemory: no binary at\n' +
      `  ${binary}\n\n` +
      'The install hook that downloads and verifies it did not run, or it was told\n' +
      'not to. Re-run it, whichever applies:\n' +
      '  npm rebuild nmemory                 # after `npm install --ignore-scripts`\n' +
      '  NMEMORY_SKIP_DOWNLOAD= npm rebuild nmemory\n\n' +
      'Or install the binary directly: https://no.tt/install, or `cargo install nmemory`.\n\n',
  );
  process.exit(1);
}

const result = spawnSync(binary, process.argv.slice(2), { stdio: 'inherit', windowsHide: true });

if (result.error) {
  process.stderr.write(`\nnmemory: cannot execute ${binary}\n  ${result.error.message}\n\n`);
  process.exit(1);
}
if (result.signal) {
  // Re-raise so the parent observes the same termination the child suffered.
  process.kill(process.pid, result.signal);
}
process.exit(result.status ?? 1);
