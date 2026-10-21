# nSPAWN quality and evidence

Read [Contributing](../../../CONTRIBUTING.md) for the regression method, safe test state, and submission rules.
The [first contribution](README.md#a-first-contribution) gives the focused command for this project.

## Checks

The [CI workflow](../../../.github/workflows/ci.yml) owns the exact lint, format, strict typing, package test, architecture, and build commands.
Run each member's checks from that member's root in the prepared root uv workspace.
For local checks in an already prepared workspace, use `uv run --no-sync`.
Use one pytest process with no worker pool for this bounded setup.
Set `TMPDIR` to an owned disk directory under `.nott/disposable` before pytest.
Preserve all test scope, refusals, and declared quality floors.
The nMEMORY coverage floor stays at 90.
A documentation change requires the existing public-document regression, local links, all four maps, JSON and TOML parsing, and repeat export.
The [CodeQL workflow](../../../.github/workflows/codeql.yml) owns the declared source analysis.
A passed source check is not installed or native behavioral proof.

## Security review

The pinned [security Prompt block](https://github.com/soumatheusgomes/vibe-coding-toolkit/blob/de66fe371a49f5a4d3b521759e5c401f390b6424/docs/prompts/13-security-review.md#prompt) owns the review procedure and private report contract.
Use the existing NOTT security/review routes or applicable installed security skills as optional equivalents.
No new scanner collection is required.

Give the following request to the review agent. This is a conversation instruction, not a shell command:

> Read the pinned security Prompt block linked above. Review this repository in repository mode. Preserve the current client, model, provider, and authentication. Keep source and index unchanged. Use synthetic disposable checks only. Save a redacted report outside the repository in a private location. Do not install tools, upload code, fix findings, or publish a report.

For a diff review, supply actual base and head references, the comparison method, and whether dirty changes are included.
Follow the prompt's scope, coverage, finding evidence, counterevidence, and remaining-validation fields.
Keep sensitive findings out of public source.
The [security policy](../../../SECURITY.md) owns vulnerability reporting.

## Evidence limits

The [catalog](setup-catalog.json) owns current results and remaining observations for this project.
Record production and development dependency audits separately for each selected lock.
An advisory service failure is an incomplete audit.
CodeQL dispositions do not prove that no vulnerability exists.
Native authentication, roster configuration, reviewer quota, client trust, and browser launch are separate requirements.
Do not report them as passed because source files or protocol queries pass.
