# nSPAWN documentation

Start with the [runbook](RUNBOOK.md) for a first useful result.
The [architecture](ARCHITECTURE.md) shows service and trust boundaries.
The [reference](REFERENCE.md) lists the source declarations and command arguments.

| Need | Page |
| --- | --- |
| Contributor development configuration and catalog | [Setup](https://github.com/menot-you/n-spawn/blob/main/docs/SETUP.md) |
| Checks and security review | [Quality](https://github.com/menot-you/n-spawn/blob/main/docs/QUALITY.md) |
| Setup and processing choices | [Installation](../../INSTALLATION.md) |
| Local data and external services | [Privacy](../../PRIVACY.md) |
| Data compatibility and rollback | [Upgrade](../../UPGRADE.md) |
| A failed operation | [Troubleshooting](../../TROUBLESHOOTING.md) |
| Project direction | [Roadmap](../../ROADMAP.md) |
| Contribution rules | [Contributing](../../../CONTRIBUTING.md) |

## A first contribution

Add a synthetic roster refusal case to [test_owner_roster.py](../../../packages/nspawn/tests/test_owner_roster.py).
Describe the failure and expected result before you change the implementation.
Run this focused check from the public repository root:

```bash
TMPDIR=/var/tmp uv run --group dev pytest tests/test_owner_roster.py -q
```

Read the [source README](../../../packages/nspawn/README.md) for the component overview.
