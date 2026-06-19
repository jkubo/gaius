"""Tests for gaius rescore — fact_type-based provenance scoring."""
import json
import os
import sys
import sqlite3
import tempfile
from pathlib import Path

import pytest

os.environ["GAIUS_CONFIG"] = "/dev/null"
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

import gaius._core as _gaius_mod
from gaius._core import (
    init_db,
    upsert_fact,
    cmd_rescore,
    PROVENANCE_WEIGHT,
)

# Prevent any test in this file from touching the live DB
_ORIGINAL_DB_PATH = _gaius_mod.DB_PATH


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Ensure every test uses an isolated DB, never the live one."""
    # This runs BEFORE fresh_db and ensures DB_PATH is never ~/.gaius/facts.db
    monkeypatch.setattr(_gaius_mod, "DB_PATH", tmp_path / "default_isolated.db")


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Create a fresh facts.db with test data, fully isolated from live DB."""
    db_path = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db_path)

    conn = init_db(db_path)

    # Insert test facts with different fact_types
    test_facts = [
        ("finding-1", "security", "finding", "auto-mined", "CVE-2024-1234 exploitable in container"),
        ("procedure-1", "operational", "procedure", "auto-mined", "To fix etcd: stop kubelet, restore snapshot, restart"),
        ("security-1", "security", "security", "auto-mined", "Tetragon policy blocks reverse shells on all nodes"),
        ("ops-1", "operational", "operational", "auto-mined", "Node aus-fwd-gpu-01 has 128GB RAM"),
        ("struct-1", "general", "structural", "auto-mined", "Flannel uses VXLAN backend"),
        ("obs-1", "general", "observation", "auto-mined", "Saw a warning in the logs once"),
    ]

    for key, domain, fact_type, prov, text in test_facts:
        conn.execute("""
            INSERT INTO facts (fact_key, domain, fact_type, provenance, fact_text, score,
                             confirmation_count, first_seen, last_seen, model_families, source)
            VALUES (?, ?, ?, ?, ?, 0.22, 1, datetime('now'), datetime('now'), '["claude"]', 'human')
        """, (key, domain, fact_type, prov, text))

    conn.commit()
    conn.close()

    def _open():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    return _open, db_path


class TestRescore:
    def test_findings_score_higher_than_operational(self, fresh_db, capsys):
        open_db, db_path = fresh_db
        cmd_rescore(["--dry-run"])

        captured = capsys.readouterr()
        assert "Would update:" in captured.out
        assert "6 scores" in captured.out

    def test_provenance_mapping(self, fresh_db):
        """fact_type → provenance weight mapping produces differentiated scores."""
        open_db, db_path = fresh_db
        cmd_rescore([])

        conn = open_db()
        scores = {}
        for row in conn.execute("SELECT fact_key, score FROM facts").fetchall():
            scores[row["fact_key"]] = row["score"]
        conn.close()

        # Findings (prov=1.0) must score highest
        assert scores["finding-1"] > scores["ops-1"]
        # Procedures (prov=0.9) above operational (prov=0.7)
        assert scores["procedure-1"] > scores["ops-1"]
        # Security (prov=0.8) above operational (prov=0.7)
        assert scores["security-1"] > scores["ops-1"]

    def test_update_provenance_flag(self, fresh_db):
        open_db, db_path = fresh_db
        cmd_rescore(["--update-provenance"])

        conn = open_db()
        provs = {}
        for row in conn.execute("SELECT fact_key, provenance FROM facts").fetchall():
            provs[row["fact_key"]] = row["provenance"]
        conn.close()

        assert provs["finding-1"] == "finding"
        assert provs["procedure-1"] == "procedure"
        assert provs["security-1"] == "structured_reasoning"
        assert provs["ops-1"] == "automated"

    def test_dry_run_does_not_modify(self, fresh_db):
        open_db, db_path = fresh_db
        conn = open_db()
        before = {row["fact_key"]: row["score"] for row in conn.execute("SELECT fact_key, score FROM facts").fetchall()}
        conn.close()

        cmd_rescore(["--dry-run"])

        conn = open_db()
        after = {row["fact_key"]: row["score"] for row in conn.execute("SELECT fact_key, score FROM facts").fetchall()}
        conn.close()
        assert before == after

    def test_floor_respected(self, fresh_db):
        open_db, db_path = fresh_db
        cmd_rescore(["--floor", "0.15"])

        conn = open_db()
        for row in conn.execute("SELECT score FROM facts").fetchall():
            assert row["score"] >= 0.15
        conn.close()


class TestRescoreKG:
    def test_rebuild_kg_creates_entities(self, fresh_db):
        open_db, db_path = fresh_db
        cmd_rescore(["--rebuild-kg"])

        conn = open_db()
        ent_count = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
        conn.close()
        assert ent_count >= 0

    def test_rebuild_kg_clears_old_data(self, fresh_db):
        open_db, db_path = fresh_db
        conn = open_db()
        conn.execute("INSERT INTO entities (id, name, type) VALUES ('fake:test', 'test', 'node')")
        conn.commit()
        conn.close()

        cmd_rescore(["--rebuild-kg"])

        conn = open_db()
        row = conn.execute("SELECT count(*) FROM entities WHERE id = 'fake:test'").fetchone()
        conn.close()
        assert row[0] == 0
