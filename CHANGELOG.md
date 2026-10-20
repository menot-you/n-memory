# Changelog

## Unreleased

### nMEMORY

- Use the Python workspace runtime with a fixed Python version and dependency lock.
- Expose 26 MCP tools for capture, recall, corrections, context, and memory maintenance.
- Configure a project with `nmemory project bootstrap`. Preserve unrelated client settings and restoration data.
- Preserve later foreign MCP changes during repeat and restoration. Refuse changes to the owned memory binding.
- Keep provenance and project scope with recalled records. Preserve the history of superseded decisions.
- Support lexical recall without model credentials and local System1 through a strict response schema.
- Export the memory engine, its required libraries, hooks, tests, and synthetic evaluation tools as one source workspace.

### nSPAWN

- Expose the supervisor, run registry, native provider routes, and resumable session interfaces through MCP and CLI.
- Keep native account configuration separate from authentication and observed quota.
- Ship standalone source, native SDK test preparation, and credential-free roster examples.

### nBROWSER

- Expose 14 MCP tools for browser sessions, static reads, search, and source evidence.
- Preserve destination checks, isolated browser profiles, and explicit browser prerequisites.
- Ship standalone source with native Python and npm entry points.

### NOTT plugin

- Package the working method, skills, hooks, and required MCP runtimes from explicit source.
- Generate separate native client views. Keep their hook and authentication differences explicit.
- Install a versioned runtime with owned configuration changes and a restoration path.
- Add visual architecture guides, contributor entry points, issue forms, and public API references.
- Add contributor setup, quality commands, diagrams, and a versioned toolkit catalog for all four projects.

### Compatibility

The previous public nMEMORY v0.3.0 release used Rust.
Its version number does not establish storage compatibility with this Python runtime.
The installer refuses known legacy data directories. Back up old data before a transition.
Read [backup and upgrade](docs/UPGRADE.md).

These entries do not declare a published release.
The release process adds the approved version, date, and source identity.
Existing public release history remains authoritative for earlier versions.
