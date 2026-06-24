"""Tests for the source-of-truth reconciler (promotion-gap fix)."""
import os
import tempfile
from pathlib import Path

from gaius._core import (
    load_source_registry, source_divergence, reconcile_source,
    _reconcile_excluded, _dir_fingerprint, init_db,
)


def test_default_registry_has_gaius():
    names = [s["name"] for s in load_source_registry()]
    assert "gaius" in names
    g = next(s for s in load_source_registry() if s["name"] == "gaius")
    assert g.get("mirror_path") and g.get("facts")  # curated facts + a mirror to diff


def test_excludes():
    assert _reconcile_excluded(".git/config")
    assert _reconcile_excluded("gaius/__pycache__/x.cpython.pyc")
    assert _reconcile_excluded("x.pyc")
    assert _reconcile_excluded("benchmarks/run.py")
    assert _reconcile_excluded("OPEN-SOURCE-PLAN.md")
    assert _reconcile_excluded("gaius_memory.egg-info/PKG-INFO")
    assert not _reconcile_excluded("gaius/_core.py")
    assert not _reconcile_excluded("README.md")


def test_divergence_in_sync_and_drift():
    with tempfile.TemporaryDirectory() as d:
        dev, mir = Path(d) / "dev", Path(d) / "mir"
        for root in (dev, mir):
            (root / "gaius").mkdir(parents=True)
            (root / "gaius" / "_core.py").write_text("print('same')\n")
            (root / "README.md").write_text("docs\n")
        # identical → in sync (excluded junk must not count)
        (dev / "__pycache__").mkdir()
        (dev / "__pycache__" / "x.pyc").write_text("junk")
        div = source_divergence(str(dev), str(mir))
        assert div["in_sync"], div

        # mirror falls behind: dev gains a file + an existing file diverges
        (dev / "gaius" / "new.py").write_text("new\n")
        (dev / "README.md").write_text("docs CHANGED\n")
        div = source_divergence(str(dev), str(mir))
        assert not div["in_sync"]
        assert "gaius/new.py" in div["missing_from_mirror"]
        assert "README.md" in div["differ"]


def test_divergence_missing_mirror():
    with tempfile.TemporaryDirectory() as d:
        dev = Path(d) / "dev"
        dev.mkdir()
        (dev / "a.py").write_text("x")
        div = source_divergence(str(dev), str(Path(d) / "does-not-exist"))
        assert div["in_sync"] is False and div["mirror_exists"] is False


def _src(facts):
    return {"name": "t", "dev_path": "/nonexistent", "facts": facts}  # no mirror_path → div=None


def test_promote_insert_once_no_count_inflation():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(Path(d) / "facts.db")
        src = _src(["fact alpha for reconcile", "fact beta for reconcile"])

        r1 = reconcile_source(conn, src, promote=True)
        conn.commit()
        assert r1["facts"] == ["inserted", "inserted"]
        assert r1["divergence"] is None  # no mirror configured

        # re-run: insert-once → already present, NOT re-corroborated
        r2 = reconcile_source(conn, src, promote=True)
        conn.commit()
        assert r2["facts"] == ["exists", "exists"]

        # confirmation_count must stay 1 (single-source; not inflated by the nightly)
        cc = conn.execute(
            "SELECT confirmation_count FROM facts WHERE fact_key='reconcile:t:0'").fetchone()[0]
        assert cc == 1, f"confirmation_count inflated to {cc} — repetition poison"

        # promoted facts are UNVERIFIED (outcome NULL, source != human)
        outcome, source = conn.execute(
            "SELECT outcome, source FROM facts WHERE fact_key='reconcile:t:0'").fetchone()
        assert outcome is None and source == "reconcile"


def test_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory() as d:
        conn = init_db(Path(d) / "facts.db")
        src = _src(["dry fact one"])
        r = reconcile_source(conn, src, promote=False)
        assert r["facts"] == ["would-insert"]
        n = conn.execute("SELECT COUNT(*) FROM facts WHERE fact_key='reconcile:t:0'").fetchone()[0]
        assert n == 0  # dry-run wrote nothing
