"""gaius.review — the human review queue: staged summaries, verdict verbs, stats.

Owns load_staged/save_staged (path-traversal-hardened), display_uid, the review
walk (show/next/done/batch/rescan), the verdict verbs (confirm/reject/defer/
agent-review — `gaius confirm` is HUMAN-ONLY, the corpus_audit trust anchor),
and cmd_stats. Owns `gaius quiz` (Leitner HITL loop — the legitimate producer
of `confidence_source='human'`).

Facade convention (see ARCHITECTURE.md): runtime-rebindable / test-patched hub
paths (STAGING_DIR, PROJECT_DIR, DB_PATH, CORPUS_DIR, MEMORY_DIR) are read at
call time as `_core.NAME` — main() rebinds them from CLI flags and tests
monkeypatch them on gaius._core.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

import gaius._core as _core
# imports from gaius._core (shared hub) — circular-by-design, see ARCHITECTURE.md
from gaius._core import (
    GREEN, YELLOW, RED, BOLD, RESET, OPERATOR_NAME,
    HAS_SQLITE_VEC, content_hash,
)
from gaius.embed import _EMBED_DIM
from gaius.extract import (
    SECTION_HEADERS, SIGNAL_SECTIONS, DOMAIN_KEYWORDS, has_signal,
    count_domain_hits, tag_domains_from_specs, extract_section,
)
from gaius.facts import init_db, upsert_fact
from gaius.leitner import (
    box_histogram,
    draw,
    grade,
    item_weight,
)
from gaius.scoring import load_domain_stats, BOOTSTRAP_THRESHOLD

# Agent harnesses set these. `gaius quiz` writes confidence_source='human' on
# confirm, so an agent running it forges the corpus_audit trust anchor.
_AGENT_ENV = (
    "GROK_AGENT",
    "GROK_SESSION_ID",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
)

# One sitting. Default is 10; the flag had no ceiling, so `--budget` equal to
# the eligible-corpus size would walk every fact in a single invocation.
QUIZ_BUDGET_MAX = 50


def running_as_agent() -> bool:
    return any(os.environ.get(k) for k in _AGENT_ENV)


def require_human(action: str) -> None:
    """Refuse mutating confirm/quiz from an agent shell or a non-tty.

    Honest bound (same class as tessera's tty gate): a determined agent can
    still forge this on a single-uid box. The product is that it is not the
    default path, and that `gaius agent-review` exists as the machine verb.
    """
    if running_as_agent():
        print(
            f"gaius {action} is human-only — an agent running it would forge "
            "confidence_source='human' (corpus_audit trust anchor). "
            "Use `gaius quiz --report` (read-only) or `gaius agent-review`.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not sys.stdin.isatty():
        print(f"gaius {action} requires a tty. Use --report for a read-only summary.",
              file=sys.stderr)
        sys.exit(2)

def load_staged() -> dict:
    """Return all staged summaries keyed by uuid."""
    _core.STAGING_DIR.mkdir(parents=True, exist_ok=True)
    result = {}
    for f in sorted(_core.STAGING_DIR.glob("*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
                result[d["uuid"]] = d
        except Exception:
            pass
    return result


# Operational state transitions only exist in session history — dismissing
# them at review as "derivable from code" is the failure mode that rotted one
# project's files twice in three days. Keyword list is the agreed spec
# from project_gaius_promotion_gap.md; false positives just surface earlier.
_STATE_CHANGE_RE = re.compile(
    r'\b(deleted|decommissioned|migrated|completed|shipped|torn down|removed|'
    r'deprecated|cutover|flipped|promoted|scaled down|terminated)\b',
    re.IGNORECASE)


def save_staged(entry: dict):
    _core.STAGING_DIR.mkdir(parents=True, exist_ok=True)
    if not entry.get("reviewed") and "state_change" not in entry:
        section_text = " ".join(
            str(v) for v in (entry.get("sections") or {}).values() if v)
        entry["state_change"] = bool(_STATE_CHANGE_RE.search(section_text))
    ts = entry.get("timestamp", "unknown")[:19].replace(":", "-")
    # Path-traversal hardening: `timestamp`/`uuid` come from session JSONLs that,
    # via `gaius s3-retire`, may be authored on another agent's host. The only
    # prior sanitization was colon->dash, so `/` and `..` survived and a crafted
    # timestamp="../../../../tmp/" escaped STAGING_DIR. Restrict both fields to
    # safe filename chars and assert containment before writing.
    ts = re.sub(r"[^0-9A-Za-z_-]", "-", ts)
    uuid8 = re.sub(r"[^0-9A-Za-z]", "", str(entry.get("uuid", ""))[:8]) or "nouuid"
    fname = f"{ts}_{uuid8}.json"
    dest = (_core.STAGING_DIR / fname).resolve()
    if dest.parent != _core.STAGING_DIR.resolve():
        raise ValueError(f"staged filename escapes STAGING_DIR: {fname!r}")
    with open(dest, "w") as f:
        json.dump(entry, f, indent=2)


def display_uid(uuid) -> str:
    """Shortest operator-facing summary id that is actually discriminating.

    Mined summaries carry a synthetic ``mined-<session-uuid>`` uuid, so a bare
    ``uuid[:8]`` spends six of its eight characters on the tag and leaves TWO
    of session id — measured 246 ambiguous labels across 3167 staged entries
    (12-way at worst). `gaius done` then hard-errors on exactly the entries
    `gaius batch` just displayed, and the operator cannot build a longer prefix
    because batch prints nothing else to disambiguate with. Keep the tag (it
    says where the summary came from) but budget the 8 chars against the
    session uuid rather than the tag.
    """
    uuid = str(uuid or "?")
    for tag in ("mined-extra-", "mined-"):
        if uuid.startswith(tag):
            return tag + uuid[len(tag):][:8]
    return uuid[:8]


def cmd_show(args):
    """List all staged summaries, unreviewed first."""
    staged = load_staged()
    entries = sorted(staged.values(), key=lambda e: e.get("timestamp", ""))

    unreviewed = [e for e in entries if not e.get("reviewed")]
    reviewed   = [e for e in entries if     e.get("reviewed")]

    # Signal = has key_concepts or errors_fixes
    sig_unrev = [e for e in unreviewed if has_signal(e)]

    print(f"Total: {len(entries)}  |  Unreviewed: {len(unreviewed)}  |  "
          f"With signal: {len(sig_unrev)}  |  Reviewed: {len(reviewed)}")
    print()

    if unreviewed:
        print("── UNREVIEWED ──────────────────────────────────────────────────")
        for e in unreviewed:
            ts  = e.get("timestamp", "")[:10]
            sid = e.get("session_id", "?")[:8]
            uid = display_uid(e.get("uuid"))
            sig = "★" if has_signal(e) else " "
            sections_present = [k.split("_")[0][0].upper()
                                 for k in SIGNAL_SECTIONS if e["sections"].get(k)]
            tags = "".join(sections_present) if sections_present else "-"
            print(f"  {sig} {uid}  {ts}  session:{sid}  [{tags}]")
        print()
        print("  ★ = has key concepts / errors / pending tasks")
        print("  Tags: K=key_concepts  E=errors_fixes  P=pending_tasks")

    if reviewed:
        print(f"\n── REVIEWED ({len(reviewed)}) "
              "──────────────────────────────────────────────")
        for e in reviewed[-5:]:
            ts  = e.get("timestamp", "")[:10]
            uid = display_uid(e.get("uuid"))
            print(f"    {uid}  {ts}  ✓")
        if len(reviewed) > 5:
            print(f"    ... and {len(reviewed)-5} more")


def _promote_event(conn: sqlite3.Connection, ev: dict, outcome: str = None) -> None:
    """Build fact_text from a staged event and upsert into facts.db.

    Works for all format types — reads model_family/model_version from the event
    dict rather than hardcoding. Falls back to gemini defaults for backward compat.
    """
    ev_type = ev.get("type", "discovery")
    if ev_type == "decision":
        subject = ev.get("subject", "").strip()
        description = ev.get("description", "").strip()
        fact_text = f"[decision] {subject}: {description}" if description else f"[decision] {subject}"
    else:
        tool = ev.get("tool", "unknown")
        output = (ev.get("output") or "")[:300].strip()
        fact_text = f"[tool:{tool}] {output}" if output else f"[tool:{tool}]"

    final_outcome = outcome if outcome is not None else ev.get("outcome")
    upsert_fact(
        conn,
        domain=ev.get("domain", "general"),
        fact_key=ev.get("fact_key", hashlib.sha256(fact_text.encode()).hexdigest()[:16]),
        fact_text=fact_text,
        agent=ev.get("agent", "gemini"),
        session_uuid=ev.get("session_uuid", ""),
        provenance=ev.get("provenance", "automated"),
        score=0.6 if ev_type == "decision" else 0.4,
        outcome=final_outcome,
        model_family=ev.get("model_family", "gemini"),
        model_version=ev.get("model_version", ""),
        source=ev.get("source", "human"),
    )


def _rewrite_staging(all_events_by_file: dict, promoted_keys: set) -> int:
    """Rewrite staged gemini-facts files, removing promoted events.

    Args:
        all_events_by_file: {Path: [event_dict, ...]}
        promoted_keys: set of fact_key values that were promoted

    Returns:
        Number of files deleted (were empty after removing promoted events).
    """
    deleted = 0
    for staged_path, events in all_events_by_file.items():
        remaining = [ev for ev in events if ev.get("fact_key") not in promoted_keys]
        if not remaining:
            staged_path.unlink(missing_ok=True)
            deleted += 1
        else:
            with open(staged_path, "w") as f:
                for ev in remaining:
                    f.write(json.dumps(ev) + "\n")
    return deleted


def cmd_next_staged_facts(conn: sqlite3.Connection, staging_dir: Path, label: str) -> None:
    """Topic-grouped review UI for staged event-based facts (any format).

    Thin wrapper — delegates to the review loop with a parameterized staging dir.
    """
    cmd_next_gemini(conn, staging_dir=staging_dir, label=label)


def cmd_next_gemini(conn: sqlite3.Connection, staging_dir: Path = None,
                    label: str = "gemini-facts") -> None:
    """Topic-grouped review UI for staged facts.

    Presents staged events clustered by domain. Reviewer approves or rejects
    at cluster level. Individual review mode available per domain.

    Flow:
      [Y] approve all in cluster → promote with outcome=null
      [n] skip cluster → events stay staged for next review
      [r] review individually → k/c/x/?/s per event
      [q] quit → rewrite files, print summary

    Individual review keys:
      k = keep (promote, outcome=null)
      c = confirmed (promote, outcome='confirmed')
      x = refuted   (promote, outcome='refuted')
      ? = open_question (promote, outcome='open_question')
      s = skip (leave staged)
      q = quit individual mode (remaining in cluster auto-skipped)
    """
    gemini_staging = staging_dir or (_core.STAGING_DIR / "gemini-facts")

    # Load all staged events grouped by source file
    all_events_by_file: dict = {}
    for staged_path in sorted(gemini_staging.glob("*.jsonl")):
        events = []
        try:
            with open(staged_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        except Exception as e:
            print(f"  warning: could not read {staged_path.name}: {e}", file=sys.stderr)
            continue
        if events:
            all_events_by_file[staged_path] = events

    if not all_events_by_file:
        return  # caller handles "nothing to review" message

    # Flatten and group by domain
    domain_groups: dict[str, list[dict]] = {}
    for events in all_events_by_file.values():
        for ev in events:
            dom = ev.get("domain", "general")
            domain_groups.setdefault(dom, []).append(ev)

    total_events = sum(len(evs) for evs in domain_groups.values())
    total_files = len(all_events_by_file)
    decisions_total = sum(1 for evs in domain_groups.values()
                          for ev in evs if ev.get("type") == "decision")
    discoveries_total = total_events - decisions_total

    print("=" * 68)
    print(f"{label.replace('-', ' ').title()} Review — {total_events} events across {len(domain_groups)} domains")
    print(f"  {decisions_total} decisions (structured_reasoning)  "
          f"{discoveries_total} discoveries (automated)")
    print(f"  Source files: {total_files}")
    print(f"  [Y]es all / [n]o skip / [r]eview individually / [q]uit")
    print("=" * 68)

    promoted_keys: set[str] = set()
    promoted_count = 0
    quit_requested = False

    for domain, events in sorted(domain_groups.items()):
        if quit_requested:
            break

        decisions = [ev for ev in events if ev.get("type") == "decision"]
        discoveries = [ev for ev in events if ev.get("type") != "decision"]

        print(f"\n── {domain} ── {len(events)} facts "
              f"({len(decisions)} decisions, {len(discoveries)} discoveries) ──")

        # Show sample decisions
        if decisions:
            print("  Decisions:")
            for ev in decisions[:5]:
                subj = ev.get("subject", "")[:70]
                print(f"    • {subj}")
            if len(decisions) > 5:
                print(f"    … and {len(decisions) - 5} more")

        # Show sample discoveries
        if discoveries:
            print("  Discoveries:")
            for ev in discoveries[:5]:
                tool = ev.get("tool", "?")
                out = (ev.get("output") or "")[:60].replace("\n", " ")
                print(f"    • [{tool}] {out}")
            if len(discoveries) > 5:
                print(f"    … and {len(discoveries) - 5} more")

        # Prompt
        try:
            choice = input("\n  [Y/n/r/q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            quit_requested = True
            break

        if choice in ("", "y"):
            # Approve all — promote with outcome=null
            for ev in events:
                fk = ev.get("fact_key")
                if fk and fk not in promoted_keys:
                    _promote_event(conn, ev, outcome=None)
                    promoted_keys.add(fk)
                    promoted_count += 1
            print(f"  ✓ Promoted {len(events)} facts from {domain}")

        elif choice == "n":
            print(f"  ↷ Skipped {domain}")

        elif choice == "r":
            # Individual review mode
            for ev in events:
                fk = ev.get("fact_key")
                if not fk:
                    continue

                ev_type = ev.get("type", "discovery")
                print(f"\n  ┌─ {ev_type.upper()} ─────────────────────────────────────────")
                if ev_type == "decision":
                    print(f"  │  Subject:     {ev.get('subject', '')}")
                    print(f"  │  Description: {ev.get('description', '')[:200]}")
                else:
                    print(f"  │  Tool:    {ev.get('tool', '')}")
                    print(f"  │  Output:  {(ev.get('output') or '')[:200]}")
                print(f"  │  Domain:  {ev.get('domain', '')} | "
                      f"Provenance: {ev.get('provenance', '')}")
                print(f"  └─ [k]eep / [c]onfirmed / [x]refuted / [?]open / [s]kip / [q]uit")

                try:
                    sub_choice = input("     > ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    quit_requested = True
                    break

                if sub_choice == "q":
                    break  # exit individual mode, rest of cluster skipped
                elif sub_choice == "s":
                    continue  # skip this event
                else:
                    outcome_map = {
                        "k": None,
                        "c": "confirmed",
                        "x": "refuted",
                        "?": "open_question",
                    }
                    outcome = outcome_map.get(sub_choice, None)
                    _promote_event(conn, ev, outcome=outcome)
                    promoted_keys.add(fk)
                    promoted_count += 1
                    tag = f"outcome={outcome}" if outcome else "outcome=null"
                    print(f"     ✓ {tag}")

        elif choice == "q":
            quit_requested = True

    # Rewrite staging files — remove promoted events
    deleted_files = _rewrite_staging(all_events_by_file, promoted_keys)

    remaining_events = total_events - promoted_count
    print(f"\n{'=' * 68}")
    print(f"Review complete: promoted {promoted_count} facts, "
          f"{remaining_events} remaining staged")
    if deleted_files:
        print(f"  Cleaned up {deleted_files} fully-reviewed staging file(s)")
    if promoted_count:
        print(f"  Facts now in: {_core.DB_PATH}")
        print(f"  Query:  sqlite3 {_core.DB_PATH} \"SELECT domain, fact_text FROM facts ORDER BY domain, score DESC\"")


def _cmd_next_pending_fact(conn) -> bool:
    """Show the highest-priority pending fact. Returns True if one was shown.

    Also re-queues deferred facts whose reopen date has passed.
    """
    # Re-queue deferred facts past their reopen date (conflict_with holds ISO date)
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE facts SET review_state='pending' "
        "WHERE review_state='deferred' AND conflict_with IS NOT NULL AND conflict_with < ?",
        (now_iso,)
    )
    conn.commit()

    row = conn.execute("""
        SELECT id, fact_text, domain, confidence, confidence_source, conflict_with, first_seen
        FROM facts
        WHERE review_state = 'pending'
        ORDER BY (1.0 - confidence) * score DESC
        LIMIT 1
    """).fetchone()

    if not row:
        return False

    pending_count = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE review_state = 'pending'"
    ).fetchone()[0]

    conflict_info = ""
    if row['conflict_with']:
        cf = conn.execute(
            "SELECT fact_text FROM facts WHERE id = ?", (row['conflict_with'],)
        ).fetchone()
        if cf:
            conflict_info = f"\nConflicts with: [{row['conflict_with']}] {cf['fact_text'][:120]}"

    conf_pct = int((row['confidence'] or 0.5) * 100)
    print("=" * 68)
    print(f"[PENDING FACT]  id={row['id']}  domain={row['domain']}")
    print(f"Confidence:     {conf_pct}%  ({row['confidence_source']})")
    print(f"First seen:     {(row['first_seen'] or '')[:19]}")
    print(f"Pending queue:  {pending_count}")
    print("=" * 68)
    print(f"\n{row['fact_text']}{conflict_info}")
    print(f"\n{'=' * 68}")
    print(f"Confirm:  gaius confirm {row['id']}")
    print(f"Reject:   gaius reject {row['id']}")
    print(f"Defer:    gaius defer {row['id']}")
    return True


def cmd_next(args):
    """Print the oldest unreviewed summary with signal.

    Priority order:
    1. Staged event facts (pentagi/ollama/gemini)
    2. Pending facts in facts.db (low-confidence or contradicted)
    3. Session compaction summaries

    Pass --facts to show only pending facts. Pass --summaries to skip to summaries.
    """
    show_facts_only = '--facts' in (args or [])
    skip_facts = '--summaries' in (args or [])

    # Event-based staged facts take priority — they need merge before facts.db is useful
    conn = init_db()
    if not show_facts_only:
        for staging_label in ("pentagi-facts", "ollama-facts", "gemini-facts"):
            staging_dir = _core.STAGING_DIR / staging_label
            if staging_dir.exists() and list(staging_dir.glob("*.jsonl")):
                cmd_next_staged_facts(conn, staging_dir, staging_label)
                return

    # Pending facts (low-confidence / contradicted) — second priority
    if not skip_facts:
        if _cmd_next_pending_fact(conn):
            return

    if show_facts_only:
        print("No pending facts in review queue.")
        return

    staged = load_staged()

    # Prefer summaries with signal; fall back to any unreviewed
    unreviewed = sorted(
        [e for e in staged.values() if not e.get("reviewed")],
        key=lambda e: (not has_signal(e), e.get("timestamp", ""))
    )

    if not unreviewed:
        print("All summaries reviewed. Nothing left in queue.")
        return

    e = unreviewed[0]
    remaining = len(unreviewed)

    source_tag = " [mined]" if e.get("source") == "mined" else ""
    print("=" * 68)
    print(f"UUID:      {e['uuid']}")
    print(f"Session:   {e['session_id']}")
    print(f"Date:      {e.get('timestamp','')[:19]}")
    print(f"Signal:    {'yes (★)' if has_signal(e) else 'no'}{source_tag}")
    print(f"Remaining: {remaining}")
    print("=" * 68)

    for key, header in SECTION_HEADERS:
        text = e["sections"].get(key, "").strip()
        if text:
            print(f"\n── {header} ──────────────────────────────────────────")
            print(text)

    print(f"\n{'=' * 68}")
    print(f"Mark done:  gaius done {display_uid(e['uuid'])}")
    print(f"Skip:       gaius next  (after gaius done {display_uid(e['uuid'])})")


def cmd_done(args):
    """Mark a summary as reviewed by UUID prefix."""
    if not args:
        print("Usage: gaius done <uuid-prefix>  (min 4 chars)", file=sys.stderr)
        sys.exit(1)

    prefix = args[0].lower()
    if len(prefix) < 4:
        print("UUID prefix must be at least 4 characters", file=sys.stderr)
        sys.exit(1)

    staged = load_staged()
    matches = [(uid, e) for uid, e in staged.items()
               if uid.lower().startswith(prefix)]

    if not matches:
        print(f"No summary matching prefix: {prefix}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        # Short labels (e.g. `mined-XX`) collide across sessions, but the intent
        # is almost always the one still UNREVIEWED. Auto-disambiguate to it;
        # only error if the collision is itself ambiguous (0 or >=2 unreviewed).
        unreviewed = [(uid, e) for uid, e in matches if not e.get("reviewed")]
        if len(unreviewed) == 1:
            matches = unreviewed
        else:
            print(f"Ambiguous prefix '{prefix}' matches {len(matches)} summaries "
                  f"({len(unreviewed)} unreviewed):", file=sys.stderr)
            for uid, e in matches:
                state = "reviewed" if e.get("reviewed") else "UNREVIEWED"
                print(f"  {uid}  [{state}]", file=sys.stderr)
            sys.exit(1)

    uid, e = matches[0]
    if e.get("reviewed"):
        print(f"Already marked reviewed: {uid[:8]}")
        return

    e["reviewed"] = True
    e["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    save_staged(e)

    remaining = sum(1 for x in staged.values()
                    if not x.get("reviewed") and x["uuid"] != uid)
    print(f"✓ Marked reviewed: {uid[:8]}  |  {remaining} remaining")


def _resolve_fact_id(args, cmd_name: str) -> int:
    """Parse and validate a numeric fact ID from args. Exits on error."""
    if not args:
        print(f"Usage: gaius {cmd_name} <fact-id>", file=sys.stderr)
        sys.exit(1)
    try:
        return int(args[0])
    except ValueError:
        print(f"fact-id must be an integer, got: {args[0]}", file=sys.stderr)
        sys.exit(1)


def cmd_confirm(args):
    """Mark a pending fact as confirmed by a human reviewer.

    Sets confidence=1.0, confidence_source='human', review_state='confirmed'.
    Human-only: require_human('confirm') refuses an agent shell or non-tty.
    Usage: gaius confirm <fact-id>
    """
    require_human("confirm")
    fact_id = _resolve_fact_id(args, 'confirm')
    conn = init_db()
    row = conn.execute("SELECT id, fact_text, domain FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if not row:
        print(f"No fact with id={fact_id}", file=sys.stderr)
        sys.exit(1)
    conn.execute(
        "UPDATE facts SET review_state='confirmed', confidence=1.0, confidence_source='human' WHERE id=?",
        (fact_id,)
    )
    conn.commit()
    pending = conn.execute("SELECT COUNT(*) FROM facts WHERE review_state='pending'").fetchone()[0]
    print(f"✓ Confirmed: [{fact_id}] {row['fact_text'][:80]}  |  {pending} pending remaining")


def cmd_reject(args):
    """Mark a pending fact as rejected (excluded from inject).

    Sets review_state='rejected'. The fact is retained in facts.db for audit but
    excluded from inject queries.
    Human-only: an agent rejecting facts would curate the quiz pool and
    thereby control which rows can be stamped confidence_source='human'.
    Usage: gaius reject <fact-id>
    """
    require_human("reject")
    fact_id = _resolve_fact_id(args, 'reject')
    conn = init_db()
    row = conn.execute("SELECT id, fact_text FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if not row:
        print(f"No fact with id={fact_id}", file=sys.stderr)
        sys.exit(1)
    # outcome='rejected' is the value every inject/search query filters on —
    # review_state alone left rejected facts in the inject candidate pool.
    conn.execute("UPDATE facts SET review_state='rejected', outcome='rejected' WHERE id=?", (fact_id,))
    conn.commit()
    pending = conn.execute("SELECT COUNT(*) FROM facts WHERE review_state='pending'").fetchone()[0]
    print(f"✗ Rejected: [{fact_id}] {row['fact_text'][:80]}  |  {pending} pending remaining")


def cmd_defer(args):
    """Defer a pending fact for re-review in 7 days.

    Sets review_state='deferred'. gaius next will re-surface it after 7 days.
    Usage: gaius defer <fact-id>
    """
    fact_id = _resolve_fact_id(args, 'defer')
    conn = init_db()
    row = conn.execute("SELECT id, fact_text FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if not row:
        print(f"No fact with id={fact_id}", file=sys.stderr)
        sys.exit(1)
    reopen_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    conn.execute(
        "UPDATE facts SET review_state='deferred', conflict_with=? WHERE id=?",
        (reopen_at, fact_id)
    )
    conn.commit()
    pending = conn.execute("SELECT COUNT(*) FROM facts WHERE review_state='pending'").fetchone()[0]
    print(f"⏸  Deferred: [{fact_id}] {row['fact_text'][:80]}  |  re-opens {reopen_at[:10]}")
    print(f"   {pending} pending remaining")


def cmd_agent_review(args):
    """Mark a pending fact as reviewed by an AUTOMATED agent (the mnemos surgeon).

    This is the machine substitute for the empirically-dead human `confirm` verb
    (1 of ~17,637 injectable facts ever human-confirmed, verified 2026-07-21). It
    sets review_state='agent-reviewed' and leaves confidence + confidence_source
    UNTOUCHED.

    HARD CONSTRAINT: this never writes confidence_source='human' — that value is the
    corpus_audit self-poison detector's trust anchor (corpus_audit.py L34/50/57,
    `!= 'human'` defines "unverified"). Faking it would poison the detector.

    Rank impact: 'agent-reviewed' is weighted ≤ auto (0.6x, same as pending — see
    gaius.maturity.REVIEW_STATE_WEIGHT). An LLM reviewer with a completion incentive
    and no live-state access would rubber-stamp contradiction-flagged facts UPWARD,
    so until outcome-grounding lands this verb is queue-hygiene + `reject` only —
    it can NEVER boost a fact's inject rank. To remove a bad fact, use `gaius reject`.

    Reversible: flip back to 'auto' (or re-review) with a plain UPDATE. Idempotent.
    Usage: gaius agent-review <fact-id>
    """
    fact_id = _resolve_fact_id(args, 'agent-review')
    conn = init_db()
    row = conn.execute("SELECT id, fact_text, review_state FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if not row:
        print(f"No fact with id={fact_id}", file=sys.stderr)
        sys.exit(1)
    # review_state ONLY — confidence / confidence_source deliberately left as-is.
    conn.execute("UPDATE facts SET review_state='agent-reviewed' WHERE id=?", (fact_id,))
    conn.commit()
    pending = conn.execute("SELECT COUNT(*) FROM facts WHERE review_state='pending'").fetchone()[0]
    print(f"⤷ Agent-reviewed: [{fact_id}] {row['fact_text'][:80]}  |  {pending} pending remaining")


def _quiz_eligible(conn) -> list:
    """Active, non-live, non-rejected facts with Leitner columns filled."""
    return conn.execute(
        """
        SELECT id, fact_text, domain, score, review_state, confidence_source,
               COALESCE(leitner_box, 0) AS leitner_box,
               COALESCE(leitner_seen, 0) AS leitner_seen,
               COALESCE(leitner_correct, 0) AS leitner_correct,
               COALESCE(leitner_wrong, 0) AS leitner_wrong
        FROM facts
        WHERE tombstoned_at IS NULL
          AND (outcome IS NULL OR outcome != 'rejected')
          AND COALESCE(fact_type, 'operational') != 'live'
        """
    ).fetchall()


def _quiz_progress(rows) -> dict:
    out = {}
    for r in rows:
        out[str(r["id"])] = {
            "b": int(r["leitner_box"] or 0),
            "n": int(r["leitner_seen"] or 0),
            "c": int(r["leitner_correct"] or 0),
            "w": int(r["leitner_wrong"] or 0),
        }
    return out


def _quiz_write_progress(conn, fact_id: int, rec: dict) -> None:
    conn.execute(
        "UPDATE facts SET leitner_box=?, leitner_seen=?, leitner_correct=?, "
        "leitner_wrong=? WHERE id=?",
        (rec["b"], rec["n"], rec["c"], rec["w"], fact_id),
    )


def cmd_quiz(args):
    """Leitner review loop over corpus facts. Human-only when mutating.

    Usage:
      gaius quiz [--budget N] [--domain D]     interactive (tty, not an agent)
      gaius quiz --report [--domain D]         read-only calibration summary
    """
    parser = argparse.ArgumentParser(
        prog="gaius quiz",
        description="Spaced-repetition human review over corpus facts",
    )
    parser.add_argument("--budget", type=int, default=10,
                        help=f"max facts in one sitting (default 10, max {QUIZ_BUDGET_MAX})")
    parser.add_argument("--domain", default="",
                        help="restrict to one domain")
    parser.add_argument("--report", action="store_true",
                        help="print box histogram + miss rate; do not write")
    parsed = parser.parse_args(args or [])

    conn = init_db()
    rows = _quiz_eligible(conn)
    if parsed.domain:
        rows = [r for r in rows if r["domain"] == parsed.domain]
    progress = _quiz_progress(rows)

    if parsed.report:
        ids = [str(r["id"]) for r in rows]
        hist = box_histogram(progress, ids)
        unseen = sum(1 for i in ids if int(progress.get(i, {}).get("n") or 0) == 0)
        seen = len(ids) - unseen
        wrong = sum(int(progress[i].get("w") or 0) for i in ids if i in progress)
        attempts = sum(int(progress[i].get("n") or 0) for i in ids if i in progress)
        by_domain: dict[str, list[int]] = {}
        for r in rows:
            rec = progress[str(r["id"])]
            n, w = int(rec.get("n") or 0), int(rec.get("w") or 0)
            if n == 0:
                continue
            d = r["domain"] or "?"
            slot = by_domain.setdefault(d, [0, 0])
            slot[0] += n
            slot[1] += w
        print("gaius quiz — calibration report (read-only)")
        print(f"  eligible: {len(ids)}  seen: {seen}  unseen: {unseen}")
        print("  boxes:    " + "  ".join(f"b{i}={n}" for i, n in enumerate(hist)))
        rate = (wrong / attempts) if attempts else 0.0
        print(f"  attempts: {attempts}  misses: {wrong}  miss-rate: {rate:.2%}")
        if by_domain:
            print("  miss-rate by domain:")
            for d, (n, w) in sorted(by_domain.items(), key=lambda kv: -kv[1][1] / max(kv[1][0], 1)):
                print(f"    {d:<22} {w}/{n}  ({w / n:.0%})")
        return

    require_human("quiz")
    if parsed.budget < 1:
        print("--budget must be >= 1", file=sys.stderr)
        sys.exit(1)
    if parsed.budget > QUIZ_BUDGET_MAX:
        print(
            f"--budget must be <= {QUIZ_BUDGET_MAX} (one sitting), got {parsed.budget}",
            file=sys.stderr,
        )
        sys.exit(1)

    ids = [str(r["id"]) for r in rows]
    by_id = {str(r["id"]): r for r in rows}
    session = draw(ids, progress, k=parsed.budget)
    if not session:
        print("No eligible facts to quiz. Run: gaius retire")
        return

    print(f"gaius quiz — {len(session)} of {len(ids)} eligible  (y=still true, n=reject, s=skip)")
    print("A miss drops the box to 0. First 'y' writes confidence_source='human'; repeats only move the box.")
    print()

    answered = 0
    for item_id in session:
        row = by_id[item_id]
        rec = progress[item_id]
        print("=" * 68)
        print(f"[{row['domain']}] id={row['id']}  box={rec['b']}  seen={rec['n']}  weight={item_weight(rec)}")
        print("=" * 68)
        print("source:")
        print(row["fact_text"])
        print()
        try:
            raw = input("still true? [y/n/s] ").strip().lower()
        except EOFError:
            print("\n(interrupted)")
            break
        if raw in ("s", "skip", ""):
            print("  skipped — box unchanged")
            continue
        if raw in ("n", "no"):
            new = grade(rec, False)
            _quiz_write_progress(conn, row["id"], new)
            conn.execute(
                "UPDATE facts SET review_state='rejected', outcome='rejected' WHERE id=?",
                (row["id"],),
            )
            conn.commit()
            progress[item_id] = new
            answered += 1
            print("  rejected + box 0")
            continue
        if raw in ("y", "yes"):
            new = grade(rec, True)
            _quiz_write_progress(conn, row["id"], new)
            # First confirm writes the human anchor. Repeats only move the box —
            # box promotion must not inflate inject rank by repetition alone.
            if row["confidence_source"] != "human":
                conn.execute(
                    "UPDATE facts SET review_state='confirmed', confidence=1.0, "
                    "confidence_source='human' WHERE id=?",
                    (row["id"],),
                )
            conn.commit()
            progress[item_id] = new
            answered += 1
            print(f"  confirmed  box {rec['b']} → {new['b']}")
            continue
        print("  unrecognized — skipped")

    print(f"\n✓ {answered} verdicts this sitting. `gaius quiz --report` for calibration.")


# cmd_kg extracted to gaius/kg.py (2026-06-28); re-imported at bottom of file.


def cmd_stats(args):
    """Show extraction statistics."""
    staged = load_staged()
    entries = list(staged.values())

    if not entries:
        print("No staged summaries. Run: gaius retire")
        return

    unreviewed  = [e for e in entries if not e.get("reviewed")]
    with_signal = [e for e in entries if has_signal(e)]
    sessions    = {e.get("session_id") for e in entries}

    oldest = min(e.get("timestamp", "") for e in entries)
    newest = max(e.get("timestamp", "") for e in entries)

    print(f"Sessions dir:  {_core.PROJECT_DIR}")
    print(f"Staging dir:   {_core.STAGING_DIR}")
    print()
    print(f"Sessions with compacts: {len(sessions)}")
    print(f"Total summaries:        {len(entries)}")
    print(f"  With signal:          {len(with_signal)}")
    print(f"  Unreviewed:           {len(unreviewed)}")
    print(f"  Reviewed:             {len(entries) - len(unreviewed)}")
    print(f"Date range:             {oldest[:10]} → {newest[:10]}")
    print()

    # facts.db statistics
    conn = init_db()
    try:
        total_facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        by_source = conn.execute("SELECT provenance, COUNT(*) FROM facts GROUP BY provenance").fetchall()
        by_domain = conn.execute("SELECT domain, COUNT(*) FROM facts GROUP BY domain").fetchall()
        
        print(f"Facts in DB (persistent): {total_facts}")
        if total_facts:
            print("  By source:")
            for src, count in by_source:
                print(f"    {src or 'unknown':<12} {count:>5}")
            print("  By domain:")
            for dom, count in by_domain:
                print(f"    {dom or 'unknown':<12} {count:>5}")
            # Per model family
            by_model = conn.execute(
                "SELECT model_family, COUNT(*) FROM facts GROUP BY model_family"
            ).fetchall()
            if by_model:
                print("  By model family:")
                for fam, count in by_model:
                    print(f"    {fam or 'unknown':<12} {count:>5}")
            # Per model version (family:version)
            by_version = conn.execute(
                "SELECT model_family, model_version, COUNT(*) FROM facts "
                "WHERE model_version != '' GROUP BY model_family, model_version"
            ).fetchall()
            if by_version:
                print("  By model version:")
                for fam, ver, count in by_version:
                    print(f"    {fam}:{ver:<16} {count:>5}")
        # Embedding stats
        if HAS_SQLITE_VEC:
            try:
                # Live facts only: tombstoned facts lose embeddings by design (dedup) —
                # counting them made 100%-embedded corpora read as a backlog.
                live_facts = conn.execute("SELECT COUNT(*) FROM facts WHERE tombstoned_at IS NULL").fetchone()[0]
                embedded_count = conn.execute(
                    "SELECT COUNT(*) FROM facts WHERE tombstoned_at IS NULL "
                    "AND id IN (SELECT fact_id FROM fact_embeddings)").fetchone()[0]
                print(f"  Embeddings: {embedded_count}/{live_facts} ({100*embedded_count//max(live_facts,1)}%)")
                print(f"  Embedding model: all-MiniLM-L6-v2 ({_EMBED_DIM}-dim)")
            except Exception:
                print("  Embeddings: not initialized (run: gaius embed)")
        else:
            print("  Embeddings: sqlite-vec not installed")

    except Exception as e:
        print(f"Warning: could not load facts.db stats: {e}")

    print()
    print("Section extraction rates:")
    for key, header in SECTION_HEADERS:
        count = sum(1 for e in entries if e["sections"].get(key))
        pct = 100 * count // len(entries) if entries else 0
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"  {header:<35} {bar} {count:3}/{len(entries)} ({pct:2d}%)")

    # Per-domain fact density
    domain_hits = count_domain_hits(entries)
    print()
    print("Domain coverage (summaries mentioning domain keywords):")
    for domain, count in sorted(domain_hits.items(), key=lambda x: -x[1]):
        pct = 100 * count // len(entries) if entries else 0
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"  {domain:<20} {bar} {count:3}/{len(entries)} ({pct:2d}%)")

    empty = [d for d, c in domain_hits.items() if c == 0]
    if empty:
        print(f"\n  No coverage: {', '.join(sorted(empty))}")

    # TF-IDF Scoring Stats
    scored = [e for e in entries if e.get("score") is not None and e.get("score", 0) > 0]
    if scored:
        scores = [e["score"] for e in scored]
        print()
        print("TF-IDF Scoring:")
        print(f"  Scored entries:   {len(scored)}/{len(entries)}")
        print(f"  Score range:      {min(scores):.3f} - {max(scores):.3f}")
        print(f"  Mean score:       {sum(scores)/len(scores):.3f}")

    # Agent sources
    sources = Counter(e.get("agent_source", "unknown") for e in entries)
    if any(s != "unknown" for s in sources):
        print()
        print("Agent sources:")
        for source, count in sources.most_common():
            print(f"  {source:<20} {count}")

    # Per-domain bootstrap status
    domain_stats = load_domain_stats()
    if domain_stats:
        print()
        print("Domain bootstrap status:")
        for dom in sorted(domain_stats.keys()):
            info = domain_stats[dom]
            sc = info.get("session_count", 0)
            status = "BOOTSTRAP" if sc < BOOTSTRAP_THRESHOLD else "scoring"
            bar_pct = min(100, 100 * sc // BOOTSTRAP_THRESHOLD)
            bar = "█" * (bar_pct // 5) + "░" * (20 - bar_pct // 5)
            print(f"  {dom:<20} {bar} {sc:3}/{BOOTSTRAP_THRESHOLD} sessions  [{status}]")

    # Corpus Statistics
    index_path = _core.CORPUS_DIR / "index.jsonl"
    if index_path.exists():
        print()
        print("Corpus Statistics:")
        indexed_count = 0
        total_corpus_entries = 0
        with open(index_path, "r") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    indexed_count += 1
                    total_corpus_entries += d.get("corpus_entries", 0)
                except Exception:
                    pass
        print(f"  Sessions indexed:       {indexed_count}")
        print(f"  Total training records: {total_corpus_entries}")

    # Facts DB Statistics
    if _core.DB_PATH.exists():
        print()
        print("Facts DB Statistics (facts.db):")
        try:
            conn = init_db()
            total_facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            active_facts = conn.execute("SELECT COUNT(*) FROM facts WHERE COALESCE(training_excluded, 0) = 0").fetchone()[0]
            excluded_facts = total_facts - active_facts
            by_domain = conn.execute(
                "SELECT domain, COUNT(*) as n FROM facts WHERE COALESCE(training_excluded, 0) = 0 GROUP BY domain ORDER BY n DESC"
            ).fetchall()
            by_prov = conn.execute(
                "SELECT provenance, COUNT(*) as n FROM facts GROUP BY provenance ORDER BY n DESC"
            ).fetchall()
            session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            gemini_sessions = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE agent = 'gemini'"
            ).fetchone()[0]
            print(f"  Total facts:            {total_facts} ({active_facts} active, {excluded_facts} excluded)")
            print(f"  Sessions registered:    {session_count}")
            print(f"  Gemini sessions:        {gemini_sessions}")
            if by_domain:
                print(f"  By domain:")
                for row in by_domain:
                    print(f"    {row[0]:<22} {row[1]}")
            if by_prov:
                print(f"  By provenance:")
                for row in by_prov:
                    print(f"    {row[0]:<22} {row[1]}")
            # Gemini staged facts
            gemini_staging = _core.STAGING_DIR / "gemini-facts"
            if gemini_staging.exists():
                staged_files = list(gemini_staging.glob("*.jsonl"))
                staged_event_count = sum(
                    sum(1 for _ in open(f)) for f in staged_files
                )
                print(f"  Gemini staged (pending merge): {len(staged_files)} sessions, {staged_event_count} events")
        except Exception as e:
            print(f"  (could not read facts.db: {e})")


def cmd_rescan(args):
    """Force re-extraction for a specific session by UUID prefix."""
    if not args:
        print("Usage: gaius rescan <uuid-prefix>  (min 4 chars)", file=sys.stderr)
        sys.exit(1)

    prefix = args[0].lower()
    if len(prefix) < 4:
        print("UUID prefix must be at least 4 characters", file=sys.stderr)
        sys.exit(1)

    staged = load_staged()
    matches = [(uid, e) for uid, e in staged.items()
               if uid.lower().startswith(prefix)]

    if not matches:
        print(f"No staged summary matching prefix: {prefix}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(f"Ambiguous prefix '{prefix}' matches {len(matches)} summaries:",
              file=sys.stderr)
        for uid, _ in matches:
            print(f"  {uid}", file=sys.stderr)
        sys.exit(1)

    uid, existing = matches[0]
    session_id = existing.get("session_id", "")

    # Find the source JSONL file
    jsonl_path = _core.PROJECT_DIR / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        print(f"Source file not found: {jsonl_path}", file=sys.stderr)
        sys.exit(1)

    # Re-scan the file for this UUID
    found = False
    with open(jsonl_path) as f:
        for line in f:
            if "isCompactSummary" not in line:
                continue
            entry = json.loads(line)
            if not entry.get("isCompactSummary"):
                continue
            if entry.get("uuid", "") != uid:
                continue

            content = entry.get("message", {}).get("content", "")
            if not content:
                continue

            sections = {
                key: extract_section(content, header)
                for key, header in SECTION_HEADERS
            }

            existing["sections"] = sections
            existing["content_hash"] = content_hash(content)
            existing["updated_at"] = datetime.now(timezone.utc).isoformat()
            existing["reviewed"] = False
            save_staged(existing)
            found = True
            print(f"✓ Rescanned: {uid[:8]}  (re-queued for review)")
            break

    if not found:
        print(f"No compaction summary with UUID {uid[:8]} found in {jsonl_path.name}",
              file=sys.stderr)
        sys.exit(1)


def cmd_batch(args):
    """Print all unreviewed summaries with signal, one after another."""
    staged = load_staged()
    # State-change summaries first — operational transitions rot project files
    # fastest when their review is delayed.
    unreviewed = sorted(
        [e for e in staged.values() if not e.get("reviewed") and has_signal(e)],
        key=lambda e: (not e.get("state_change"), e.get("timestamp", ""))
    )

    if not unreviewed:
        print("No unreviewed summaries with signal. Run: gaius show")
        return

    sc_count = sum(1 for e in unreviewed if e.get("state_change"))
    print(f"Batch mode: {len(unreviewed)} summaries with signal"
          + (f" ({sc_count} ⚡state-change, listed first)" if sc_count else "") + "\n")

    for i, e in enumerate(unreviewed, 1):
        source_tag = " [mined]" if e.get("source") == "mined" else ""
        sc_tag = " ⚡STATE-CHANGE — verify project files reflect this" if e.get("state_change") else ""
        print("=" * 68)
        print(f"[{i}/{len(unreviewed)}]  {display_uid(e['uuid'])}  {e.get('timestamp','')[:10]}{source_tag}{sc_tag}")
        print("=" * 68)

        for key, header in SECTION_HEADERS:
            if key not in SIGNAL_SECTIONS:
                continue
            text = e["sections"].get(key, "").strip()
            if text:
                print(f"\n── {header} ──")
                print(text)

        print()
