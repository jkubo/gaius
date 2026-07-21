"""Loop-2 automated gates — item 5: cmd_agent_review + the REVIEW_STATE_WEIGHT ranker registry.

agent-review is the MACHINE substitute for the empirically-dead human `confirm` verb. Hard
constraints under test:
  - never writes confidence_source='human' (corpus_audit's trust anchor)
  - ranks ≤ auto (0.6x, same as pending) — never a boost, never escapes the penalty
  - leaves confidence / confidence_source untouched
"""
import os
import sys
import sqlite3
from pathlib import Path

import pytest

os.environ["GAIUS_CONFIG"] = "/dev/null"
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

import gaius._core as _gaius_mod
from gaius._core import init_db, cmd_agent_review, REVIEW_STATE_WEIGHT


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_gaius_mod, "DB_PATH", tmp_path / "isolated.db")


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db_path)
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO facts (fact_key, domain, fact_text, review_state, confidence, "
        "confidence_source, confirmation_count, first_seen, last_seen) "
        "VALUES ('k1','ops','a pending fact','pending',0.30,'contradiction',3,"
        "datetime('now'),datetime('now'))")
    conn.commit()
    fid = conn.execute("SELECT id FROM facts WHERE fact_key='k1'").fetchone()[0]
    conn.close()

    def _open():
        c = sqlite3.connect(str(db_path)); c.row_factory = sqlite3.Row; return c
    return _open, fid


# ── ranker registry: agent-reviewed weighted ≤ auto, never a boost ──────────────

def test_registry_agent_reviewed_le_auto():
    assert REVIEW_STATE_WEIGHT["agent-reviewed"] <= REVIEW_STATE_WEIGHT["auto"]
    # equal to pending (0.6), never above it — no rank boost from machine review
    assert REVIEW_STATE_WEIGHT["agent-reviewed"] == REVIEW_STATE_WEIGHT["pending"]
    assert REVIEW_STATE_WEIGHT["agent-reviewed"] < 0.8  # the rubber-stamp footgun value


def test_registry_defer_and_pending_still_penalized():
    # defer footgun stays fixed: deferred keeps the penalty (must not reward a punted fact)
    assert REVIEW_STATE_WEIGHT["pending"] == 0.6
    assert REVIEW_STATE_WEIGHT["deferred"] == 0.6


# ── cmd_agent_review behavior ───────────────────────────────────────────────────

def test_agent_review_sets_state_only(fresh_db):
    open_db, fid = fresh_db
    cmd_agent_review([str(fid)])
    conn = open_db()
    row = conn.execute(
        "SELECT review_state, confidence, confidence_source FROM facts WHERE id=?", (fid,)).fetchone()
    conn.close()
    assert row["review_state"] == "agent-reviewed"
    # HARD CONSTRAINT: confidence + confidence_source untouched (never 'human')
    assert row["confidence"] == 0.30
    assert row["confidence_source"] == "contradiction"


def test_agent_review_never_writes_human(fresh_db):
    open_db, fid = fresh_db
    cmd_agent_review([str(fid)])
    conn = open_db()
    n_human = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE confidence_source='human'").fetchone()[0]
    conn.close()
    assert n_human == 0


def test_agent_review_reversible_and_idempotent(fresh_db):
    open_db, fid = fresh_db
    cmd_agent_review([str(fid)])
    cmd_agent_review([str(fid)])  # idempotent — still one row, still agent-reviewed
    conn = open_db()
    state = conn.execute("SELECT review_state FROM facts WHERE id=?", (fid,)).fetchone()[0]
    # reversible: a plain UPDATE restores 'auto'
    conn.execute("UPDATE facts SET review_state='auto' WHERE id=?", (fid,))
    conn.commit()
    restored = conn.execute("SELECT review_state FROM facts WHERE id=?", (fid,)).fetchone()[0]
    conn.close()
    assert state == "agent-reviewed"
    assert restored == "auto"


def test_agent_review_missing_fact_exits(fresh_db):
    open_db, fid = fresh_db
    with pytest.raises(SystemExit):
        cmd_agent_review([str(fid + 999)])
