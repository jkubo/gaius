#!/usr/bin/env python3
"""gaius MCP Server — mid-session memory access for Claude Code.

Install: claude mcp add gaius -- python3 -m gaius.mcp_server

Tools (read):
  gaius_search         — hybrid keyword + semantic search across facts.db
  gaius_kg_query       — query entity relationships in the knowledge graph
  gaius_kg_timeline    — chronological story of an entity
  gaius_stats          — facts.db overview (counts, domains, embeddings)
  gaius_prime_session  — load session-type behavioral priming (fighter/trader/etc.)
  gaius_skill_recommend — score and recommend skills for a task + active files

Tools (write):
  gaius_fact_add       — record a fact during session (direct to facts.db, skips staging)
"""

import json
import math
import os
import re
import sqlite3
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ── Import gaius internals ───────────────────────────────────────────────────
# When run as `python3 -m gaius.mcp_server` or installed via pip, gaius is
# already importable. For dev runs (python3 gaius/mcp_server.py), add the
# repo root (parent of the gaius/ package dir) to sys.path.
_pkg_root = Path(__file__).parent.parent
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

from gaius._core import (  # noqa: E402
    DB_PATH,
    SKILLS_DIR,
    _EMBED_DIM as EMBED_DIM,
    _embed_text as _embed,
    _parse_frontmatter,
    load_skills,
    compute_skill_score,
)


def _get_db():
    """Get a read-only connection to facts.db with sqlite-vec loaded."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (ImportError, Exception):
        pass
    conn.row_factory = sqlite3.Row
    return conn


# ── MCP Server ───────────────────────────────────────────────────────────────

mcp = FastMCP("gaius", instructions="""
gaius is an ops memory lifecycle manager for AI coding agents.
It stores facts extracted from past Claude Code and Gemini CLI sessions.
Use gaius_search to find relevant facts by meaning (semantic) or keywords.
Use gaius_kg_query to explore entity relationships (nodes, services, incidents).
Use gaius_fact_add to record new facts discovered during this session.
""")


@mcp.tool()
def gaius_search(query: str, domain: str = "", limit: int = 10) -> str:
    """Search facts.db using hybrid keyword + semantic scoring.

    Args:
        query: Natural language search query (e.g., "DRBD split brain recovery")
        domain: Optional domain filter (storage, networking, security, etc.)
        limit: Max results to return (default 10)
    """
    conn = _get_db()

    # Keyword search
    terms = re.sub(r'[^\w\s]', ' ', query.lower()).split()
    facts = conn.execute(
        "SELECT id, domain, fact_text, score, first_seen, last_seen FROM facts "
        "WHERE tombstoned_at IS NULL AND (outcome IS NULL OR outcome != 'rejected')"
    ).fetchall()

    scored = []
    for fact in facts:
        if domain and fact["domain"] != domain:
            continue
        text = (fact["fact_text"] or "").lower()
        kw_score = sum(1 for t in terms if t in text) / max(len(terms), 1)
        scored.append({"fact": dict(fact), "kw_score": kw_score, "sem_score": 0.0})

    # Semantic search via sqlite-vec KNN
    query_vec = _embed(query)
    if query_vec:
        vec_blob = struct.pack(f'{EMBED_DIM}f', *query_vec)
        try:
            knn_sql = """
                SELECT fe.fact_id, fe.distance, f.id, f.domain, f.fact_text, f.score, f.first_seen, f.last_seen
                FROM fact_embeddings fe
                JOIN facts f ON f.id = fe.fact_id
                WHERE fe.embedding MATCH ? AND k = ?
                  AND f.tombstoned_at IS NULL
            """
            params = [vec_blob, limit * 3]
            rows = conn.execute(knn_sql, params).fetchall()
            sem_by_id = {}
            for row in rows:
                l2_dist = row[1]
                cosine_sim = max(0, 1.0 - (l2_dist ** 2 / 2.0))
                sem_by_id[row[2]] = cosine_sim

            for item in scored:
                fid = item["fact"]["id"]
                if fid in sem_by_id:
                    item["sem_score"] = sem_by_id[fid]
        except Exception:
            pass

    # Hybrid score: RRF-style blend with semantic floor
    # Facts with sem_score < 0.3 are penalized to avoid surfacing irrelevant boilerplate
    for item in scored:
        sem = item["sem_score"]
        if sem > 0 and sem < 0.3:
            item["hybrid"] = 0.4 * item["kw_score"] * 0.1  # heavily penalize
        else:
            item["hybrid"] = 0.4 * item["kw_score"] + 0.6 * sem

    scored.sort(key=lambda x: x["hybrid"], reverse=True)
    top = scored[:limit]

    # Format results
    results = []
    for i, item in enumerate(top):
        f = item["fact"]
        if item["hybrid"] <= 0:
            continue
        results.append(
            f"**[{i+1}]** (domain: {f['domain']}, score: {item['hybrid']:.3f})\n"
            f"{f['fact_text'][:300]}\n"
            f"_First seen: {(f['first_seen'] or '')[:10]}, Last seen: {(f['last_seen'] or '')[:10]}_"
        )

    if not results:
        return f"No facts found matching '{query}'" + (f" in domain '{domain}'" if domain else "")

    return f"## gaius search: {query}\n\n" + "\n\n".join(results)


@mcp.tool()
def gaius_kg_query(entity: str) -> str:
    """Query the knowledge graph for an entity's relationships.

    Args:
        entity: Entity name or partial match (e.g., "api-server", "worker-node", "k8s-cluster")
    """
    conn = _get_db()
    term = entity.lower()
    entities = conn.execute(
        "SELECT id, name, type, domain FROM entities WHERE id LIKE ? OR name LIKE ?",
        (f"%{term}%", f"%{term}%")
    ).fetchall()

    if not entities:
        return f"No entities matching '{entity}'"

    parts = []
    for ent in entities[:5]:  # limit to 5 matches
        lines = [f"**{ent[1]}** ({ent[2]}, domain: {ent[3] or '?'})"]
        # Outgoing
        for t in conn.execute(
            "SELECT predicate, object, valid_from, valid_to FROM triples WHERE subject = ? ORDER BY valid_from",
            (ent[0],)
        ).fetchall():
            ended = f" (ended {t[3][:10]})" if t[3] else ""
            since = f" since {t[2][:10]}" if t[2] else ""
            lines.append(f"  → {t[0]} {t[1]}{since}{ended}")
        # Incoming
        for t in conn.execute(
            "SELECT subject, predicate, valid_from, valid_to FROM triples WHERE object = ? ORDER BY valid_from",
            (ent[0],)
        ).fetchall():
            ended = f" (ended {t[3][:10]})" if t[3] else ""
            since = f" since {t[2][:10]}" if t[2] else ""
            lines.append(f"  ← {t[0]} {t[1]}{since}{ended}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


@mcp.tool()
def gaius_kg_timeline(entity: str) -> str:
    """Get a chronological timeline for an entity.

    Args:
        entity: Entity name or partial match
    """
    conn = _get_db()
    term = entity.lower()
    entities = conn.execute(
        "SELECT id, name, type FROM entities WHERE id LIKE ? OR name LIKE ?",
        (f"%{term}%", f"%{term}%")
    ).fetchall()

    if not entities:
        return f"No entities matching '{entity}'"

    eid = entities[0][0]
    events = conn.execute("""
        SELECT valid_from, predicate, object, valid_to, source_agent, 'out' as dir FROM triples WHERE subject = ?
        UNION ALL
        SELECT valid_from, predicate, subject, valid_to, source_agent, 'in' as dir FROM triples WHERE object = ?
        ORDER BY valid_from NULLS LAST
    """, (eid, eid)).fetchall()

    if not events:
        return f"No timeline events for {entities[0][1]}"

    lines = [f"## Timeline: {entities[0][1]} ({entities[0][2]})\n"]
    for ev in events:
        date = ev[0][:10] if ev[0] else "????"
        arrow = "→" if ev[5] == "out" else "←"
        ended = f" (ended {ev[3][:10]})" if ev[3] else ""
        agent = f" [{ev[4]}]" if ev[4] else ""
        lines.append(f"  {date}  {arrow} {ev[1]} {ev[2]}{ended}{agent}")

    return "\n".join(lines)


@mcp.tool()
def gaius_stats() -> str:
    """Get an overview of the gaius facts database — counts, domains, embeddings, KG stats."""
    conn = _get_db()
    total = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM facts WHERE tombstoned_at IS NULL AND (outcome IS NULL OR outcome != 'rejected')").fetchone()[0]
    domains = conn.execute("SELECT domain, COUNT(*) c FROM facts GROUP BY domain ORDER BY c DESC").fetchall()

    parts = [
        f"## gaius facts.db",
        f"Total facts: {total} ({active} active)",
        "",
        "Domains:",
    ]
    for d in domains:
        parts.append(f"  {d[0]:<20} {d[1]:>5}")

    # Embeddings
    try:
        embedded = conn.execute("SELECT COUNT(*) FROM fact_embeddings").fetchone()[0]
        parts.append(f"\nEmbeddings: {embedded}/{total} ({100*embedded//max(total,1)}%)")
    except Exception:
        parts.append("\nEmbeddings: not available")

    # KG
    try:
        n_entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        n_triples = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        parts.append(f"Knowledge Graph: {n_entities} entities, {n_triples} triples")
    except Exception:
        parts.append("Knowledge Graph: not initialized")

    return "\n".join(parts)


@mcp.tool()
def gaius_fact_add(fact_text: str, domain: str, source: str = "session") -> str:
    """Record a new fact directly to facts.db. Use for important discoveries during a session.

    Args:
        fact_text: The fact to record (be concise — one sentence or short paragraph)
        domain: Domain category (storage, networking, security, services, general, etc.)
        source: Source label (default: "session")
    """
    import hashlib
    from gaius._core import init_db, upsert_fact, _is_noise

    # Single ingest pipeline: noise filter, semantic dedup, confidence scoring,
    # race-safe insert all live in upsert_fact — this tool used to be a second
    # divergent copy that skipped the noise filter and confidence scorer.
    if _is_noise(fact_text):
        return "Rejected by noise filter — looks like boilerplate/navigation, not a durable fact."

    conn = init_db()
    fact_key = hashlib.sha256(fact_text.encode()).hexdigest()[:32]
    existing = conn.execute(
        "SELECT id, confirmation_count FROM facts WHERE fact_key = ? AND tombstoned_at IS NULL",
        (fact_key,)
    ).fetchone()

    upsert_fact(conn, domain, fact_key, fact_text,
                agent="operator", session_uuid=source or "mcp",
                provenance="mcp-session", score=0.6, source=source)

    if existing:
        return (f"Fact already exists (id: {existing['id']}) — corroborated "
                f"(confirmations: {existing['confirmation_count'] + 1}).")
    row = conn.execute(
        "SELECT id FROM facts WHERE fact_key = ? AND tombstoned_at IS NULL",
        (fact_key,)
    ).fetchone()
    if row:
        return f"Fact recorded (id: {row['id']}, domain: {domain}). Will appear in future inject outputs."
    return "Semantically similar fact found — corroboration merged instead of inserting a duplicate."


@mcp.tool()
def gaius_prime_session(session_type: str) -> str:
    """Load a session-type behavioral priming skill.

    Session types change HOW you reason, not just WHAT you know.
    Each type has a distinct mindset, pre-action checklist, and completion standard.

    Args:
        session_type: One of: ops (ops triage), quant (trading/execution),
                      malware (security/malware), pentest (red team),
                      audit (security audits), console (UI), mnemos (memory maintenance)
    """
    valid_types = ["ops", "quant", "malware", "pentest", "audit", "console", "mnemos"]
    session_type = session_type.lower().strip()
    if session_type not in valid_types:
        return f"Unknown session type '{session_type}'. Valid types: {', '.join(valid_types)}"

    skill_file = SKILLS_DIR / f"{session_type}.md"
    if not skill_file.exists():
        return f"Skill file not found: {skill_file}"

    text = skill_file.read_text()
    fm, body = _parse_frontmatter(text)

    # Include also_load dependencies
    also_load = fm.get("also_load", "")
    deps = []
    if also_load:
        dep_names = [also_load] if isinstance(also_load, str) else also_load
        for dep_name in dep_names:
            dep_file = SKILLS_DIR / f"{dep_name}.md"
            if dep_file.exists():
                _, dep_body = _parse_frontmatter(dep_file.read_text())
                deps.append(f"\n---\n## Dependency: {dep_name}\n\n{dep_body}")

    result = f"## Session Priming: {session_type}\n\n{body}"
    if deps:
        result += "\n".join(deps)
    return result


@mcp.tool()
def gaius_skill_recommend(task: str, files: list[str] | None = None) -> str:
    """Score and recommend skills for a task based on gaius density scoring.

    Complements Claude Code's native path-triggered skill loading by using
    gaius's BM25-style scoring with frontmatter metadata (gate, trigger, domain).

    Args:
        task: Description of what you're working on (e.g., "fix DRBD split-brain")
        files: Optional list of active file paths (e.g., ["manifests/piraeus/satellite.yaml"])
    """
    skills = load_skills()
    if not skills:
        return "No skills found in SKILLS_DIR"

    # Build context terms from task + file paths
    context_terms = set(re.sub(r'[^\w\s]', ' ', task.lower()).split())
    if files:
        for f in files:
            context_terms.update(re.sub(r'[^\w\s/]', ' ', f.lower()).replace('/', ' ').split())

    # Score all non-always skills
    scored = []
    for skill in skills:
        if skill["gate"] == "always":
            continue  # always-inject skills are unconditional, not recommendations
        score = compute_skill_score(skill, context_terms)
        if score > 0:
            scored.append((score, skill))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:5]

    if not top:
        return f"No skills matched task: '{task}'"

    lines = [f"## Skill Recommendations for: {task}\n"]
    for i, (score, skill) in enumerate(top):
        gate_label = f" [{skill['gate'].upper()}]" if skill["gate"] in ("mandate", "hard") else ""
        lines.append(
            f"**{i+1}. /{skill['name']}**{gate_label} (score: {score:.4f})\n"
            f"  {skill['fm'].get('description', 'No description')[:150]}\n"
            f"  Domain: {skill.get('domain', '?')} | "
            f"Tokens: {skill['tokens']} | "
            f"Gate: {skill['gate']}"
        )
        if skill["fm"].get("also_load"):
            lines.append(f"  Also loads: {skill['fm']['also_load']}")
        lines.append("")

    lines.append("_Load a skill with: `/skill-name` or read its file directly._")
    return "\n".join(lines)


# ── Threat Tiering MCP Tools ─────────────────────────────────────────────────
# Thin HTTP wrappers around detonate-api /api/threat/* endpoints.

_DETONATE_API = os.environ.get(
    "DETONATE_API_URL", "http://detonate-api.security.svc.cluster.local:8080"
)


def _threat_fetch(path: str, params: dict | None = None) -> str:
    """Fetch from detonate-api threat endpoint. Returns JSON string."""
    import urllib.request
    import urllib.parse

    url = f"{_DETONATE_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode()
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def threat_landscape(technique: str = "", family: str = "") -> str:
    """Active threat campaigns and per-technique scores from CVE intel + attribution.

    Args:
        technique: Optional MITRE technique ID to filter (e.g., T1496)
        family: Optional malware family to search for in groups
    """
    data = json.loads(_threat_fetch("/api/threat/landscape", {"technique": technique}))
    if family and "techniques" in data:
        family_lower = family.lower()
        data["techniques"] = [
            t for t in data["techniques"]
            if any(family_lower in g.lower() for g in (t.get("top_groups") or []))
        ]
        data["count"] = len(data["techniques"])
        data["filter_family"] = family
    return json.dumps(data, indent=2)


@mcp.tool()
def threat_tp_status(node: str = "", tp: str = "") -> str:
    """TP priority assignments per node — which TPs are assigned and why.

    Args:
        node: Optional node name to filter (e.g., web-prod-01)
        tp: Optional TP name to filter from results
    """
    data = json.loads(_threat_fetch("/api/threat/tp-status", {"node": node}))
    if tp and "assignments" in data:
        tp_lower = tp.lower()
        data["assignments"] = [
            a for a in data["assignments"]
            if tp_lower in a.get("tp_name", "").lower()
        ]
        data["count"] = len(data["assignments"])
        data["filter_tp"] = tp
    return json.dumps(data, indent=2)


@mcp.tool()
def threat_cve_exposure(cve: str = "", state: str = "") -> str:
    """CVE alert states — EXPOSED (red), DETECTED (yellow), MITIGATED (green).

    Args:
        cve: Optional CVE ID to filter (e.g., CVE-2026-31431)
        state: Optional state filter (EXPOSED, DETECTED, MITIGATED)
    """
    data = json.loads(_threat_fetch("/api/threat/cve-alerts", {"state": state}))
    if cve and "alerts" in data:
        cve_upper = cve.upper()
        data["alerts"] = [a for a in data["alerts"] if cve_upper in a.get("cve_id", "")]
        data["count"] = len(data["alerts"])
        data["filter_cve"] = cve
    return json.dumps(data, indent=2)


@mcp.tool()
def threat_yara_priority(status: str = "") -> str:
    """YARA rule tier distribution — hot (active campaigns), standard, cold.

    Args:
        status: Optional tier filter (hot, standard, cold)
    """
    # Forward the tier filter to the endpoint; _threat_fetch drops it when empty.
    return _threat_fetch("/api/threat/yara-priority", {"tier": status})


@mcp.tool()
def threat_recommend(scenario: str = "") -> str:
    """Get threat landscape summary for a described scenario.

    Args:
        scenario: Description of the threat scenario to analyze (e.g., "Kinsing cryptominer campaign")
    """
    data = json.loads(_threat_fetch("/api/threat/landscape"))
    if not scenario:
        return json.dumps(data, indent=2)

    # Filter techniques whose groups/CVEs match the scenario keywords
    keywords = set(scenario.lower().split())
    relevant = []
    for t in data.get("techniques", []):
        text = " ".join(t.get("top_groups", []) + t.get("top_cves", []) + [t.get("technique_id", "")]).lower()
        if any(kw in text for kw in keywords):
            relevant.append(t)

    return json.dumps({
        "scenario": scenario,
        "matching_techniques": relevant,
        "count": len(relevant),
        "total_techniques": data.get("count", 0),
        "last_computed": data.get("last_computed"),
    }, indent=2)


if __name__ == "__main__":
    mcp.run()
