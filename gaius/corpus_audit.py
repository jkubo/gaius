"""gaius.corpus_audit — read-only corpus integrity + retrieval-augmented routing.

Repetition/prune candidates, self-poison audit signals, corpus content search,
and ``route_suggest`` (keyword router + corpus + win-rates). Read-only analytics
surface over facts.db. Provides the ``corpus-audit`` and ``route-suggest`` commands.

Facade convention (see ARCHITECTURE.md): shared helpers imported from gaius._core
at top; _core re-imports this module's public symbols before the COMMANDS dict.
"""
import argparse
import json
import os
import re
import sqlite3
from collections import Counter

# imports from gaius._core (shared hub) — circular-by-design, see ARCHITECTURE.md
from gaius._core import DB_PATH, route_domains


# READ-ONLY BY DEFAULT. Surfaces the risk the operator named: facts corroborated by REPETITION
# but never outcome- or human-verified, plus contradiction clusters (same fact_key, divergent
# live facts). This is the shadow half of outcome-grounding. The default audit MUTATES NOTHING;
# a DEMOTE-ONLY enforcement pass (reclassify review_state 'auto'→'pending', reversible) is
# available ONLY behind the CORPUS_AUDIT_ENFORCE gate (see enforce_demote / cmd_corpus_audit).

REPETITION_THRESHOLD = 2  # confirmation_count at/above which a fact is "rewarded by repetition"

# Contradiction-cluster enforcement bar — strictly HIGHER than REPETITION_THRESHOLD (2): a
# contradiction cluster (same fact_key, >1 live rows) is only enforced when some member is
# reinforced (confirmation_count >= this). Avoids churning one-off divergences; only demotes
# losers inside conflicts that have actually been repeated.
CONTRADICTION_ENFORCE_MIN_CC = 3


def repetition_candidates(conn: sqlite3.Connection, limit: int = 20) -> list:
    """List repetition-only facts (corroborated by repeats, never outcome/human-verified) — the
    candidates an operator-gated enforcement pass would demote. Read-only; worst (most-repeated) first."""
    rows = conn.execute(
        "SELECT id, domain, confirmation_count, COALESCE(score,0), substr(fact_text,1,140) "
        "FROM facts WHERE tombstoned_at IS NULL AND confirmation_count >= ? "
        "AND outcome IS NULL AND (confidence_source IS NULL OR confidence_source != 'human') "
        "ORDER BY confirmation_count DESC, COALESCE(score,0) DESC LIMIT ?",
        (REPETITION_THRESHOLD, limit)).fetchall()
    return [{"id": r[0], "domain": r[1], "confirmation_count": r[2], "score": r[3], "text": r[4]}
            for r in rows]


def enforce_demote(conn: sqlite3.Connection,
                   contradiction_min_cc: int = CONTRADICTION_ENFORCE_MIN_CC) -> dict:
    """DEMOTE-ONLY enforcement — the machine substitute for the dead human review verb.

    Reclassifies FLAGGED facts review_state 'auto' → 'pending' so the ranker's 0.6x pending
    penalty demotes them at inject time. This is the only lever that actually works: the
    `score` column feeds the ranker as a BOOST (only when > 0.35) and cannot demote, so a
    low-score write is inert; reclassifying to 'pending' reuses the real demote path.

    Guarantees (see HARD CONSTRAINTS in the confidence-review spec):
      • DEMOTE-ONLY — never tombstones, never DELETEs.
      • Touches ONLY review_state — confidence_source is left untouched, so the
        'human' trust anchor (corpus_audit's `!= 'human'`) is never machine-forged.
      • Reversible — an operator flips review_state back to 'auto' to undo.
      • Idempotent — only 'auto' rows are eligible, so re-runs are no-ops.

    Requires a WRITABLE conn; callers gate this behind CORPUS_AUDIT_ENFORCE (default-off).
    Returns {"repetition_demoted": N, "contradiction_demoted": M}.
    """
    # Tier 1 — repetition-only: corroborated by repeats, never outcome/human-verified.
    # Same predicate as repetition_candidates(), restricted to still-'auto' rows (idempotent,
    # demote-only). One set-based UPDATE.
    rep = conn.execute(
        "UPDATE facts SET review_state='pending' "
        "WHERE tombstoned_at IS NULL AND review_state='auto' "
        "AND confirmation_count >= ? AND outcome IS NULL "
        "AND (confidence_source IS NULL OR confidence_source != 'human')",
        (REPETITION_THRESHOLD,))
    rep_n = rep.rowcount if rep.rowcount and rep.rowcount > 0 else 0

    # Tier 2 — contradiction-cluster losers, behind the strictly HIGHER confirmation bar.
    # For each fact_key with >1 live rows where some member is reinforced
    # (confirmation_count >= contradiction_min_cc), keep the single most-trustworthy row and
    # demote the REST that are still 'auto' and not human/outcome-verified. Uncommitted Tier-1
    # updates are visible on this same conn, so a loser already demoted above shows as
    # 'pending' here and is not double-counted.
    cont_n = 0
    keys = conn.execute(
        "SELECT fact_key FROM facts WHERE tombstoned_at IS NULL "
        "GROUP BY fact_key HAVING COUNT(*) > 1 AND MAX(confirmation_count) >= ?",
        (contradiction_min_cc,)).fetchall()

    def _verified(outcome, csrc):
        return csrc == "human" or (outcome is not None and outcome != "rejected")

    for (fact_key,) in keys:
        rows = conn.execute(
            "SELECT id, review_state, confirmation_count, COALESCE(score,0), outcome, confidence_source "
            "FROM facts WHERE fact_key = ? AND tombstoned_at IS NULL",
            (fact_key,)).fetchall()
        # Winner = most trustworthy, never demoted: verified first, then highest
        # confirmation_count, then score, then newest id.
        winner = max(rows, key=lambda r: (1 if _verified(r[4], r[5]) else 0, r[2], r[3], r[0]))
        for _id, _rs, _cc, _s, _out, _cs in rows:
            if _id == winner[0] or _rs != "auto" or _verified(_out, _cs):
                continue
            conn.execute("UPDATE facts SET review_state='pending' WHERE id=?", (_id,))
            cont_n += 1

    conn.commit()
    return {"repetition_demoted": rep_n, "contradiction_demoted": cont_n}


def corpus_audit_stats(conn: sqlite3.Connection) -> dict:
    """Compute read-only corpus integrity signals over the facts table. Never writes."""
    def scalar(q, *a):
        row = conn.execute(q, a).fetchone()
        return (row[0] if row and row[0] is not None else 0)

    stats = {
        "live_facts": scalar("SELECT COUNT(*) FROM facts WHERE tombstoned_at IS NULL"),
        "human_verified": scalar(
            "SELECT COUNT(*) FROM facts WHERE tombstoned_at IS NULL AND confidence_source='human'"),
        "outcome_verified": scalar(
            "SELECT COUNT(*) FROM facts WHERE tombstoned_at IS NULL AND outcome IS NOT NULL AND outcome != 'rejected'"),
        "rejected": scalar("SELECT COUNT(*) FROM facts WHERE outcome='rejected'"),
        # corroborated by repeats, never outcome- or human-verified — the self-poison surface
        "repetition_unverified": scalar(
            "SELECT COUNT(*) FROM facts WHERE tombstoned_at IS NULL AND confirmation_count >= ? "
            "AND outcome IS NULL AND (confidence_source IS NULL OR confidence_source != 'human')",
            REPETITION_THRESHOLD),
        # same fact_key, more than one live fact → divergent claims under one key
        "contradiction_keys": scalar(
            "SELECT COUNT(*) FROM (SELECT fact_key FROM facts WHERE tombstoned_at IS NULL "
            "GROUP BY fact_key HAVING COUNT(*) > 1)"),
        "contradiction_facts": scalar(
            "SELECT COALESCE(SUM(c),0) FROM (SELECT COUNT(*) c FROM facts WHERE tombstoned_at IS NULL "
            "GROUP BY fact_key HAVING COUNT(*) > 1)"),
        "conflict_flagged": scalar(
            "SELECT COUNT(*) FROM facts WHERE tombstoned_at IS NULL AND conflict_with IS NOT NULL"),
    }
    # Outcome cross-ref, only if task_outcomes exists (avoids a write on a read-only conn).
    has_to = scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='task_outcomes'")
    if has_to:
        stats["task_outcomes"] = scalar("SELECT COUNT(*) FROM task_outcomes")
        rows = conn.execute(
            "SELECT COALESCE(scope,''), COUNT(*), COALESCE(SUM(success),0) "
            "FROM task_outcomes GROUP BY scope ORDER BY scope").fetchall()
        stats["outcome_by_scope"] = [
            {"scope": s, "total": t, "success": su, "rate": (su / t if t else 0.0)}
            for s, t, su in rows
        ]
    return stats


def cmd_corpus_audit(args):
    """Corpus integrity / self-poison audit — READ-ONLY by default (Phase 2, mutates nothing).

    Surfaces facts rewarded by repetition but never outcome/human-verified, contradiction
    clusters, and (with --candidates N) the prune candidates an enforcement pass would demote.
    With --enforce (or env CORPUS_AUDIT_ENFORCE) it also runs a DEMOTE-ONLY pass that
    reclassifies flagged 'auto' facts → 'pending' (reversible; confidence_source untouched).
    Usage: gaius corpus-audit [--json] [--samples N] [--candidates N] [--enforce]
    """
    p = argparse.ArgumentParser(prog="gaius corpus-audit")
    p.add_argument("--json", action="store_true")
    p.add_argument("--samples", type=int, default=5)
    p.add_argument("--candidates", type=int, default=0,
                   help="list the top-N repetition-only prune candidates (read-only)")
    p.add_argument("--enforce", action="store_true",
                   help="DEMOTE-ONLY: reclassify flagged 'auto' facts → 'pending' (also via "
                        "env CORPUS_AUDIT_ENFORCE). Reversible; default-off.")
    ns = p.parse_args(args)

    # ENFORCE MODE (flag-gated, DEFAULT-OFF): env CORPUS_AUDIT_ENFORCE or --enforce. DEMOTE-ONLY —
    # reclassify flagged 'auto' facts → 'pending' on a WRITABLE conn, THEN fall through to the
    # (unchanged) read-only audit so the printed stats reflect the post-enforce state. When
    # disabled this whole block is skipped and enforce_result stays None → byte-identical output.
    enforce_result = None
    if ns.enforce or os.environ.get("CORPUS_AUDIT_ENFORCE"):
        wconn = sqlite3.connect(str(DB_PATH))
        enforce_result = enforce_demote(wconn)
        wconn.close()

    # READ-ONLY connection — the default audit MUST NOT mutate the corpus (enforced at the SQLite layer).
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    stats = corpus_audit_stats(conn)
    samples = conn.execute(
        "SELECT fact_key, COUNT(*) c FROM facts WHERE tombstoned_at IS NULL "
        "GROUP BY fact_key HAVING c > 1 ORDER BY c DESC LIMIT ?", (ns.samples,)).fetchall()
    cands = repetition_candidates(conn, ns.candidates) if ns.candidates > 0 else []

    if ns.json:
        payload = {**stats,
                   "contradiction_samples": [{"fact_key": k, "count": c} for k, c in samples],
                   "repetition_candidates": cands}
        if enforce_result is not None:
            payload["enforce"] = enforce_result
        print(json.dumps(payload, indent=2))
        return

    live = stats["live_facts"] or 1
    pct = lambda n: f"{n / live * 100:.1f}%"
    print("\nCORPUS AUDIT (read-only shadow — Phase 2, mutates nothing)")
    print(f"  live facts:           {stats['live_facts']}")
    print(f"  human-verified:       {stats['human_verified']} ({pct(stats['human_verified'])})")
    print(f"  outcome-verified:     {stats['outcome_verified']} ({pct(stats['outcome_verified'])})")
    print(f"  ⚠ repetition-only:    {stats['repetition_unverified']} ({pct(stats['repetition_unverified'])})  — corroborated by REPEATS, never outcome/human-verified")
    print(f"  ⚠ contradiction:      {stats['contradiction_keys']} keys / {stats['contradiction_facts']} facts (same fact_key, divergent)")
    print(f"  conflict-flagged:     {stats['conflict_flagged']}")
    if "task_outcomes" in stats:
        print(f"  task_outcomes:        {stats['task_outcomes']}")
    if samples:
        print("  top contradiction keys:")
        for k, c in samples:
            print(f"    {c}x  {k}")
    if cands:
        print(f"  repetition-only prune candidates (top {len(cands)}, operator-gated to enforce):")
        for c in cands:
            print(f"    [{c['id']}] cc={c['confirmation_count']} {c['domain']}: {c['text']}")
    if enforce_result is not None:
        print(f"  ⚑ ENFORCE (demote-only, review_state auto→pending; reversible):")
        print(f"      repetition-only demoted:     {enforce_result['repetition_demoted']}")
        print(f"      contradiction-loser demoted: {enforce_result['contradiction_demoted']}  "
              f"(cluster max cc ≥ {CONTRADICTION_ENFORCE_MIN_CC})")
    print()


# --- Phase 3: gaius-grounded router (retrieval-augmented, read-only) ---
# Augments the keyword router (route_domains) with the ACTUAL corpus facts behind each domain
# + task_outcomes win-rates, so a route is grounded in own history and TRANSPARENT (returns the
# supporting facts) — vs Fugu's trained black box. Crucially flags how many supporting facts are
# UNVERIFIED (repetition-only), tying routing to the Phase 2 self-poison signal. Read-only;
# mutates nothing. The orchestrator adopting this for real routing is a later, flag-gated step.

def _corpus_domain_search(conn: sqlite3.Connection, query: str, limit: int = 8):
    """Cheap corpus-CONTENT retrieval signal: live facts whose text contains query terms,
    grouped by domain. BM25-lite over the corpus itself (not the hardcoded keyword map), so
    routing reflects what the corpus actually knows — covering the keyword router's blind spots.
    Read-only. Returns (domains_ranked, facts)."""
    terms = re.findall(r"[a-z0-9-]{4,}", query.lower())[:10]
    if not terms:
        return [], []
    where = " OR ".join(["LOWER(fact_text) LIKE ?"] * len(terms))
    rows = conn.execute(
        "SELECT id, domain, fact_text, confirmation_count, COALESCE(score,0), outcome, confidence_source "
        "FROM facts WHERE tombstoned_at IS NULL AND (" + where + ") "
        "ORDER BY COALESCE(score,0) DESC, confirmation_count DESC LIMIT ?",
        [f"%{t}%" for t in terms] + [limit]).fetchall()
    ranked = [d for d, _ in Counter(r[1] for r in rows).most_common()]
    facts = [{
        "id": r[0], "domain": r[1], "text": (r[2] or "")[:160],
        "confirmation_count": r[3], "score": r[4],
        "verified": bool(r[5] and r[5] != "rejected") or r[6] == "human",
    } for r in rows]
    return ranked, facts


def _entity_domain_votes(conn: sqlite3.Connection, query: str, limit: int = 5):
    """Entity-grounded routing signal: extract KG entities from the query, then vote
    domains by how many live facts mention those entities (fact_entities join).
    More precise than substring LIKE — 'drbd' votes storage because the corpus's
    drbd facts live there, not because a keyword map says so. Read-only.
    Returns ([], []) when fact_entities is absent/empty (pre-Gap-13 DBs)."""
    has_fe = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='fact_entities'").fetchone()[0]
    if not has_fe:
        return [], []
    from gaius.kg import extract_entities  # lazy: avoids import-order coupling
    ents = extract_entities(query)
    if not ents:
        return [], []
    ids = [e[0] for e in ents]
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT f.domain, COUNT(DISTINCT f.id) c FROM fact_entities fe JOIN facts f ON f.id = fe.fact_id "
        f"WHERE fe.entity_id IN ({ph}) AND f.tombstoned_at IS NULL AND f.domain != 'general' "
        "GROUP BY f.domain ORDER BY c DESC LIMIT ?", ids + [limit]).fetchall()
    return [{"domain": r[0], "fact_count": r[1]} for r in rows], ids


def route_suggest(conn: sqlite3.Connection, query: str, hint: str = None, max_facts: int = 5) -> dict:
    """Retrieval-augmented routing recommendation (read-only). Combines the keyword router with an
    entity-grounded KG signal and a corpus-content search so the route reflects what the corpus
    actually knows; returns supporting facts (each with a verified flag), how many are UNVERIFIED,
    and task_outcomes win-rates. Never writes."""
    kw_domains = route_domains(query, primary_hint=hint, max_files=3, max_chars=8000)
    corpus_domains, corpus_facts = _corpus_domain_search(conn, query, limit=max_facts)
    entity_domains, matched_entities = _entity_domain_votes(conn, query)

    # Prefer the keyword router when it hits (cheap, precise); then the entity-grounded
    # KG signal (corpus-derived term→domain mapping); then the corpus-content LIKE signal
    # (covers remaining blind spots); else the explicit hint.
    primary = (kw_domains[0]["domain"] if kw_domains
               else entity_domains[0]["domain"] if entity_domains
               else corpus_domains[0] if corpus_domains
               else hint)

    # Supporting facts: entity-linked facts FIRST (grounded via fact_entities —
    # a fact linked to the query's entities is evidence even when its text
    # shares no query substring), topped up with corpus-matched facts, then the
    # primary domain's top facts as a last resort. The 2026-07-03 audit showed
    # pure LIKE ranking buried the single most relevant fact for
    # "gemma4-31b OOM" below generic high-confirmation facts.
    facts = []
    if matched_entities:
        ph = ",".join("?" * len(matched_entities))
        rows = conn.execute(
            "SELECT DISTINCT f.id, f.domain, f.fact_text, f.confirmation_count, "
            "COALESCE(f.score,0), f.outcome, f.confidence_source "
            f"FROM facts f JOIN fact_entities fe ON fe.fact_id = f.id "
            f"WHERE fe.entity_id IN ({ph}) AND f.tombstoned_at IS NULL "
            "ORDER BY COALESCE(f.score,0) DESC, f.confirmation_count DESC LIMIT ?",
            matched_entities + [max_facts]).fetchall()
        facts = [{
            "id": r[0], "domain": r[1], "text": (r[2] or "")[:160],
            "confirmation_count": r[3], "score": r[4],
            "verified": bool(r[5] and r[5] != "rejected") or r[6] == "human",
        } for r in rows]
    seen_ids = {f["id"] for f in facts}
    facts += [f for f in corpus_facts if f["id"] not in seen_ids][:max(0, max_facts - len(facts))]
    if not facts and primary:
        rows = conn.execute(
            "SELECT id, fact_text, confirmation_count, COALESCE(score,0), outcome, confidence_source "
            "FROM facts WHERE domain = ? AND tombstoned_at IS NULL "
            "ORDER BY COALESCE(score,0) DESC, confirmation_count DESC LIMIT ?",
            (primary, max_facts)).fetchall()
        facts = [{
            "id": fid, "domain": primary, "text": (text or "")[:160],
            "confirmation_count": cc, "score": score,
            "verified": bool(outcome and outcome != "rejected") or csrc == "human",
        } for fid, text, cc, score, outcome, csrc in rows]

    winrates = []
    has_to = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='task_outcomes'").fetchone()[0]
    if has_to:
        rows = conn.execute(
            "SELECT COALESCE(scope,''), COUNT(*), COALESCE(SUM(success),0) "
            "FROM task_outcomes GROUP BY scope ORDER BY 3 DESC").fetchall()
        winrates = [{"scope": s, "total": t, "success": su, "rate": (su / t if t else 0.0)}
                    for s, t, su in rows]

    return {
        "query": query,
        "domains": kw_domains,
        "corpus_domains": corpus_domains,
        "entity_domains": entity_domains,
        "matched_entities": matched_entities,
        "primary_domain": primary,
        "supporting_facts": facts,
        "unverified_supporting": sum(1 for f in facts if not f["verified"]),
        "outcome_winrates": winrates,
    }


def cmd_route_suggest(args):
    """Retrieval-augmented routing recommendation grounded in corpus facts (read-only).

    Unlike 'route' (keyword domains only), this returns the supporting facts + how many are
    UNVERIFIED + outcome win-rates. Usage: gaius route-suggest <task...> [--hint D] [--json]
    """
    p = argparse.ArgumentParser(prog="gaius route-suggest")
    p.add_argument("query", nargs="+", help="task / query text to route")
    p.add_argument("--hint", default=None, help="primary domain hint")
    p.add_argument("--max-facts", type=int, default=5)
    p.add_argument("--json", action="store_true")
    ns = p.parse_args(args)
    query = " ".join(ns.query)

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)  # read-only: routing never mutates
    res = route_suggest(conn, query, hint=ns.hint, max_facts=ns.max_facts)

    if ns.json:
        print(json.dumps(res, indent=2))
        return
    print(f"\nROUTE: {query[:100]}{'...' if len(query) > 100 else ''}")
    print(f"  primary domain: {res['primary_domain']}")
    if res["domains"]:
        print("  candidates: " + ", ".join(f"{d['domain']}({d['score']:.2f})" for d in res["domains"]))
    if res["entity_domains"]:
        ents = ", ".join(res["matched_entities"][:6])
        votes = ", ".join(f"{d['domain']}({d['fact_count']})" for d in res["entity_domains"])
        print(f"  entity signal: [{ents}] → {votes}")
    if res["supporting_facts"]:
        print(f"  supporting facts ({res['unverified_supporting']}/{len(res['supporting_facts'])} UNVERIFIED — repetition-only):")
        for f in res["supporting_facts"]:
            mark = "✓" if f["verified"] else "⚠"
            print(f"    {mark} [{f['id']}] {f['text']}")
    if res["outcome_winrates"]:
        print("  scope win-rates (from task_outcomes):")
        for w in res["outcome_winrates"][:5]:
            print(f"    {w['scope'] or '(none)'}: {w['success']}/{w['total']} ({w['rate']*100:.0f}%)")
    print()
