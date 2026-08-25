"""gaius reject is human-only.

An agent that can write outcome='rejected' curates _quiz_eligible and thereby
controls which facts a human is shown — the path that stamps
confidence_source='human'. Gate is require_human('reject'); the row must stay
unrejected on both the agent-env and non-tty refusals.
"""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

os.environ["GAIUS_CONFIG"] = "/dev/null"
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

import gaius._core as _gaius_mod
import gaius.review as review_mod
from gaius._core import init_db, cmd_reject
from gaius.review import _AGENT_ENV


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_gaius_mod, "DB_PATH", tmp_path / "isolated.db")


def _clear_agent_env(monkeypatch):
    for k in _AGENT_ENV:
        monkeypatch.delenv(k, raising=False)


def _insert_pending(db_path):
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO facts (fact_key, domain, fact_text, review_state, confidence, "
        "confidence_source, confirmation_count, fact_type, first_seen, last_seen) "
        "VALUES ('k1', 'ops', 'a pending fact', 'pending', 0.30, 'inferred', 1, "
        "'structural', datetime('now'), datetime('now'))",
    )
    conn.commit()
    fid = conn.execute("SELECT id FROM facts WHERE fact_key='k1'").fetchone()[0]
    conn.close()
    return fid


def _row(db_path, fid):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT review_state, outcome FROM facts WHERE id=?", (fid,)
    ).fetchone()
    conn.close()
    return row


def test_reject_refuses_agent_env_and_leaves_row(tmp_path, monkeypatch, capsys):
    db = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db)
    fid = _insert_pending(db)
    monkeypatch.setenv("GROK_AGENT", "1")
    with pytest.raises(SystemExit) as ei:
        cmd_reject([str(fid)])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "human-only" in err
    assert "reject" in err
    row = _row(db, fid)
    assert row["review_state"] == "pending"
    assert row["outcome"] is None


def test_reject_refuses_non_tty_and_leaves_row(tmp_path, monkeypatch, capsys):
    db = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db)
    fid = _insert_pending(db)
    _clear_agent_env(monkeypatch)
    monkeypatch.setattr(review_mod.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as ei:
        cmd_reject([str(fid)])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "requires a tty" in err
    row = _row(db, fid)
    assert row["review_state"] == "pending"
    assert row["outcome"] is None
