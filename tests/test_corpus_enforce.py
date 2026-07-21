"""Loop-2 automated gates — item 2: corpus_audit demote-only enforce (enforce_demote).

DEMOTE-ONLY: reclassify flagged 'auto' facts → 'pending'. Under test:
  - repetition-only tier (cc >= REPETITION_THRESHOLD, unverified) demoted
  - contradiction-cluster losers demoted behind a strictly HIGHER cc bar
  - never touches confidence_source, never tombstones, never DELETEs
  - reversible + idempotent (only 'auto' rows eligible)
"""
import sqlite3

from gaius._core import (
    enforce_demote, REPETITION_THRESHOLD, CONTRADICTION_ENFORCE_MIN_CC,
)


def _db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "facts.db"))
    conn.execute(
        "CREATE TABLE facts (id INTEGER PRIMARY KEY, fact_key TEXT, domain TEXT, fact_text TEXT, "
        "review_state TEXT, confirmation_count INTEGER, score REAL, outcome TEXT, "
        "confidence_source TEXT, tombstoned_at TEXT)")
    return conn


def _seed(conn, rows):
    conn.executemany(
        "INSERT INTO facts (fact_key,domain,fact_text,review_state,confirmation_count,score,"
        "outcome,confidence_source,tombstoned_at) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def test_threshold_ordering():
    # contradiction bar MUST be strictly higher than the repetition bar
    assert CONTRADICTION_ENFORCE_MIN_CC > REPETITION_THRESHOLD


def test_repetition_tier_demotes_auto_only(tmp_path):
    conn = _db(tmp_path)
    _seed(conn, [
        ("k1", "ops", "rep unverified auto",   "auto",    3, 0.9, None,        "agent", None),  # demote
        ("k2", "ops", "single sighting",       "auto",    1, 0.5, None,        "agent", None),  # cc<thr, keep
        ("k3", "ops", "outcome verified",      "auto",    5, 0.9, "confirmed", "agent", None),  # verified, keep
        ("k4", "ops", "human verified",        "auto",    5, 0.9, None,        "human", None),  # human, keep
        ("k5", "ops", "already pending",        "pending", 5, 0.9, None,        "agent", None),  # not auto, keep
        ("k6", "ops", "tombstoned",             "auto",    5, 0.9, None,        "agent", "2026-01-01"),  # dead, keep
    ])
    res = enforce_demote(conn)
    assert res["repetition_demoted"] == 1
    states = dict(conn.execute("SELECT fact_key, review_state FROM facts").fetchall())
    assert states["k1"] == "pending"     # the only auto+repetition+unverified row
    assert states["k2"] == "auto"
    assert states["k3"] == "auto"
    assert states["k4"] == "auto"
    assert states["k5"] == "pending"     # untouched (was already pending)
    assert states["k6"] == "auto"


def test_confidence_source_never_forged(tmp_path):
    conn = _db(tmp_path)
    _seed(conn, [
        ("k1", "ops", "rep", "auto", 4, 0.9, None, "agent", None),
    ])
    enforce_demote(conn)
    # HARD CONSTRAINT: enforce touches review_state only — no fact becomes 'human'
    n_human = conn.execute("SELECT COUNT(*) FROM facts WHERE confidence_source='human'").fetchone()[0]
    assert n_human == 0
    # and it NEVER tombstones (demote-only)
    n_tomb = conn.execute("SELECT COUNT(*) FROM facts WHERE tombstoned_at IS NOT NULL").fetchone()[0]
    assert n_tomb == 0


def test_contradiction_tier_demotes_loser_below_repetition_bar(tmp_path):
    # A low-cc divergent claim (cc=1, below repetition bar) inside a REINFORCED contradiction
    # cluster gets demoted by the contradiction tier even though repetition alone wouldn't catch it.
    conn = _db(tmp_path)
    _seed(conn, [
        ("dup", "ops", "winner reinforced", "auto", CONTRADICTION_ENFORCE_MIN_CC, 0.9, None, "agent", None),
        ("dup", "ops", "loser one-off",     "auto", 1, 0.4, None, "agent", None),  # contradiction loser
    ])
    res = enforce_demote(conn)
    rows = conn.execute(
        "SELECT fact_text, review_state FROM facts ORDER BY confirmation_count DESC").fetchall()
    states = {t: rs for t, rs in rows}
    # winner (cc == bar) demoted by repetition tier; loser (cc=1) demoted by contradiction tier
    assert states["loser one-off"] == "pending"
    assert res["contradiction_demoted"] == 1
    assert res["repetition_demoted"] == 1  # winner cc >= REPETITION_THRESHOLD


def test_contradiction_bar_respected(tmp_path):
    # cluster whose max cc is BELOW the contradiction bar and below repetition bar → untouched
    conn = _db(tmp_path)
    _seed(conn, [
        ("dup", "ops", "a", "auto", 1, 0.5, None, "agent", None),
        ("dup", "ops", "b", "auto", 1, 0.5, None, "agent", None),
    ])
    res = enforce_demote(conn)
    assert res["contradiction_demoted"] == 0
    assert res["repetition_demoted"] == 0
    states = [rs for (rs,) in conn.execute("SELECT review_state FROM facts").fetchall()]
    assert states == ["auto", "auto"]


def test_idempotent(tmp_path):
    conn = _db(tmp_path)
    _seed(conn, [
        ("k1", "ops", "rep", "auto", 4, 0.9, None, "agent", None),
        ("dup", "ops", "w", "auto", 5, 0.9, None, "agent", None),
        ("dup", "ops", "l", "auto", 1, 0.4, None, "agent", None),
    ])
    first = enforce_demote(conn)
    second = enforce_demote(conn)
    assert first["repetition_demoted"] >= 1
    assert second["repetition_demoted"] == 0   # nothing left in 'auto' to demote
    assert second["contradiction_demoted"] == 0
