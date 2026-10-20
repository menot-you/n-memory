"""A test-owned provider roster, written to the test's own temp directory.

The live roster is `~/.nott/spawn.toml` and no test reads it; the versioned one
is `templates/nott/spawn.toml`. This one carries every shape the roster tests
judge: native accounts, gateway routes in both their shapes, `[bins]`, a vendor
exhaustion code and per-machine accounts with the comments around them.
"""

from __future__ import annotations

import os
from pathlib import Path

TEXT = """\
# A test-owned roster: every path and endpoint here is a placeholder.

[defaults]
system_prompt = "/opt/nott/plugin/NOTT.md"
plugins = ["/opt/nott/plugin"]
args = []

[bins]
claude = "/opt/nott/bin/claude"
codex = "/opt/nott/bin/codex"
agy = "/opt/nott/bin/agy"

[defaults.env]
API_TIMEOUT_MS = "3000000"

## ----- Classes ---------------------------------------------------------------
[classes]
unclassified = { effort = "low",    ceiling = "medium" }
review       = { effort = "medium", ceiling = "high" }
planning     = { effort = "high",   ceiling = "ultra" }

## ----- Anthropic -------------------------------------------------------------
[[provider]]
default = true
name = "claude"
kind = "claude"
secrets = "~/.nott/secrets/claude"
model = "opus"
model_fast = "haiku"
models = ["opus", "sonnet", "haiku"]
effort = "low"
efforts = ["low", "medium", "high", "xhigh", "max"]

## ----- A gateway with one endpoint for the whole provider --------------------
[[provider]]
name = "fugu"
kind = "claude"
secrets = "~/.nott/secrets/fugu"
auth_env = "ANTHROPIC_AUTH_TOKEN"
model = "fugu-ultra"
model_fast = "fugu"
models = ["fugu-ultra", "fugu"]
effort = "high"
efforts = ["high", "xhigh", "max", "ultra"]

[provider.env]
ANTHROPIC_BASE_URL = "https://fugu.invalid"

## ----- Google ----------------------------------------------------------------
[[provider]]
name = "agy"
kind = "agy"
model = "gemini-high"
model_fast = "gemini-low"
models = ["gemini-high", "gemini-low"]
effort = "high"
efforts = ["low", "medium", "high"]

[provider.accounts.agy1]
home = "~/.agy"

## ----- OpenAI ----------------------------------------------------------------
[[provider]]
name = "codex"
kind = "codex"
model = "gpt-main"
model_fast = "gpt-fast"
models = ["gpt-main", "gpt-fast"]
effort = "low"
efforts = ["low", "medium", "high", "xhigh", "max", "ultra"]
accounts = ["~/.codex-main"]

## ----- Z.AI ------------------------------------------------------------------
[[provider]]
name = "zai"
kind = "claude"
secrets = "~/.nott/secrets/zai"
auth_env = "ANTHROPIC_AUTH_TOKEN"
model = "glm"
model_fast = "glm-flash"
models = ["glm", "glm-flash"]
effort = "max"
efforts = ["low", "medium", "high", "xhigh", "max"]
# 1113: "Insufficient balance or no resource package", sent as a 429.
exhausted_codes = ["1113"]

[provider.env]
ANTHROPIC_BASE_URL = "https://zai.invalid/api/anthropic"

## ----- Qwen: one endpoint per machine ----------------------------------------
[[provider]]
name = "qwen"
kind = "claude"
secrets = "~/.nott/secrets/qwen"
model = "qwen3.8-27b"
model_fast = "qwen3.8-27b"
effort = "low"
efforts = ["low", "medium", "high"]

[provider.env]
CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS = "1"

# `bootstrap` is argv, never a string: without a shell there is no expansion nor
# chaining.
[provider.accounts.server]
base_url = "http://server.invalid:18020"
auth_env = "ANTHROPIC_AUTH_TOKEN"
model = "qwen3.8-27b"
capacity = 4
bootstrap = ["systemctl", "--user", "start", "qwen-serving"]
env = { CLAUDE_CODE_AUTO_COMPACT_WINDOW = "245760" }

[provider.accounts.m5]
base_url = "http://m5.invalid:11434"
auth_env = "ANTHROPIC_AUTH_TOKEN"
model = "qwen3.8:27b"
capacity = 2
env = { CLAUDE_CODE_AUTO_COMPACT_WINDOW = "65536" }

# m1 is NOT in. Its window does not fit the client's prompt, and a route that
# does not serve is not announced.

## ----- System1 ---------------------------------------------------------------
[system1]
model = "laya-nott"
timeout_seconds = 120.0
"""


def write(directory: Path) -> Path:
    """Write the roster as the owner keeps his: one file, mode 600."""
    path = directory / "spawn.toml"
    path.write_text(TEXT, encoding="utf-8")
    os.chmod(path, 0o600)
    return path
