# nott-netdoor

Loopback HTTP bind and bearer admission. nMEMORY and nBROWSER serve the MCP
SDK's Streamable HTTP app through it; uvicorn owns HTTP parsing.

## Surface

`nott_netdoor.http` exposes `parse`, `bind`, `ApiKey`, `admit` and `serve_app`.

- `parse(spec)` reads `<ip>:<port>` (or `[<ipv6>]:<port>`) and refuses anything
  that is not loopback with `NotLoopbackError`, and anything that is not an
  address with `HttpNotAnAddressError`. A reverse proxy owns remote exposure.
- `bind(address)` returns a listening socket and refuses a non-loopback
  address even when the caller skipped `parse`. A failed bind is `BindError`.
- `ApiKey.load(path)` refuses missing, unreadable or empty files with
  `UnreadableKeyError` and trims boundary ASCII whitespace. `ApiKey.matches`
  uses `hmac.compare_digest`.
- `admit(app, key)` answers 401 with `WWW-Authenticate: Bearer` to any HTTP
  request without exactly one `Authorization: Bearer <key>` header; such a
  request never reaches the app.
- `serve_app(app, listener, key)` runs `admit(app, key)` on uvicorn with no
  request logging (requests may carry credentials).

Every error derives from `NetDoorError`.

## Verification

From this member's directory, run
`TMPDIR=/var/tmp uv run --group dev pytest tests -q`. The conformance suite
drives every row of `http-door-auth.tsv` through `admit`.
