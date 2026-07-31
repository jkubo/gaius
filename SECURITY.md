# Security Policy

How to report a vulnerability in gaius and what response to expect. gaius is
local-first — it reads session logs and writes a corpus under `~/.gaius/` —
but it also ships an MCP server and hooks that run inside coding-agent
sessions, so file-handling, injection-surface, and prompt-content bugs are all
in scope.

## Reporting a vulnerability

Use GitHub private vulnerability reporting on this repository:

1. Go to the [Security tab](https://github.com/jkubo/gaius/security) of
   `jkubo/gaius`.
2. Click **Report a vulnerability**.
3. Include the affected version (`pip show gaius-memory`), reproduction
   steps, and your read on the impact.

Do **not** open a public issue or PR for a security report — that discloses
the bug before a fix exists.

## What to expect

Best-effort acknowledgment. This is a maintainer-run open-source project:
reports are read and triaged, and confirmed issues are fixed in the next
release, but there is no SLA on response or fix time. Please allow a
reasonable window before public disclosure; coordinated disclosure timing can
be discussed in the report thread.

## Supported versions

Only the latest release receives security fixes. There are no backports —
upgrade to the newest version to pick up a fix.

| Version | Supported |
|---------------|-----|
| latest release | yes |
| anything older | no |
