"""Phase 3 — retrieval-augmented routing (route_suggest). Read-only; grounds + flags unverified."""
import sqlite3

from gaius._core import route_suggest


def _facts_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "facts.db"))
    conn.execute(
        "CREATE TABLE facts (id INTEGER PRIMARY KEY, domain TEXT, fact_text TEXT, "
        "confirmation_count INTEGER, score REAL, outcome TEXT, confidence_source TEXT, tombstoned_at TEXT)")
    return conn


def test_route_suggest_grounds_and_flags_unverified(tmp_path):
    conn = _facts_db(tmp_path)
    conn.executemany(
        "INSERT INTO facts (domain,fact_text,confirmation_count,score,outcome,confidence_source,tombstoned_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            ("networking", "flannel MTU is 1050", 5, 0.9, None, "agent", None),       # unverified
            ("networking", "headscale is the overlay", 3, 0.8, None, "agent", None),  # unverified
            ("networking", "DNS is technitium", 2, 0.7, "confirmed", "agent", None),  # verified (outcome)
            ("storage", "seaweedfs replaced minio", 4, 0.6, None, "human", None),     # other domain
            ("networking", "tombstoned old fact", 9, 1.0, None, "agent", "2026-01-01"),  # excluded
        ])
    conn.commit()

    res = route_suggest(conn, "flannel headscale technitium overlay", hint="networking", max_facts=10)
    assert res["primary_domain"] == "networking"
    texts = [f["text"] for f in res["supporting_facts"]]
    assert any("flannel MTU" in t for t in texts)
    assert all("tombstoned" not in t for t in texts)   # tombstoned excluded
    assert all("seaweedfs" not in t for t in texts)    # other domain excluded
    assert len(res["supporting_facts"]) == 3
    assert res["unverified_supporting"] == 2           # 2 agent/no-outcome; 1 outcome-verified
    verified = [f for f in res["supporting_facts"] if f["verified"]]
    assert len(verified) == 1 and "technitium" in verified[0]["text"]


def test_route_suggest_no_facts_no_crash(tmp_path):
    conn = _facts_db(tmp_path)
    res = route_suggest(conn, "something with no matching facts", hint="storage")
    assert res["primary_domain"] == "storage"
    assert res["supporting_facts"] == []
    assert res["unverified_supporting"] == 0
    assert res["outcome_winrates"] == []  # no task_outcomes table in this fixture
