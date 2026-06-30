#!/usr/bin/env python3
"""gaius HTTP Adapter — REST API over gaius._core for MEMINT and other consumers.

Exposes the same capabilities as the MCP server but as plain HTTP endpoints.
Runs locally, optionally exposed via reverse proxy (Traefik, nginx, Caddy).

Auth: X-Gaius-Token header (set GAIUS_API_TOKEN env var).
      If GAIUS_API_TOKEN is unset, all requests are accepted (dev mode, warn on startup).

Endpoints:
  GET  /health                   liveness
  GET  /search?q=...&domain=...&limit=N
  POST /inject    {"task": "...", "budget": 3000, "skills_budget": 500}
  GET  /kg?entity=...&limit=N
  GET  /kg/timeline?entity=...
  GET  /stats
  POST /facts     {"fact_text": "...", "domain": "...", "source": "session"}
  POST /chat      {"message": "...", "history": [...], "pillar": "geoint|finint|malint|null"}
"""

import json
import os
import re
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import yaml

# Add gaius package root to path
sys.path.insert(0, str(Path(__file__).parent))

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse
from pydantic import BaseModel

from gaius._core import (
    DB_PATH,
    DEFAULT_READINESS,
    DOMAIN_KEYWORDS,
    MATURITY_BOOTSTRAP_MIN,
    READINESS_THRESHOLDS,
    _EMBED_DIM,
    _embed_text,
    _maturity_score,
    init_db,
    route_suggest,
)

# ── Pillar config ────────────────────────────────────────────────────────────

_PILLARS: dict = {}


def _load_pillar_config():
    global _PILLARS
    config_path = Path.home() / ".gaius" / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        _PILLARS = cfg.get("pillars", {})

# ── Auth ─────────────────────────────────────────────────────────────────────

GAIUS_API_TOKEN = os.environ.get("GAIUS_API_TOKEN", "")
_AUTH_WARN = not GAIUS_API_TOKEN


def _check_auth(request: Request):
    if not GAIUS_API_TOKEN:
        return  # dev mode — no auth
    token = request.headers.get("X-Gaius-Token", "")
    if not token:
        # Also accept Authorization: Bearer <token>
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if token != GAIUS_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Gaius-Token")


# ── DB helpers (same as mcp_server.py) ───────────────────────────────────────

import sqlite3


def _get_db():
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


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    if _AUTH_WARN:
        print("WARNING: GAIUS_API_TOKEN not set — auth disabled (dev mode)", flush=True)
    init_db()
    _load_pillar_config()
    if _PILLARS:
        print(f"Pillar chat enabled: {', '.join(_PILLARS.keys())}", flush=True)
    print(f"gaius HTTP adapter ready. DB: {DB_PATH}", flush=True)
    yield


app = FastAPI(
    title="gaius HTTP API",
    description="Session memory system — search, inject, knowledge graph",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("GAIUS_CORS_ORIGINS", "*").split(","),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-Gaius-Token", "Authorization", "Content-Type"],
)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "db": str(DB_PATH)}


@app.get("/search")
async def search(
    request: Request,
    q: str = Query(..., description="Search query"),
    domain: str = Query("", description="Domain filter (optional)"),
    limit: int = Query(10, ge=1, le=50),
):
    _check_auth(request)
    conn = _get_db()
    import re

    terms = re.sub(r'[^\w\s]', ' ', q.lower()).split()
    facts = conn.execute(
        "SELECT id, domain, fact_key, fact_text, score, first_seen, last_seen FROM facts "
        "WHERE tombstoned_at IS NULL AND (outcome IS NULL OR outcome != 'rejected')"
    ).fetchall()

    scored = []
    for fact in facts:
        if domain and fact["domain"] != domain:
            continue
        text = (fact["fact_text"] or "").lower()
        kw_score = sum(1 for t in terms if t in text) / max(len(terms), 1)
        scored.append({"id": fact["id"], "domain": fact["domain"],
                       "fact_key": fact["fact_key"], "text": fact["fact_text"],
                       "score": fact["score"], "first_seen": fact["first_seen"],
                       "last_seen": fact["last_seen"],
                       "kw_score": kw_score, "sem_score": 0.0})

    # Semantic via sqlite-vec KNN
    query_vec = _embed_text(q)
    if query_vec:
        vec_blob = struct.pack(f'{_EMBED_DIM}f', *query_vec)
        try:
            rows = conn.execute(
                "SELECT fe.fact_id, fe.distance FROM fact_embeddings fe "
                "JOIN facts f ON f.id = fe.fact_id "
                "WHERE fe.embedding MATCH ? AND k = ? AND f.tombstoned_at IS NULL",
                [vec_blob, limit * 3]
            ).fetchall()
            sem_by_id = {
                r[0]: max(0, 1.0 - (r[1] ** 2 / 2.0))
                for r in rows
            }
            for item in scored:
                item["sem_score"] = sem_by_id.get(item["id"], 0.0)
        except Exception:
            pass

    for item in scored:
        relevance = 0.4 * item["kw_score"] + 0.6 * item["sem_score"]
        # Factor in DB score (recency-decayed) as a soft multiplier
        db_score = item["score"] if item["score"] else 0.5
        item["hybrid"] = relevance * (0.5 + 0.5 * db_score)

    scored.sort(key=lambda x: x["hybrid"], reverse=True)
    results = [
        {
            "rank": i + 1,
            "domain": item["domain"],
            "fact_key": item["fact_key"],
            "text": item["text"],
            "hybrid_score": round(item["hybrid"], 4),
            "kw_score": round(item["kw_score"], 4),
            "sem_score": round(item["sem_score"], 4),
            "first_seen": (item["first_seen"] or "")[:10],
            "last_seen": (item["last_seen"] or "")[:10],
        }
        for i, item in enumerate(scored[:limit])
        if item["hybrid"] > 0
    ]

    return {"query": q, "domain": domain or None, "count": len(results), "results": results}


class InjectRequest(BaseModel):
    task: str
    budget: int = 3000
    skills_budget: int = 500
    domain: str = ""
    session_type: str = ""


@app.post("/inject")
async def inject(request: Request, body: InjectRequest):
    _check_auth(request)
    import subprocess

    # Use subprocess to call `gaius inject` cleanly (cmd_inject touches globals)
    cmd = [
        "gaius", "inject", "--task", body.task,
        "--budget", str(body.budget),
        "--skills-budget", str(body.skills_budget),
        "--no-always-skills",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0 and not result.stdout:
        raise HTTPException(status_code=500, detail=result.stderr[:500])

    context = result.stdout

    # Prepend session-type priming skill if requested
    if body.session_type:
        from gaius._core import SKILLS_DIR, _parse_frontmatter
        valid_types = ["ops", "quant", "malware", "pentest", "audit", "console", "mnemos"]
        stype = body.session_type.lower().strip()
        if stype in valid_types:
            skill_file = SKILLS_DIR / f"{stype}.md"
            if skill_file.exists():
                _, skill_body = _parse_frontmatter(skill_file.read_text())
                context = f"## Session Priming: {stype}\n\n{skill_body}\n\n---\n\n{context}"

    return {"task": body.task, "budget": body.budget, "session_type": body.session_type, "context": context}


@app.get("/kg")
async def kg_query(
    request: Request,
    entity: str = Query(..., description="Entity name or partial match"),
    limit: int = Query(5, ge=1, le=20),
):
    _check_auth(request)
    conn = _get_db()
    term = entity.lower()
    entities = conn.execute(
        "SELECT id, name, type, domain FROM entities WHERE id LIKE ? OR name LIKE ?",
        (f"%{term}%", f"%{term}%")
    ).fetchall()

    if not entities:
        return {"entity": entity, "matches": []}

    results = []
    for ent in entities[:limit]:
        outgoing = [
            {"predicate": t[0], "object": t[1],
             "valid_from": (t[2] or "")[:10], "valid_to": (t[3] or "")[:10]}
            for t in conn.execute(
                "SELECT predicate, object, valid_from, valid_to FROM triples "
                "WHERE subject = ? ORDER BY valid_from", (ent[0],)
            ).fetchall()
        ]
        incoming = [
            {"subject": t[0], "predicate": t[1],
             "valid_from": (t[2] or "")[:10], "valid_to": (t[3] or "")[:10]}
            for t in conn.execute(
                "SELECT subject, predicate, valid_from, valid_to FROM triples "
                "WHERE object = ? ORDER BY valid_from", (ent[0],)
            ).fetchall()
        ]
        results.append({
            "id": ent[0], "name": ent[1], "type": ent[2], "domain": ent[3],
            "outgoing": outgoing, "incoming": incoming,
        })

    return {"entity": entity, "matches": results}


@app.get("/kg/timeline")
async def kg_timeline(
    request: Request,
    entity: str = Query(..., description="Entity name or partial match"),
):
    _check_auth(request)
    conn = _get_db()
    term = entity.lower()
    entities = conn.execute(
        "SELECT id, name, type FROM entities WHERE id LIKE ? OR name LIKE ?",
        (f"%{term}%", f"%{term}%")
    ).fetchall()

    if not entities:
        return {"entity": entity, "events": []}

    eid, ename, etype = entities[0]
    rows = conn.execute("""
        SELECT valid_from, predicate, object, valid_to, source_agent, 'out' as dir
          FROM triples WHERE subject = ?
        UNION ALL
        SELECT valid_from, predicate, subject, valid_to, source_agent, 'in' as dir
          FROM triples WHERE object = ?
        ORDER BY valid_from NULLS LAST
    """, (eid, eid)).fetchall()

    events = [
        {
            "date": (r[0] or "")[:10],
            "direction": r[5],
            "predicate": r[1],
            "counterpart": r[2],
            "valid_to": (r[3] or "")[:10],
            "source_agent": r[4],
        }
        for r in rows
    ]

    return {"entity": ename, "type": etype, "event_count": len(events), "events": events}


@app.get("/stats")
async def stats(request: Request):
    _check_auth(request)
    conn = _get_db()
    total = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE tombstoned_at IS NULL "
        "AND (outcome IS NULL OR outcome != 'rejected')"
    ).fetchone()[0]
    tombstoned = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE tombstoned_at IS NOT NULL"
    ).fetchone()[0]
    by_domain = dict(conn.execute(
        "SELECT domain, COUNT(*) FROM facts WHERE tombstoned_at IS NULL "
        "GROUP BY domain ORDER BY COUNT(*) DESC"
    ).fetchall())

    embedded = 0
    try:
        embedded = conn.execute("SELECT COUNT(*) FROM fact_embeddings").fetchone()[0]
    except Exception:
        pass

    kg = {}
    try:
        kg = {
            "entities": conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
            "triples": conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0],
        }
    except Exception:
        pass

    return {
        "total_facts": total,
        "active_facts": active,
        "tombstoned_facts": tombstoned,
        "by_domain": by_domain,
        "embeddings": {"count": embedded, "total": total,
                       "coverage_pct": round(100 * embedded / max(total, 1))},
        "knowledge_graph": kg,
        "db_path": str(DB_PATH),
    }


@app.get("/snapshot")
async def snapshot(request: Request):
    """Domain maturity snapshot — powers the dashboard overview panel."""
    _check_auth(request)
    conn = _get_db()
    rows = conn.execute(
        "SELECT domain, confirmation_count, provenance, outcome, first_seen, "
        "model_families, source FROM facts WHERE tombstoned_at IS NULL"
    ).fetchall()
    by_domain: dict[str, list] = {}
    for row in rows:
        by_domain.setdefault(row["domain"], []).append(row)

    all_domains = sorted(set(list(by_domain.keys()) + list(DOMAIN_KEYWORDS.keys())))
    total_facts = sum(len(v) for v in by_domain.values())
    live_count = 0
    domains_out = []

    for domain in all_domains:
        facts = by_domain.get(domain, [])
        n = len(facts)
        score = _maturity_score(facts) if n >= MATURITY_BOOTSTRAP_MIN else 0.0
        thresh = READINESS_THRESHOLDS.get(domain, DEFAULT_READINESS)
        ready = score >= thresh["score"] and n >= thresh["min_facts"]

        if score >= 0.45 and n >= MATURITY_BOOTSTRAP_MIN:
            status = "live"
            live_count += 1
        elif score >= 0.25 and n >= MATURITY_BOOTSTRAP_MIN:
            status = "warm"
        elif n >= MATURITY_BOOTSTRAP_MIN:
            status = "cold"
        else:
            status = "bootstrap"

        domains_out.append({
            "domain": domain, "facts": n,
            "score": round(score, 4), "status": status, "ready": ready,
        })

    # Sort: live first, then by score desc
    domains_out.sort(key=lambda d: (
        {"live": 0, "warm": 1, "cold": 2, "bootstrap": 3}[d["status"]], -d["score"]
    ))

    return {
        "snapshot_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_facts": total_facts,
        "live_domains": live_count,
        "total_domains": len(all_domains),
        "domains": domains_out,
    }


@app.get("/route-suggest")
async def route_suggest_endpoint(
    request: Request,
    q: str = Query(..., description="task / query to route"),
    hint: str = Query(None, description="primary domain hint"),
    max_facts: int = Query(5, ge=1, le=25),
):
    """Phase 3 — retrieval-augmented routing recommendation (read-only). Returns the primary
    domain, supporting corpus facts (each with a verified flag), the UNVERIFIED count, and
    task_outcomes win-rates. The orchestrator adopts this (flag-gated) for grounded, transparent
    routing. Read-only: route_suggest issues only SELECTs."""
    _check_auth(request)
    conn = _get_db()
    return route_suggest(conn, q, hint=hint, max_facts=max_facts)


@app.get("/recent")
async def recent(
    request: Request,
    limit: int = Query(15, ge=1, le=50),
):
    """Most recently added/updated facts — powers the activity feed."""
    _check_auth(request)
    conn = _get_db()
    rows = conn.execute(
        "SELECT domain, fact_text, first_seen, last_seen, score FROM facts "
        "WHERE tombstoned_at IS NULL AND (outcome IS NULL OR outcome != 'rejected') "
        "ORDER BY last_seen DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return {
        "count": len(rows),
        "facts": [
            {
                "domain": r["domain"],
                "text": (r["fact_text"] or "")[:200],
                "first_seen": (r["first_seen"] or "")[:10],
                "last_seen": (r["last_seen"] or "")[:10],
                "score": r["score"],
            }
            for r in rows
        ],
    }


# ── Telemetry endpoints ──────────────────────────────────────────────────────

@app.get("/telemetry/summary")
async def telemetry_summary(
    request: Request,
    hours: int = Query(24, ge=1, le=720),
):
    """Aggregated telemetry for dashboard — prompt quality, injection quality, enforcement."""
    _check_auth(request)
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from gaius.telemetry import get_summary
        return get_summary(hours)
    except Exception as e:
        return {"error": str(e), "telemetry_available": False}


@app.get("/telemetry/violations")
async def telemetry_violations(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
):
    """Recent enforcement events — blocks and bypasses for review."""
    _check_auth(request)
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from gaius.telemetry import get_violations
        return {"violations": get_violations(limit)}
    except Exception as e:
        return {"error": str(e), "violations": []}


@app.get("/telemetry/injection")
async def telemetry_injection(
    request: Request,
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(20, ge=1, le=100),
):
    """Per-fact injection frequency — detect popularity bias and staleness."""
    _check_auth(request)
    try:
        import sqlite3
        tel_path = os.path.expanduser("~/.gaius/telemetry.db")
        if not os.path.exists(tel_path):
            return {"facts": [], "note": "telemetry.db not found"}
        conn = sqlite3.connect(tel_path)
        conn.row_factory = sqlite3.Row
        import time
        cutoff = time.time() - hours * 3600
        rows = conn.execute(
            """SELECT fact_key, source, COUNT(*) as injection_count,
                      AVG(score) as avg_score, AVG(cosine) as avg_cosine,
                      MIN(ts) as first_injected, MAX(ts) as last_injected
               FROM injection_facts WHERE ts > ?
               GROUP BY fact_key ORDER BY injection_count DESC LIMIT ?""",
            (cutoff, limit)
        ).fetchall()
        return {
            "period_hours": hours,
            "facts": [dict(r) for r in rows],
        }
    except Exception as e:
        return {"error": str(e), "facts": []}


@app.get("/telemetry/coaching")
async def telemetry_coaching(request: Request):
    """All coaching tips — for dashboard display and user guidance."""
    _check_auth(request)
    try:
        import sqlite3
        tel_path = os.path.expanduser("~/.gaius/telemetry.db")
        if not os.path.exists(tel_path):
            return {"tips": []}
        conn = sqlite3.connect(tel_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM coaching_tips ORDER BY category, severity DESC").fetchall()
        return {"tips": [dict(r) for r in rows]}
    except Exception as e:
        return {"error": str(e), "tips": []}


class FactAddRequest(BaseModel):
    fact_text: str
    domain: str
    source: str = "session"
    # Real writer identity, threaded into corroboration (agents/sessions/principals arrays).
    # When a multi-tenant router (or any caller) supplies these, two distinct writers of the
    # same fact corroborate as two — instead of collapsing into one "http-adapter" session.
    # Omitted → the legacy defaults below, so existing callers are byte-for-byte unchanged.
    agent: Optional[str] = None
    session_uuid: Optional[str] = None


@app.post("/facts")
async def fact_add(request: Request, body: FactAddRequest):
    _check_auth(request)
    import hashlib
    from gaius._core import init_db, upsert_fact, _is_noise

    # Same quality gates as every other ingest path (noise filter, semantic
    # dedup, confidence scoring, race-safe insert). The old raw INSERT at
    # score 1.0 let remote agents outrank every reviewed fact, sight unseen.
    if _is_noise(body.fact_text):
        return {"status": "rejected", "reason": "noise filter"}

    conn = init_db()
    fact_key = hashlib.sha256(body.fact_text.encode()).hexdigest()[:32]
    existing = conn.execute(
        "SELECT id FROM facts WHERE fact_key = ? AND tombstoned_at IS NULL",
        (fact_key,)
    ).fetchone()

    upsert_fact(conn, body.domain, fact_key, body.fact_text,
                agent=body.agent or body.source or "http",
                session_uuid=body.session_uuid or "http-adapter",
                provenance="mcp-session", score=0.6,
                source=body.source or "session")

    if existing:
        return {"status": "duplicate", "id": existing["id"]}
    row = conn.execute(
        "SELECT id FROM facts WHERE fact_key = ? AND tombstoned_at IS NULL",
        (fact_key,)
    ).fetchone()
    if row:
        return {"status": "created", "id": row["id"], "fact_key": fact_key, "domain": body.domain}
    return {"status": "merged", "reason": "semantically similar fact corroborated instead"}


# ── Chat endpoint ────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    pillar: Optional[str] = None


# Domain → pillar mapping (derived from config)
_DOMAIN_PILLAR_MAP: dict[str, str] = {}


def _build_domain_pillar_map():
    global _DOMAIN_PILLAR_MAP
    _DOMAIN_PILLAR_MAP = {}
    for pillar_name, pcfg in _PILLARS.items():
        for domain in pcfg.get("domains", []):
            _DOMAIN_PILLAR_MAP[domain] = pillar_name


def _detect_pillar(message: str) -> str:
    """Detect pillar from message content using domain keywords."""
    msg_lower = message.lower()
    scores: dict[str, int] = {}

    # Load domain keywords from config.yaml
    config_path = Path.home() / ".gaius" / "config.yaml"
    domain_keywords: dict[str, list[str]] = {}
    if config_path.exists():
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        domain_keywords = cfg.get("domain_keywords", {})

    for domain, keywords in domain_keywords.items():
        pillar = _DOMAIN_PILLAR_MAP.get(domain)
        if not pillar:
            continue
        for kw in keywords:
            if kw in msg_lower:
                scores[pillar] = scores.get(pillar, 0) + 1

    if scores:
        return max(scores, key=scores.get)
    return "geoint"  # default pillar


def _search_corpus(query: str, domains: list[str], limit: int = 8) -> str:
    """Search corpus for relevant facts and return formatted context."""
    conn = _get_db()
    terms = re.sub(r'[^\w\s]', ' ', query.lower()).split()

    facts = conn.execute(
        "SELECT id, domain, fact_text, score FROM facts "
        "WHERE tombstoned_at IS NULL AND (outcome IS NULL OR outcome != 'rejected')"
    ).fetchall()

    scored = []
    for fact in facts:
        domain = fact["domain"]
        # Boost facts from pillar-relevant domains
        domain_boost = 1.5 if domain in domains else 0.5
        # Factor in recency-decayed DB score
        db_score = fact["score"] if fact["score"] else 0.5
        text = (fact["fact_text"] or "").lower()
        kw_score = sum(1 for t in terms if t in text) / max(len(terms), 1)
        if kw_score > 0:
            scored.append((fact["fact_text"], kw_score * domain_boost * (0.5 + 0.5 * db_score)))

    # Also try semantic search
    query_vec = _embed_text(query)
    if query_vec:
        vec_blob = struct.pack(f'{_EMBED_DIM}f', *query_vec)
        try:
            rows = conn.execute(
                "SELECT fe.fact_id, fe.distance FROM fact_embeddings fe "
                "JOIN facts f ON f.id = fe.fact_id "
                "WHERE fe.embedding MATCH ? AND k = ? AND f.tombstoned_at IS NULL",
                [vec_blob, limit * 2]
            ).fetchall()
            sem_by_id = {r[0]: max(0, 1.0 - (r[1] ** 2 / 2.0)) for r in rows}
            # Add semantic hits not already in keyword results
            for fact in facts:
                sem = sem_by_id.get(fact["id"], 0)
                if sem > 0.3:
                    domain_boost = 1.5 if fact["domain"] in domains else 0.5
                    db_score = fact["score"] if fact["score"] else 0.5
                    scored.append((fact["fact_text"], sem * domain_boost * (0.5 + 0.5 * db_score)))
        except Exception:
            pass

    # Deduplicate and take top results
    seen = set()
    unique = []
    for text, score in sorted(scored, key=lambda x: x[1], reverse=True):
        if text not in seen:
            seen.add(text)
            unique.append(text)
        if len(unique) >= limit:
            break

    if not unique:
        return ""

    return "Relevant context from institutional memory:\n" + "\n".join(
        f"- {fact}" for fact in unique
    )


@app.get("/suggestions")
async def suggestions(
    request: Request,
    pillar: str = Query("gaius", description="Pillar name"),
    context: str = Query("", description="Subpage context (e.g. autotrade, cctv)"),
):
    _check_auth(request)
    conn = _get_db()

    # Determine which domains to search
    pcfg = _PILLARS.get(pillar, {})
    domains = pcfg.get("domains", [])

    # Build query from context + pillar name
    query_terms = [pillar] if pillar != "gaius" else []
    if context:
        query_terms.append(context)
    query = " ".join(query_terms) if query_terms else "cluster operations"

    # Fetch recent facts from relevant domains, prefer facts with high signal
    facts = conn.execute(
        "SELECT id, domain, fact_key, fact_text, score, last_seen FROM facts "
        "WHERE tombstoned_at IS NULL AND (outcome IS NULL OR outcome != 'rejected') "
        "ORDER BY last_seen DESC"
    ).fetchall()

    # Score by keyword match + domain boost + fact signal
    import random
    terms = re.sub(r'[^\w\s]', ' ', query.lower()).split()
    scored = []
    # Patterns that indicate session-log/meta facts (not domain knowledge)
    meta_patterns = re.compile(
        r'^(commit|working on|successfully|an async|resume|`npx|hub page|ingress\.html)',
        re.IGNORECASE,
    )
    for fact in facts:
        domain = fact["domain"]
        text = (fact["fact_text"] or "").strip()
        text_lower = text.lower()
        key = (fact["fact_key"] or "").lower()

        # Skip very short or meta/session-log facts
        if len(text) < 30 or meta_patterns.match(text):
            continue

        kw_score = sum(1 for t in terms if t in text_lower or t in key) / max(len(terms), 1)
        if kw_score == 0:
            continue

        # Domain boost (prefer pillar-matched, but don't exclude others)
        domain_boost = 1.5 if (domains and domain in domains) else 1.0
        fact_score = fact["score"] or 1.0
        combined = kw_score * domain_boost * 0.4 + min(fact_score / 10.0, 1.0) * 0.6
        label = text[:120].strip()
        scored.append((label, combined, fact["domain"]))

    scored.sort(key=lambda x: x[1], reverse=True)

    # Deduplicate by first 40 chars, pick top candidates
    seen = set()
    candidates = []
    for label, score, domain in scored:
        dedup_key = label[:40].lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        candidates.append(label)
        if len(candidates) >= 12:
            break

    # Shuffle top candidates for variety, take 3
    if len(candidates) > 3:
        top = candidates[:8]
        random.shuffle(top)
        candidates = top[:3]

    # Start with static suggestions for this pillar/context
    static = _STATIC_SUGGESTIONS.get(pillar, {}).get(context, []) or \
             _STATIC_SUGGESTIONS.get(pillar, {}).get("", []) or \
             _STATIC_SUGGESTIONS.get("gaius", {}).get("", [])
    result = list(static[:3])

    # Optionally mix in 1 corpus-derived suggestion if high quality
    for label, score, _ in scored[:6]:
        if score < 0.7:
            break  # below quality threshold
        topic = label.split("\n")[0].strip()
        topic = re.sub(r'^[-*#\d.]+\s*', '', topic)
        topic = re.sub(r'\*\*([^*]+)\*\*', r'\1', topic)
        # Reject session-log artifacts
        if len(topic) < 20 or len(topic) > 60:
            continue
        if re.match(r'^("|`|http|An |The |Juleis|Geminius|Successfully|Implement|Resume|Cloudflare|Hub |Initial |Docker )', topic, re.I):
            continue
        # Must look like a real topic (contains a recognizable noun)
        if not re.search(r'(strategy|camera|aircraft|corpus|feeder|pipeline|alert|model|probe|trading|detonation|verdict|signature)', topic, re.I):
            continue
        question = topic.lower().rstrip(".…")
        if len(question) > 45:
            question = question[:42].rsplit(" ", 1)[0] + "…"
        result = ["what about " + question + "?"] + result[:2]
        break

    return {"pillar": pillar, "context": context or None, "suggestions": result[:3]}


_STATIC_SUGGESTIONS: dict[str, dict[str, list[str]]] = {
    "finint": {
        "": ["how are strategies performing?", "what's the current P&L?", "assess portfolio risk"],
        "autotrade": ["how are strategies performing today?", "which strategies are live?", "any recent losses?"],
        "altdata": ["what's the options flow bias?", "any unusual insider activity?", "show congress trades"],
        "greeks": ["what's the execution quality?", "show recent fill rates", "any adverse selection?"],
    },
    "geoint": {
        "": ["how many cameras are online?", "show aircraft count by region", "any stale feeds?"],
        "cctv": ["how many cameras per region?", "which cameras are stale?", "show archival stats"],
        "adsb": ["how many aircraft tracked?", "show feeder coverage", "which region has most traffic?"],
    },
    "malint": {
        "": ["show corpus stats", "any recent detections?", "what's the verdict breakdown?"],
        "assay": ["show detonation results", "how many samples analyzed?", "any high-severity verdicts?"],
        "hunt": ["any active threats?", "show recent Tetragon events", "what's the MITRE coverage?"],
    },
    "gaius": {
        "": ["how many facts in the corpus?", "show knowledge graph stats", "what domains have most coverage?"],
    },
}


@app.post("/chat")
async def chat(request: Request, body: ChatRequest):
    _check_auth(request)

    if not _PILLARS:
        raise HTTPException(503, "Pillar chat not configured")

    # Build domain→pillar map on first call
    if not _DOMAIN_PILLAR_MAP:
        _build_domain_pillar_map()

    pillar = body.pillar or _detect_pillar(body.message)
    if pillar not in _PILLARS:
        # Fall back to geoint
        pillar = next(iter(_PILLARS))

    pcfg = _PILLARS[pillar]
    model_url = pcfg["model_url"].rstrip("/")
    model_name = pcfg["model_name"]
    system_prefix = pcfg.get("system_prefix", "You are a helpful assistant.")

    # Inject corpus context
    domains = pcfg.get("domains", [])
    context = _search_corpus(body.message, domains)

    system_prompt = system_prefix
    if context:
        system_prompt += "\n\n" + context

    # Build OpenAI-format messages
    messages = [{"role": "system", "content": system_prompt}]
    for msg in body.history[-10:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": body.message})

    # Stream from vLLM
    async def generate():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                async with client.stream(
                    "POST",
                    f"{model_url}/v1/chat/completions",
                    json={
                        "model": model_name,
                        "messages": messages,
                        "stream": True,
                        "max_tokens": 2048,
                        "temperature": 0.7,
                    },
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status_code != 200:
                        body_text = ""
                        async for chunk in resp.aiter_bytes():
                            body_text += chunk.decode(errors="replace")
                        yield f"data: {json_dumps({'error': f'Model returned {resp.status_code}: {body_text[:200]}'})}\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if line:
                            yield line + "\n"
                        else:
                            yield "\n"
        except httpx.ConnectError:
            yield f'data: {{"error": "Cannot reach model at {model_url}"}}\n\n'
        except Exception as e:
            yield f'data: {{"error": "{str(e)[:200]}"}}\n\n'

    return StreamingResponse(generate(), media_type="text/event-stream")


def json_dumps(obj):
    import json
    return json.dumps(obj)


# ── UI Settings ──────────────────────────────────────────────────────────────

_UI_CONFIG_PATH = Path.home() / ".gaius" / "ui-config.json"
# Skills dir: prefer local checkout, fall back to pod's /tmp/agent-memory clone
_SKILLS_DIR = (Path.home() / "Projects" / "agent-memory" / "skills"
               if (Path.home() / "Projects" / "agent-memory" / "skills").exists()
               else Path("/tmp/agent-memory/skills"))

_DEFAULT_SKILL_COLORS: dict[str, str] = {
    "console": "#051a20", "uiux": "#051a20", "frontend": "#051a20",
    "quant": "#051a08", "finint": "#051a08",
    "ops": "#1a0a03", "fighter": "#1a0a03",
    "mnemos": "#0f0520", "surgeon": "#0f0520",
    "malware": "#1a0303", "malint": "#1a0303",
    "audit": "#1a1503",
    "pentest": "#1a0505",
}


def _load_ui_config() -> dict:
    try:
        return json.loads(_UI_CONFIG_PATH.read_text())
    except Exception:
        return {}


def _save_ui_config(cfg: dict) -> None:
    _UI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _UI_CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def _read_skill_frontmatter(path: Path) -> dict:
    """Parse YAML frontmatter from a skill markdown file."""
    try:
        text = path.read_text()
        if not text.startswith("---"):
            return {}
        end = text.index("---", 3)
        return yaml.safe_load(text[3:end]) or {}
    except Exception:
        return {}


@app.get("/settings")
async def get_settings(request: Request):
    _check_auth(request)
    cfg = _load_ui_config()
    colors = {**_DEFAULT_SKILL_COLORS, **cfg.get("skill_colors", {})}
    return {"skill_colors": colors, "raw": cfg}


@app.post("/settings")
async def post_settings(request: Request):
    _check_auth(request)
    body = await request.json()
    cfg = _load_ui_config()
    if "skill_colors" in body:
        cfg.setdefault("skill_colors", {}).update(body["skill_colors"])
    _save_ui_config(cfg)
    return {"status": "ok", "skill_colors": {**_DEFAULT_SKILL_COLORS, **cfg.get("skill_colors", {})}}


@app.get("/skills-list")
async def skills_list(request: Request):
    _check_auth(request)

    # List skill files with parsed frontmatter
    skills = []
    if _SKILLS_DIR.exists():
        for p in sorted(_SKILLS_DIR.glob("*.md")):
            fm = _read_skill_frontmatter(p)
            if not fm:
                continue
            skills.append({
                "file": p.name,
                "name": fm.get("name", p.stem),
                "description": fm.get("description", ""),
                "domain": fm.get("domain", ""),
                "gate": fm.get("gate", ""),
                "trigger": fm.get("trigger", ""),
            })

    # Skill gap suggestions from facts.db (domains with facts but no skill coverage)
    conn = _get_db()
    domain_counts = dict(conn.execute(
        "SELECT domain, COUNT(*) FROM facts WHERE tombstoned_at IS NULL "
        "AND (outcome IS NULL OR outcome != 'rejected') "
        "GROUP BY domain ORDER BY COUNT(*) DESC"
    ).fetchall())
    covered = {s["domain"] for s in skills if s["domain"]}
    gaps = [
        {"domain": d, "facts": n, "sessions": 0}
        for d, n in domain_counts.items()
        if d not in covered and n >= 20
    ][:8]

    return {"skills": skills, "gaps": gaps}


@app.post("/skills-create")
async def skills_create(request: Request):
    _check_auth(request)
    body = await request.json()
    name = re.sub(r'[^a-z0-9-]', '-', body.get("name", "").lower().strip()).strip('-')
    if not name:
        raise HTTPException(400, "name required")
    domain = body.get("domain", "").strip()
    description = body.get("description", f"Skill for {name} domain work").strip()
    trigger = body.get("trigger", f"any work touching {name}").strip()

    path = _SKILLS_DIR / f"{name}.md"
    if path.exists():
        raise HTTPException(409, f"skills/{name}.md already exists")

    content = f"""---
name: {name}
description: {description}
origin: kub0
domain: {domain or name}
gate: optional
trigger: "{trigger}"
---

# Skill: {name.title()}

<!-- Auto-generated via gaius UI. Add section content here. -->
"""
    path.write_text(content)
    return {"status": "created", "file": path.name, "name": name}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("GAIUS_HTTP_PORT", "8765"))
    host = os.environ.get("GAIUS_HTTP_HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")
