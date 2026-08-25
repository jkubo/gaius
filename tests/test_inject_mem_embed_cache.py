"""Memory-file embedding cache + --task-skill expansion (2026-08-22).

Before this cache, cmd_inject's memory-file semantic gate re-embedded every
candidate LIVE on every prompt — ~360 unix-socket round trips, measured at ~5.9s
of the UserPromptSubmit hook's 8s budget. Corpus facts never had that problem
(fact_embeddings is indexed and batch-loaded); memory files just never got the
same treatment.

These tests pin the properties that make the cache safe to sit in that hot path:

  1. the key is the EXACT embed input, so a content change is a miss by
     construction and there is no staleness window to invalidate
  2. it FAILS OPEN — a corrupt blob, a missing table, or an unwritable DB must
     degrade to live embedding (slow-but-correct), never raise, never return a
     wrong vector. cmd_inject runs on every prompt behind `2>/dev/null` + exit 0,
     so anything that raises here is an INVISIBLE loss of context.
  3. the prune protects rows still in use. created_at is insert time and is never
     refreshed on a hit, so it is NOT a recency signal — a never-edited file's
     vector is among the OLDEST rows precisely because it has always been valid.

And for --task-skill, the one that actually bit in review:

  4. an UNKNOWN skill injects NOTHING. The slash-command hook's regex matches any
     leading /word, so it fires for /commit, /config, /model — none of which are
     gaius skills. Falling back to the bare name made each run a full-budget
     retrieval on one English word.
"""
import sqlite3
import struct

import pytest

from gaius import _core
from gaius import landscape as L
from gaius._core import _EMBED_DIM


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point gaius at a scratch facts.db (init_db refuses the live one under pytest)."""
    p = tmp_path / "facts.db"
    monkeypatch.setattr(_core, "DB_PATH", p)
    return p


def _vec(fill=0.5):
    return [fill] * _EMBED_DIM


# ── 1. key identity ──────────────────────────────────────────────────────────

def test_key_is_deterministic_and_content_sensitive():
    a = L._mem_embed_key("name: desc. body")
    assert a == L._mem_embed_key("name: desc. body")
    assert a != L._mem_embed_key("name: desc. bodyX")


def test_key_tracks_the_embed_input_not_the_path():
    """Two files with identical embed input share a row; a rename keeps its vector."""
    same = "x: y. z"
    assert L._mem_embed_key(same) == L._mem_embed_key(same)


# ── 2. round-trip ────────────────────────────────────────────────────────────

def test_store_then_load_round_trips_within_float32(db):
    v = [i / 1000.0 for i in range(_EMBED_DIM)]
    L._mem_embed_cache_store({"k1": v})
    got = L._mem_embed_cache_load()
    assert "k1" in got
    # stored as float32; drift must stay far below the gate thresholds (0.20-0.50)
    assert max(abs(a - b) for a, b in zip(v, got["k1"])) < 1e-6


def test_store_is_a_noop_on_empty_pending(db):
    L._mem_embed_cache_store({})
    assert L._mem_embed_cache_load() == {}


def test_store_rejects_wrong_dimension_vectors(db):
    L._mem_embed_cache_store({"short": [0.1, 0.2]})
    assert L._mem_embed_cache_load() == {}


# ── 3. fail-open contract (the load-bearing one) ─────────────────────────────

def test_load_returns_empty_when_table_absent(tmp_path, monkeypatch):
    """A DB with no cache table must read as a total miss, not an exception."""
    p = tmp_path / "empty.db"
    sqlite3.connect(str(p)).close()
    monkeypatch.setattr(_core, "DB_PATH", p)
    assert L._mem_embed_cache_load() == {}


def test_load_skips_corrupt_blob_without_dropping_good_rows(db):
    L._mem_embed_cache_store({"good": _vec()})
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO memory_file_embeddings (content_hash, embedding, created_at) "
        "VALUES ('bad', ?, '2026-01-01')", (b"\x00\x01\x02",)  # too short to unpack
    )
    conn.commit()
    conn.close()
    got = L._mem_embed_cache_load()
    assert "good" in got and "bad" not in got   # corrupt row misses; it re-embeds


def test_store_never_raises_on_unwritable_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_core, "DB_PATH", tmp_path / "nope" / "x" / "facts.db")
    monkeypatch.setattr(L, "init_db", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    L._mem_embed_cache_store({"k": _vec()})   # must swallow


def test_load_never_raises_on_broken_init(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "init_db", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert L._mem_embed_cache_load() == {}


# ── 4. prune protects the live set ───────────────────────────────────────────

def test_prune_evicts_only_unprotected_rows(db):
    """created_at is insert order, NOT recency — so live keys must be protected.

    Seed past the cap with rows that are all OLDER than the live set, then store
    one new vector while declaring a live set. The declared-live rows must survive
    even though they are the oldest thing in the table.
    """
    conn = _core.init_db()
    blob = struct.pack(f"{_EMBED_DIM}f", *_vec())
    live = [f"live{i}" for i in range(50)]
    conn.executemany(
        "INSERT OR REPLACE INTO memory_file_embeddings VALUES (?,?,?)",
        [(k, blob, "2026-01-01T00:00:00") for k in live]
        + [(f"orph{i}", blob, "2026-01-02T00:00:00") for i in range(L._MEM_EMBED_CACHE_MAX + 100)],
    )
    conn.commit()
    conn.close()

    L._mem_embed_cache_store({"fresh": _vec()}, set(live))

    got = L._mem_embed_cache_load()
    assert len(got) <= L._MEM_EMBED_CACHE_MAX
    assert "fresh" in got
    # every protected key survived despite being the oldest rows in the table
    assert all(k in got for k in live)


def test_prune_does_not_fire_below_cap(db):
    L._mem_embed_cache_store({f"k{i}": _vec() for i in range(20)}, set())
    assert len(L._mem_embed_cache_load()) == 20


# ── 5. --task-skill expansion ────────────────────────────────────────────────

def _run_inject(capsys, *args):
    L.cmd_inject(list(args))
    return capsys.readouterr().out


def test_task_skill_unknown_injects_nothing(db, capsys):
    """The slash hook fires for ANY leading /word — a non-skill must cost zero."""
    out = _run_inject(
        capsys, "--task-skill", "definitely-not-a-real-skill-xyz",
        "--budget", "2000", "--skills-budget", "0", "--no-always-skills",
        "--no-semantic", "--format", "plain",
    )
    assert out.strip() == ""


def test_task_skill_is_ignored_when_task_given(db, capsys):
    """--task wins; --task-skill must not override an explicit query."""
    out = _run_inject(
        capsys, "--task", "explicit query here", "--task-skill", "not-a-skill",
        "--budget", "1", "--skills-budget", "0", "--no-always-skills",
        "--no-semantic", "--format", "plain",
    )
    # did NOT early-return on the unknown skill, because --task was supplied
    assert "not a known skill" not in out
