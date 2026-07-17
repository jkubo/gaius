"""gaius.outcomes — orchestrator task-outcome ingestion.

Owns the ``task_outcomes`` table lifecycle: idempotent upsert of production task
results, per-scope win-rate computation, and the ``ingest-outcomes`` command.
Closes the self-improvement loop (production outcomes feed fact rescoring).

Facade convention (see ARCHITECTURE.md): shared helpers imported from gaius._core
at top; _core re-imports this module's public symbols before the COMMANDS dict.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

# imports from gaius._core (shared hub) — circular-by-design, see ARCHITECTURE.md
from gaius._core import init_db


# Pulls completed-task outcomes from the orchestrator GET /outcomes into facts.db's
# task_outcomes table. ADDITIVE — never touches the facts table; idempotent by task key.
# Foundation for outcome-grounded corpus scoring (Phase 2) + the gaius router (Phase 3).
# Scope: ~/ansible/drafts/closed-loop-self-improvement-scope-20260623.md

def _ensure_outcomes_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_outcomes (
            key           TEXT PRIMARY KEY,
            scope         TEXT,
            model         TEXT,
            status        TEXT,
            result_status TEXT,
            success       INTEGER,
            summary       TEXT,
            cost_usd      REAL,
            verdicts      TEXT,
            at            TEXT,
            ingested_at   TEXT
        )
        """
    )
    conn.commit()


def ingest_outcomes(conn: sqlite3.Connection, records: list) -> tuple:
    """Upsert orchestrator task-outcome records into task_outcomes. Idempotent by key;
    never touches the facts table. Records lacking a key are skipped. Returns (inserted, updated)."""
    _ensure_outcomes_table(conn)
    now = datetime.now(timezone.utc).isoformat()
    ins = upd = 0
    for r in records:
        key = (r.get("key") or "").strip()
        if not key:
            continue
        existed = conn.execute("SELECT 1 FROM task_outcomes WHERE key = ?", (key,)).fetchone()
        conn.execute(
            """
            INSERT INTO task_outcomes
                (key, scope, model, status, result_status, success, summary, cost_usd, verdicts, at, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                scope=excluded.scope, model=excluded.model, status=excluded.status,
                result_status=excluded.result_status, success=excluded.success,
                summary=excluded.summary, cost_usd=excluded.cost_usd,
                verdicts=excluded.verdicts, at=excluded.at, ingested_at=excluded.ingested_at
            """,
            (
                key, r.get("scope"), r.get("model"), r.get("status"), r.get("result_status"),
                1 if r.get("success") else 0, (r.get("summary") or "")[:500],
                float(r.get("cost_usd") or 0), json.dumps(r.get("verdicts") or []),
                r.get("at"), now,
            ),
        )
        if existed:
            upd += 1
        else:
            ins += 1
    conn.commit()
    return ins, upd


def outcome_winrates(conn: sqlite3.Connection) -> list:
    """Per-scope success rate over task_outcomes — the routing/grounding signal."""
    _ensure_outcomes_table(conn)
    rows = conn.execute(
        "SELECT COALESCE(scope,''), COUNT(*), COALESCE(SUM(success),0) "
        "FROM task_outcomes GROUP BY scope ORDER BY scope"
    ).fetchall()
    return [
        {"scope": scope, "total": total, "success": success,
         "rate": (success / total) if total else 0.0}
        for scope, total, success in rows
    ]


def cmd_ingest_outcomes(args):
    """Pull completed-task outcomes from the orchestrator into facts.db task_outcomes.

    Additive (never touches facts); idempotent by key. Foundation for outcome-grounded
    corpus scoring. Usage: gaius ingest-outcomes [--orch URL] [--limit N] [--dry-run]
    """
    import urllib.request
    p = argparse.ArgumentParser(prog="gaius ingest-outcomes")
    p.add_argument("--orch", default=os.environ.get("AGENT_ORCH_URL", "http://localhost:8080"),
                   help="orchestrator base URL (or set AGENT_ORCH_URL)")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--dry-run", action="store_true", help="fetch + print; write nothing")
    ns = p.parse_args(args)

    url = ns.orch.rstrip("/") + f"/outcomes?limit={ns.limit}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"fetch {url}: {e}", file=sys.stderr)
        sys.exit(1)
    records = data.get("recent") or []
    print(f"fetched {len(records)} outcomes (orchestrator count={data.get('count')}) from {url}")
    if ns.dry_run:
        for r in records[:10]:
            print(f"  {r.get('key')}  {r.get('scope')}  success={r.get('success')}")
        print("(--dry-run: nothing written)")
        return
    conn = init_db()
    ins, upd = ingest_outcomes(conn, records)
    print(f"task_outcomes: +{ins} new, {upd} updated")
    for wr in outcome_winrates(conn):
        print(f"  {wr['scope'] or '(none)'}: {wr['success']}/{wr['total']} ({wr['rate']*100:.0f}%)")
