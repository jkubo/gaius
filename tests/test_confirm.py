"""gaius confirm — HUMAN-ONLY gate.

cmd_confirm writes confidence_source='human' (corpus_audit trust anchor).
An agent shell or a non-tty must be refused with exit 2 and must not mint
that row. The interactive human path still confirms.
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
from gaius._core import init_db, cmd_confirm
from gaius.review import _AGENT_ENV


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_gaius_mod, "DB_PATH", tmp_path / "isolated.db")


def _clear_agent_env(monkeypatch):
    for key in _AGENT_ENV:
        monkeypatch.delenv(key, raising=False)


def _insert_pending(conn, key="k1", text="a pending fact"):
    conn.execute(
        "INSERT INTO facts (fact_key, domain, fact_text, review_state, confidence, "
        "confidence_source, confirmation_count, fact_type, first_seen, last_seen) "
        "VALUES (?, 'ops', ?, 'pending', 0.30, 'inferred', 3, 'structural', "
        "datetime('now'), datetime('now'))",
        (key, text),
    )
    conn.commit()
    return conn.execute("SELECT id FROM facts WHERE fact_key=?", (key,)).fetchone()[0]


def _row(db_path, fid):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT review_state, confidence, confidence_source FROM facts WHERE id=?",
        (fid,),
    ).fetchone()
    conn.close()
    return row


def test_confirm_refuses_agent_env_exit_2(tmp_path, monkeypatch):
    db = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db)
    conn = init_db(db)
    fid = _insert_pending(conn)
    conn.close()

    monkeypatch.setenv("GROK_AGENT", "1")
    monkeypatch.setattr(review_mod.sys.stdin, "isatty", lambda: True)

    with pytest.raises(SystemExit) as ei:
        cmd_confirm([str(fid)])
    assert ei.value.code == 2

    row = _row(db, fid)
    assert row["review_state"] == "pending"
    assert row["confidence"] == 0.30
    assert row["confidence_source"] == "inferred"


def test_confirm_refuses_non_tty_exit_2(tmp_path, monkeypatch):
    db = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db)
    conn = init_db(db)
    fid = _insert_pending(conn)
    conn.close()

    _clear_agent_env(monkeypatch)
    monkeypatch.setattr(review_mod.sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit) as ei:
        cmd_confirm([str(fid)])
    assert ei.value.code == 2

    row = _row(db, fid)
    assert row["review_state"] == "pending"
    assert row["confidence"] == 0.30
    assert row["confidence_source"] == "inferred"


def test_confirm_human_tty_writes_confidence_source_human(tmp_path, monkeypatch, capsys):
    db = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db)
    conn = init_db(db)
    fid = _insert_pending(conn)
    conn.close()

    _clear_agent_env(monkeypatch)
    monkeypatch.setattr(review_mod.sys.stdin, "isatty", lambda: True)

    cmd_confirm([str(fid)])

    row = _row(db, fid)
    assert row["review_state"] == "confirmed"
    assert row["confidence"] == 1.0
    assert row["confidence_source"] == "human"
    assert "Confirmed" in capsys.readouterr().out
