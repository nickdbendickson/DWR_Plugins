# Security Policy

## Reporting a vulnerability

Please report security problems **privately**, not in a public issue or pull
request.

Use GitHub's private vulnerability reporting for this repository:
**Security → Advisories → Report a vulnerability**
(<https://github.com/nickdbendickson/crest/security/advisories/new>).

Include the version or commit, the platform, and the steps to reproduce.
You'll get an acknowledgement, and a fix or an explanation of why it is not a
problem.

Please do not publish details until a fix is available.

## Scope

Crest is a desktop/offline dam-screening tool. Its local API binds to loopback
only and is protected by a per-launch token; data a user opens (dam files,
offline packs, imported records) is treated as untrusted input. Reports that
matter most involve code execution, reading or writing files outside the app's
own folders, or exposure of the local API token.

## Secrets

Never commit API keys, tokens, or other credentials. If one is exposed, rotate
it at the provider immediately — rotation, not deletion from git history, is
what makes an exposed credential safe.
