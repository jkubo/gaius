"""Phase 1b — production-outcome ingestion (task_outcomes). Additive, never touches facts."""
import sqlite3

from gaius._core import ingest_outcomes, outcome_winrates, _ensure_outcomes_table


def _conn(tmp_path):
    return sqlite3.connect(str(tmp_path / "facts.db"))


def test_ingest_idempotent_and_winrates(tmp_path):
    conn = _conn(tmp_path)
    recs = [
        {"key": "kub0-ai/x#1", "scope": "scope:a", "model": "sonnet", "status": "done",
         "result_status": "done", "success": True, "cost_usd": 0.1, "verdicts": ["g:pass"],
         "at": "2026-06-23T00:00:00Z"},
        {"key": "kub0-ai/x#2", "scope": "scope:a", "model": "sonnet", "status": "done",
         "result_status": "error", "success": False, "cost_usd": 0.2, "verdicts": [],
         "at": "2026-06-23T00:01:00Z"},
        {"key": "kub0-ai/x#3", "scope": "scope:b", "model": "opus", "status": "done",
         "result_status": "done", "success": True, "cost_usd": 0.3, "verdicts": ["g:pass"],
         "at": "2026-06-23T00:02:00Z"},
    ]
    ins, upd = ingest_outcomes(conn, recs)
    assert (ins, upd) == (3, 0)

    # Re-ingest the same keys → all updates, no duplicates (idempotent by key).
    ins2, upd2 = ingest_outcomes(conn, recs)
    assert (ins2, upd2) == (0, 3)
    assert conn.execute("SELECT COUNT(*) FROM task_outcomes").fetchone()[0] == 3

    wr = {w["scope"]: w for w in outcome_winrates(conn)}
    assert wr["scope:a"]["total"] == 2 and wr["scope:a"]["success"] == 1 and wr["scope:a"]["rate"] == 0.5
    assert wr["scope:b"]["total"] == 1 and wr["scope:b"]["rate"] == 1.0


def test_skips_records_without_key(tmp_path):
    conn = _conn(tmp_path)
    ins, upd = ingest_outcomes(conn, [{"key": "", "scope": "x"}, {"scope": "y"}, {"key": "  "}])
    assert (ins, upd) == (0, 0)
    assert conn.execute("SELECT COUNT(*) FROM task_outcomes").fetchone()[0] == 0


def test_never_touches_facts_table(tmp_path):
    # task_outcomes is the only table this path creates; the facts corpus is untouched.
    conn = _conn(tmp_path)
    ingest_outcomes(conn, [{"key": "kub0-ai/x#9", "scope": "scope:z", "success": True}])
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "task_outcomes" in tables
    assert "facts" not in tables  # ingestion never creates/mutates facts


def test_update_changes_fields(tmp_path):
    conn = _conn(tmp_path)
    ingest_outcomes(conn, [{"key": "k#1", "scope": "scope:a", "success": False}])
    ingest_outcomes(conn, [{"key": "k#1", "scope": "scope:a", "success": True}])
    row = conn.execute("SELECT success FROM task_outcomes WHERE key='k#1'").fetchone()
    assert row[0] == 1
