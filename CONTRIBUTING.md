# Contributing

How to set up a dev environment, run the tests, and get a change merged. gaius
is a small, dependency-light Python codebase: the core needs only `pyyaml`,
most logic lives in `gaius/_core.py` and the split modules under `gaius/`, and
the test suite runs in about two seconds — there is no excuse for skipping it.

## Dev setup

Requires Python >= 3.11 and [uv](https://docs.astral.sh/uv/) (plain pip works
too).

```bash
git clone https://github.com/jkubo/gaius
cd gaius
uv sync --extra dev     # core deps + pytest
uv run pytest -q        # full suite, ~2s
```

Optional extras if your change touches them:

```bash
uv sync --extra dev --extra mcp        # MCP server
uv sync --extra dev --extra semantic   # embedding-based inject scoring
```

pip equivalent:

```bash
pip install -e ".[dev]"
pytest -q
```

## Making changes

- **Behavior changes need tests.** A PR that changes what a command does, what
  gets injected, or how facts are scored ships with a test that fails without
  the change. Bug fixes ship with a regression test.
- **Match the surrounding style.** No formatter is enforced; keep diffs
  minimal and local. Don't reflow code you aren't changing.
- **Don't add dependencies casually.** The core stays `pyyaml`-only. Anything
  heavier belongs behind an optional extra.

## Fixture rules (this one is enforced)

Test fixtures ship in the published tree and CI scans them. Do not put real
paths or endpoints in fixtures:

- No absolute home directories (`/home/<user>/...`). Use `tmp_path` or
  `~/path/to/gaius`-style placeholders.
- No real hostnames, domains, or IPs. Use `example.com` and documentation
  ranges.
- Session/config fixtures should use generic names (`build--api`,
  `my-project`), not anything copied from a live machine.

CI runs a leak scan over the whole tree — tests included — and fails the build
on hits. Writing the fixture with placeholders from the start is faster than
arguing with the scanner.

## PR expectations

1. `pytest -q` green locally on Python 3.11 or 3.12 (CI runs both).
2. Tests included for behavior changes (see above).
3. One concern per PR. Small and reviewable beats comprehensive.
4. If you change a CLI flag or command, update the matching docs page under
   `docs/` and the `CHANGELOG.md` unreleased section.

## Maintainer cadence

Development happens in a maintainer-side tree and is mirrored to this repo, so
maintainer pushes may batch several changes at once and commits can arrive in
bursts rather than one-per-change. This does not affect PRs — they are
reviewed and merged here normally. If your PR conflicts after a batch push,
rebase on `main`.

## Reporting bugs

Open an issue at https://github.com/jkubo/gaius/issues with the gaius version
(`pip show gaius-memory`), the exact command, and the output. For anything
security-sensitive, see [SECURITY.md](SECURITY.md) — do not open a public
issue.
