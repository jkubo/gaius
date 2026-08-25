# Changelog

All notable changes to gaius (`gaius-memory` on PyPI) are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-08-25

### Security

- `gaius confirm` and `gaius reject` are now gated behind `require_human()`,
  which refuses known agent environments and a non-tty. `confidence_source='human'`
  is the trust anchor of the review loop: an agent able to mint it can launder its
  own output into "human-confirmed". Only a human at a terminal can set it.
- Corpus facts carrying credential material are filtered out of `inject` and
  `search` before they reach a model provider. Credential shapes (provider tokens,
  private-key headers, vault markers, URLs with embedded userinfo) are a DROP
  signal; prose *about* a leak is kept, since that is the reusable lesson.

### Fixed

- `gaius inject` no longer crashes on an install with no memory directory.
  `MEMORY_DIR` is `Path | None`, and a fresh install resolves it to `None` — the
  memory-file pass then raised `TypeError: unsupported operand type(s) for /:
  'NoneType' and 'str'` on its first iteration. The memory-file scan is now
  skipped when there is no memory directory to scan.
- `gaius spin` is a runnable command again. `gaius/spin.py` shipped in 0.1.x
  while its CLI registration did not, so `gaius spin` was not dispatchable; its
  `--transcript` path also raised `ModuleNotFoundError` on an unshipped import.

### Added

- `gaius spin` — HITL context-spin: reuse or replace the newest same-skill handoff
  without evicting peers. `gaius baton` remains as a tombstone verb pointing at it.
- `gaius.baton` now ships as the author library that `spin` builds on. It was
  previously excluded, which also stripped `spin`'s CLI registration — so
  `gaius/spin.py` shipped while `gaius spin` was not a runnable command.
- `gaius quiz` — Leitner-box human review over corpus facts. Mutating path is
  human-only (refuses known agent environments and a non-tty); `--report` is
  read-only calibration (box histogram + miss-rate by domain). First confirm
  writes `confidence_source='human'`; repeats only move the box and do not
  increment `confirmation_count`.
- `gaius.leitner` — content-agnostic scheduler (`qid`, `grade`, `draw`) shared
  by the quiz CLI and any card deck.
- `gaius.cards` — MCQ validator and JS renderer. Question content is
  caller-owned and does not ship.

- Profile provenance (`kub0.profile.sha256`, BA-6): hash over instruction
  files (CLAUDE.md / AGENTS.md), the skills *name* list, structural MCP
  config (name/type/command/url — never env or headers), model, and
  sampling. Digest only — file bodies never leave the node. New
  `session_profiles` table (CREATE TABLE IF NOT EXISTS, upsert on
  session_id). SessionStart hook `gaius-profile-stamp` (Claude + Grok);
  `gaius-claude-wrap` stamps `OTEL_RESOURCE_ATTRIBUTES` at process start
  (a SessionStart hook is too late for the resource). CLI:
  `python3 -m gaius.telemetry {session-start,profile-hash,profile-env,profile}`.
  Fail-open everywhere.
  Post-review (3-lens panel, 12/17 confirmed): instruction-file walk
  stops at $HOME or git root (never `/`); MCP `command`/args run the
  same token-shape redaction as telemetry; `timeout --kill-after` on
  wrap+stamp; wrap honors `KUB0_PROFILE_SHA256` pin and `--model`;
  `merge_otel_resource` only preserves a 64-hex pin; unset HOME no
  longer aborts wrap/stamp.

- Before/after state capture on `tool_events` (BA-1): two new columns,
  `state_sha256` (hash of the file at `tool_input.file_path`/`notebook_path` —
  before the call on `pre` rows, after it on `post` rows, with `absent` /
  `over-cap:<bytes>` sentinels) and `result_sha256` (hash of the canonical-JSON
  `tool_response` on `post` rows). Hashes only — file content and responses are
  never stored. The schema migrates additively (`ALTER TABLE`) on first write;
  existing rows read NULL. Pre/post rows pair on `(session_id, args_sha256)`,
  since the args hash covers `tool_input` only. A third column `call_id`
  captures the harness `tool_use_id`/`toolUseId` when present, making the
  pre/post pairing exact even for repeated identical calls (the args-hash join
  is ambiguous there). The `gaius-observe` hook now computes the file hash
  synchronously in-shell before its backgrounded write (a backgrounded hash
  races the tool call itself — TOCTOU) and hands it over via
  `GAIUS_STATE_SHA256`; wire it with a PostToolUse `""` matcher alongside the
  existing PreToolUse entry. If the column migration cannot land (locked or
  readonly DB at init), inserts degrade to the legacy column list so rows are
  still logged — never silently dropped for the life of a cached connection.
  Known limitations (documented, accepted): the sync hash adds bounded latency
  to file-path tool calls by design; state/result hashes are deterministic
  unsalted digests — treat the telemetry DB itself as sensitive, since a
  digest of low-entropy secret content is an offline-confirmation oracle.

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
