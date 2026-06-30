"""gaius.kg — Knowledge Graph subsystem (entities, relations, triples).

Extracted from gaius._core (2026-06-28) as the first step of the _core.py split.
Follows the same pattern as gaius.parsers: this module imports the few shared
foundation symbols it needs from gaius._core, and _core re-imports the public
names at the bottom of the file (after they are defined) so the cycle is broken
by import ordering and `from gaius._core import kg_index_fact` (etc.) keeps working.

Entity/relation extraction, triple storage with temporal validity, co-occurrence
edges, and the `gaius kg` command. The graph is consulted by inject scoring via
infra_entity_boost (which stays in _core and calls extract_entities from here).
"""

import re
import sys
import sqlite3
from datetime import datetime, timezone

# Foundation symbols owned by _core. All are defined well before _core's bottom
# `from gaius.kg import ...` (line ~8618), so this top-level import resolves
# against the partially-initialised _core module — the proven parsers.py pattern.
from gaius._core import _gaius_cfg, init_db, BOLD, RESET


_BUILTIN_ENTITY_PATTERNS: dict[str, str] = {
    # K8s node naming convention — customize node_pattern in config for your scheme
    "node":      r'\b(?:[a-z]+-[a-z]+-[\w]+-[\w]+-\d+)\b',
    # Common K8s services (widely used, not kub0-specific)
    "service":   r'\b(?:traefik|nginx|cert-manager|oauth2-proxy|grafana|prometheus|loki|mimir|alloy|otel-collector|jupyterlab|gitea|forgejo|timescaledb|postgresql|mysql|redis|mongodb|elasticsearch|kibana|jaeger|tempo)\b',
    "namespace": r'\b(?:kube-system|kube-public|default|monitoring|logging|networking|security|storage|cert-manager|ingress-nginx)\b',
    # Incident / failure vocabulary (generic)
    "incident":  r'\b(?:cascade|outage|split-brain|quorum[\s-]loss|crashloop|oomkill|deadlock|timeout|degraded|unreachable)\b',
}


def _load_entity_patterns() -> dict:
    """Build entity regex patterns from config, merging with built-in baseline.

    Config schema (in ~/.gaius/config.yaml):
      entities:
        preset: k8s        # "k8s" (default) or "none" to disable built-ins
        patterns:          # additional or override patterns
          service: '\\b(?:my-service|other-service)\\b'
          node: '\\bmy-node-prefix-\\d+\\b'
    """
    cfg_entities = _gaius_cfg.get("entities", {})
    preset = cfg_entities.get("preset", "k8s")
    custom_patterns: dict = cfg_entities.get("patterns", {})

    base: dict[str, str] = {}
    if preset != "none":
        base = dict(_BUILTIN_ENTITY_PATTERNS)

    base.update(custom_patterns)

    compiled: dict = {}
    for name, pattern in base.items():
        try:
            compiled[name] = re.compile(pattern, re.I)
        except re.error as e:
            print(f"[gaius] warning: invalid entity pattern '{name}': {e}", file=sys.stderr)
    return compiled


_ENTITY_PATTERNS = _load_entity_patterns()

# Relationship patterns: (subject_type, predicate, object_type, regex)
_RELATION_PATTERNS = [
    # "X runs on Y" / "X deployed on Y"
    (re.compile(r'(\b[\w-]+(?:-api|executor|proxy)\b)\s+(?:runs?|deployed|scheduled)\s+(?:on|to)\s+(k8s-[\w-]+)', re.I),
     "service", "runs_on", "node"),
    # "X uses Y" storage
    (re.compile(r'(\b[\w-]+\b)\s+(?:uses?|on|backed by)\s+(block-\w+)', re.I),
     "service", "uses_storage", "storage"),
    # "X in namespace Y"
    (re.compile(r'(\b[\w-]+(?:-api|executor|proxy)\b)\s+(?:in|namespace)\s+(\w+)\s+namespace', re.I),
     "service", "in_namespace", "namespace"),
]


def extract_entities(text: str) -> list[tuple[str, str, str]]:
    """Extract (entity_id, entity_name, entity_type) tuples from text using regex patterns."""
    entities = []
    seen = set()
    for etype, pattern in _ENTITY_PATTERNS.items():
        for match in pattern.finditer(text):
            name = match.group(0).lower().strip()
            eid = f"{etype}:{name}"
            if eid not in seen:
                seen.add(eid)
                entities.append((eid, name, etype))
    return entities


def extract_relations(text: str) -> list[tuple[str, str, str, str, str]]:
    """Extract (subject_id, predicate, object_id, subject_type, object_type) from text."""
    relations = []
    for pattern, subj_type, predicate, obj_type in _RELATION_PATTERNS:
        for match in pattern.finditer(text):
            subj_name = match.group(1).lower().strip()
            obj_name = match.group(2).lower().strip()
            subj_id = f"{subj_type}:{subj_name}"
            obj_id = f"{obj_type}:{obj_name}"
            relations.append((subj_id, predicate, obj_id, subj_type, obj_type))
    return relations


def upsert_entity(conn: sqlite3.Connection, entity_id: str, name: str, etype: str, domain: str = None):
    """Insert entity if not exists."""
    conn.execute("""
        INSERT OR IGNORE INTO entities (id, name, type, domain) VALUES (?, ?, ?, ?)
    """, (entity_id, name, etype, domain))


def add_triple(conn: sqlite3.Connection, subject: str, predicate: str, obj: str,
               valid_from: str = None, confidence: float = 1.0,
               source_session: str = None, source_agent: str = None, source_fact_id: int = None):
    """Add a relationship triple. Deduplicates by (subject, predicate, object, valid_from)."""
    existing = conn.execute(
        "SELECT id FROM triples WHERE subject = ? AND predicate = ? AND object = ? AND valid_from IS ?",
        (subject, predicate, obj, valid_from)
    ).fetchone()
    if not existing:
        conn.execute("""
            INSERT INTO triples (subject, predicate, object, valid_from, confidence,
                                source_session, source_agent, source_fact_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (subject, predicate, obj, valid_from, confidence,
              source_session, source_agent, source_fact_id))


def invalidate_triple(conn: sqlite3.Connection, subject: str, predicate: str, obj: str, ended: str = None):
    """Mark a triple as ended (set valid_to). Does not delete — preserves history."""
    if ended is None:
        ended = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        UPDATE triples SET valid_to = ? WHERE subject = ? AND predicate = ? AND object = ? AND valid_to IS NULL
    """, (ended, subject, predicate, obj))
    conn.commit()


def kg_index_fact(conn: sqlite3.Connection, fact_id: int, fact_text: str, domain: str,
                  session_uuid: str = None, agent: str = None, timestamp: str = None):
    """Extract entities and relations from a fact and add to the KG.
    Uses both explicit relation patterns and co-occurrence (entities in same fact = related)."""
    entities = extract_entities(fact_text)
    for eid, name, etype in entities:
        upsert_entity(conn, eid, name, etype, domain)

    # Explicit relation patterns
    relations = extract_relations(fact_text)
    for subj_id, predicate, obj_id, subj_type, obj_type in relations:
        upsert_entity(conn, subj_id, subj_id.split(":", 1)[1], subj_type, domain)
        upsert_entity(conn, obj_id, obj_id.split(":", 1)[1], obj_type, domain)
        add_triple(conn, subj_id, predicate, obj_id,
                   valid_from=timestamp, source_session=session_uuid,
                   source_agent=agent, source_fact_id=fact_id)

    # Co-occurrence triples: if a node and service/incident appear in same fact, link them
    nodes = [(eid, name) for eid, name, etype in entities if etype == "node"]
    others = [(eid, name, etype) for eid, name, etype in entities if etype in ("service", "incident", "storage", "model")]
    for node_id, node_name in nodes:
        for other_id, other_name, other_type in others:
            predicate = {
                "service": "mentioned_with",
                "incident": "affected_by",
                "storage": "has_storage",
                "model": "runs_model",
            }.get(other_type, "related_to")
            add_triple(conn, node_id, predicate, other_id,
                       valid_from=timestamp, confidence=0.7,
                       source_session=session_uuid, source_agent=agent, source_fact_id=fact_id)


def cmd_kg(args):
    """Knowledge Graph operations: query, timeline, index, invalidate, stats.

    Usage:
      gaius kg stats                          — overview of entities + triples
      gaius kg query <entity>                 — all triples for an entity
      gaius kg timeline <entity>              — chronological story of an entity
      gaius kg index                          — backfill KG from all facts in facts.db
      gaius kg invalidate <subj> <pred> <obj> — mark a triple as ended
    """
    if not args or args[0] in ("-h", "--help"):
        print(cmd_kg.__doc__)
        return

    subcmd = args[0]
    conn = init_db()

    if subcmd == "stats":
        n_entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        n_triples = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        n_active = conn.execute("SELECT COUNT(*) FROM triples WHERE valid_to IS NULL").fetchone()[0]
        print(f"Knowledge Graph Statistics:")
        print(f"  Entities:       {n_entities}")
        print(f"  Triples:        {n_triples} ({n_active} active, {n_triples - n_active} ended)")
        print()
        if n_entities > 0:
            print("  By entity type:")
            for row in conn.execute("SELECT type, COUNT(*) c FROM entities GROUP BY type ORDER BY c DESC"):
                print(f"    {row[0]:<15} {row[1]:>5}")
        if n_triples > 0:
            print("  By predicate:")
            for row in conn.execute("SELECT predicate, COUNT(*) c FROM triples GROUP BY predicate ORDER BY c DESC"):
                print(f"    {row[0]:<20} {row[1]:>5}")

    elif subcmd == "query":
        if len(args) < 2:
            print("Usage: gaius kg query <entity-name-or-id>")
            return
        term = args[1].lower()
        # Search by name or id substring
        entities = conn.execute(
            "SELECT id, name, type, domain FROM entities WHERE id LIKE ? OR name LIKE ?",
            (f"%{term}%", f"%{term}%")
        ).fetchall()
        if not entities:
            print(f"No entities matching '{term}'")
            return
        for ent in entities:
            print(f"\n{BOLD}{ent[1]}{RESET} ({ent[2]}, domain: {ent[3] or '?'})")
            # Outgoing triples
            for t in conn.execute(
                "SELECT predicate, object, valid_from, valid_to, confidence FROM triples WHERE subject = ? ORDER BY valid_from",
                (ent[0],)
            ).fetchall():
                ended = f" → ended {t[3][:10]}" if t[3] else ""
                since = f" since {t[2][:10]}" if t[2] else ""
                print(f"  → {t[0]} {t[1]}{since}{ended}")
            # Incoming triples
            for t in conn.execute(
                "SELECT subject, predicate, valid_from, valid_to FROM triples WHERE object = ? ORDER BY valid_from",
                (ent[0],)
            ).fetchall():
                ended = f" → ended {t[3][:10]}" if t[3] else ""
                since = f" since {t[2][:10]}" if t[2] else ""
                print(f"  ← {t[0]} {t[1]}{since}{ended}")

    elif subcmd == "timeline":
        if len(args) < 2:
            print("Usage: gaius kg timeline <entity-name-or-id>")
            return
        term = args[1].lower()
        entities = conn.execute(
            "SELECT id, name, type FROM entities WHERE id LIKE ? OR name LIKE ?",
            (f"%{term}%", f"%{term}%")
        ).fetchall()
        if not entities:
            print(f"No entities matching '{term}'")
            return
        eid = entities[0][0]
        print(f"\nTimeline for {BOLD}{entities[0][1]}{RESET} ({entities[0][2]}):\n")
        events = conn.execute("""
            SELECT valid_from, predicate, object, valid_to, source_agent, 'out' as dir FROM triples WHERE subject = ?
            UNION ALL
            SELECT valid_from, predicate, subject, valid_to, source_agent, 'in' as dir FROM triples WHERE object = ?
            ORDER BY valid_from NULLS LAST
        """, (eid, eid)).fetchall()
        for ev in events:
            date = ev[0][:10] if ev[0] else "????"
            arrow = "→" if ev[5] == "out" else "←"
            ended = f" (ended {ev[3][:10]})" if ev[3] else ""
            agent = f" [{ev[4]}]" if ev[4] else ""
            print(f"  {date}  {arrow} {ev[1]} {ev[2]}{ended}{agent}")

    elif subcmd == "index":
        print("Indexing knowledge graph from facts.db...")
        facts = conn.execute("SELECT id, fact_text, domain, first_seen FROM facts WHERE tombstoned_at IS NULL").fetchall()
        before_e = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        before_t = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        for fact in facts:
            kg_index_fact(conn, fact[0], fact[1], fact[2], timestamp=fact[3])
        conn.commit()
        after_e = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        after_t = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        print(f"Done. Entities: {before_e} → {after_e} (+{after_e - before_e}). Triples: {before_t} → {after_t} (+{after_t - before_t}).")

    elif subcmd == "invalidate":
        if len(args) < 4:
            print("Usage: gaius kg invalidate <subject-id> <predicate> <object-id>")
            return
        invalidate_triple(conn, args[1], args[2], args[3])
        print(f"✓ Invalidated: {args[1]} {args[2]} {args[3]}")

    else:
        print(f"Unknown kg subcommand: {subcmd}")
        print("Available: stats, query, timeline, index, invalidate")
