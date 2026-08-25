"""Curated interpretability samples — schema, validation, and table isolation.

The isolation test is the load-bearing one: it pins the reason `interpretability_samples`
is a SEPARATE table from `degradation_events`. If someone ever merges them, that test
goes red before the degradation rate curve starts silently lying.

All tests use a tmp sqlite conn — the real ~/.gaius/telemetry.db is never touched.
"""
import sqlite3

import pytest

from gaius import degradation as d
from gaius import interp as i


def _conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "telemetry.db"))
    c.row_factory = sqlite3.Row
    d._init_schema(c)   # degradation tables
    i._init_schema(c)   # interpretability_samples — same DB, different lifecycle
    return c


def _sample(**over):
    rec = {
        "sample_key": "sample-a", "ts": 1778231899.0,
        "behavior_class": "stale_tree_work", "severity": "warning",
        "title": "t", "summary": "s", "provenance": "p",
        "transcript_state": "summary_only", "confidence": "inferred",
        "evidence": [{"claim": "c", "artifact": "a", "confidence": "confirmed"}],
    }
    rec.update(over)
    return rec


# ── validation ───────────────────────────────────────────────────────────────
def test_valid_sample_roundtrips(tmp_path):
    conn = _conn(tmp_path)
    key, errs = i.add_sample(conn, _sample())
    assert errs == [] and key == "sample-a"
    r = conn.execute("SELECT * FROM interpretability_samples").fetchone()
    assert r["behavior_class"] == "stale_tree_work"
    assert r["recorded_at"] >= r["ts"]      # curated after the fact, never before


@pytest.mark.parametrize("field,bad", [
    ("behavior_class", "not_a_class"),
    ("severity", "apocalyptic"),
    ("confidence", "pretty_sure"),
    ("transcript_state", "vanished"),
])
def test_enum_fields_reject_junk(tmp_path, field, bad):
    conn = _conn(tmp_path)
    key, errs = i.add_sample(conn, _sample(**{field: bad}))
    assert key is None and any(field in e for e in errs)
    assert conn.execute("SELECT COUNT(*) c FROM interpretability_samples").fetchone()["c"] == 0


def test_missing_required_field_rejected(tmp_path):
    conn = _conn(tmp_path)
    key, errs = i.add_sample(conn, _sample(provenance=""))
    assert key is None and any("provenance" in e for e in errs)


def test_evidence_without_artifact_rejected(tmp_path):
    """An unbacked claim is not evidence — this is the whole point of the grading."""
    conn = _conn(tmp_path)
    key, errs = i.add_sample(
        conn, _sample(evidence=[{"claim": "trust me", "confidence": "confirmed"}]))
    assert key is None and any("artifact" in e for e in errs)


def test_evidence_confidence_must_be_graded(tmp_path):
    conn = _conn(tmp_path)
    key, errs = i.add_sample(
        conn, _sample(evidence=[{"claim": "c", "artifact": "a", "confidence": "vibes"}]))
    assert key is None and any("evidence[0].confidence" in e for e in errs)


def test_upsert_is_idempotent_and_updates(tmp_path):
    conn = _conn(tmp_path)
    i.add_sample(conn, _sample())
    i.add_sample(conn, _sample(summary="revised"))
    rows = conn.execute("SELECT summary FROM interpretability_samples").fetchall()
    assert len(rows) == 1 and rows[0]["summary"] == "revised"


# ── table isolation — the design invariant ───────────────────────────────────
def test_samples_do_not_pollute_degradation_report(tmp_path):
    """A curated sample must NEVER be counted as a scanned degradation event.

    degradation.report() selects `WHERE event_type != 'compact_boundary'` and folds
    everything left into n_events / events_by_band. If samples ever shared that table,
    every curated row would silently inflate the red-threshold rate curve while being
    invisible in the by-type breakdown (which iterates a fixed IN_SESSION_EVENTS tuple).
    """
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO turn_fuel (session_id, turn_index, working, total, floor) "
                 "VALUES ('s', 0, 1000, 2000, 1000)")
    conn.commit()
    before = d.report(conn)

    for n in range(5):
        i.add_sample(conn, _sample(sample_key=f"sample-{n}"))

    after = d.report(conn)
    assert after["n_events"] == before["n_events"] == 0
    assert after["events_by_band"] == before["events_by_band"]
    assert after["by_type"] == before["by_type"]


def test_taxonomy_slugs_are_stable():
    """Samples cite these by value and old rows are never migrated — append only."""
    for slug in ("architecture_substitution", "stale_tree_work", "destructive_recovery",
                 "persona_collapse", "unauthorized_deploy", "scope_drift",
                 "fabricated_verification"):
        assert slug in i.BEHAVIOR_CLASSES


def test_show_missing_returns_1(tmp_path, monkeypatch, capsys):
    """#206: missing sample is a real failure, not implicit 0 via discarded rc."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(i, "_get_conn", lambda: conn)
    assert i.cmd_interp(["show", "no-such-key"]) == 1
    assert "no such sample" in capsys.readouterr().out


def test_add_reject_returns_1(tmp_path, monkeypatch, capsys):
    """add used to sys.exit(1) to dodge discarded returns; now returns 1."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(i, "_get_conn", lambda: conn)
    bad = tmp_path / "bad.json"
    bad.write_text('{"sample_key": "x", "title": "t", "summary": "s"}')
    assert i.cmd_interp(["add", "--from-json", str(bad)]) == 1
    assert "REJECTED" in capsys.readouterr().out
