"""Check the admitted file immediately before its normal path execution."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence


def main(arguments: Sequence[str]) -> int:
    try:
        expected = json.loads(arguments[0])
        target = arguments[1]
        current = os.stat(target)
        actual = {
            "device": current.st_dev,
            "inode": current.st_ino,
            "size": current.st_size,
            "modifiedNs": current.st_mtime_ns,
        }
        if actual != expected:
            print("The executable changed after command admission.", file=sys.stderr)
            return 126
        os.execv(target, list(arguments[1:]))
    except IndexError, OSError, TypeError, ValueError:
        print("The admitted executable could not start.", file=sys.stderr)
        return 126


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
