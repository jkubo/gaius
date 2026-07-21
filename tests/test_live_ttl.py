"""Loop-2 automated gates — item 4: fact_type='live' auto-tombstone after N days in cmd_decay.

Env GAIUS_LIVE_TTL_DAYS / --live-ttl-days (default 0 = disabled). Under test:
  - default-off → NO tombstones (decay behaves as before)
  - flag-on → stale 'live' facts soft-tombstoned; fresh 'live' + non-'live' untouched
  - soft tombstone only (tombstoned_at + tombstone_reason set; never DELETE)
  - dry-run mutates nothing
"""
import os
import sys
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

os.environ["GAIUS_CONFIG"] = "/dev/null"
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

import gaius._core as _gaius_mod
from gaius._core import init_db, cmd_decay


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_gaius_mod, "DB_PATH", tmp_path / "isolated.db")
    monkeypatch.delenv("GAIUS_LIVE_TTL_DAYS", raising=False)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "facts.db"
    monkeypatch.setattr(_gaius_mod, "DB_PATH", db_path)
    conn = init_db(db_path)
    rows = [
        # fact_key, fact_type, last_seen(age)
        ("live_stale", "live",        _iso(100)),   # stale live → tombstone when enabled
        ("live_fresh", "live",        _iso(1)),     # fresh live → keep
        ("op_stale",   "operational", _iso(100)),   # non-live, old → NEVER tombstone
    ]
    for key, ft, ls in rows:
        conn.execute(
            "INSERT INTO facts (fact_key, domain, fact_text, fact_type, score, confirmation_count, "
            "first_seen, last_seen, provenance, model_families, source) "
            "VALUES (?,?,?,?,0.5,1,?,?,'automated','[\"claude\"]','human')",
            (key, "ops", f"text for {key}", ft, ls, ls))
    conn.commit()
    conn.close()

    def _open():
        c = sqlite3.connect(str(db_path)); c.row_factory = sqlite3.Row; return c
    return _open, db_path


def _tombstoned_keys(open_db):
    conn = open_db()
    keys = [r["fact_key"] for r in conn.execute(
        "SELECT fact_key FROM facts WHERE tombstoned_at IS NOT NULL").fetchall()]
    conn.close()
    return set(keys)


# ── default-off ─────────────────────────────────────────────────────────────────

def test_default_off_no_tombstones(fresh_db):
    open_db, _ = fresh_db
    cmd_decay([])  # no flag, no env
    assert _tombstoned_keys(open_db) == set()


def test_ttl_zero_no_tombstones(fresh_db):
    open_db, _ = fresh_db
    cmd_decay(["--live-ttl-days", "0"])
    assert _tombstoned_keys(open_db) == set()


# ── flag-on ───────────────────────────────────────────────────────────────────

def test_flag_tombstones_only_stale_live(fresh_db):
    open_db, _ = fresh_db
    cmd_decay(["--live-ttl-days", "30"])
    assert _tombstoned_keys(open_db) == {"live_stale"}


def test_env_var_also_enables(fresh_db, monkeypatch):
    open_db, _ = fresh_db
    monkeypatch.setenv("GAIUS_LIVE_TTL_DAYS", "30")
    cmd_decay([])
    assert _tombstoned_keys(open_db) == {"live_stale"}


def test_soft_tombstone_sets_reason_and_keeps_row(fresh_db):
    open_db, _ = fresh_db
    cmd_decay(["--live-ttl-days", "30"])
    conn = open_db()
    row = conn.execute(
        "SELECT tombstoned_at, tombstone_reason FROM facts WHERE fact_key='live_stale'").fetchone()
    total = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    conn.close()
    assert row["tombstoned_at"] is not None
    assert "live-ttl" in row["tombstone_reason"]
    assert total == 3  # soft: row retained, never DELETEd


def test_non_live_never_tombstoned_even_when_older_than_ttl(fresh_db):
    open_db, _ = fresh_db
    cmd_decay(["--live-ttl-days", "10"])  # op_stale is 100d old but not 'live'
    assert "op_stale" not in _tombstoned_keys(open_db)


def test_dry_run_mutates_nothing(fresh_db):
    open_db, _ = fresh_db
    cmd_decay(["--live-ttl-days", "30", "--dry-run"])
    assert _tombstoned_keys(open_db) == set()
