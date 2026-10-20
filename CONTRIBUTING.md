# Contributing

A small reproducible correction is a useful first contribution.
Use English for issues, comments, commits, and pull requests.
Follow the [code of conduct](CODE_OF_CONDUCT.md).
Report vulnerabilities through [security](SECURITY.md).

## Choose one result

Open docs/README.md in the public repository.
Its first-contribution section links a focused test and the matching source.
Choose one incorrect example, missing refusal case, or reproducible defect.
Search existing issues before creating a report.
Describe the current result and expected result with synthetic inputs.
Keep the correction within that concern. Preserve unrelated work.

## Prepare and check

Use the Python version in `.python-version` and the root `uv.lock`.
Run from the repository root:

```bash
uv sync --group dev
```

Write the regression before a behavior correction. Observe its intended failure.
A missing dependency is a setup failure.
Run the same check after the correction.
Run affected lint, format, typing, and architecture checks when code changes.
Use disposable homes and stores. Never test against a live account or database.
Do not reduce a check or refusal to obtain a passing result.

Documentation changes need local-link checks, figure inspection, and the available strict documentation build.
The public documentation regression lives in the plugin's `tests/test_public_documents.py`.
It checks all four final document layouts against source declarations.
The private producer portal is not part of a public source installation.

## Submit a contribution

State the problem, resulting behavior, exact checks, and their exits.
Include the regression failure for a behavior correction.
Name missing proof and external prerequisites.
Never attach credentials, live stores, private transcripts, account homes, or activation receipts.
Read [privacy](docs/PRIVACY.md) before sharing a report.
A behavior change includes its changelog entry under the repository's declared convention.
Maintainers review the candidate before integration.
Public repositories receive selected source from the canonical workspace.
An accepted contribution enters that workspace before a later public release.
A contribution does not bypass publication approval or source-integrity checks.
