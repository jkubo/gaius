"""Session-scoped injection dedup (2026-08-06).

The per-prompt hook fires on every turn and each injected block persists in
conversation history, so re-injecting an entry costs full price for zero new
information. These tests pin the three properties that make suppression safe:

  1. it is OPT-IN — no --session-dedup, no behaviour change for other callers
  2. it EXPIRES — a compaction can drop earlier blocks, so nothing is suppressed
     forever, and hard gates come back ~4x sooner than ordinary entries
  3. it FAILS OPEN — a corrupt/unreadable/absent state file re-injects rather
     than silently withholding a rule

Property 3 is the load-bearing one: the failure mode of a lost state file must
be a wasted token, never a missing hard gate.

State lives in /tmp; every test uses its own session id and cleans up, so no
test can observe another's suppression.
"""
import json

from gaius import landscape as L


def _sid(request_name):
    return f"pytest-dedup-{request_name}"


def _clean(sid):
    p = L._dedup_path(sid)
    if p.exists():
        p.unlink()


# ── state file location / hygiene ──────────────────────────────────────────

def test_path_is_tmp_scoped_and_sanitized():
    """Session ids arrive from the environment — they must never escape /tmp."""
    p = L._dedup_path("../../etc/passwd")
    assert p.parent.as_posix() == "/tmp"
    assert "/" not in p.name.replace(".gaius-injected-", "").replace(".json", "")


def test_path_truncates_absurd_session_id():
    p = L._dedup_path("x" * 500)
    assert len(p.name) < 120


# ── fail-open contract ─────────────────────────────────────────────────────

def test_missing_file_reads_as_nothing_seen():
    sid = _sid("missing")
    _clean(sid)
    st = L._dedup_load(sid)
    assert st == {"seq": 0, "seen": {}}


def test_corrupt_file_fails_open_not_closed():
    """A truncated write must re-inject, not suppress everything."""
    sid = _sid("corrupt")
    L._dedup_path(sid).write_text("{not json at all")
    try:
        st = L._dedup_load(sid)
        assert st["seen"] == {}
        assert not L._dedup_seen(st, "Feedback:anything")
    finally:
        _clean(sid)


def test_wrong_shape_fails_open():
    sid = _sid("shape")
    L._dedup_path(sid).write_text(json.dumps({"seq": 5, "seen": ["not", "a", "dict"]}))
    try:
        assert L._dedup_load(sid)["seen"] == {}
    finally:
        _clean(sid)


def test_empty_session_id_is_a_noop():
    """No session id (e.g. a non-hook caller) must not create files or suppress."""
    st = L._dedup_load("")
    assert st == {"seq": 0, "seen": {}}
    L._dedup_commit("", st, ["Feedback:x"])  # must not raise, must not write
    assert not L._dedup_path("").exists()


def test_commit_never_raises_on_unwritable_path(monkeypatch):
    """Dedup is an optimization; it must not be able to take injection down."""
    def boom(*a, **k):
        raise OSError("read-only fs")
    monkeypatch.setattr(L.Path, "write_text", boom)
    L._dedup_commit("pytest-dedup-boom", {"seq": 1, "seen": {}}, ["Feedback:x"])


# ── TTL semantics ──────────────────────────────────────────────────────────

def test_unseen_key_is_not_suppressed():
    st = {"seq": 1, "seen": {}}
    assert not L._dedup_seen(st, "Feedback:gaius invariants")


def test_just_injected_is_suppressed():
    st = {"seq": 2, "seen": {"Feedback:x": 1}}
    assert L._dedup_seen(st, "Feedback:x")


def test_ordinary_entry_expires_at_ttl_boundary():
    key = "corpus:deadbeef"
    inside = {"seq": 0 + L._DEDUP_TTL_TURNS - 1, "seen": {key: 0}}
    at_ttl = {"seq": 0 + L._DEDUP_TTL_TURNS, "seen": {key: 0}}
    assert L._dedup_seen(inside, key), "still within window — should suppress"
    assert not L._dedup_seen(at_ttl, key), "TTL reached — must re-inject"


def test_hard_gate_expires_sooner_than_ordinary():
    """A compaction can drop the block; safety rules must return faster."""
    key = "Feedback:some hard gate"
    st = {"seq": L._DEDUP_TTL_TURNS_HARD_GATE, "seen": {key: 0}}
    assert not L._dedup_seen(st, key, is_hard_gate=True), "hard gate must be back"
    assert L._dedup_seen(st, key, is_hard_gate=False), "ordinary entry still suppressed"
    assert L._DEDUP_TTL_TURNS_HARD_GATE < L._DEDUP_TTL_TURNS


# ── commit / prune ─────────────────────────────────────────────────────────

def test_commit_records_keys_at_current_seq():
    sid = _sid("commit")
    _clean(sid)
    try:
        st = {"seq": 7, "seen": {}}
        L._dedup_commit(sid, st, ["Feedback:a", "corpus:b"])
        back = L._dedup_load(sid)
        assert back["seq"] == 7
        assert back["seen"]["Feedback:a"] == 7
        assert back["seen"]["corpus:b"] == 7
    finally:
        _clean(sid)


def test_commit_prunes_entries_past_the_longest_ttl():
    """Bounded file: an entry too old to ever suppress again is dropped."""
    sid = _sid("prune")
    _clean(sid)
    try:
        st = {"seq": 100, "seen": {"corpus:ancient": 10, "corpus:recent": 95}}
        L._dedup_commit(sid, st, [])
        seen = L._dedup_load(sid)["seen"]
        assert "corpus:ancient" not in seen
        assert "corpus:recent" in seen
    finally:
        _clean(sid)


def test_empty_commit_still_advances_the_turn_counter():
    """A no-match turn must age TTLs, or a run of them freezes the window."""
    sid = _sid("advance")
    _clean(sid)
    try:
        L._dedup_commit(sid, {"seq": 3, "seen": {}}, [])
        assert L._dedup_load(sid)["seq"] == 3
    finally:
        _clean(sid)
