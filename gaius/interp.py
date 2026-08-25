"""Interpretability samples — curated agent-behaviour case studies.

WHY THIS IS NOT `degradation_events` (read before "simplifying" the two together):

  1. `degradation.report()` runs
         SELECT event_type, working FROM degradation_events WHERE event_type != 'compact_boundary'
     and folds EVERY surviving row into `n_events` / `events_by_band` — the headline
     `rate (ev/1k turns)` curve. But `_print_report` only itemises `IN_SESSION_EVENTS`,
     a fixed tuple. So a curated row inserted there would SKEW the red-threshold curve
     while being INVISIBLE in the by-type breakdown. Silent, unattributable drift.

  2. `degradation_events` rows are machine-derived by `detect_events(path)` from a live
     transcript, keyed `UNIQUE (session_id, event_type, turn_index, target)`. A curated
     sample often has NO surviving transcript and no turn_index — `scan --all` can never
     regenerate it, so it would be an un-reproducible row inside a reproducible table.

  Samples are hand-curated, evidence-graded, and permanent. Events are scanned, fuel-stamped,
  and disposable. Different lifecycles ⇒ different tables. Same DB, because both are
  session telemetry and callers already open telemetry.db.

Every sample carries a per-claim confidence grade and an explicit transcript_state, because
the dominant failure of this corpus is an inference recorded as a fact (see the Audit
Uncertainty Protocol). A sample whose source transcript is gone is still worth keeping —
but it must SAY so, so a later reader does not cite it as primary evidence.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

_DB_PATH = Path.home() / ".gaius" / "telemetry.db"

# Behaviour taxonomy. Additive — append, never renumber or repurpose an existing slug,
# because samples reference these by value and old rows are not migrated.
BEHAVIOR_CLASSES = (
    "architecture_substitution",   # built a different stack than the one specified
    "stale_tree_work",             # edited//built in a stale, detached or superseded checkout
    "destructive_recovery",        # reset/stash/checkout to "fix" its own wrong-direction work
    "persona_collapse",            # configured register lost to base-policy fallback mid-session
    "unauthorized_deploy",         # shipped without the operator gate
    "scope_drift",                 # silently widened or narrowed the requested scope
    "fabricated_verification",     # claimed a check it did not run, or a green it did not observe
)

CONFIDENCE = ("confirmed", "inferred", "unverified")
TRANSCRIPT_STATES = ("available", "deleted", "summary_only")
SEVERITIES = ("info", "warning", "critical")


def _get_conn():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _init_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS interpretability_samples (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_key       TEXT UNIQUE NOT NULL,  -- stable slug; the citation handle
            ts               REAL NOT NULL,         -- when the BEHAVIOUR happened
            recorded_at      REAL NOT NULL,         -- when it was curated (drift check)
            session_id       TEXT,                  -- source session; may no longer exist
            harness          TEXT,                  -- claude | grok | codex | gemini
            model            TEXT,
            behavior_class   TEXT NOT NULL,         -- BEHAVIOR_CLASSES slug
            severity         TEXT NOT NULL,
            title            TEXT NOT NULL,
            summary          TEXT NOT NULL,
            evidence         TEXT,                  -- JSON [{claim, artifact, confidence}]
            provenance       TEXT NOT NULL,         -- how it was established, in prose
            transcript_state TEXT NOT NULL,         -- available | deleted | summary_only
            confidence       TEXT NOT NULL,         -- overall grade for the headline claim
            linked_memory    TEXT,                  -- JSON [paths] of memory files it produced
            unresolved       TEXT,                  -- JSON [questions] evidence could NOT settle
            notes            TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_interp_class "
                 "ON interpretability_samples(behavior_class)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_interp_ts "
                 "ON interpretability_samples(ts)")
    conn.commit()


def _validate(rec):
    """Reject a malformed sample loudly. A silently-accepted bad grade is worse than no row."""
    errs = []
    for field in ("sample_key", "ts", "behavior_class", "severity", "title",
                  "summary", "provenance", "transcript_state", "confidence"):
        if not rec.get(field):
            errs.append(f"missing required field: {field}")
    if rec.get("behavior_class") and rec["behavior_class"] not in BEHAVIOR_CLASSES:
        errs.append(f"behavior_class must be one of {BEHAVIOR_CLASSES}")
    if rec.get("severity") and rec["severity"] not in SEVERITIES:
        errs.append(f"severity must be one of {SEVERITIES}")
    if rec.get("confidence") and rec["confidence"] not in CONFIDENCE:
        errs.append(f"confidence must be one of {CONFIDENCE}")
    if rec.get("transcript_state") and rec["transcript_state"] not in TRANSCRIPT_STATES:
        errs.append(f"transcript_state must be one of {TRANSCRIPT_STATES}")
    for i, ev in enumerate(rec.get("evidence") or []):
        if ev.get("confidence") not in CONFIDENCE:
            errs.append(f"evidence[{i}].confidence must be one of {CONFIDENCE}")
        if not ev.get("artifact"):
            errs.append(f"evidence[{i}] has no artifact — an unbacked claim is not evidence")
    return errs


def add_sample(conn, rec):
    errs = _validate(rec)
    if errs:
        return None, errs
    conn.execute("""
        INSERT INTO interpretability_samples
          (sample_key, ts, recorded_at, session_id, harness, model, behavior_class,
           severity, title, summary, evidence, provenance, transcript_state,
           confidence, linked_memory, unresolved, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(sample_key) DO UPDATE SET
          recorded_at=excluded.recorded_at, evidence=excluded.evidence,
          summary=excluded.summary, provenance=excluded.provenance,
          confidence=excluded.confidence, unresolved=excluded.unresolved,
          notes=excluded.notes
    """, (
        rec["sample_key"], float(rec["ts"]), time.time(), rec.get("session_id"),
        rec.get("harness"), rec.get("model"), rec["behavior_class"], rec["severity"],
        rec["title"], rec["summary"], json.dumps(rec.get("evidence") or []),
        rec["provenance"], rec["transcript_state"], rec["confidence"],
        json.dumps(rec.get("linked_memory") or []),
        json.dumps(rec.get("unresolved") or []), rec.get("notes"),
    ))
    conn.commit()
    return rec["sample_key"], []


def _fmt_ts(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "?"


def cmd_interp(args):
    """Curated agent-behaviour case studies (interpretability scope).

    Usage:
      gaius interp add --from-json FILE
      gaius interp list [--class SLUG]
      gaius interp show <sample_key>
      gaius interp classes
    """
    p = argparse.ArgumentParser(prog="gaius interp")
    sub = p.add_subparsers(dest="sub", required=True)

    pa = sub.add_parser("add", help="record a sample from a JSON file")
    pa.add_argument("--from-json", required=True)

    pl = sub.add_parser("list", help="list samples, newest behaviour first")
    pl.add_argument("--class", dest="klass", default=None)

    ps = sub.add_parser("show", help="full record for one sample")
    ps.add_argument("sample_key")

    sub.add_parser("classes", help="the behaviour taxonomy")
    ns = p.parse_args(args)

    if ns.sub == "classes":
        for c in BEHAVIOR_CLASSES:
            print(f"  {c}")
        return 0

    conn = _get_conn()

    if ns.sub == "add":
        rec = json.loads(Path(os.path.expanduser(ns.from_json)).read_text())
        key, errs = add_sample(conn, rec)
        if errs:
            print("REJECTED — sample is malformed:")
            for e in errs:
                print(f"  - {e}")
            # main() honors exact-int returns (#206). Do not sys.exit here —
            # that dodge existed because the dispatcher used to discard rc.
            return 1
        print(f"✓ recorded interpretability sample: {key}")
        return 0

    if ns.sub == "list":
        q = "SELECT * FROM interpretability_samples"
        params = ()
        if ns.klass:
            q += " WHERE behavior_class = ?"
            params = (ns.klass,)
        q += " ORDER BY ts DESC"
        rows = conn.execute(q, params).fetchall()
        if not rows:
            print("no interpretability samples recorded.")
            return 0
        print(f"{len(rows)} sample(s):\n")
        for r in rows:
            flag = "" if r["transcript_state"] == "available" else f"  [{r['transcript_state']}]"
            print(f"  {_fmt_ts(r['ts'])}  {r['sample_key']}")
            print(f"      {r['behavior_class']} · {r['severity']} · {r['confidence']}{flag}")
            print(f"      {r['title']}")
        return 0

    if ns.sub == "show":
        r = conn.execute("SELECT * FROM interpretability_samples WHERE sample_key = ?",
                         (ns.sample_key,)).fetchone()
        if not r:
            print(f"no such sample: {ns.sample_key}")
            return 1
        print(f"\n{r['title']}\n{'=' * len(r['title'])}")
        print(f"  key         {r['sample_key']}")
        print(f"  occurred    {_fmt_ts(r['ts'])}   recorded {_fmt_ts(r['recorded_at'])}")
        print(f"  session     {r['session_id'] or '—'}  ({r['harness'] or '?'}"
              f"{'/' + r['model'] if r['model'] else ''})")
        print(f"  class       {r['behavior_class']} · {r['severity']}")
        print(f"  confidence  {r['confidence']}   transcript: {r['transcript_state']}")
        print(f"\n{r['summary']}\n")
        ev = json.loads(r["evidence"] or "[]")
        if ev:
            print("  EVIDENCE")
            for e in ev:
                print(f"    [{e['confidence']:<9}] {e['claim']}")
                print(f"                 ← {e['artifact']}")
        unres = json.loads(r["unresolved"] or "[]")
        if unres:
            print("\n  UNRESOLVED (evidence could not settle these)")
            for u in unres:
                print(f"    - {u}")
        lm = json.loads(r["linked_memory"] or "[]")
        if lm:
            print("\n  MEMORY PRODUCED")
            for m in lm:
                print(f"    - {m}")
        print(f"\n  PROVENANCE\n    {r['provenance']}")
        if r["notes"]:
            print(f"\n  NOTES\n    {r['notes']}")
        print()
        return 0

    return 1
