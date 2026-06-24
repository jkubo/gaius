"""Phase 2 shadow — corpus_audit_stats over a seeded facts table. Pure read-only logic."""
import sqlite3

from gaius._core import corpus_audit_stats, REPETITION_THRESHOLD


def _facts_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "facts.db"))
    conn.execute(
        "CREATE TABLE facts (fact_key TEXT, tombstoned_at TEXT, confirmation_count INTEGER, "
        "outcome TEXT, confidence_source TEXT, conflict_with TEXT)")
    return conn


def test_corpus_audit_signals(tmp_path):
    conn = _facts_db(tmp_path)
    rows = [
        ("k1", None, 3, None, "agent", None),                    # repetition-only
        ("k1", None, 1, None, "agent", None),                    # dup of k1 -> contradiction
        ("k2", None, 5, None, "human", None),                    # human-verified
        ("k3", None, 4, "confirmed", "agent", None),             # outcome-verified
        ("k4", "2026-06-23T00:00:00Z", 9, None, "agent", None),  # tombstoned -> excluded
        ("k5", None, 2, None, "agent", "k9"),                    # repetition-only + conflict-flagged
    ]
    conn.executemany(
        "INSERT INTO facts (fact_key,tombstoned_at,confirmation_count,outcome,confidence_source,conflict_with) "
        "VALUES (?,?,?,?,?,?)", rows)
    conn.commit()

    s = corpus_audit_stats(conn)
    assert s["live_facts"] == 5            # all but k4 (tombstoned)
    assert s["human_verified"] == 1        # k2
    assert s["outcome_verified"] == 1      # k3
    assert s["repetition_unverified"] == 2 # k1(cc3) + k5(cc2)
    assert s["contradiction_keys"] == 1    # k1 has two live facts
    assert s["contradiction_facts"] == 2
    assert s["conflict_flagged"] == 1      # k5
    assert "task_outcomes" not in s        # no such table in this fixture


def test_repetition_threshold_sane():
    assert REPETITION_THRESHOLD >= 2


def test_repetition_candidates(tmp_path):
    from gaius._core import repetition_candidates
    conn = sqlite3.connect(str(tmp_path / "facts.db"))
    conn.execute(
        "CREATE TABLE facts (id INTEGER PRIMARY KEY, domain TEXT, fact_key TEXT, fact_text TEXT, "
        "confirmation_count INTEGER, score REAL, outcome TEXT, confidence_source TEXT, tombstoned_at TEXT)")
    conn.executemany(
        "INSERT INTO facts (domain,fact_key,fact_text,confirmation_count,score,outcome,confidence_source,tombstoned_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            ("infra", "k1", "fact one", 7, 0.9, None, "agent", None),               # candidate (worst)
            ("infra", "k2", "fact two", 3, 0.5, None, "agent", None),               # candidate
            ("infra", "k3", "fact three", 1, 0.5, None, "agent", None),             # below threshold
            ("infra", "k4", "fact four", 9, 0.9, "confirmed", "agent", None),       # outcome-verified
            ("infra", "k5", "fact five", 9, 0.9, None, "human", None),              # human-verified
            ("infra", "k6", "fact six", 9, 0.9, None, "agent", "2026-01-01T00:00"), # tombstoned
        ])
    conn.commit()
    cands = repetition_candidates(conn, limit=10)
    assert [c["confirmation_count"] for c in cands] == [7, 3]  # worst-first; others excluded
    assert cands[0]["text"] == "fact one"
