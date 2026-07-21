"""gaius.maturity — fact-maturity / training-readiness scoring + CLI.

Read-only analytics over facts.db: the maturity score, the readiness commands
(``maturity`` / ``readiness`` / ``snapshot`` / ``governor`` / ``route``), and the
scoring weight tables (PROVENANCE_WEIGHT, OUTCOME_MODIFIER, SOURCE_RELIABILITY,
CROSS_MODEL_MULTIPLIER, NO_DECAY_PROVENANCES, MATURITY_BOOTSTRAP_MIN). Those
tables live here but are re-exported by gaius/_core.py because cmd_decay /
cmd_rescore (resident in _core) consume them.

Facade convention (see ARCHITECTURE.md): shared helpers imported from gaius._core
at top; _core re-imports this module's public symbols before the COMMANDS dict.
"""
import argparse
import glob
import json
import math
import sys
from datetime import datetime, timezone

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

# imports from gaius._core (shared hub) — circular-by-design, see ARCHITECTURE.md
from gaius._core import (
    init_db, load_domain_specs, DOMAIN_KEYWORDS, READINESS_THRESHOLDS,
    DEFAULT_READINESS, route_domains, SPECS_DIR, HAS_YAML,
)


PROVENANCE_WEIGHT = {
    "automated":           0.7,
    "auto-mined":          0.6,   # machine-mined from session JSONLs, unreviewed (78% of corpus)
    "structured_reasoning": 0.8,
    "compaction":          0.9,
    "distillation":        0.85,  # relay agent output — structured, validated intent
    "finding":             1.0,
    "procedure":           0.9,
    "mcp-session":         0.8,   # agent deliberately recorded mid-session
    "human_reviewed":      1.0,
}

# Provenances exempt from time decay (permanent records)
NO_DECAY_PROVENANCES = frozenset(["finding", "procedure"])

OUTCOME_MODIFIER = {
    "confirmed":      1.2,
    "open_question":  0.7,
    "refuted":        0.3,
    None:             1.0,
}

MATURITY_BOOTSTRAP_MIN = 20   # need at least this many facts to compute score

# Cross-model confirmation multiplier — applied when the same signal unit has been
# independently confirmed by agents on architecturally distinct model families.
# Three-tier hierarchy:
#   single-session baseline  <  cold-start same-model convergence  <  cross-model (this)
# Configurable: adjust here without touching scoring logic.
CROSS_MODEL_MULTIPLIER = 1.5

# Source reliability — discounts autonomous (machine-generated) facts to prevent
# hallucination reinforcement loops (G1). Domain files are curated ground truth.
SOURCE_RELIABILITY = {
    "autonomous": 0.7,   # machine-generated (briefing CronJob, Tier 2 triage)
    "human":      1.0,   # interactive sessions (default, backward compat)
    "domain":     1.2,   # curated domain/*.md files
}

# Review-state → inject-rank multiplier (registry; the canonical source consumed by
# the landscape ranker's review-state penalty). The human review verb is empirically
# DEAD (1 of ~17,637 injectable facts ever human-confirmed, verified 2026-07-21), so
# review_state is a QUALITY signal, not a human gate:
#   auto            1.0  — de-facto default injectable tier
#   confirmed       1.0  — human-confirmed (rare); no extra inject boost (removal verb is 'reject')
#   pending         0.6  — low-confidence / contradiction-flagged at ingest; still injects, demoted
#   deferred        0.6  — reviewer punted; MUST keep the penalty (punting must not REWARD a shaky fact)
#   agent-reviewed  0.6  — machine-drained from the pending queue by the mnemos surgeon.
#       Weighted ≤ auto and NEVER above pending: an LLM with a completion incentive and no
#       live-state access would rubber-stamp contradiction-flagged facts upward, so until
#       outcome-grounding lands, agent-review is queue-hygiene + reject only — never a rank boost.
# Any state absent here (or NULL) ranks at 1.0 (unpenalized), so this table is additive:
# no fact carries review_state='agent-reviewed' until the opt-in `gaius agent-review` verb
# sets it, so the new row is INERT on today's corpus (default-off by construction).
REVIEW_STATE_WEIGHT = {
    "auto":           1.0,
    "confirmed":      1.0,
    "pending":        0.6,
    "deferred":       0.6,
    "agent-reviewed": 0.6,
}


def _maturity_score(facts: list[dict], decay_rate: float = 0.005) -> float:
    """Compute a [0,1] maturity score for a list of fact rows.

    Formula: sum(confirmation_count × decay × provenance_weight × outcome_modifier × cross_model)
             ──────────────────────────────────────────────────────────────────────────────────────
             total_facts

    decay = exp(-rate × age_days), where age_days is days since first_seen.
    cross_model = CROSS_MODEL_MULTIPLIER when model_families has 2+ distinct families,
                  1.0 otherwise.
    """
    import math
    now = datetime.now(timezone.utc)
    total = len(facts)
    if total < MATURITY_BOOTSTRAP_MIN:
        return 0.0

    weighted_sum = 0.0
    for row in facts:
        conf  = max(1, row["confirmation_count"])
        prov_key = row["provenance"] if row["provenance"] else "automated"
        prov  = PROVENANCE_WEIGHT.get(prov_key, 0.5)
        out   = OUTCOME_MODIFIER.get(row["outcome"], 1.0)
        try:
            first_seen = datetime.fromisoformat(row["first_seen"])
            if first_seen.tzinfo is None:
                first_seen = first_seen.replace(tzinfo=timezone.utc)
            age_days = (now - first_seen).total_seconds() / 86400
        except (TypeError, ValueError):
            age_days = 0.0
        # Exempt certain provenances from time decay (permanent records)
        if prov_key in NO_DECAY_PROVENANCES:
            decay = 1.0
        else:
            decay = math.exp(-decay_rate * age_days)
        # Cross-model confirmation multiplier
        try:
            families = json.loads(row["model_families"] or '["claude"]')
            cross_mult = CROSS_MODEL_MULTIPLIER if len(set(families)) >= 2 else 1.0
        except (TypeError, ValueError, KeyError):
            cross_mult = 1.0
        # Source reliability (G1: discount autonomous, boost curated)
        source_mult = SOURCE_RELIABILITY.get(row["source"] or "human", 1.0)
        weighted_sum += conf * decay * prov * out * cross_mult * source_mult

    raw = weighted_sum / total
    # Normalise to [0,1]: clamp rather than divide (avoids requiring a max baseline)
    return min(1.0, raw)


def cmd_maturity(args):
    """Print per-domain maturity scores derived from facts.db."""
    import argparse as _ap
    parser = _ap.ArgumentParser(prog="gaius maturity")
    parser.add_argument("--domain", type=str, default=None,
                        help="Show detail for a single domain")
    parsed = parser.parse_args(args)

    conn = init_db()
    domain_specs = load_domain_specs()

    # Fetch all facts grouped by domain (exclude training_excluded facts)
    rows = conn.execute(
        "SELECT domain, confirmation_count, provenance, outcome, first_seen, model_families, source FROM facts WHERE COALESCE(training_excluded, 0) = 0 ORDER BY domain"
    ).fetchall()

    by_domain: dict[str, list] = {}
    for row in rows:
        by_domain.setdefault(row["domain"], []).append(row)

    # Load full YAML specs (for maturity_decay_rate) separately from keyword index
    full_specs: dict[str, dict] = {}
    if HAS_YAML and SPECS_DIR.exists():
        for spec_file in SPECS_DIR.glob("*.yaml"):
            try:
                with open(spec_file) as f:
                    spec = yaml.safe_load(f)
                if isinstance(spec, dict):
                    full_specs[spec.get("domain", spec_file.stem)] = spec
            except Exception:
                pass

    if parsed.domain:
        domains = [parsed.domain] if parsed.domain in by_domain else []
    else:
        domains = sorted(by_domain.keys())

    if not domains:
        print("No facts found." if not by_domain else f"Domain '{parsed.domain}' not found.")
        return

    header = f"{'Domain':<22} {'Facts':>6}  {'Score':>6}  {'Maturity':>10}"
    print(header)
    print("─" * len(header))

    for domain in domains:
        facts = by_domain[domain]
        spec  = full_specs.get(domain, {})
        rate  = spec.get("maturity_decay_rate", 0.005)
        score = _maturity_score(facts, decay_rate=rate)
        n     = len(facts)
        bar_len = int(score * 20)
        bar   = "█" * bar_len + "░" * (20 - bar_len)
        status = "▲ LIVE" if score >= 0.45 else ("~ warm" if score >= 0.25 else "· cold")
        if n < MATURITY_BOOTSTRAP_MIN:
            status = f"  ({n} facts, need {MATURITY_BOOTSTRAP_MIN})"
            bar    = "░" * 20
            score  = 0.0
        print(f"{domain:<22} {n:>6}  {score:>6.3f}  {bar}  {status}")

    print()
    print(f"Total facts: {sum(len(v) for v in by_domain.values())}")
    if not parsed.domain:
        alive = sum(1 for d in domains
                    if len(by_domain[d]) >= MATURITY_BOOTSTRAP_MIN
                    and _maturity_score(by_domain[d],
                                        full_specs.get(d, {}).get("maturity_decay_rate", 0.005)) >= 0.45)
        print(f"Live domains (score ≥ 0.45): {alive}/{len(domains)}")


def cmd_readiness(args):
    """Show domain training readiness against thresholds."""
    conn = init_db()
    
    # Query domain scores
    rows = conn.execute("SELECT domain, confirmation_count, provenance, outcome, first_seen, model_families FROM facts ORDER BY domain").fetchall()
    by_domain = {}
    for row in rows:
        by_domain.setdefault(row["domain"], []).append(row)

    # Load specs for decay rates
    full_specs = {}
    if HAS_YAML and SPECS_DIR.exists():
        for spec_file in SPECS_DIR.glob("*.yaml"):
            try:
                with open(spec_file) as f:
                    spec = yaml.safe_load(f)
                if isinstance(spec, dict):
                    full_specs[spec.get("domain", spec_file.stem)] = spec
            except Exception:
                pass

    header = f"{'Domain':<22} {'Facts':>6} {'Score':>6} {'Status':<10} {'Threshold'}"
    print(header)
    print("─" * len(header))

    domains = sorted(DOMAIN_KEYWORDS.keys())
    for domain in domains:
        facts = by_domain.get(domain, [])
        n = len(facts)
        
        spec = full_specs.get(domain, {})
        rate = spec.get("maturity_decay_rate", 0.005)
        score = _maturity_score(facts, decay_rate=rate) if n >= MATURITY_BOOTSTRAP_MIN else 0.0
        
        thresh = READINESS_THRESHOLDS.get(domain, DEFAULT_READINESS)
        t_score = thresh["score"]
        t_facts = thresh["min_facts"]
        
        status = "NOT READY"
        if score >= t_score and n >= t_facts:
            status = "▲ READY"
        elif score >= t_score * 0.8 and n >= t_facts * 0.8:
            status = "~ MARGINAL"
            
        print(f"{domain:<22} {n:>6} {score:>6.3f} {status:<10} (score ≥ {t_score}, facts ≥ {t_facts})")

def cmd_snapshot(args):
    """Output a maturity + readiness snapshot as JSON for observatory telemetry.

    Writes to stdout (or --output FILE). Designed to be run from a CronJob:
      python gaius snapshot --json | curl -X POST .../observatory/maturity/ingest
    """
    import argparse as _ap
    parser = _ap.ArgumentParser(prog="gaius snapshot")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Output file path (default: stdout)")
    parsed = parser.parse_args(args)

    conn = init_db()
    rows = conn.execute(
        "SELECT domain, confirmation_count, provenance, outcome, first_seen, model_families, source FROM facts ORDER BY domain"
    ).fetchall()
    by_domain = {}
    for row in rows:
        by_domain.setdefault(row["domain"], []).append(row)

    full_specs = {}
    if HAS_YAML and SPECS_DIR.exists():
        for spec_file in SPECS_DIR.glob("*.yaml"):
            try:
                with open(spec_file) as f:
                    spec = yaml.safe_load(f)
                if isinstance(spec, dict):
                    full_specs[spec.get("domain", spec_file.stem)] = spec
            except Exception:
                pass

    all_domains = sorted(set(list(by_domain.keys()) + list(DOMAIN_KEYWORDS.keys())))
    total_facts = sum(len(v) for v in by_domain.values())
    live_count = 0
    domains_out = []

    for domain in all_domains:
        facts = by_domain.get(domain, [])
        n = len(facts)
        spec = full_specs.get(domain, {})
        rate = spec.get("maturity_decay_rate", 0.005)
        score = _maturity_score(facts, decay_rate=rate) if n >= MATURITY_BOOTSTRAP_MIN else 0.0

        thresh = READINESS_THRESHOLDS.get(domain, DEFAULT_READINESS)
        t_score = thresh["score"]
        t_facts = thresh["min_facts"]
        ready = score >= t_score and n >= t_facts

        if score >= 0.45 and n >= MATURITY_BOOTSTRAP_MIN:
            status = "live"
            live_count += 1
        elif score >= 0.25 and n >= MATURITY_BOOTSTRAP_MIN:
            status = "warm"
        elif n >= MATURITY_BOOTSTRAP_MIN:
            status = "cold"
        else:
            status = "bootstrap"

        # Compute per-domain raw stats for client-side recomputation
        provenance_counts = {}
        total_conf = 0
        total_age = 0.0
        cross_model_count = 0
        now_snap = datetime.now(timezone.utc)
        for row in facts:
            prov_key = row["provenance"] if row["provenance"] else "automated"
            provenance_counts[prov_key] = provenance_counts.get(prov_key, 0) + 1
            total_conf += max(1, row["confirmation_count"])
            try:
                fs = datetime.fromisoformat(row["first_seen"])
                if fs.tzinfo is None:
                    fs = fs.replace(tzinfo=timezone.utc)
                total_age += (now_snap - fs).total_seconds() / 86400
            except (TypeError, ValueError):
                pass
            try:
                families = json.loads(row["model_families"] or '["claude"]')
                if len(set(families)) >= 2:
                    cross_model_count += 1
            except (TypeError, ValueError, KeyError):
                pass

        domains_out.append({
            "domain": domain,
            "facts": n,
            "score": round(score, 4),
            "status": status,
            "ready": ready,
            "threshold_score": t_score,
            "threshold_facts": t_facts,
            "provenance_counts": provenance_counts,
            "mean_age_days": round(total_age / n, 2) if n > 0 else 0,
            "avg_confirmation": round(total_conf / n, 3) if n > 0 else 0,
            "cross_model_frac": round(cross_model_count / n, 4) if n > 0 else 0,
        })

    snapshot = {
        "snapshot_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "snapshot_ts": datetime.now(timezone.utc).isoformat(),
        "gaius_version": "2",
        "total_facts": total_facts,
        "live_domains": live_count,
        "domains": domains_out,
    }

    output = json.dumps(snapshot, indent=2)
    if parsed.output:
        with open(parsed.output, "w") as f:
            f.write(output)
        print(f"Snapshot written to {parsed.output} ({total_facts} facts, {live_count} live domains)",
              file=sys.stderr)
    else:
        print(output)


def cmd_governor(args):
    """Show knowledge gap analysis: which principals have confirmed which facts.

    Four states per fact:
      both-confirm   — both principals confirmed (high confidence)
      primary-only   — only principal-a confirmed (gap: principal-b hasn't seen this)
      secondary-only — only principal-b confirmed (gap: principal-a hasn't seen this)
      both-disagree  — reserved; requires content comparison (TBD)
    """
    import argparse as _ap
    parser = _ap.ArgumentParser(prog="gaius governor")
    parser.add_argument("--domain", type=str, default=None,
                        help="Filter to a single domain")
    parser.add_argument("--state", type=str, default=None,
                        choices=["both-confirm", "primary-only", "secondary-only"],
                        help="Filter to a specific state")
    parser.add_argument("--principal-a", type=str, default="operator",
                        help="Primary principal name (default: operator)")
    parser.add_argument("--principal-b", type=str, default="gemini",
                        help="Secondary principal name (default: gemini)")
    parser.add_argument("--limit", type=int, default=20,
                        help="Max facts to show per state (default: 20)")
    parsed = parser.parse_args(args)

    pa = parsed.principal_a
    pb = parsed.principal_b

    conn = init_db()

    domain_clause = "AND domain = ?" if parsed.domain else ""
    domain_params = (parsed.domain,) if parsed.domain else ()

    def fetch_state(label, where_clause, params):
        sql = f"""
            SELECT domain, fact_key, fact_text, principals, confirmation_count, score
            FROM facts
            WHERE (outcome IS NULL OR outcome != 'rejected')
            {domain_clause}
            AND {where_clause}
            ORDER BY domain, score DESC
            LIMIT ?
        """
        return conn.execute(sql, domain_params + params + (parsed.limit,)).fetchall()

    states = {
        "both-confirm": (
            f"principals LIKE '%\"{pa}\"%' AND principals LIKE '%\"{pb}\"%'",
            ()
        ),
        "primary-only": (
            f"principals LIKE '%\"{pa}\"%' AND principals NOT LIKE '%\"{pb}\"%'",
            ()
        ),
        "secondary-only": (
            f"principals NOT LIKE '%\"{pa}\"%' AND principals LIKE '%\"{pb}\"%'",
            ()
        ),
    }

    if parsed.state:
        states_to_show = {parsed.state: states[parsed.state]}
    else:
        states_to_show = states

    for state_label, (where, params) in states_to_show.items():
        rows = fetch_state(state_label, where, params)
        count_sql = f"""
            SELECT COUNT(*) FROM facts
            WHERE (outcome IS NULL OR outcome != 'rejected')
            {domain_clause}
            AND {where}
        """
        total = conn.execute(count_sql, domain_params + params).fetchone()[0]

        print(f"\n── {state_label} ({total} facts) {'─' * max(0, 50 - len(state_label) - len(str(total)) - 12)}")
        if not rows:
            print("  (none)")
            continue
        for row in rows:
            principals_list = json.loads(row["principals"] or "[]")
            p_str = ",".join(principals_list) if principals_list else "?"
            print(f"  [{row['domain']}] {row['fact_key'][:60]}  ({p_str})  score={row['score']:.2f}")
            if parsed.state:
                # Show fact text when filtered to one state
                print(f"    {row['fact_text'][:120]}")

    print()
    print("Note: both-disagree state requires content comparison — not yet implemented.")
    print("Open question per council: route automatically to council log or surface to governors first?")


def cmd_route(args):
    """Route a query to relevant domain files for RAG injection."""
    import argparse
    parser = argparse.ArgumentParser(prog="gaius route",
                                     description="Route a query to domain context files")
    parser.add_argument("query", nargs="+", help="Query text to route")
    parser.add_argument("--hint", type=str, default=None,
                        help="Primary domain hint (e.g. from question metadata)")
    parser.add_argument("--max-files", type=int, default=3)
    parser.add_argument("--max-chars", type=int, default=10000)
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parsed = parser.parse_args(args)

    query = " ".join(parsed.query)
    results = route_domains(query, primary_hint=parsed.hint,
                            max_files=parsed.max_files, max_chars=parsed.max_chars)

    if parsed.json:
        print(json.dumps(results, indent=2))
        return

    if not results:
        print("No domain matches found.")
        return

    print(f"Query: {query[:100]}{'...' if len(query) > 100 else ''}")
    if parsed.hint:
        print(f"Hint:  {parsed.hint}")
    print()
    for i, r in enumerate(results):
        tag = " (primary)" if i == 0 else ""
        print(f"  {r['domain']:<16} score={r['score']:.3f}  budget={r['budget']}c{tag}")
