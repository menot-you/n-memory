# nSPAWN contributor setup

Verified on 2026-10-18.

This page owns the optional developer setup. Product operation stays in the [runbook](RUNBOOK.md).
The [catalog](setup-catalog.json) records this project's 161 decisions, source references, validation, and proof limits.
It uses the toolkit revision `de66fe371a49f5a4d3b521759e5c401f390b6424`.
The project keeps its current Python workspace, public interfaces, and package boundaries.
NOTT process, nMEMORY, nSPAWN, and nBROWSER are optional developer capabilities.
They add no runtime dependency to a standalone service.
Reuse applicable global skills after reading their instructions. Do not copy client caches.

## Context7 development configuration

The portable [Claude JSON](../../../.claude/context7.json) emits as `.claude/context7.json`.
The portable [Codex TOML](../../../.codex/config.toml) emits as `.codex/config.toml`.
Both use `https://mcp.context7.com/mcp` without a credential.
The files define development access only. They do not change the product runtime MCP declarations.

From the emitted project root, start Claude with its native option:

```bash
claude --mcp-config .claude/context7.json
```

Codex reads `.codex/config.toml` in a trusted project. Restart the selected client after configuration changes.
Complete its native trust step. File presence does not prove tool discovery.
If Context7 is already registered at another scope, use one registration in that client.
Do not add a second connection or replace unrelated configuration.
Resolve the public library ID before a query. Match the installed library version.
Send no private source, account data, or secret in a documentation query.

## Explicit RTK use

Use RTK only when the selected command needs it. Add no interception hook.
For example, from the emitted project root:

```bash
rtk proxy uv run --no-sync ruff check .
```

Keep raw stdout, stderr, and the command exit for diagnosis.
A compressed view is insufficient evidence that an input or failure is absent.

## Operator prerequisites

Use the existing Python pin and uv workspace. Follow the [installation guide](../../INSTALLATION.md) for product prerequisites.
A missing machine tool is an operator prerequisite. A configuration file does not install that tool.
Use the official instructions only when the selected task needs the tool:

| Tool | Official instructions | When needed |
| --- | --- | --- |
| uv and Python | [uv](https://docs.astral.sh/uv/getting-started/installation/) | Source development with the existing Python pin |
| OS package manager | [Ubuntu](https://documentation.ubuntu.com/server/how-to/software/package-management/), [Homebrew](https://docs.brew.sh/Installation), [WinGet](https://learn.microsoft.com/en-us/windows/package-manager/winget/) | Separate operator tool management, only when needed |
| Git | [Git](https://git-scm.com/downloads) | Source history and contribution |
| Claude Code | [Claude setup](https://code.claude.com/docs/en/setup) | Claude development client |
| Codex | [Codex CLI](https://developers.openai.com/codex/cli) | Codex development client |
| RTK | [RTK](https://github.com/rtk-ai/rtk) | Optional explicit command proxy |
| Node.js and npm | [Node.js](https://nodejs.org/en/download) | Declared SDK tests or npm launcher only |
| fnm | [fnm](https://github.com/Schniz/fnm) | Optional Node version management |
| GitHub CLI | [GitHub CLI](https://cli.github.com/manual/installation) | An authorized remote CI diagnosis |
| ripgrep, fd, jq | [ripgrep](https://github.com/BurntSushi/ripgrep#installation), [fd](https://github.com/sharkdp/fd#installation), [jq](https://jqlang.org/download/) | Optional local inspection |
| pnpm | [pnpm](https://pnpm.io/installation) | Not required by this setup |
| Docker and Compose | [Docker](https://docs.docker.com/get-started/get-docker/), [Compose](https://docs.docker.com/compose/install/) | Not required by this setup |

Keep OS package management separate from project configuration.
Use the existing declared SDK preparation command only for its tests.
Do not install another browser, change accounts, or register a service to fill a catalog row.

## Toolkit choices

The table summarizes the 22 guides. Guide 17 has two entries in the full catalog.
The JSON keeps all tools, skills, prompts, templates, and machine prerequisites.

| Guide | Tool | Decision |
| --- | --- | --- |
| 01 | Superpowers | Use the optional NOTT process. Superpowers is absent. |
| 02 | Subagent orchestration | Use optional nSPAWN for authorized independent work. Account dispatch is unproved. |
| 03 | RTK | Use the explicit proxy. Output and exit preservation were observed. |
| 04 | Ponytail | Reuse the applicable global skill at ultra. Fresh loading is pending. |
| 05 | Caveman | Reuse the current voice. No proxy or usage collection is needed. |
| 06 | ESLint/Biome | Use declared Ruff, strict mypy, pytest, and native checks. |
| 07 | Graphify | No present need. Keep source-backed architecture and package boundaries. |
| 08 | Obsidian | No second store. Use optional nMEMORY when durable memory is needed. |
| 09 | Claude memory | Use the contributor index and optional nMEMORY. No second durable file store. |
| 10 | Hooks | Keep native hooks in the plugin. Add no hook for this setup. |
| 11 | agent-browser | Use optional nBROWSER. Static reads, Chromium navigation, and a snapshot passed. |
| 12 | Context7 | Native Codex configuration reading and protocol queries passed. Native agent use is blocked by host admission policy. |
| 13 | Anthropic skills | Reuse an applicable global skill only for a present task. Copy no collection. |
| 14 | Chrome DevTools | Use scoped nBROWSER diagnostics when needed. Full tracing is unproved. |
| 15 | Codex workflow | Use the root CLAUDE.md contributor contract. Native discovery is pending. |
| 16 | Vercel skills | No web stack. Reuse applicable prose or discovery guidance on demand. |
| 17 | Next.js runtime and bundles | Both catalog entries are inapplicable. Keep the current stack. |
| 18 | PostgreSQL skills | Inapplicable. Keep SQLite through nott-database. |
| 19 | GitHub CI repair | Use existing CI and diagnosis for an actual failure. No repair task is open. |
| 20 | Security review | Use the pinned prompt and existing read-only review method. See quality. |
| 21 | shadcn | Inapplicable. No application UI or component kit is selected. |
| 22 | Sentry | No present need. Keep current logs and typed failures. |

## Repeat and restore

Read the selected client configuration before setup. Keep unrelated settings and registrations.
Repeat the document export with the same inputs. It must retain one root CLAUDE.md contract and one Context7 registration per client.
If setup changes an existing development file, save its prior contents first and restore those contents on rollback.
Remove only an owned added registration or development file. Keep user data, stores, accounts, and unrelated configuration.
Use the [runbook](RUNBOOK.md) for product restoration.

## Proof and next observations

The catalog's `observations` owns the current integration results.
Installation, configuration, native loading, and execution are separate fields.
Native trust, authentication, roster admission, quota, and browser startup require their own observations.
The integration owner updates those results after the final battery.
Static files do not establish full toolkit delivery.
Read [Quality](QUALITY.md) before a completion claim.
