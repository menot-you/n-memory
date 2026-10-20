# nott-system1

The transport sends typed System1 questions through the Python standard library.
The default `native` protocol uses `/v1/systemone` with bearer authentication.
The `openai-compatible` protocol uses a local `/v1/chat/completions` endpoint without a credential.
The service behind the native protocol is a self-hosted Laya (model `laya-nott`).

Descended from nSPAWN's own typed-question module (`apps/spawn`) when nMEMORY became the SECOND
in-tree consumer — two consumers make a lib (D15).

The library owns HTTP requests, `Answer`, the question ceiling and the shared `env` lookup.
Consumers still supply the endpoint, model and timeout to the transport.
An absent endpoint raises `System1Unavailable`.

For `NOTT_SYSTEM1_*`, process variables take precedence over `~/.nott/.env`, including an empty process value.
The file path uses `Path.home()`, so a changed `HOME` selects that home's file.
The reader keeps one System1 snapshot per process. Restart the process to load file edits.
The snapshot contains only `NOTT_SYSTEM1_*` entries. The reader does not change the environment or write a file.
Other credential names use only the process environment. The vault file is outside this reader's scope.
A complete process question route wins. Without one, a complete process base skips file-only question routes.
Missing route names use the file when the process does not supply a complete base.

The parser follows the fish secret loader's ASCII rules. It trims each line and skips blanks and whole-line comments.
It removes only the exact `export ` prefix and splits at the first `=`.
It skips lines without `=`. A hash inside a value stays in the value.
It trims value whitespace, then removes outside double quotes and outside single quotes, including unpaired quotes.
An empty value is absent. The first nonempty assignment wins.
The reader does not execute shell syntax or expand variables. It refuses old variable spellings by name.

The file must be regular, belong to the effective user and have one hard link.
Group and other mode bits must be zero. The size limit is 64 KiB.
The reader refuses links, unsafe opens, unreadable files, changes during reading and invalid UTF-8.
Errors contain only the file path and failed property, or the refused variable name.
A missing file preserves absence. A partial question route remains a named refusal.
nMEMORY's `is_configured` returns `False` on `System1Unavailable`, so a refused file does not stop its curator loop.

The shared database pytest plugin sets `NOTT_TEST_SYSTEM1_FILE` before collection and for every test and its children.
Only tests set this file-location variable. The fence falsifier proves absence with the fence and a read without it.
System1 declares that plugin as a dev dependency, so a sync of this member alone keeps the fence.
The falsifier also covers collection and session-scoped and module-scoped fixtures.

Native AGY and orchestrator workers with a changed `HOME` are outside this change.
Their route forwarding and key redaction need a separate unit.
Unicode whitespace and line separators can differ from fish. With no HOME and no test fence, Path.home() uses the account's default home.

## Local structured output

The local server must support JSON Schema through the OpenAI-compatible `response_format` field.
Plain JSON output is insufficient.
See [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs).
Server and model support must be checked before use.
This transport correction does not establish model quality or completion within a caller's deadline.

The caller selects `protocol="openai-compatible"` and supplies the endpoint, model, and timeout.
The endpoint must use HTTP on a loopback address or `localhost`.
It must end in `/chat/completions` and contain no credentials, query, or fragment.
The transport refuses redirects and disables proxies.
It sends no bearer header and reads at most 1,000,001 response bytes.
It refuses a response larger than 1,000,000 bytes.

The request uses `POST` with `Content-Type: application/json`.
Its JSON body contains these fields:

| Field | Value |
| --- | --- |
| `model` | The caller's model name. |
| `messages` | One system message with the response rules, then one user message. |
| `messages[1].content` | A JSON string with `state` and `questions`. |
| `temperature` | `0`. |
| `response_format.type` | `"json_schema"`. |
| `response_format.json_schema.name` | `"system1_answers"`. |
| `response_format.json_schema.strict` | `true`. |
| `response_format.json_schema.schema` | The closed response schema described below. |

The schema requires an `answers` object with every exact question ID as a required property.
Each answer requires its exact `type` and these fields:

| Question type | Required value fields |
| --- | --- |
| `noul` | `noul`: a number in `0..1`. |
| `choice` | `choice`: one requested criterion key. `probabilities`: every criterion key with a number in `0..1`. `confidence`: a number in `0..1`. |
| `score` | `score`: a number. `confidence`: a number in `0..1`. |

Every object uses `additionalProperties: false` and requires all its properties.
The schema preserves question IDs and criterion keys without renaming them.
Choice questions require a nonempty criteria map with string keys.
Unsupported question types fail before HTTP starts.

The server returns the answer JSON as a string in `choices[0].message.content`.
The transport still checks exact answer IDs, answer types, finite numbers, and complete distributions.
It also checks that each distribution sums to one, with the existing numeric tolerance.
JSON Schema cannot enforce this sum across properties.
Missing fields raise `System1Unavailable` with only the static protocol field name.
Errors never turn a missing answer into a negative answer.
The transport adds no reasoning option, retry, or deadline extension.
Servers that support only `json_object` do not meet this prerequisite.
