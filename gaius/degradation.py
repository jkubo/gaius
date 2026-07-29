"""gaius.degradation — intra-session degradation detection over Claude Code transcripts.

Walks a session JSONL and produces two NUMERIC, band-free tables in
``~/.gaius/telemetry.db`` (session-keyed, alongside ``prompt_events``):

  * ``turn_fuel``          — one row per main-assistant turn: raw context-fuel
                             ``(working, total, floor)`` token integers. This is the
                             fuel trajectory; the GREEN/YELLOW/… band is NEVER stored,
                             it is derived at read (``band_for``) so a threshold change
                             re-buckets all history. Thresholds are an OUTPUT of the
                             observed fuel distribution, not an input.
  * ``degradation_events`` — detected degradation moments (tool errors, thrash,
                             edit-reverts, interrupted calls, output truncation,
                             auto-compaction), each stamped with the raw fuel at the
                             event.

The payoff (``gaius degradation report``) joins the two into an event RATE per fuel
band (events ÷ turns-in-band). Rate — not raw count — is the falsifiable signal:
raw counts skew to mid-fuel simply because sessions spend most turns there (dwell-time
confound). If the rate rises into RED the band threshold is validated; if it is flat,
degradation is fuel-independent and we are ending sessions before their prime.

Fuel primitive (``_fill`` / ``_main_assistant_usage``) and the BANDS table are ported
from the ``gaius-context-gauge`` hook — that hook is a standalone hot-path script that
must not import the package, so the ~12 lines are duplicated on purpose.
KEEP BANDS + _fill IN SYNC with hooks/gaius-context-gauge and skills/base.md § Context gauge.

Facade convention (see ARCHITECTURE.md): _core re-imports this module's public symbols
before the COMMANDS dict.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

_DB_PATH = Path.home() / ".gaius" / "telemetry.db"

# ── Band table — MIRROR of hooks/gaius-context-gauge (working-set tokens). ──────
# Bands are for READ-TIME bucketing only; never persisted. See module docstring.
GREEN_MAX, YELLOW_MAX, ORANGE_MAX, RED_MAX = 40_000, 90_000, 150_000, 250_000
BANDS = [
    (GREEN_MAX, "GREEN"), (YELLOW_MAX, "YELLOW"), (ORANGE_MAX, "ORANGE"),
    (RED_MAX, "RED"), (float("inf"), "BLACK"),
]

EVENT_TYPES = (
    "tool_error", "tool_thrash", "edit_revert",
    "interrupted", "truncation", "compact_boundary",
)
# In-session working-set degradation events. compact_boundary is EXCLUDED — it is a
# total-fill ceiling marker (mostly operator-driven `manual` /compact at ~290K total),
# reported on its own axis (preTokens), not on the working-set rate curve.
IN_SESSION_EVENTS = (
    "tool_error", "tool_thrash", "edit_revert", "interrupted", "truncation",
)


def band_for(working: int) -> str:
    for ceiling, name in BANDS:
        if working < ceiling:
            return name
    return BANDS[-1][1]


# ── Fuel primitive (ported from gaius-context-gauge — keep in sync) ─────────────
def _fill(usage: dict) -> int:
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )


def _main_assistant_usage(obj: dict):
    """usage dict of a MAIN (non-sidechain, non-meta) assistant turn, else None.

    ABORTED-TURN GUARD (2026-07-28, keep in sync with hooks/gaius-context-gauge): an interrupted
    turn (dropped connection, cancel, timeout) still writes a usage record with EVERY token field
    0. That is the absence of a measurement, not a measurement of zero. In the gauge it produced
    a false GREEN at ~280K fill; here it would poison the turn_fuel table with phantom 0-fill
    turns and drag every band's event-rate denominator. A real assistant turn always carries
    nonzero input+cache_read, so an all-zero fill is unambiguously a stub — drop it.
    """
    if obj.get("isSidechain") or obj.get("isMeta"):
        return None
    msg = obj.get("message") or {}
    usage = msg.get("usage")
    if usage and (msg.get("role") == "assistant" or obj.get("type") == "assistant"):
        return usage if _fill(usage) > 0 else None
    return None


def _epoch(ts_iso: str) -> float:
    if not ts_iso:
        return 0.0
    try:
        return datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _tool_target(name: str, inp: dict) -> str:
    """Stable per-tool identity key for attribution + thrash grouping."""
    if not isinstance(inp, dict):
        return ""
    if name == "Bash":
        return str(inp.get("command", ""))[:160]
    if name in ("Edit", "Write", "Read", "NotebookEdit"):
        return str(inp.get("file_path", ""))
    # fall back to a compact input signature
    return json.dumps(inp, sort_keys=True)[:120]


# ── Detector ────────────────────────────────────────────────────────────────────
def detect_events(path):
    """Parse one transcript JSONL → (session_id, turns, events).

    turns  : list of {turn_index, ts, working, total, floor}  (requestId-deduped)
    events : list of {session_id, ts, event_type, tool, target, detail,
                      turn_index, working, total, floor}
    Streams line-by-line (memory-bounded); file order is chronological for CC.
    """
    path = Path(path)
    session_id = path.stem
    floor = None
    cur = 0
    turn = 0
    last_request = None
    tool_map = {}                 # tool_use_id -> (name, target)
    edits_by_file = {}            # filePath -> list[(old, new)]
    last_err_target = None
    err_run = 0
    turns, events = [], []

    def _emit(etype, ts, tool, target, detail, working=None, total=None):
        f = floor if floor is not None else cur
        events.append({
            "session_id": session_id, "ts": ts, "event_type": etype,
            "tool": tool or "", "target": (target or "")[:200], "detail": (detail or "")[:200],
            "turn_index": turn,
            "working": working if working is not None else max(0, cur - f),
            "total": total if total is not None else cur, "floor": f,
        })

    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                sid = obj.get("sessionId")
                if sid:
                    session_id = sid
                ts = _epoch(obj.get("timestamp", ""))

                # ── main-assistant turn: advance the fuel trajectory ──
                usage = _main_assistant_usage(obj)
                if usage is not None:
                    fill = _fill(usage)
                    if fill:
                        cur = fill
                        if floor is None:
                            floor = fill
                    rid = obj.get("requestId")
                    if rid != last_request:          # dedup: one turn per requestId
                        last_request = rid
                        turn += 1
                        f = floor if floor is not None else cur
                        turns.append({"turn_index": turn, "ts": ts,
                                      "working": max(0, cur - f), "total": cur, "floor": f})
                    msg = obj.get("message") or {}
                    # record tool_use blocks for later tool_result attribution
                    content = msg.get("content")
                    if isinstance(content, list):
                        for blk in content:
                            if isinstance(blk, dict) and blk.get("type") == "tool_use":
                                tool_map[blk.get("id")] = (
                                    blk.get("name", ""), _tool_target(blk.get("name", ""), blk.get("input")))
                    if msg.get("stop_reason") == "max_tokens":
                        _emit("truncation", ts, "", "", "assistant stop_reason=max_tokens")
                    continue

                # ── system: compaction is a total-fill ceiling marker ──
                # compactMetadata.preTokens = real fill at compaction (this transcript's
                # `cur` is unreliable — a resumed session's boundary sits at turn ~0 but
                # preTokens reflects the PRIOR context's peak). trigger ∈ {auto, manual}:
                # `manual` = operator/harness /compact (discretion, not degradation);
                # `auto` = the real window-ceiling hit. Stored in `tool`, pre in `total`.
                if obj.get("type") == "system":
                    if obj.get("subtype") == "compact_boundary":
                        cm = obj.get("compactMetadata") or {}
                        trig = cm.get("trigger", "?")
                        pre, post = cm.get("preTokens"), cm.get("postTokens")
                        f = floor if floor is not None else cur
                        w = max(0, pre - f) if isinstance(pre, int) and pre else None
                        _emit("compact_boundary", ts, trig, "", f"pre={pre} post={post}",
                              working=w, total=pre if isinstance(pre, int) and pre else None)
                    continue

                # ── user record: tool_results + enriched toolUseResult ──
                if obj.get("type") == "user":
                    msg = obj.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, list):
                        for blk in content:
                            if not isinstance(blk, dict) or blk.get("type") != "tool_result":
                                continue
                            tool, target = tool_map.get(blk.get("tool_use_id"), ("", ""))
                            if blk.get("is_error"):
                                if target and target == last_err_target:
                                    err_run += 1
                                else:
                                    err_run, last_err_target = 1, target
                                etype = "tool_thrash" if err_run >= 2 else "tool_error"
                                snippet = str(blk.get("content"))[:160]
                                _emit(etype, ts, tool, target, snippet)
                            else:
                                err_run, last_err_target = 0, None  # success breaks the run
                    # enriched result (top-level): interrupted + edit-revert
                    tur = obj.get("toolUseResult")
                    if isinstance(tur, dict):
                        if tur.get("interrupted"):
                            _emit("interrupted", ts, "", str(tur.get("filePath", "")),
                                  "tool call interrupted")
                        fp, old, new = tur.get("filePath"), tur.get("oldString"), tur.get("newString")
                        if fp and old is not None and new is not None:
                            hist = edits_by_file.setdefault(fp, [])
                            # revert = this edit inverts a prior edit on the same file
                            if any(p_old == new and p_new == old for p_old, p_new in hist):
                                _emit("edit_revert", ts, "Edit", fp,
                                      "edit reverts an earlier edit to this file")
                            hist.append((old, new))
                    continue
    except OSError:
        pass
    return session_id, turns, events


# ── Storage ─────────────────────────────────────────────────────────────────────
def _get_conn():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _init_schema(conn)
    return conn


def _init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS turn_fuel (
            session_id  TEXT NOT NULL,
            turn_index  INTEGER NOT NULL,
            ts          REAL,
            working     INTEGER,   -- raw working-set tokens (total - floor); band derived at read
            total       INTEGER,   -- raw total context fill
            floor       INTEGER,   -- raw session floor (first assistant turn)
            scanned_at  REAL,
            PRIMARY KEY (session_id, turn_index)
        );
        CREATE TABLE IF NOT EXISTS degradation_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            ts          REAL,
            event_type  TEXT NOT NULL,
            tool        TEXT,
            target      TEXT,
            detail      TEXT,
            turn_index  INTEGER,
            working     INTEGER,   -- raw fuel at event; band derived at read
            total       INTEGER,
            floor       INTEGER,
            scanned_at  REAL,
            UNIQUE (session_id, event_type, turn_index, target)
        );
        CREATE INDEX IF NOT EXISTS idx_degr_session ON degradation_events(session_id);
        CREATE INDEX IF NOT EXISTS idx_turnfuel_session ON turn_fuel(session_id);
    """)
    conn.commit()


def store(conn, session_id, turns, events):
    """Idempotent upsert of a scan. Returns (turns_written, events_written)."""
    now = time.time()
    for t in turns:
        conn.execute(
            """INSERT INTO turn_fuel (session_id, turn_index, ts, working, total, floor, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, turn_index) DO UPDATE SET
                 ts=excluded.ts, working=excluded.working, total=excluded.total,
                 floor=excluded.floor, scanned_at=excluded.scanned_at""",
            (session_id, t["turn_index"], t["ts"], t["working"], t["total"], t["floor"], now),
        )
    ev_written = 0
    for e in events:
        cur = conn.execute(
            """INSERT OR IGNORE INTO degradation_events
               (session_id, ts, event_type, tool, target, detail, turn_index,
                working, total, floor, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (e["session_id"], e["ts"], e["event_type"], e["tool"], e["target"], e["detail"],
             e["turn_index"], e["working"], e["total"], e["floor"], now),
        )
        ev_written += cur.rowcount
    conn.commit()
    return len(turns), ev_written


# ── Analysis (the falsifiable red-threshold report) ─────────────────────────────
def _percentile(sorted_vals, p):
    if not sorted_vals:
        return 0
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return int(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo))


def report(conn):
    """Join turn_fuel + degradation_events → per-band event RATE. The payoff metric.

    compact_boundary is split off onto its own total-fill axis (trigger + preTokens);
    the rate curve is over IN_SESSION_EVENTS stamped at their real working fuel.
    """
    turns = [r["working"] for r in conn.execute("SELECT working FROM turn_fuel")]
    band_names = [b[1] for b in BANDS]
    turns_by_band = {b: 0 for b in band_names}
    for w in turns:
        turns_by_band[band_for(w)] += 1

    evs = conn.execute(
        "SELECT event_type, working FROM degradation_events WHERE event_type != 'compact_boundary'"
    ).fetchall()
    evs_by_band = {b: 0 for b in band_names}
    by_type, ev_workings = {}, []
    for r in evs:
        evs_by_band[band_for(r["working"])] += 1
        ev_workings.append(r["working"])
        by_type.setdefault(r["event_type"], []).append(r["working"])

    comp = conn.execute(
        "SELECT tool AS trigger, total FROM degradation_events WHERE event_type = 'compact_boundary'"
    ).fetchall()
    comp_trig, comp_pre = {}, []
    for r in comp:
        comp_trig[r["trigger"]] = comp_trig.get(r["trigger"], 0) + 1
        if r["total"]:
            comp_pre.append(r["total"])
    return {
        "n_turns": len(turns), "n_events": len(evs),
        "turns_by_band": turns_by_band, "events_by_band": evs_by_band,
        "band_names": band_names, "event_working": sorted(ev_workings),
        "turn_working": sorted(turns), "by_type": by_type,
        "comp_trig": comp_trig, "comp_pre": sorted(comp_pre),
    }


# ── CLI ─────────────────────────────────────────────────────────────────────────
def _default_project_dir():
    cwd = os.getcwd()
    enc = cwd.replace("/", "-")
    return Path.home() / ".claude" / "projects" / enc


def cmd_degradation(args):
    """Intra-session degradation detection + red-threshold validation.

    Usage:
      gaius degradation scan [--transcript F | --session SID | --all]
                             [--project DIR] [--since-days N]
      gaius degradation report
    """
    p = argparse.ArgumentParser(prog="gaius degradation")
    sub = p.add_subparsers(dest="sub", required=True)
    ps = sub.add_parser("scan", help="detect events + fuel trajectory, store to telemetry.db")
    g = ps.add_mutually_exclusive_group()
    g.add_argument("--transcript", help="a single .jsonl transcript")
    g.add_argument("--session", help="session-uuid stem within --project dir")
    g.add_argument("--all", action="store_true", help="all transcripts in --project dir")
    ps.add_argument("--project", default=None, help="~/.claude/projects/<enc> dir (default: cwd)")
    ps.add_argument("--since-days", type=float, default=30.0, help="mtime cutoff for --all")
    ps.add_argument("--dry-run", action="store_true", help="detect + count; write nothing")
    sub.add_parser("report", help="per-band event RATE (the falsifiable red-threshold check)")
    ns = p.parse_args(args)

    if ns.sub == "report":
        conn = _get_conn()
        _print_report(report(conn))
        return

    # scan
    proj = Path(ns.project) if ns.project else _default_project_dir()
    if ns.transcript:
        files = [Path(ns.transcript)]
    elif ns.session:
        files = [proj / f"{ns.session}.jsonl"]
    else:  # --all (default)
        cutoff = time.time() - ns.since_days * 86400
        files = sorted(
            (f for f in proj.glob("*.jsonl") if f.stat().st_mtime >= cutoff),
            key=lambda f: f.stat().st_mtime,
        ) if proj.is_dir() else []
    if not files:
        print(f"no transcripts found (project={proj})", file=sys.stderr)
        return

    conn = None if ns.dry_run else _get_conn()
    tot_t = tot_e = 0
    for f in files:
        if not f.exists():
            print(f"  skip (missing): {f}", file=sys.stderr)
            continue
        sid, turns, events = detect_events(f)
        if ns.dry_run:
            tw, ew = len(turns), len(events)
        else:
            tw, ew = store(conn, sid, turns, events)
        tot_t += tw
        tot_e += ew
        if events:
            kinds = {}
            for e in events:
                kinds[e["event_type"]] = kinds.get(e["event_type"], 0) + 1
            summary = " ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
            print(f"  {sid[:8]}  turns={len(turns):3d}  events={len(events):3d}  {summary}")
    tag = " (dry-run, nothing written)" if ns.dry_run else ""
    print(f"scanned {len(files)} transcript(s): {tot_t} turns, {tot_e} new events{tag}")


def _print_report(rep):
    nt, ne = rep["n_turns"], rep["n_events"]
    print(f"degradation report — {nt} turns, {ne} events across scanned sessions\n")
    if nt == 0:
        print("no turn_fuel rows — run `gaius degradation scan --all` first.")
        return
    ew, tw = rep["event_working"], rep["turn_working"]
    print(f"  turn fuel (working-set tokens):  p50={_percentile(tw,.5):>7,}  "
          f"p90={_percentile(tw,.9):>7,}  max={tw[-1] if tw else 0:>7,}")
    if ew:
        print(f"  event fuel (working-set tokens): p50={_percentile(ew,.5):>7,}  "
              f"p90={_percentile(ew,.9):>7,}  max={ew[-1]:>7,}")
    print()
    print(f"  {'band':<7} {'turns':>7} {'events':>7} {'rate(ev/1k turns)':>18}")
    overall = (ne / nt) * 1000 if nt else 0
    for b in rep["band_names"]:
        t = rep["turns_by_band"][b]
        e = rep["events_by_band"][b]
        rate = (e / t * 1000) if t else 0
        flag = ""
        if t:
            if rate > overall * 1.5:
                flag = "  ← elevated"
            elif rate < overall * 0.5:
                flag = "  ← quiet"
        print(f"  {b:<7} {t:>7,} {e:>7,} {rate:>18.1f}{flag}")
    print(f"  {'ALL':<7} {nt:>7,} {ne:>7,} {overall:>18.1f}")
    print()
    print("  by in-session event type (p50 working fuel):")
    for et in IN_SESSION_EVENTS:
        vals = sorted(rep["by_type"].get(et, []))
        if vals:
            print(f"    {et:<16} n={len(vals):>4}  p50={_percentile(vals,.5):>7,}  p90={_percentile(vals,.9):>7,}")
    print()

    # compaction ceiling markers — a separate TOTAL-fill axis, not working-set
    cp = rep["comp_pre"]
    if cp or rep["comp_trig"]:
        trig = "  ".join(f"{k}={v}" for k, v in sorted(rep["comp_trig"].items()))
        print(f"  compaction ceiling (total fill at /compact — separate axis): {trig}")
        if cp:
            print(f"    preTokens: p25={_percentile(cp,.25):>7,}  p50={_percentile(cp,.5):>7,}  "
                  f"p90={_percentile(cp,.9):>7,}  max={cp[-1]:>7,}")
        print("    (mostly `manual`: where operators/harness chose to compact, not degradation onset.)")
        print()

    print("  READ: rate = in-session events per 1k turns spent in that band (dwell-time-normalized).")
    print("  If rate rises monotonically into RED/BLACK, the band threshold is validated.")
    print("  If rate is flat/declining, degradation is fuel-independent — the RED line may be")
    print("  set too low and sessions are being ended before their prime (J's question).")
    print("  Caveat: tool_error fires at all fuel levels (early mistakes ≠ context rot); weight")
    print("  edit_revert / truncation as the fuel-linked in-session signals.")
