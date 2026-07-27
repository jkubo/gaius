"""Phase 3 — intra-session degradation detection + numeric fuel stamping.

Covers the fuel trajectory (working/total/floor, requestId dedup), the six event
detectors, idempotent storage, and the dwell-time-normalized rate report. All tests
use a tmp sqlite conn — the real ~/.gaius/telemetry.db is never touched.
"""
import json
import sqlite3

from gaius import degradation as d


def _write(tmp_path, lines):
    p = tmp_path / "sess-abc123.jsonl"
    p.write_text("\n".join(json.dumps(o) for o in lines) + "\n")
    return p


def _assistant(rid, fill, content=None, stop="tool_use", ts="2026-07-24T00:00:00Z"):
    return {
        "type": "assistant", "requestId": rid, "sessionId": "sess-abc123", "timestamp": ts,
        "message": {"role": "assistant", "stop_reason": stop,
                    "usage": {"input_tokens": fill, "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0},
                    "content": content or []},
    }


def _tool_use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _tool_result(tid, is_error=False, content="ok", tur=None):
    r = {"type": "user", "sessionId": "sess-abc123", "timestamp": "2026-07-24T00:00:01Z",
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": tid,
                                  "is_error": is_error, "content": content}]}}
    if tur is not None:
        r["toolUseResult"] = tur
    return r


def _conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "telemetry.db"))
    c.row_factory = sqlite3.Row
    d._init_schema(c)
    return c


# ── band derivation ──────────────────────────────────────────────────────────
def test_band_for_boundaries():
    assert d.band_for(0) == "GREEN"
    assert d.band_for(39_999) == "GREEN"
    assert d.band_for(40_000) == "YELLOW"
    assert d.band_for(149_999) == "ORANGE"
    assert d.band_for(250_000) == "BLACK"
    assert d.band_for(2_000_000) == "BLACK"


# ── fuel trajectory ──────────────────────────────────────────────────────────
def test_fuel_trajectory_and_requestid_dedup(tmp_path):
    lines = [
        _assistant("r1", 60_000),          # floor
        _assistant("r1", 60_000),          # same requestId → NOT a new turn
        _assistant("r2", 200_000),         # working = 140k
    ]
    sid, turns, events = d.detect_events(_write(tmp_path, lines))
    assert sid == "sess-abc123"
    assert len(turns) == 2                 # deduped on requestId
    assert turns[0]["floor"] == 60_000 and turns[0]["working"] == 0
    assert turns[1]["total"] == 200_000 and turns[1]["working"] == 140_000


# ── tool_error attribution + thrash ──────────────────────────────────────────
def test_tool_error_and_thrash(tmp_path):
    lines = [
        _assistant("r0", 60_000),                             # floor turn (first = floor)
        _assistant("r1", 100_000, content=[_tool_use("t1", "Bash", {"command": "make x"})]),
        _tool_result("t1", is_error=True, content="exit 2"),
        _assistant("r2", 110_000, content=[_tool_use("t2", "Bash", {"command": "make x"})]),
        _tool_result("t2", is_error=True, content="exit 2"),   # same target → thrash
        _assistant("r3", 120_000, content=[_tool_use("t3", "Bash", {"command": "make x"})]),
        _tool_result("t3", is_error=False, content="ok"),       # success breaks the run
    ]
    _, _, events = d.detect_events(_write(tmp_path, lines))
    kinds = [e["event_type"] for e in events]
    assert kinds == ["tool_error", "tool_thrash"]
    assert events[0]["tool"] == "Bash" and events[0]["target"] == "make x"
    assert events[0]["working"] == 40_000   # 100k - 60k floor


# ── edit revert ──────────────────────────────────────────────────────────────
def test_edit_revert(tmp_path):
    lines = [
        _assistant("r1", 80_000),
        _tool_result("t1", tur={"filePath": "/x.py", "oldString": "A", "newString": "B"}),
        _assistant("r2", 90_000),
        _tool_result("t2", tur={"filePath": "/x.py", "oldString": "B", "newString": "A"}),  # inverse
    ]
    _, _, events = d.detect_events(_write(tmp_path, lines))
    reverts = [e for e in events if e["event_type"] == "edit_revert"]
    assert len(reverts) == 1
    assert reverts[0]["target"] == "/x.py"


# ── compaction ceiling marker (own axis: trigger + preTokens) ────────────────
def test_compact_boundary_uses_pretokens(tmp_path):
    lines = [
        _assistant("r1", 60_000),
        {"type": "system", "subtype": "compact_boundary", "sessionId": "sess-abc123",
         "timestamp": "2026-07-24T00:05:00Z",
         "compactMetadata": {"trigger": "manual", "preTokens": 292_000, "postTokens": 10_000}},
    ]
    _, _, events = d.detect_events(_write(tmp_path, lines))
    cb = [e for e in events if e["event_type"] == "compact_boundary"]
    assert len(cb) == 1
    assert cb[0]["tool"] == "manual"        # trigger stored in tool
    assert cb[0]["total"] == 292_000        # preTokens, not this transcript's cur
    assert cb[0]["working"] == 232_000      # 292k - 60k floor


# ── storage idempotency ──────────────────────────────────────────────────────
def test_store_idempotent(tmp_path):
    lines = [
        _assistant("r1", 100_000, content=[_tool_use("t1", "Bash", {"command": "c"})]),
        _tool_result("t1", is_error=True, content="err"),
    ]
    sid, turns, events = d.detect_events(_write(tmp_path, lines))
    conn = _conn(tmp_path)
    tw1, ew1 = d.store(conn, sid, turns, events)
    tw2, ew2 = d.store(conn, sid, turns, events)      # re-scan
    assert ew1 == 1 and ew2 == 0                        # events INSERT OR IGNORE
    assert conn.execute("SELECT COUNT(*) FROM degradation_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM turn_fuel").fetchone()[0] == len(turns)


# ── report: rate excludes compaction; compaction on its own axis ─────────────
def test_report_rate_and_compaction_split(tmp_path):
    lines = [
        _assistant("r0", 60_000),                                                        # floor
        _assistant("r1", 100_000, content=[_tool_use("t1", "Bash", {"command": "c"})]),  # working 40k → YELLOW
        _tool_result("t1", is_error=True, content="err"),
        {"type": "system", "subtype": "compact_boundary", "sessionId": "sess-abc123",
         "timestamp": "2026-07-24T00:05:00Z",
         "compactMetadata": {"trigger": "manual", "preTokens": 292_000, "postTokens": 9_000}},
    ]
    sid, turns, events = d.detect_events(_write(tmp_path, lines))
    conn = _conn(tmp_path)
    d.store(conn, sid, turns, events)
    rep = d.report(conn)
    assert rep["n_events"] == 1                          # compaction NOT counted as in-session
    assert rep["events_by_band"]["YELLOW"] == 1
    assert rep["comp_trig"] == {"manual": 1}
    assert rep["comp_pre"] == [292_000]
