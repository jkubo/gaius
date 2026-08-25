"""`gaius inject` must survive an install with NO memory directory.

MEMORY_DIR is typed `Path | None` and resolves via GAIUS_MEMORY_DIR, config.yaml,
or auto-discovery under ~/.claude/projects/*/memory/. A fresh install has none of
those, so it is None — and cmd_inject's memory-file pass did
`_MEMORY_BASE / subdir` unguarded, raising

    TypeError: unsupported operand type(s) for /: 'NoneType' and 'str'

on the very first iteration. That is the headline command failing outright for a
new user.

Pinned in its own file because the bug is invisible on any machine that HAS a
memory dir: the full suite passed locally, in both the development and the
published tree, and only reddened in CI where HOME is empty. A test that reads
the maintainer's ambient environment proves nothing about a fresh install.
"""
import pytest

from gaius import _core
from gaius import landscape as L


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point gaius at a scratch facts.db (init_db refuses the live one under pytest)."""
    monkeypatch.setattr(_core, "DB_PATH", tmp_path / "facts.db")


@pytest.fixture
def no_memory_dir(monkeypatch):
    """The state of a fresh install: nothing configured, nothing discoverable."""
    monkeypatch.setattr(L, "MEMORY_DIR", None)


def test_cmd_inject_runs_with_no_memory_dir(db, no_memory_dir, capsys):
    """Must not raise. Before the guard this died on `None / 'feedback'`."""
    L.cmd_inject(["--budget", "2000", "--skills-budget", "0", "--no-always-skills",
                  "--no-semantic", "--format", "plain", "--task", "some task"])
    capsys.readouterr()


def test_cmd_inject_with_no_memory_dir_emits_no_memory_section(db, no_memory_dir, capsys):
    """The memory-file section must be absent, not present-but-empty."""
    L.cmd_inject(["--budget", "2000", "--skills-budget", "0", "--no-always-skills",
                  "--no-semantic", "--format", "plain", "--task", "some task"])
    out = capsys.readouterr().out
    assert "Memory Files" not in out
