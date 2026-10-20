# Troubleshooting

Use a disposable home with synthetic records to reproduce a problem.
Do not attach a live store or private receipt to a public issue.

| Symptom | Check | Next action |
| --- | --- | --- |
| A sibling package cannot import | Source workspace and root lockfile | Run `uv sync` from the workspace root |
| Store missing | Was this account initialized? | Use `nmemory database create` only for a new store |
| Existing v0.3.0 store | Exact old format and backup | Follow [upgrade](UPGRADE.md) before reuse |
| No MCP tools | Client executable path and the MCP tools/list result | Correct registration and start a fresh session |
| No recalled decision | Project ID, terms, validity, and supersession | Inspect the capsule and recall exclusions |
| Vector retrieval unavailable | `nmemory embedding show --json` | Correct provider configuration or use lexical recall |
| System1 refuses capture | Route completeness and protocol response | Correct the named configuration or response failure |
| System1 works but dispatch fails | Native account login and roster | Authenticate the selected native client |
| Browser operation unavailable | Chromium runtime and reported prerequisite | Install the supported browser runtime |
| Chromium reports `Socket path too long` | Length of the inherited `TMPDIR` | Use a short private temporary directory on disk |
| Web search has no usable lane | Provider readiness | Configure a provider only after accepting external processing |
| `jbcontext` refuses authentication | Optional index account | Authenticate the integration or inspect known source paths directly |
| Plugin rollback refuses | Receipt and later owned changes | Preserve both states and resolve the named conflict |
| AGY dispatch refuses admission | Current nSPAWN policy | Use an admitted native provider |

A missing listener and a listener owned by another account are different failures.
Do not kill an unknown listener or bypass its ownership check.
A partial setup must restore its prior owned links and configuration.
If restoration refuses, retain the receipt and redacted error for private diagnosis.

For an issue, report the version, platform, exact command, expected result, and redacted error.
Use [support](../SUPPORT.md) for routing and [security](../SECURITY.md) for sensitive failures.
