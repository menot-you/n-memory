# socket

The shared unix-socket door primitive and the bounded stdio MCP client.

It lives under `libs/` and carries no `n` prefix because that prefix marks an MCP server and this package is a shared library (`d58-nomenclature-rulings`, ruling 2).

The door binds an owner-only unix socket inside a private staging directory, installs it without replacing anything, admits callers by peer credentials, and cleans up on drop. The harness's node service is its consumer.

## What it provides

- **`door`** — `UnixDoor`, `ListenError`, `Admission`: the bind, stage, prove, accept, and clean-up lifecycle.
- **`mcp`** — `McpClient`, `configured_server`: a bounded stdio MCP client for the harness and nSPAWN.

## What it does not provide

Session driving, request dispatch, or concurrency strategy. Each consumer owns those.

## The four load-bearing properties

1. The socket is `0600` from birth — created inside a `0700` staging directory, never widened first and narrowed after.
2. Every admitted uid is proven able to reach the socket (the owner or root) before anything is created.
3. Every accepted connection is checked against peer credentials on every accept (`SO_PEERCRED` on Linux, `LOCAL_PEERCRED` on macOS).
4. An occupied path is refused — the door never unlinks a path it did not create.

Consumers that replace stale sockets must hold ownership(path) across their stale check and bind.
The door retains the socket identity from its private staging directory.
Close uses the same lock for the identity check and removal.
The socket descriptor remains open through removal to prevent inode reuse.
Lock files remain beside socket paths.
Consumers must remove these files when they dispose of a private directory after its doors and processes stop.
