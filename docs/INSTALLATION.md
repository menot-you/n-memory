# Installation and prerequisites

Select the component that supplies the result you need.
Use nMEMORY for project memory, nSPAWN for native sessions, or nBROWSER for web evidence.
Use the plugin for the working method and all three MCP services.
Read [privacy](PRIVACY.md) before choosing external processing.

## Source installation

Use an approved source archive or checkout for the selected repository.
Keep its root layout, `.python-version`, `pyproject.toml`, and `uv.lock` together.
Run from that repository's root:

```bash
uv sync --group dev
```

Open docs/RUNBOOK.md for its configuration and first useful result.
Every MCP project places its named Python module beside `pyproject.toml`, `tests`, and `docs`.
Workspace dependencies live under `packages`.
The plugin places its manifests, law, skills, hooks, and scripts at the root.
No public command requires the private source workspace.

The public wizard entry point is `https://no.tt/install.sh`.
Run it with:

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://no.tt/install.sh | sh
```

The default component is the plugin, which installs the plugin and the three MCP servers.
Use `sh -s -- --component nmemory` (or `nspawn`, `nbrowser`) to install one MCP server only.
It needs an approved published version and matching installer bytes.
A prepared source tree does not prove that the public URL is active.
Do not use an unapproved installer on an existing account.

## Separate prerequisites

| Component | Required input | Independent optional input |
| --- | --- | --- |
| nMEMORY | Account-owned store, MCP client, selected capture configuration | Embeddings, System1 model route, curator route |
| nSPAWN | Private provider roster, authenticated native client, admitted host sandbox | Additional accounts and classification routes |
| nBROWSER | Admitted URLs, browser engine and host facilities for browser operations | Keyed external search providers |
| Plugin | Native client, selected view, three configured MCP services | Semantic code search and keyed web search |

The source metadata declares Python and dependency floors.
Do not infer platform support from a source package alone.
The browser launcher currently needs its session bus and service-manager scope.
An explicit browser override does not establish equivalent behavior on another platform.
Native clients use different hook events and trust controls.
The current nSPAWN policy refuses new AGY launches.

## MCP registration

Use the absolute executable path from the installed runtime.
Each component uses arguments `mcp serve`.
nMEMORY can also take `--project example` to fence its context.
Preserve unrelated MCP registrations. Restart the client after a change.
Compare the MCP tools/list result with the selected component's docs/REFERENCE.md.
The stdio process waits for a client. It is not a terminal chat interface.
Keep optional HTTP listeners local and authenticated.
An HTTP token does not grant authority to capture, dispatch, log in, or submit a form.

## Existing data and restoration

Use [upgrade](UPGRADE.md) before opening old data with a new runtime.
Keep private backups, activation receipts, and the previous runtime.
Follow the component runbook's restoration check before replacing active files.
Use [troubleshooting](TROUBLESHOOTING.md) for a failed prerequisite or refused operation.
