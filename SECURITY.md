# Security policy

## Report privately

Use the [private security report form](https://github.com/menot-you/n-memory/security/advisories/new)
for a suspected vulnerability in nMEMORY, nSPAWN, nBROWSER, or the plugin.
Name the affected repository in the report.
Do not publish exploit details in an issue or pull request before coordinated disclosure.
Include the affected version, operating system, reproduction, impact, and a proposed correction if available.
Remove credentials and private data from the report.
Test only systems and data that you control.

This project does not promise a response deadline or a bounty.
The latest approved release is the maintenance target.
Older versions do not receive a separate support guarantee.

## Security boundaries

The account owns its local SQLite stores and configuration.
MCP stdio trusts the local process that starts it.
Authenticated HTTP access does not make the service safe to expose publicly.
Keep the HTTP listener on its intended local interface.
Do not forward it to an untrusted network.

Imported text and recalled text can contain hostile instructions.
Provenance and taint metadata help the reader assess them.
They do not grant authority to a tool or agent.
Client hooks have host-specific enforcement limits.
See [privacy](docs/PRIVACY.md) and the component architecture page under docs/ARCHITECTURE.md.

Inspect the complete public artifact before publication.
A clean pattern scan does not prove that an artifact contains no secret.
Public release approval binds the source version and target.
The owner approves publication through the release pull request.
