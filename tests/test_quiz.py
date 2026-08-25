"""gaius quiz — HITL loop.

Mutating quiz refuses an agent environment. --report is read-only.
First confirm writes confidence_source='human'; a repeat only moves the box
and does not bump confirmation_count.
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
from gaius._core import init_db, cmd_quiz
from gaius.review import (
    QUIZ_BUDGET_MAX,
    _AGENT_ENV,
    grade,
    require_human,
    running_as_agent,
    _quiz_write_progress,
)


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_gaius_mod, "DB_PATH", tmp_path / "isolated.db")


def _insert(conn, key="k1", text="a structural fact", domain="ops",
            source="inferred", state="pending"):
    conn.execute(
        "INSERT INTO facts (fact_key, domain, fact_text, review_state, confidence, "
        "confidence_source, confirmation_count, fact_type, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, 0.30, ?, 3, 'structural', datetime('now'), datetime('now'))",
        (key, domain, text, state, source),
    )
    conn.commit()
    return conn.execute("SELECT id FROM facts WHERE fact_key=?", (key,)).fetchone()[0]


def test_running_as_agent_sees_grok_and_claude(monkeypatch):
    monkeypatch.delenv("GROK_AGENT", raising=False)
    monkeypatch.delenv("GROK_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    assert running_as_agent() is False
    monkeypatch.setenv("GROK_AGENT", "1")
    assert running_as_agent() is True


def test_require_human_exits_2_in_agent(monkeypatch):
    monkeypatch.setenv("GROK_AGENT", "1")
    with pytest.raises(SystemExit) as ei:
        require_human("quiz")
    assert ei.value.code == 2


def test_report_does_not_require_human(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GROK_AGENT", "1")
    db = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db)
    conn = init_db(db)
    _insert(conn)
    conn.close()
    cmd_quiz(["--report"])
    out = capsys.readouterr().out
    assert "calibration report" in out
    assert "eligible:" in out


def test_quiz_refuses_agent_without_report(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_AGENT", "1")
    db = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db)
    conn = init_db(db)
    _insert(conn)
    conn.close()
    with pytest.raises(SystemExit) as ei:
        cmd_quiz(["--budget", "1"])
    assert ei.value.code == 2


def test_quiz_budget_above_max_exits_nonzero(tmp_path, monkeypatch, capsys):
    for k in _AGENT_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(review_mod.sys.stdin, "isatty", lambda: True)
    db = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db)
    conn = init_db(db)
    _insert(conn)
    conn.close()
    with pytest.raises(SystemExit) as ei:
        cmd_quiz(["--budget", str(QUIZ_BUDGET_MAX + 1)])
    assert ei.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "--budget" in err
    assert str(QUIZ_BUDGET_MAX) in err


def test_first_confirm_writes_human_repeat_does_not_bump_count(tmp_path, monkeypatch):
    db = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db)
    conn = init_db(db)
    fid = _insert(conn)
    rec = {"b": 0, "n": 0, "c": 0, "w": 0}
    new = grade(rec, True)
    _quiz_write_progress(conn, fid, new)
    conn.execute(
        "UPDATE facts SET review_state='confirmed', confidence=1.0, "
        "confidence_source='human' WHERE id=?",
        (fid,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT confidence_source, confirmation_count, leitner_box FROM facts WHERE id=?",
        (fid,),
    ).fetchone()
    assert row["confidence_source"] == "human"
    assert row["confirmation_count"] == 3  # unchanged from insert
    assert row["leitner_box"] == 1

    new2 = grade(dict(new), True)
    _quiz_write_progress(conn, fid, new2)
    conn.commit()
    row = conn.execute(
        "SELECT confirmation_count, leitner_box, confidence_source FROM facts WHERE id=?",
        (fid,),
    ).fetchone()
    assert row["confirmation_count"] == 3
    assert row["leitner_box"] == 2
    assert row["confidence_source"] == "human"
    conn.close()


def test_quiz_prints_bound_fact_source_excerpt(tmp_path, monkeypatch, capsys):
    """HITL quiz must surface the corpus row itself, labelled as source.

    A generated card can paraphrase; the human has to adjudicate
    row['fact_text'] before answering. Pin that the bound fact's own
    text reaches stdout ahead of the answer prompt.
    """
    for k in _AGENT_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(review_mod.sys.stdin, "isatty", lambda: True)

    def _stub_input(prompt=""):
        sys.stdout.write(prompt)
        sys.stdout.flush()
        return "s"

    monkeypatch.setattr("builtins.input", _stub_input)
    db = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db)
    conn = init_db(db)
    _insert(conn, text="ops fact containing EXCERPT-SENTINEL-7f3a for quiz display")
    conn.close()
    cmd_quiz(["--budget", "1"])
    out = capsys.readouterr().out
    assert "EXCERPT-SENTINEL-7f3a" in out
    assert "source:" in out
    assert out.index("source:") < out.index("EXCERPT-SENTINEL-7f3a")
    assert out.index("EXCERPT-SENTINEL-7f3a") < out.index("still true?")


def test_live_facts_are_not_eligible(tmp_path, monkeypatch, capsys):
    db = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db)
    conn = init_db(db)
    conn.execute(
        "INSERT INTO facts (fact_key, domain, fact_text, review_state, confidence, "
        "confidence_source, confirmation_count, fact_type, first_seen, last_seen) "
        "VALUES ('live1','ops','replica count is 3','auto',0.9,'inferred',1,'live',"
        "datetime('now'),datetime('now'))"
    )
    conn.commit()
    conn.close()
    cmd_quiz(["--report"])
    out = capsys.readouterr().out
    assert "eligible: 0" in out
