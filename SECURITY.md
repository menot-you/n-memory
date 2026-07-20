# Security policy

nmemory's security posture is structural: the binary opens **no network
socket** (compiled without a networking stack), touches **one SQLite file on
your disk**, and treats everything it returns as **data, never instructions**
(`ADVISORY_NOT_AUTHORITY` framing on every envelope). The threat that matters
most here is memory poisoning — stored content trying to become a command — and
the armor is the framing contract, not a detector.

## Supported versions

| Version | Supported |
|---|---|
| latest release (`v0.x`) | yes |
| anything older | no — upgrade first |

## Reporting a vulnerability

Use **GitHub private vulnerability reporting**: the *Security* tab of this
repository → *Report a vulnerability*. That opens a private advisory only the
maintainer can see.

Please include: the version (`nmemory --version`), a minimal reproduction, and
what an attacker gains. A capture/recall payload that escapes the
`ADVISORY_NOT_AUTHORITY` framing, bypasses provenance enforcement, or makes
recall fabricate an outcome is in scope and taken seriously — that class is the
project's reason to exist.

Do not open public issues for exploitable findings before a fix ships.

## What is out of scope

- Attacks requiring write access to the store file itself (your disk, your
  trust boundary — protect the file like any local secret).
- Prompt-injection resilience of the *consuming agent*: nmemory guarantees its
  own output framing; what an agent does with framed data is that agent's
  boundary.
