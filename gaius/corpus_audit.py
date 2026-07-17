"""gaius.corpus_audit — read-only corpus integrity + retrieval-augmented routing.

Repetition/prune candidates, self-poison audit signals, corpus content search,
and ``route_suggest`` (keyword router + corpus + win-rates). Read-only analytics
surface over facts.db. Provides the ``corpus-audit`` and ``route-suggest`` commands.

Facade convention (see ARCHITECTURE.md): shared helpers imported from gaius._core
at top; _core re-imports this module's public symbols before the COMMANDS dict.
"""
import argparse
import json
import re
import sqlite3
from collections import Counter

# imports from gaius._core (shared hub) — circular-by-design, see ARCHITECTURE.md
from gaius._core import DB_PATH, route_domains


# READ-ONLY. Surfaces the risk the operator named: facts corroborated by REPETITION but never
# outcome- or human-verified, plus contradiction clusters (same fact_key, divergent live facts).
# This is the shadow half of outcome-grounding — it MUTATES NOTHING. Enforcement (demote/
# tombstone) is a later, operator-gated step once the retrieval->outcome linkage exists.

REPETITION_THRESHOLD = 2  # confirmation_count at/above which a fact is "rewarded by repetition"


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
    """Read-only corpus integrity / self-poison shadow audit (Phase 2 — mutates nothing).

    Surfaces facts rewarded by repetition but never outcome/human-verified, contradiction
    clusters, and (with --candidates N) the prune candidates an operator-gated enforcement pass
    would demote. Usage: gaius corpus-audit [--json] [--samples N] [--candidates N]
    """
    p = argparse.ArgumentParser(prog="gaius corpus-audit")
    p.add_argument("--json", action="store_true")
    p.add_argument("--samples", type=int, default=5)
    p.add_argument("--candidates", type=int, default=0,
                   help="list the top-N repetition-only prune candidates (read-only)")
    ns = p.parse_args(args)

    # READ-ONLY connection — Phase 2 shadow MUST NOT mutate the corpus (enforced at the SQLite layer).
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    stats = corpus_audit_stats(conn)
    samples = conn.execute(
        "SELECT fact_key, COUNT(*) c FROM facts WHERE tombstoned_at IS NULL "
        "GROUP BY fact_key HAVING c > 1 ORDER BY c DESC LIMIT ?", (ns.samples,)).fetchall()
    cands = repetition_candidates(conn, ns.candidates) if ns.candidates > 0 else []

    if ns.json:
        print(json.dumps({**stats,
                          "contradiction_samples": [{"fact_key": k, "count": c} for k, c in samples],
                          "repetition_candidates": cands}, indent=2))
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
