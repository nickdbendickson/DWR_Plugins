# Security Policy

## Reporting a vulnerability

Please report security problems **privately**, not in a public issue or pull
request.

Use GitHub's private vulnerability reporting for this repository:
**Security → Advisories → Report a vulnerability**
(<https://github.com/nickdbendickson/DWR_Plugins/security/advisories/new>).

Include the version or commit, which component (a DamSafety plugin or
`dwr_crest`), the platform, and the steps to reproduce. You'll
get an acknowledgement, and a fix or an explanation of why it is not a
problem.

Please do not publish details until a fix is available.

If the private advisory form is not available, contact the maintainer
privately rather than opening a public issue.

## Scope

This repository holds the DamSafety QGIS plugins, the standalone Crest desktop
app (`dwr_crest`). The local AI tools (Heim and Vor) moved to the damit
repository (`damit_ai/`); report them there. Data a user opens or
imports — QGIS project files, dam files, offline data packs, DSS-WISE
packages, zip archives, and other shared records — is treated as untrusted
input. Reports that matter most involve code execution, reading or writing
files outside a component's intended output folders, following a solver or
executable path from an untrusted project or pack, unpacking a malicious zip
or DSS-WISE package, or exposure of a local credential (for example the Crest
desktop server's loopback token).

## Secrets

Never commit API keys, tokens, or other credentials. If one is exposed, rotate
it at the provider immediately — rotation, not deletion from git history, is
what makes an exposed credential safe.
