"""Prepare the Node packages that the public SDK tests require."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PACKAGES = {
    "@agentclientprotocol/claude-agent-acp": "0.70.0",
    "@anthropic-ai/claude-agent-sdk": "0.3.232",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    prefix = args.prefix.resolve()
    if not args.check:
        if not any(
            parent.name == "disposable" and parent.parent.name == ".nott"
            for parent in (prefix, *prefix.parents)
        ):
            parser.error("Select a prefix under .nott/disposable.")
        prefix.mkdir(parents=True, exist_ok=True)
        user_config = prefix / "user.npmrc"
        user_config.write_text("")
        subprocess.run(
            [
                "npm",
                "install",
                "--prefix",
                str(prefix),
                "--cache",
                str(prefix / "cache"),
                "--userconfig",
                str(user_config),
                "--globalconfig",
                os.devnull,
                "--no-save",
                "--package-lock=false",
                "--ignore-scripts",
                *(f"{name}@{version}" for name, version in PACKAGES.items()),
            ],
            check=True,
            stdout=sys.stderr,
        )
    for name, version in PACKAGES.items():
        path = prefix / "node_modules" / name / "package.json"
        document = json.loads(path.read_text())
        if document.get("name") != name or document.get("version") != version:
            parser.error(f"The installed package must be {name}@{version}.")
    sdk = prefix / "node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs"
    subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            (
                "const sdk = await import(process.argv[1]); "
                "if (typeof sdk.listSessions !== 'function' || "
                "typeof sdk.getSessionMessages !== 'function') process.exit(1);"
            ),
            sdk.as_uri(),
        ],
        check=True,
        stdout=sys.stderr,
    )
    binary = prefix / "node_modules/.bin/claude-agent-acp"
    if not binary.is_file():
        parser.error("The installed ACP adapter is absent.")
    print(binary.parent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
