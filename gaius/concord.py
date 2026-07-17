"""gaius.concord — local cross-session coordination (2026-07-17).

Born from a real incident (2026-07-17): a network outage took the remote coordination
plane dark while 11 interactive sessions triaged the same storage cascade invisible to
each other. Lesson: coordination must live where the sessions live. This module is the
LOCAL, offline-first tier — a sidecar SQLite DB at ~/.gaius/concord.db that keeps
working when the cluster, the router, and the ISP are gone.

Guide: docs/concord.md (primitives, hook wiring, kill-switch, design bright line).
The findings schema mirrors the remote concord server's store field-for-field so the
optional sync bridge is a dumb POST loop.

Four primitives:
  claims    — advisory leases on resources (`subsystem:storage`, `node:web-01`,
              `incident:IC`). Atomic single-winner via a partial UNIQUE index; a lease
              self-expires on TTL or holder-pid death. ADVISORY: surfaced by hooks
              (inject brief, warn-then-block), never auto-enforced by this module.
  findings  — discoveries published for sibling sessions, with the adversarial review
              loop (open → reviewing → confirmed/refuted).
  task pool — a shared, claimable work queue so an incident commander can seed divided
              work and N terminals take tasks without stepping on each other.
  roster    — who is alive. NOT stored here: Claude Code already ships a session
              registry (~/.claude/sessions/{pid}.json, peerProtocol:1); we read it,
              add pid-liveness (the registry has no GC), and join it with claims.

BRIGHT LINE (the cross-session-awareness protocol): automate AWARENESS, gate ACTION.
A claim tells a sibling "this is held", it never acts on the sibling. Block messages
render structured fields only (resource/holder/age) — never peer free-text mid-loop.

SIDECAR DB by design: same doctrine as ledger.db — isolates coordination churn from
facts.db (WAL corrupted 4x Apr-May) and keeps the fact corpus a corpus. Facade
convention (ARCHITECTURE.md): _core re-imports cmd_concord before the COMMANDS dict.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

CONCORD_DB = Path.home() / ".gaius" / "concord.db"
SESSIONS_DIR = Path.home() / ".claude" / "sessions"

DEFAULT_TTL_SEC = 4 * 3600        # a lease is a shift, not a squat
SUMMARY_LIMIT = 280               # == concordSummaryLimit in concord.go
FINDING_STATUSES = {"open", "reviewing", "confirmed", "refuted"}
SEVERITIES = {"info", "minor", "major", "critical"}

_C = {"green": "\033[0;32m", "yellow": "\033[1;33m", "red": "\033[0;31m",
      "dim": "\033[2m", "reset": "\033[0m"}


def _utcnow():
    # Microsecond resolution: the prompt-delta cursor compares these strings with
    # strict `>` — at 1s resolution a finding published the same second as a prompt
    # would be permanently skipped (cursor advances past it). Lexical ordering is
    # preserved (same prefix), and _parse_ts accepts both resolutions for old rows.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(ts):
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _age_sec(ts):
    t = _parse_ts(ts)
    if not t:
        return 0
    return max(0, int((datetime.now(timezone.utc) - t).total_seconds()))


def _fmt_age(sec):
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec // 60}m"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def _set_terminal_title(title):
    """Retitle the invoking session's terminal tab (ghostty/kitty/xterm OSC 0).
    concord runs as a child of the session's Bash tool, so /dev/tty IS that
    session's tab — same idiom as gaius-inject-prompt's /rename. Fail-silent
    (no tty in hooks/cron/CI)."""
    try:
        with open("/dev/tty", "w") as t:
            t.write(f"\033]0;{title}\007")
            t.flush()
    except Exception:
        pass


def _retitle_from_claims(conn, sid, name):
    """Auto-rename the tab to reflect what this session holds: '⚑ res1+res2 · name'.
    No claims left → restore the plain session name."""
    held = [r[0] for r in conn.execute(
        "SELECT resource FROM claims WHERE session_id=? AND released_at IS NULL "
        "ORDER BY created_at ASC", (sid,)).fetchall()]
    label = name or sid[:8]
    if held:
        short = "+".join(h.split(":", 1)[-1] for h in held)
        _set_terminal_title(f"⚑ {short} · {label}")
    else:
        _set_terminal_title(label)


def _pid_alive(pid):
    """True if pid exists (or we can't tell). 0/None → unknown → not provably dead."""
    if not pid:
        return None  # unknown — caller falls back to TTL-only validity
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def init_concord(path=None):
    """Open (creating if absent) the sidecar concord DB and ensure the schema."""
    p = Path(path) if path else Path(os.environ.get("GAIUS_CONCORD_DB", "") or CONCORD_DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS claims (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            resource        TEXT NOT NULL,
            session_id      TEXT NOT NULL,
            pid             INTEGER DEFAULT 0,
            holder          TEXT DEFAULT '',
            note            TEXT DEFAULT '',
            created_at      TEXT NOT NULL,
            ttl_sec         INTEGER NOT NULL DEFAULT 14400,
            released_at     TEXT,
            released_reason TEXT DEFAULT ''
        )
    """)
    # Partial unique index = the atomic single-winner claim. INSERT of a second active
    # claim on the same resource raises IntegrityError — the loser is told, never queued.
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_active
        ON claims(resource) WHERE released_at IS NULL
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS findings (
            id         TEXT PRIMARY KEY,
            session_id TEXT DEFAULT '',
            repo       TEXT DEFAULT '',
            summary    TEXT NOT NULL,
            files      TEXT DEFAULT '[]',
            severity   TEXT DEFAULT 'info',
            status     TEXT DEFAULT 'open',
            reviewer   TEXT DEFAULT '',
            created_at TEXT,
            updated_at TEXT,
            synced_at  TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pool_tasks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL,
            detail     TEXT DEFAULT '',
            resource   TEXT DEFAULT '',
            status     TEXT NOT NULL DEFAULT 'open',
            created_by TEXT DEFAULT '',
            taken_by   TEXT DEFAULT '',
            taken_pid  INTEGER DEFAULT 0,
            created_at TEXT,
            taken_at   TEXT DEFAULT '',
            done_at    TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_cursors (
            session_id   TEXT PRIMARY KEY,
            last_checked TEXT DEFAULT '',
            last_sync    TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pool_status ON pool_tasks(status)")
    conn.commit()
    return conn


# ── session identity ────────────────────────────────────────────────────────────────

def _read_registry():
    """Parse Claude Code's own session registry. Returns [{pid, sessionId, name, status,
    cwd, updatedAt, alive}]. Registry files are harness-owned — read-only, never pruned."""
    out = []
    if not SESSIONS_DIR.is_dir():
        return out
    for f in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            j = json.loads(f.read_text())
        except Exception:
            continue
        j["alive"] = _pid_alive(j.get("pid"))
        out.append(j)
    return out


def _self_session():
    """Resolve the invoking session: (session_id, pid, name). Works from inside a
    Claude Code Bash tool (CLAUDE_CODE_SESSION_ID is exported) or a plain shell."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    pid, name = 0, ""
    if sid:
        for j in _read_registry():
            if j.get("sessionId") == sid:
                pid, name = j.get("pid", 0), j.get("name", "")
                break
    else:
        sid = f"shell-{os.getppid()}"
        pid = os.getppid()
        name = os.environ.get("USER", "shell")
    return sid, pid, name


# ── claim validity ──────────────────────────────────────────────────────────────────

def _claim_invalid_reason(row):
    """A claim row (resource, session_id, pid, created_at, ttl_sec) is invalid if the
    holder pid is provably dead or the TTL expired. Unknown pid → TTL-only."""
    _, _, pid, created_at, ttl_sec = row
    if _pid_alive(pid) is False:
        return "holder-dead"
    if ttl_sec and _age_sec(created_at) > int(ttl_sec):
        return "expired"
    return None


def _active_claims(conn, reap=True):
    """Active claims, after (optionally) auto-releasing dead/expired ones."""
    rows = conn.execute(
        "SELECT id, resource, session_id, pid, holder, note, created_at, ttl_sec "
        "FROM claims WHERE released_at IS NULL ORDER BY created_at ASC").fetchall()
    live = []
    for r in rows:
        cid, resource, session_id, pid, holder, note, created_at, ttl_sec = r
        reason = _claim_invalid_reason((resource, session_id, pid, created_at, ttl_sec))
        if reason and reap:
            conn.execute(
                "UPDATE claims SET released_at=?, released_reason=? WHERE id=? AND released_at IS NULL",
                (_utcnow(), reason, cid))
        elif not reason:
            live.append({"id": cid, "resource": resource, "session_id": session_id,
                         "pid": pid, "holder": holder, "note": note,
                         "created_at": created_at, "ttl_sec": ttl_sec,
                         "age": _fmt_age(_age_sec(created_at))})
    if reap:
        conn.commit()
    return live


# Advisory overlap detection — closes the exact-match-only collision gap: two sessions
# naming the same work differently (subsystem:db-replication vs subsystem:mysql-replication-
# fix) never collide on the UNIQUE index. Surfaced as a WARNING; the claim still
# succeeds. Bright line (module docstring): automate AWARENESS, gate ACTION — never enforce.
_OVERLAP_STOPWORDS = {
    "fix", "recovery", "recover", "sync", "patch", "update", "config", "decision",
    "validation", "test", "setup", "migration", "health", "drift", "interface",
    "node", "svc", "subsystem", "incident", "cluster", "prod", "staging",
}


def _significant_tokens(resource):
    """Meaningful tokens of a resource key: drop the type prefix, split the body, keep
    tokens >=4 chars that aren't generic. `node:nyc-web-gpu-02` -> set() (site/hw are short);
    `subsystem:mysql-replication-lag` -> {mysql, replication}."""
    body = str(resource).split(":", 1)[-1].lower()
    return {t for t in re.split(r"[-_:./\s]+", body)
            if len(t) >= 4 and t not in _OVERLAP_STOPWORDS}


def _overlapping_claims(active, resource, self_sid):
    """Active claims on a DIFFERENT resource key that share a significant token with
    `resource` and aren't held by this session. Advisory only; never blocks."""
    mine = _significant_tokens(resource)
    if not mine:
        return []
    out = []
    for c in active:
        if c["resource"] == resource or c["session_id"] == self_sid:
            continue
        shared = mine & _significant_tokens(c["resource"])
        if shared:
            out.append({**c, "shared": sorted(shared)})
    return out


def _try_claim(conn, resource, sid, pid, name, note, ttl_sec):
    """Atomic claim. Returns (won: bool, holder_row_or_None)."""
    for attempt in (1, 2):
        try:
            conn.execute(
                "INSERT INTO claims (resource, session_id, pid, holder, note, created_at, ttl_sec) "
                "VALUES (?,?,?,?,?,?,?)",
                (resource, sid, pid, name, note, _utcnow(), ttl_sec))
            conn.commit()
            return True, None
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id, resource, session_id, pid, holder, note, created_at, ttl_sec "
                "FROM claims WHERE resource=? AND released_at IS NULL", (resource,)).fetchone()
            if row is None:
                continue  # racer released between INSERT and SELECT — retry
            cid = row[0]
            if row[2] == sid:
                # Re-claim by the same session renews the lease clock.
                conn.execute("UPDATE claims SET created_at=?, ttl_sec=?, note=? WHERE id=?",
                             (_utcnow(), ttl_sec, note or row[5], cid))
                conn.commit()
                return True, None
            reason = _claim_invalid_reason((row[1], row[2], row[3], row[6], row[7]))
            if reason and attempt == 1:
                conn.execute(
                    "UPDATE claims SET released_at=?, released_reason=? WHERE id=? AND released_at IS NULL",
                    (_utcnow(), reason, cid))
                conn.commit()
                continue
            return False, {"id": cid, "resource": row[1], "session_id": row[2], "pid": row[3],
                           "holder": row[4], "note": row[5], "created_at": row[6],
                           "ttl_sec": row[7], "age": _fmt_age(_age_sec(row[6]))}
    return False, None


# ── subcommand implementations ──────────────────────────────────────────────────────

def _print_overlap_warning(overlaps):
    """Advisory: related active claims under a DIFFERENT name (the exact-match UNIQUE
    index can't see these). Warn, never block — the claim already succeeded."""
    if not overlaps:
        return
    print(f"{_C['yellow']}⚠ related active claim(s) — different name, possibly the same "
          f"work; coordinate before you dig in:{_C['reset']}")
    for o in overlaps:
        who = o.get("holder") or (o.get("session_id") or "")[:8]
        print(f"    {_C['yellow']}{o['resource']:<28}{_C['reset']} {who:<24} "
              f"(shared: {', '.join(o['shared'])})")


def _concord_claim(ns, steal=False):
    conn = init_concord(ns.db or None)
    sid, pid, name = _self_session()
    if steal:
        cur = conn.execute(
            "UPDATE claims SET released_at=?, released_reason=? WHERE resource=? AND released_at IS NULL",
            (_utcnow(), f"stolen by {name or sid[:8]}", ns.resource))
        conn.commit()
        if cur.rowcount:
            print(f"{_C['yellow']}⚠ stole {ns.resource} from previous holder{_C['reset']}")
    won, holder = _try_claim(conn, ns.resource, sid, pid, name, ns.note, ns.ttl)
    try:
        overlap = _overlapping_claims(_active_claims(conn), ns.resource, sid)
    except Exception:
        overlap = []  # advisory only — a bug here must never break a claim
    if won and not ns.no_title:
        _retitle_from_claims(conn, sid, name)
    conn.close()
    if ns.json:
        print(json.dumps({"won": won, "resource": ns.resource, "holder": holder,
                          "overlaps": overlap}))
        sys.exit(0 if won else 1)
    if won:
        print(f"{_C['green']}✓ claimed {ns.resource}{_C['reset']} "
              f"(session {name or sid[:8]}, ttl {_fmt_age(ns.ttl)})")
        _print_overlap_warning(overlap)
        sys.exit(0)
    print(f"{_C['red']}✗ {ns.resource} is HELD{_C['reset']} by "
          f"{holder['holder'] or holder['session_id'][:8]} (pid {holder['pid']}, "
          f"{holder['age']} ago, ttl {_fmt_age(int(holder['ttl_sec']))})")
    if holder.get("note"):
        print(f"  note: {holder['note'][:120]}")
    print(f"  coordinate with that session, or take over: gaius concord steal {ns.resource}")
    _print_overlap_warning(overlap)
    sys.exit(1)


def _concord_release(ns):
    conn = init_concord(ns.db or None)
    sid, _, name = _self_session()
    now = _utcnow()
    if ns.all:
        cur = conn.execute(
            "UPDATE claims SET released_at=?, released_reason='released' "
            "WHERE session_id=? AND released_at IS NULL", (now, sid))
    else:
        if not ns.resource:
            print("usage: gaius concord release <resource> | --all", file=sys.stderr)
            sys.exit(2)
        cur = conn.execute(
            "UPDATE claims SET released_at=?, released_reason='released' "
            "WHERE resource=? AND released_at IS NULL", (now, ns.resource))
    conn.commit()
    if not ns.no_title:
        _retitle_from_claims(conn, sid, name)
    conn.close()
    n = cur.rowcount
    if ns.json:
        print(json.dumps({"released": n}))
        return
    print(f"released {n} claim(s)" if n else "nothing to release")


def _concord_claims(ns):
    conn = init_concord(ns.db or None)
    live = _active_claims(conn)
    conn.close()
    if ns.json:
        print(json.dumps(live, indent=2))
        return
    if not live:
        print("no active claims")
        return
    print(f"\n  {len(live)} active claim(s):")
    for c in live:
        print(f"  {_C['yellow']}{c['resource']:<28}{_C['reset']} "
              f"{c['holder'] or c['session_id'][:8]:<36} {c['age']:>6} ago"
              + (f"  — {c['note'][:60]}" if c['note'] else ""))
    print()


def _concord_roster(ns):
    conn = init_concord(ns.db or None)
    live_claims = _active_claims(conn)
    conn.close()
    by_session = {}
    for c in live_claims:
        by_session.setdefault(c["session_id"], []).append(c["resource"])
    sessions = [j for j in _read_registry() if j.get("alive")]
    sid_self = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if ns.json:
        out = [{"pid": j.get("pid"), "session_id": j.get("sessionId"), "name": j.get("name"),
                "status": j.get("status"), "cwd": j.get("cwd"),
                "claims": by_session.get(j.get("sessionId"), []),
                "self": j.get("sessionId") == sid_self} for j in sessions]
        print(json.dumps(out, indent=2))
        return
    if not sessions:
        print("no live sessions in the registry")
        return
    print(f"\n  {len(sessions)} live session(s):")
    for j in sessions:
        mark = "▶" if j.get("sessionId") == sid_self else " "
        claims = by_session.get(j.get("sessionId"), [])
        cstr = f"  holds: {', '.join(claims)}" if claims else ""
        print(f"  {mark} {j.get('name', '?')[:44]:<44} {j.get('status', '?'):<8} "
              f"pid {j.get('pid', 0):<8}{_C['yellow']}{cstr}{_C['reset']}")
    orphaned = [c for c in live_claims
                if c["session_id"] not in {j.get("sessionId") for j in sessions}]
    for c in orphaned:
        print(f"    {_C['dim']}(claim {c['resource']} held by non-registry session "
              f"{c['holder'] or c['session_id'][:8]} — TTL-governed){_C['reset']}")
    print()


def _concord_finding_add(ns):
    conn = init_concord(ns.db or None)
    sid, _, name = _self_session()
    fid = str(uuid.uuid4())
    now = _utcnow()
    files = json.dumps([f for f in (ns.files or "").split(",") if f])
    sev = ns.severity if ns.severity in SEVERITIES else "info"
    conn.execute(
        "INSERT INTO findings (id, session_id, repo, summary, files, severity, status, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,'open',?,?)",
        (fid, sid, ns.repo, ns.summary[:SUMMARY_LIMIT], files, sev, now, now))
    conn.commit()
    conn.close()
    if ns.json:
        print(json.dumps({"id": fid}))
        return
    print(f"{_C['green']}✓ finding published{_C['reset']} [{sev}] {fid[:8]} — "
          f"siblings see it on their next status/roster check")


def _concord_finding_list(ns):
    conn = init_concord(ns.db or None)
    q = ("SELECT id, session_id, summary, files, severity, status, reviewer, created_at "
         "FROM findings")
    params = ()
    if ns.status:
        q += " WHERE status=?"
        params = (ns.status,)
    q += " ORDER BY created_at DESC LIMIT ?"
    rows = conn.execute(q, params + (ns.limit,)).fetchall()
    conn.close()
    if ns.json:
        print(json.dumps([{"id": r[0], "session_id": r[1], "summary": r[2],
                           "files": json.loads(r[3] or "[]"), "severity": r[4],
                           "status": r[5], "reviewer": r[6], "created_at": r[7]}
                          for r in rows], indent=2))
        return
    if not rows:
        print("no findings")
        return
    color = {"open": "yellow", "reviewing": "yellow", "confirmed": "green", "refuted": "dim"}
    print()
    for r in rows:
        c = _C[color.get(r[5], "reset")]
        print(f"  {c}{r[5]:<10}{_C['reset']} [{r[4]:<8}] {r[0][:8]} "
              f"{_fmt_age(_age_sec(r[7])):>6} ago — {r[2][:100]}"
              + (f" (reviewer: {r[6]})" if r[6] else ""))
    print()


def _concord_finding_review(ns):
    if ns.status not in FINDING_STATUSES:
        print(f"status must be one of {sorted(FINDING_STATUSES)}", file=sys.stderr)
        sys.exit(2)
    conn = init_concord(ns.db or None)
    _, _, name = _self_session()
    reviewer = ns.reviewer or name or "unknown"
    cur = conn.execute(
        "UPDATE findings SET status=?, reviewer=?, updated_at=? WHERE id LIKE ?",
        (ns.status, reviewer, _utcnow(), ns.id + "%"))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        print(f"no finding matching {ns.id!r}", file=sys.stderr)
        sys.exit(1)
    if cur.rowcount > 1:
        print(f"{_C['red']}⚠ id prefix {ns.id!r} matched {cur.rowcount} findings — "
              f"all updated; use a longer prefix next time{_C['reset']}", file=sys.stderr)
    print(f"finding {ns.id} → {ns.status} (reviewer: {reviewer})")


def _reap_pool(conn):
    """Tasks taken by a provably-dead pid revert to open (died mid-task)."""
    rows = conn.execute(
        "SELECT id, taken_pid FROM pool_tasks WHERE status='taken'").fetchall()
    reaped = 0
    for tid, pid in rows:
        if _pid_alive(pid) is False:
            conn.execute(
                "UPDATE pool_tasks SET status='open', taken_by='', taken_pid=0, taken_at='', "
                "detail = detail || ' [reclaimed from dead session]' WHERE id=? AND status='taken'",
                (tid,))
            reaped += 1
    if reaped:
        conn.commit()
    return reaped


def _concord_task(ns):
    conn = init_concord(ns.db or None)
    sid, pid, name = _self_session()

    if ns.tsub == "add":
        conn.execute(
            "INSERT INTO pool_tasks (title, detail, resource, created_by, created_at) "
            "VALUES (?,?,?,?,?)",
            (ns.title, ns.detail, ns.resource, name or sid[:8], _utcnow()))
        conn.commit()
        tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        print(json.dumps({"id": tid}) if ns.json else
              f"{_C['green']}✓ pool task #{tid}{_C['reset']} — {ns.title}")
        return

    if ns.tsub == "list":
        reaped = _reap_pool(conn)
        q = "SELECT id, title, resource, status, taken_by, created_by, created_at FROM pool_tasks"
        params = ()
        if not ns.all:
            q += " WHERE status IN ('open','taken')"
        q += " ORDER BY id ASC"
        rows = conn.execute(q, params).fetchall()
        conn.close()
        if ns.json:
            print(json.dumps([{"id": r[0], "title": r[1], "resource": r[2], "status": r[3],
                               "taken_by": r[4], "created_by": r[5], "created_at": r[6]}
                              for r in rows], indent=2))
            return
        if reaped:
            print(f"{_C['yellow']}({reaped} task(s) reclaimed from dead sessions){_C['reset']}")
        if not rows:
            print("pool is empty")
            return
        print()
        for r in rows:
            c = {"open": "yellow", "taken": "green", "done": "dim", "dropped": "dim"}.get(r[3], "reset")
            who = f" ← {r[4]}" if r[4] else ""
            res = f" [{r[2]}]" if r[2] else ""
            print(f"  {_C[c]}#{r[0]:<4} {r[3]:<7}{_C['reset']} {r[1][:80]}{res}{who}")
        print()
        return

    if ns.tsub == "next":
        _reap_pool(conn)
        row = conn.execute(
            "SELECT id, title, detail, resource FROM pool_tasks WHERE status='open' "
            "ORDER BY id ASC LIMIT 1").fetchone()
        conn.close()
        if ns.json:
            print(json.dumps(None if not row else
                             {"id": row[0], "title": row[1], "detail": row[2], "resource": row[3]}))
            return
        if not row:
            print("pool has no open tasks")
            return
        print(f"next: #{row[0]} — {row[1]}" + (f"\n  {row[2]}" if row[2] else "")
              + (f"\n  suggested claim: {row[3]}" if row[3] else "")
              + f"\n  take it: gaius concord task take {row[0]}")
        return

    if ns.tsub == "take":
        _reap_pool(conn)
        if ns.id:
            cur = conn.execute(
                "UPDATE pool_tasks SET status='taken', taken_by=?, taken_pid=?, taken_at=? "
                "WHERE id=? AND status='open'", (name or sid[:8], pid, _utcnow(), ns.id))
            tid = ns.id
        else:
            row = conn.execute(
                "SELECT id FROM pool_tasks WHERE status='open' ORDER BY id ASC LIMIT 1").fetchone()
            if not row:
                conn.close()
                print("pool has no open tasks")
                sys.exit(1)
            tid = row[0]
            cur = conn.execute(
                "UPDATE pool_tasks SET status='taken', taken_by=?, taken_pid=?, taken_at=? "
                "WHERE id=? AND status='open'", (name or sid[:8], pid, _utcnow(), tid))
        conn.commit()
        won = cur.rowcount == 1
        row = conn.execute(
            "SELECT title, detail, resource, taken_by FROM pool_tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        if ns.json:
            print(json.dumps({"won": won, "id": tid,
                              "title": row[0] if row else None,
                              "resource": row[2] if row else None,
                              "taken_by": row[3] if row else None}))
            sys.exit(0 if won else 1)
        if won:
            print(f"{_C['green']}✓ took #{tid}{_C['reset']} — {row[0]}"
                  + (f"\n  {row[1]}" if row[1] else ""))
            if row[2]:
                print(f"  now claim its resource: gaius concord claim {row[2]}")
            sys.exit(0)
        print(f"{_C['red']}✗ #{tid} already {row[3] and 'taken by ' + row[3] or 'gone'}{_C['reset']}"
              f" — try: gaius concord task next")
        sys.exit(1)

    if ns.tsub in ("done", "drop"):
        status = "done" if ns.tsub == "done" else "dropped"
        col = "done_at" if ns.tsub == "done" else "done_at"
        cur = conn.execute(
            f"UPDATE pool_tasks SET status=?, {col}=? WHERE id=? AND status IN ('open','taken')",
            (status, _utcnow(), ns.id))
        conn.commit()
        conn.close()
        print(f"#{ns.id} → {status}" if cur.rowcount else f"no open/taken task #{ns.id}")
        return


def _concord_status(ns):
    conn = init_concord(ns.db or None)
    live = _active_claims(conn)
    reaped = _reap_pool(conn)
    open_findings = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE status IN ('open','reviewing')").fetchone()[0]
    pool_open = conn.execute(
        "SELECT COUNT(*) FROM pool_tasks WHERE status='open'").fetchone()[0]
    pool_taken = conn.execute(
        "SELECT COUNT(*) FROM pool_tasks WHERE status='taken'").fetchone()[0]
    conn.close()
    sessions = [j for j in _read_registry() if j.get("alive")]
    if ns.json:
        print(json.dumps({"sessions": len(sessions), "claims": live,
                          "findings_open": open_findings,
                          "pool": {"open": pool_open, "taken": pool_taken,
                                   "reaped": reaped}}, indent=2))
        return
    print(f"\n  concord: {len(sessions)} live session(s) · {len(live)} claim(s) · "
          f"{open_findings} open finding(s) · pool {pool_open} open / {pool_taken} taken\n")
    if live:
        for c in live:
            print(f"    {_C['yellow']}{c['resource']:<28}{_C['reset']} "
                  f"{c['holder'] or c['session_id'][:8]} ({c['age']} ago)")
        print()
    if pool_open:
        print(f"    {pool_open} unclaimed pool task(s) — gaius concord task next\n")


def _concord_brief(ns):
    """Hook-facing render. --scope session-start = full orientation block;
    --scope prompt = DELTA since this session's cursor (new sibling findings,
    steals of my claims). Emits NOTHING when there is nothing to say — the
    calling hook skips empty output. Everything here is advisory awareness
    (provenance-tagged observations), never authorization — the bright line
    (docs/concord.md § Design)."""
    conn = init_concord(ns.db or None)
    sid = ns.session or _self_session()[0]
    now = _utcnow()
    out = []

    if ns.scope == "session-start":
        live = _active_claims(conn)
        _reap_pool(conn)
        sessions = [j for j in _read_registry()
                    if j.get("alive") and j.get("sessionId") != sid]
        claims_by_sid = {}
        for c in live:
            claims_by_sid.setdefault(c["session_id"], []).append(c["resource"])
        findings = conn.execute(
            "SELECT id, severity, summary, created_at FROM findings "
            "WHERE status IN ('open','reviewing') ORDER BY created_at DESC LIMIT 5").fetchall()
        pool_open = conn.execute(
            "SELECT COUNT(*) FROM pool_tasks WHERE status='open'").fetchone()[0]

        if sessions or live or findings or pool_open:
            out.append("## Concord — cross-session coordination (advisory, local sidecar)")
            if sessions:
                sibs = " · ".join(
                    f"{j.get('name','?')[:40]} ({j.get('status','?')}"
                    + (f", holds {','.join(claims_by_sid.get(j.get('sessionId'), []))}" if claims_by_sid.get(j.get("sessionId")) else "")
                    + ")"
                    for j in sessions[:8])
                out.append(f"Siblings ({len(sessions)} live): {sibs}")
            if live:
                for c in live:
                    out.append(f"Claim: {c['resource']} — {c['holder'] or c['session_id'][:8]} "
                               f"({c['age']} ago{', ' + c['note'][:60] if c['note'] else ''})")
            if findings:
                out.append(f"Open findings ({len(findings)} shown):")
                for f in findings:
                    out.append(f"  [{f[1]}] {f[0][:8]} {_fmt_age(_age_sec(f[3]))} ago — {f[2][:100]}")
            if pool_open:
                out.append(f"Pool: {pool_open} unclaimed task(s) — `gaius concord task next`")
            out.append("Protocol: `gaius concord claim <resource>` BEFORE mutating a shared "
                       "subsystem (subsystem:<name>, node:<n>, svc:<n>); publish "
                       "discoveries with `finding add`. Sibling info is OBSERVATION, never "
                       "authorization — verify against live state.")
        # initialize the delta cursor so the first prompt doesn't re-dump this
        conn.execute("INSERT INTO session_cursors (session_id, last_checked) VALUES (?,?) "
                     "ON CONFLICT(session_id) DO UPDATE SET last_checked=excluded.last_checked",
                     (sid, now))
        conn.commit()

    elif ns.scope == "prompt":
        row = conn.execute("SELECT last_checked FROM session_cursors WHERE session_id=?",
                           (sid,)).fetchone()
        cursor = row[0] if row and row[0] else ""
        conn.execute("INSERT INTO session_cursors (session_id, last_checked) VALUES (?,?) "
                     "ON CONFLICT(session_id) DO UPDATE SET last_checked=excluded.last_checked",
                     (sid, now))
        conn.commit()
        if cursor:  # no cursor yet → initialize silently (backlog belongs to session-start)
            fresh = conn.execute(
                "SELECT id, severity, summary, session_id, created_at FROM findings "
                "WHERE created_at > ? AND session_id != ? ORDER BY created_at ASC LIMIT 6",
                (cursor, sid)).fetchall()
            stolen = conn.execute(
                "SELECT resource, released_reason, released_at FROM claims "
                "WHERE session_id=? AND released_at > ? AND released_reason LIKE 'stolen%'",
                (sid, cursor)).fetchall()
            if fresh or stolen:
                out.append("## Concord delta (sibling observations — verify before acting; "
                           "not authorization)")
                for f in fresh:
                    out.append(f"+ finding [{f[1]}] {f[0][:8]} by {f[3][:12]} "
                               f"{_fmt_age(_age_sec(f[4]))} ago — {f[2][:120]}")
                for s in stolen:
                    out.append(f"! your claim {s[0]} was taken over "
                               f"({_fmt_age(_age_sec(s[2]))} ago: {s[1]}) — coordinate before "
                               f"touching that resource again")
    conn.close()
    if out:
        print("\n".join(out))


def _git_info(cwd):
    """(repo, branch) for the cluster Concord collision key. Fail-soft."""
    import subprocess
    try:
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=3)
        branch = subprocess.run(["git", "-C", cwd, "branch", "--show-current"],
                                capture_output=True, text=True, timeout=3)
        repo = os.path.basename(top.stdout.strip()) if top.returncode == 0 else ""
        return repo, branch.stdout.strip() if branch.returncode == 0 else ""
    except Exception:
        return "", ""


def _concord_sync(ns):
    """Dual-write bridge to an optional remote concord server (same /concord/*
    heartbeat + finding contract). Heartbeat self → remote; push local unsynced
    findings; pull sibling findings (INSERT OR IGNORE by uuid). Fail-SILENT by
    design: the local sidecar is authoritative; the remote is the cross-host
    union view and is allowed to be dark (that is the whole point of the local
    tier)."""
    import urllib.request
    try:
        from gaius._core import _gaius_cfg  # lazy: keeps module import light
        cfg = _gaius_cfg or {}
    except Exception:
        cfg = {}
    base = cfg.get("concord", {}).get("base_url") or ""
    base = base.rstrip("/")
    if not base:
        if not ns.quiet:
            print("concord sync: no base_url (config concord.base_url) — skipped")
        return
    api_key = cfg.get("concord", {}).get("api_key") or ""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    conn = init_concord(ns.db or None)
    sid, pid, name = _self_session()
    cwd = os.getcwd()
    repo, branch = _git_info(cwd)
    reg_status = "active"
    for j in _read_registry():
        if j.get("sessionId") == sid:
            reg_status = {"busy": "active", "idle": "idle"}.get(j.get("status", ""), "active")
            break
    beat = {"id": sid, "host": os.uname().nodename, "user": os.environ.get("USER", ""),
            "cwd": cwd, "repo": repo, "branch": branch, "task": name, "status": reg_status}
    pulled = pushed = 0
    try:
        req = urllib.request.Request(f"{base}/concord/heartbeat",
                                     data=json.dumps(beat).encode(), headers=headers,
                                     method="POST")
        resp = json.loads(urllib.request.urlopen(req, timeout=int(ns.timeout)).read())
        now = _utcnow()
        for f in resp.get("new_findings", []) or []:
            cur = conn.execute(
                "INSERT OR IGNORE INTO findings (id, session_id, repo, summary, files, "
                "severity, status, reviewer, created_at, updated_at, synced_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f.get("id"), f.get("session_id", ""), f.get("repo", ""),
                 f.get("summary", "")[:SUMMARY_LIMIT], json.dumps(f.get("files") or []),
                 f.get("severity", "info"), f.get("status", "open"), f.get("reviewer", ""),
                 f.get("created_at", now), f.get("updated_at", now), now))
            pulled += cur.rowcount
        # push local findings the cluster hasn't seen
        for row in conn.execute(
                "SELECT id, session_id, repo, summary, files, severity FROM findings "
                "WHERE synced_at='' ").fetchall():
            body = {"session_id": row[1], "repo": row[2], "summary": row[3],
                    "files": json.loads(row[4] or "[]"), "severity": row[5]}
            try:
                r2 = urllib.request.Request(f"{base}/concord/finding",
                                            data=json.dumps(body).encode(), headers=headers,
                                            method="POST")
                urllib.request.urlopen(r2, timeout=int(ns.timeout))
                conn.execute("UPDATE findings SET synced_at=? WHERE id=?", (now, row[0]))
                pushed += 1
            except Exception:
                break  # cluster write path down — retry next sync
        conn.commit()
        if not ns.quiet:
            sibs = len(resp.get("siblings") or [])
            cols = len(resp.get("collisions") or [])
            print(f"concord sync: beat ok — {sibs} cluster sibling(s), {cols} collision(s), "
                  f"pulled {pulled}, pushed {pushed}")
    except Exception as e:
        if not ns.quiet:
            print(f"concord sync: cluster unreachable ({str(e)[:80]}) — local-only mode (fine)")
    finally:
        conn.close()


def cmd_concord(args):
    """Local cross-session coordination (Concord P0). Sidecar DB at ~/.gaius/concord.db.

    Usage:
      gaius concord status                                   one-screen sitrep
      gaius concord roster   [--json]                        live sessions + claims held
      gaius concord claim    <resource> [--note N] [--ttl S] atomic advisory lease (exit 1 = held)
      gaius concord steal    <resource> [--note N] [--ttl S] take over a held lease
      gaius concord release  <resource> | --all              release lease(s)
      gaius concord claims   [--json]                        list active leases
      gaius concord finding  add --summary S [--files a,b] [--severity info|minor|major|critical]
      gaius concord finding  list [--status S] [--limit N]
      gaius concord finding  review <id-prefix> --status confirmed|refuted|reviewing
      gaius concord task     add TITLE [--detail D] [--resource R]   seed the shared pool
      gaius concord task     list [--all] | next | take [ID] | done ID | drop ID

    Resource key conventions: subsystem:<name> (e.g. subsystem:storage), node:<name>, svc:<name>,
    incident:IC (incident commander). Claims are ADVISORY — surfaced, never self-enforcing.
    """
    p = argparse.ArgumentParser(prog="gaius concord")
    p.add_argument("--db", default="", help="override DB path (testing)")
    sub = p.add_subparsers(dest="sub", required=True)

    pcl = sub.add_parser("claim", help="atomically claim an advisory lease")
    pcl.add_argument("resource")
    pcl.add_argument("--note", default="")
    pcl.add_argument("--ttl", type=int, default=DEFAULT_TTL_SEC)
    pcl.add_argument("--no-title", action="store_true", help="skip terminal tab retitle")
    pcl.add_argument("--json", action="store_true")

    pst = sub.add_parser("steal", help="take over a held lease")
    pst.add_argument("resource")
    pst.add_argument("--note", default="")
    pst.add_argument("--ttl", type=int, default=DEFAULT_TTL_SEC)
    pst.add_argument("--no-title", action="store_true", help="skip terminal tab retitle")
    pst.add_argument("--json", action="store_true")

    prl = sub.add_parser("release", help="release lease(s)")
    prl.add_argument("resource", nargs="?", default="")
    prl.add_argument("--all", action="store_true", help="release all held by this session")
    prl.add_argument("--no-title", action="store_true", help="skip terminal tab retitle")
    prl.add_argument("--json", action="store_true")

    pcs = sub.add_parser("claims", help="list active leases")
    pcs.add_argument("--json", action="store_true")

    pro = sub.add_parser("roster", help="live sessions + claims held")
    pro.add_argument("--json", action="store_true")

    pf = sub.add_parser("finding", help="publish/review findings")
    fsub = pf.add_subparsers(dest="fsub", required=True)
    pfa = fsub.add_parser("add")
    pfa.add_argument("--summary", required=True)
    pfa.add_argument("--files", default="")
    pfa.add_argument("--severity", default="info")
    pfa.add_argument("--repo", default="")
    pfa.add_argument("--json", action="store_true")
    pfl = fsub.add_parser("list")
    pfl.add_argument("--status", default="")
    pfl.add_argument("--limit", type=int, default=50)
    pfl.add_argument("--json", action="store_true")
    pfr = fsub.add_parser("review")
    pfr.add_argument("id")
    pfr.add_argument("--status", required=True)
    pfr.add_argument("--reviewer", default="")

    pt = sub.add_parser("task", help="shared claimable task pool")
    tsub = pt.add_subparsers(dest="tsub", required=True)
    pta = tsub.add_parser("add")
    pta.add_argument("title")
    pta.add_argument("--detail", default="")
    pta.add_argument("--resource", default="")
    pta.add_argument("--json", action="store_true")
    ptl = tsub.add_parser("list")
    ptl.add_argument("--all", action="store_true")
    ptl.add_argument("--json", action="store_true")
    ptn = tsub.add_parser("next")
    ptn.add_argument("--json", action="store_true")
    ptt = tsub.add_parser("take")
    ptt.add_argument("id", type=int, nargs="?", default=0)
    ptt.add_argument("--json", action="store_true")
    ptd = tsub.add_parser("done")
    ptd.add_argument("id", type=int)
    ptx = tsub.add_parser("drop")
    ptx.add_argument("id", type=int)

    psu = sub.add_parser("status", help="one-screen sitrep")
    psu.add_argument("--json", action="store_true")

    pbr = sub.add_parser("brief", help="hook-facing render (P1): session-start block or prompt delta")
    pbr.add_argument("--scope", choices=["session-start", "prompt"], required=True)
    pbr.add_argument("--session", default="", help="session id override (hooks pass theirs)")

    psy = sub.add_parser("sync", help="P3: dual-write heartbeat + findings to cluster Concord")
    psy.add_argument("--quiet", action="store_true")
    psy.add_argument("--timeout", type=int, default=4)

    ns = p.parse_args(args)

    if ns.sub == "claim":
        return _concord_claim(ns)
    if ns.sub == "steal":
        return _concord_claim(ns, steal=True)
    if ns.sub == "release":
        return _concord_release(ns)
    if ns.sub == "claims":
        return _concord_claims(ns)
    if ns.sub == "roster":
        return _concord_roster(ns)
    if ns.sub == "finding":
        if ns.fsub == "add":
            return _concord_finding_add(ns)
        if ns.fsub == "list":
            return _concord_finding_list(ns)
        if ns.fsub == "review":
            return _concord_finding_review(ns)
    if ns.sub == "task":
        return _concord_task(ns)
    if ns.sub == "status":
        return _concord_status(ns)
    if ns.sub == "brief":
        return _concord_brief(ns)
    if ns.sub == "sync":
        return _concord_sync(ns)
