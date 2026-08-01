# Changelog

All notable changes to gaius (`gaius-memory` on PyPI) are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.2] - 2026-08-01

### Changed

- The poster identity used by `gaius drift --post-council` is now read from
  `council.agent` in `~/.gaius/config.yaml` and defaults to `gaius`. It was
  previously hardcoded to one deployment's agent name, so every other install
  posted under a name that was not theirs. Set `council.agent` to keep a
  specific identity.
- Domain-keyword and skill-to-domain defaults no longer ship deployment-specific
  product names. Use `domain_keywords` in `~/.gaius/config.yaml` to add your own.

### Security

- The CI leak gates held their denylist inline, which meant the files that exist
  to keep internal naming out were the one place a code search returned it. The
  terms now load from a single encoded source shared by all three gates, and
  every gate fails closed when that source is missing or does not decode — an
  empty pattern would otherwise read as "clean" while checking nothing.
- The unit-test guards that assert no internal name reached a shipped default
  had the same problem: they spelled the roster inline, in files that ship. They
  now load it from the same source. One guard had split a string literal to
  avoid tripping the leak scanner, and documented that it did so; it now matches
  against the shared denylist instead.

## [0.1.1] - 2026-08-01

### Added

- Five new docs pages: `getting-started`, `hard-gates`, `inject`,
  `review-lifecycle`, `kg`.
- `gaius init --backend <name>` and `--yes` — non-interactive setup for
  scripted installs and CI.

### Changed

- `gaius inject --budget` is now optional with a default (was required).
- Quickstart now uses `gaius init` instead of hand-editing
  `~/.gaius/config.yaml`.

### Fixed

- `LICENSE` restored to the canonical Apache-2.0 text so GitHub license
  detection recognizes it.

### Security

- Dependency updates: `mcp`, `starlette`, `python-multipart`, `cryptography`,
  `pyjwt`, and related transitive pins.
- GitHub Actions pinned to full commit SHAs.
- Publish-pipeline hardening (tighter leak-scan and artifact guards).

## [0.1.0] - 2026-07-29

### Added

- Initial PyPI release: `gaius` CLI, MCP server (`gaius-mcp`), and Claude Code
  plugin.

[0.1.1]: https://github.com/jkubo/gaius/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jkubo/gaius/releases/tag/v0.1.0
