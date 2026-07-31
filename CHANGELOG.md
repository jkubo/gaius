# Changelog

All notable changes to gaius (`gaius-memory` on PyPI) are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
