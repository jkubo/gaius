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

import argparse
import math
import re
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Foundation symbols owned by _core. All are defined well before _core's bottom
# `from gaius.kg import ...` (line ~8618), so this top-level import resolves
# against the partially-initialised _core module — the proven parsers.py pattern.
from gaius._core import _gaius_cfg, init_db, BOLD, RESET, MEMORY_DIR


_BUILTIN_ENTITY_PATTERNS: dict[str, str] = {
    # K8s node naming convention (3-5 short dash-joined segments ending in a
    # 2-3 digit ordinal, e.g. k8s-r1-web-gpu-01). Segments capped at 4 chars —
    # longer segments are prose slugs, not node names. The pre-2026-07-03
    # pattern required letter-only leading segments and 5 segments exactly — it
    # never matched names like "k8s-..." (digit in segment) and left the KG
    # without a single node entity. Customize via entities.patterns in config.
    "node":      r'\b(?:[a-z][a-z0-9]{0,3}-){3,5}\d{2,3}\b',
    # Widely-used infrastructure software (generic products only — deployment-specific
    # names belong in entities.patterns in config; see presets/k8s.yaml).
    # Compound-product guards: "Grafana Alloy/Loki/Mimir/Tempo" is a mention of
    # the second product, not the Grafana dashboard; "Docker Hub" is a registry,
    # not the docker runtime.
    "service":   r'\b(?:traefik|nginx|haproxy|envoy|istio|cert-manager|oauth2-proxy|keycloak|authelia'
                 r'|grafana(?!\s+(?:alloy|loki|mimir|tempo))|prometheus|thanos|loki|mimir|alloy|otel-collector|jaeger|tempo'
                 r'|jupyterlab|gitea|forgejo|gitlab|argocd|fluxcd'
                 r'|timescaledb|postgresql|postgres|mariadb|mysql|redis|mongodb|elasticsearch|opensearch|kibana'
                 r'|clickhouse|couchdb|cassandra|memcached|rabbitmq|kafka|redpanda|nats'
                 r'|etcd|coredns|flannel|multus|cilium|calico|metallb'
                 r'|longhorn|linstor|drbd|seaweedfs|minio|ceph|rook|velero'
                 r'|headscale|tailscale|wireguard|nebula|cloudflared'
                 r'|openbao|tetragon|falco|osquery|kube-bench|splunk'
                 r'|ollama|containerd|docker(?!\s+hub)|podman|helm|k3s|kubevirt|kuberay'
                 r'|technitium|hedgedoc|headlamp|piraeus)\b',
    # Namespace mentions: HIGH-PRECISION anchors only ("kubectl ... -n X",
    # "--namespace X", "in namespace X"). Looser forms flood the graph with
    # prose false positives — "X namespace"/"namespace X" catch adjectives/
    # verbs, the colon form ("namespace: X") catches Tetragon Linux-ns
    # selectors and YAML listings, and a bare "-n X" catches every shell flag
    # (grep -n, sort -n) — so the short flag requires a k8s CLI word earlier on
    # the same line. All measured on a 13K-fact corpus; precision wins over
    # recall here. First non-empty group = the name.
    "namespace": r'\b(?:in|into)\s+namespace\s+([a-z][a-z0-9-]{1,30})\b'
                 r'|\b(?:kubectl|helm|k9s|kubens|oc)\b[^\n]{0,120}?\s-n[= ]([a-z][a-z0-9-]{1,30})\b'
                 r'|(?:^|\s)--namespace[= ]([a-z][a-z0-9-]{1,30})\b',
    # Incident / failure vocabulary (generic; high-frequency prose words like
    # "timeout"/"degraded" deliberately excluded — they drown the graph)
    "incident":  r'\b(?:cascade|outage|split-brain|quorum[\s-]loss|crashloop(?:backoff)?|oomkill(?:ed)?|deadlock|unreachable|data[\s-]loss)\b',
    # CVE identifiers — highest-precision entity type
    "cve":       r'\bCVE-\d{4}-\d{4,7}\b',
    # Model-name convention like gemma4-31b / llama-3-70b / qwen2-72b — middle
    # segments may be bare version numbers ("-3-"). pvc- excluded: K8s PVC
    # resource names (pvc-273b...) match the size suffix.
    "model":     r'\b(?!pvc-)[a-z][\w.]*-(?:[\w.]+-)*\d{1,3}b\b',
}

# Alias → canonical entity-name merges, applied post-extraction. Builtins cover
# generic morphology (verb forms, product-name variants); deployment-specific
# aliases (short node names, brand↔codename) belong in entities.aliases in
# config. Without this, one real-world thing splits into twin entities and
# every query silently misses half its facts.
_BUILTIN_ALIASES = {
    "postgres": "postgresql",
    "oomkilled": "oomkill",
    "crashloopbackoff": "crashloop",
}


def _load_entity_aliases() -> dict:
    """Merge builtin + config alias maps (config wins on conflicts).

    Config schema:
      entities:
        aliases:
          acme-web-platform: awp
          web-gpu-01: k8s-r1-web-gpu-01
    """
    aliases = dict(_BUILTIN_ALIASES)
    cfg = _gaius_cfg.get("entities", {}).get("aliases", {}) or {}
    aliases.update({str(k).lower(): str(v).lower() for k, v in cfg.items()})
    return aliases


_ENTITY_ALIASES = _load_entity_aliases()

# Captured entity names that are prose artifacts, not entities (the context-
# anchored namespace patterns can capture a determiner, preposition, verb, or
# adverb: "this namespace", "in namespace X", "namespace is broken").
_NAME_STOPWORDS = {
    "the", "a", "an", "this", "that", "these", "those", "same", "new", "old",
    "each", "every", "all", "any", "some", "no", "not", "own", "its", "their",
    "your", "our", "my", "one", "wrong", "right", "which", "whole", "entire",
    "in", "into", "per", "from", "to", "of", "for", "with", "between",
    "across", "on", "at", "by", "under", "over", "via",
    "is", "was", "are", "were", "be", "been", "being", "has", "have", "had",
    "do", "does", "did", "will", "would", "can", "could", "should", "shall",
    "may", "might", "must", "now", "then", "here", "there", "also", "only",
    "still", "just", "yet", "again", "already", "and", "or", "but", "if",
    "as", "so", "than", "too", "very", "it", "when", "where", "gets", "got",
    "ok", "yes", "true", "false", "null", "none",
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


def _extract_entities_spans(text: str) -> list[tuple[str, str, str, int]]:
    """Extract (entity_id, entity_name, entity_type, first_offset) from text.

    Patterns may use capture groups (e.g. the context-anchored namespace pattern);
    the first non-empty group is the entity name, otherwise the whole match.
    Captured names that are prose stopwords are dropped; aliases are canonicalized
    (postgres → postgresql, short node names → full names via config).
    first_offset is the character position of the entity's first mention — used
    for proximity-windowed co-occurrence pairing.
    """
    entities = []
    seen = set()
    for etype, pattern in _ENTITY_PATTERNS.items():
        for match in pattern.finditer(text):
            if match.groups():
                name = next((g for g in match.groups() if g), None)
                if not name:
                    continue
            else:
                name = match.group(0)
            name = name.lower().strip()
            if not name or name in _NAME_STOPWORDS:
                continue
            name = _ENTITY_ALIASES.get(name, name)
            eid = f"{etype}:{name}"
            if eid not in seen:
                seen.add(eid)
                entities.append((eid, name, etype, match.start()))
    return entities


def extract_entities(text: str) -> list[tuple[str, str, str]]:
    """Extract (entity_id, entity_name, entity_type) tuples from text. See
    _extract_entities_spans for extraction semantics."""
    return [(eid, name, etype) for eid, name, etype, _pos in _extract_entities_spans(text)]


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


def add_cooccurrence(conn: sqlite3.Connection, subject: str, predicate: str, obj: str,
                     valid_from: str = None, confidence: float = 0.6,
                     source_session: str = None, source_agent: str = None,
                     source_fact_id: int = None):
    """Insert or reinforce an aggregated co-occurrence edge.

    Unlike add_triple (temporal — dedup includes valid_from, so each fact makes
    a new row), co-occurrence edges aggregate: one active row per (s, p, o),
    with weight = number of facts that mentioned the pair together. Weight is
    the edge-strength signal for kg query ranking and export-links."""
    row = conn.execute(
        "SELECT id FROM triples WHERE subject = ? AND predicate = ? AND object = ? AND valid_to IS NULL",
        (subject, predicate, obj)).fetchone()
    if row:
        conn.execute("UPDATE triples SET weight = COALESCE(weight, 1) + 1 WHERE id = ?", (row[0],))
    else:
        conn.execute("""
            INSERT INTO triples (subject, predicate, object, valid_from, confidence,
                                source_session, source_agent, source_fact_id, weight)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (subject, predicate, obj, valid_from, confidence,
              source_session, source_agent, source_fact_id))


# Co-occurrence predicates are deliberately WEAK: two entities appearing in the
# same fact proves only that they were mentioned together, so co-occurrence
# emits co_occurs_with (same type) / mentioned_with (different types) — always
# symmetric, canonical subject = lexicographically smaller id. Strong semantics
# (affected_by, runs_on, uses_storage) are reserved for the explicit
# _RELATION_PATTERNS, which match an actual verb phrase. The adversarial audit
# of the first rebuild (2026-07-03) caught co-occurrence-derived affected_by
# asserting that postgresql was hit by a Splunk CVE because both appeared in
# one threat-intel fact — a class of misattribution this rule eliminates.

# Types excluded from co-occurrence pairing (they appear in too many facts to
# carry edge signal — e.g. the posting agent's own name). They still get
# entities + fact_entities rows, so retrieval and export-links can use them.
_COOCCUR_EXCLUDED_TYPES = {"agent", "skill"}

# Per-fact cap on entities eligible for pairing: C(8,2)=28 edges max per fact.
# kg index reports how many entity slots the cap dropped (no silent truncation).
_MAX_COOCCUR_ENTITIES = 8

# Only pair entities whose first mentions sit within this many characters of
# each other. Long multi-topic facts (incident timelines, audit dumps) otherwise
# pair a header entity with an unrelated footnote entity.
_COOCCUR_WINDOW_CHARS = 300


def refresh_entity_domains(conn: sqlite3.Connection):
    """Set each entity's domain to the majority domain of its linked facts
    (ignoring 'general' unless it is the only domain seen). Uses fact_entities;
    entities with no fact links keep their first-insert domain."""
    conn.execute("""
        UPDATE entities SET domain = COALESCE(
            (SELECT f.domain FROM fact_entities fe JOIN facts f ON f.id = fe.fact_id
             WHERE fe.entity_id = entities.id AND f.tombstoned_at IS NULL AND f.domain != 'general'
             GROUP BY f.domain ORDER BY COUNT(*) DESC, f.domain LIMIT 1),
            (SELECT f.domain FROM fact_entities fe JOIN facts f ON f.id = fe.fact_id
             WHERE fe.entity_id = entities.id AND f.tombstoned_at IS NULL
             GROUP BY f.domain ORDER BY COUNT(*) DESC, f.domain LIMIT 1),
            domain)
    """)


def kg_index_fact(conn: sqlite3.Connection, fact_id: int, fact_text: str, domain: str,
                  session_uuid: str = None, agent: str = None, timestamp: str = None) -> int:
    """Extract entities and relations from a fact and add to the KG.

    - every extracted entity → entities row + fact_entities membership row
      (relation-derived entities included — the first rebuild left them without
      fact links, breaking the every-entity-has-a-fact invariant)
    - explicit relation patterns → temporal triples with strong predicates
    - co-occurrence within _COOCCUR_WINDOW_CHARS → weight-aggregated SYMMETRIC
      weak edges only (co_occurs_with / mentioned_with), capped at
      _MAX_COOCCUR_ENTITIES eligible entities per fact; returns the number of
      entity slots dropped by the cap
    - stamps facts.kg_indexed_at so incremental `kg index` runs never
      double-count a fact's pairs (weight aggregation is not idempotent)
    """
    entities = _extract_entities_spans(fact_text)
    for eid, name, etype, _pos in entities:
        upsert_entity(conn, eid, name, etype, domain)
        if fact_id is not None:
            conn.execute("INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?, ?)",
                         (fact_id, eid))

    # Explicit relation patterns
    relations = extract_relations(fact_text)
    for subj_id, predicate, obj_id, subj_type, obj_type in relations:
        upsert_entity(conn, subj_id, subj_id.split(":", 1)[1], subj_type, domain)
        upsert_entity(conn, obj_id, obj_id.split(":", 1)[1], obj_type, domain)
        if fact_id is not None:
            conn.execute("INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?, ?)",
                         (fact_id, subj_id))
            conn.execute("INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?, ?)",
                         (fact_id, obj_id))
        add_triple(conn, subj_id, predicate, obj_id,
                   valid_from=timestamp, source_session=session_uuid,
                   source_agent=agent, source_fact_id=fact_id)

    # Weak co-occurrence: entities first-mentioned near each other are related.
    eligible = [(eid, etype, pos) for eid, _name, etype, pos in entities
                if etype not in _COOCCUR_EXCLUDED_TYPES]
    skipped = max(0, len(eligible) - _MAX_COOCCUR_ENTITIES)
    eligible = eligible[:_MAX_COOCCUR_ENTITIES]
    for i in range(len(eligible)):
        for j in range(i + 1, len(eligible)):
            (id_a, type_a, pos_a), (id_b, type_b, pos_b) = eligible[i], eligible[j]
            if abs(pos_a - pos_b) > _COOCCUR_WINDOW_CHARS:
                continue
            predicate = "co_occurs_with" if type_a == type_b else "mentioned_with"
            subj, obj = sorted((id_a, id_b))
            add_cooccurrence(conn, subj, predicate, obj,
                             valid_from=timestamp, source_session=session_uuid,
                             source_agent=agent, source_fact_id=fact_id)

    if fact_id is not None:
        conn.execute("UPDATE facts SET kg_indexed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                     (fact_id,))
    return skipped


def cmd_kg(args):
    """Knowledge Graph operations: query, timeline, index, export-links, invalidate, stats.

    Usage:
      gaius kg stats                          — overview of entities + triples
      gaius kg query <entity>                 — all triples for an entity
      gaius kg timeline <entity>              — chronological story of an entity
      gaius kg index [--rebuild]              — index un-indexed facts (--rebuild: wipe + full pass)
      gaius kg export-links [--dry-run]       — write derived Related-links into vault markdown
      gaius kg invalidate <subj> <pred> <obj> — mark a triple as ended
    """
    if not args or args[0] in ("-h", "--help"):
        print(cmd_kg.__doc__)
        return

    subcmd = args[0]

    if subcmd == "export-links":
        cmd_kg_export_links(args[1:])
        return

    conn = init_db()

    if subcmd == "stats":
        n_entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        n_triples = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        n_active = conn.execute("SELECT COUNT(*) FROM triples WHERE valid_to IS NULL").fetchone()[0]
        n_fe = conn.execute("SELECT COUNT(*) FROM fact_entities").fetchone()[0]
        print(f"Knowledge Graph Statistics:")
        print(f"  Entities:       {n_entities}")
        print(f"  Triples:        {n_triples} ({n_active} active, {n_triples - n_active} ended)")
        print(f"  Fact links:     {n_fe} (fact ↔ entity memberships)")
        print()
        if n_entities > 0:
            print("  By entity type:")
            for row in conn.execute("SELECT type, COUNT(*) c FROM entities GROUP BY type ORDER BY c DESC"):
                print(f"    {row[0]:<15} {row[1]:>5}")
        if n_triples > 0:
            print("  By predicate:")
            for row in conn.execute("SELECT predicate, COUNT(*) c FROM triples GROUP BY predicate ORDER BY c DESC"):
                print(f"    {row[0]:<20} {row[1]:>5}")
            print("  Heaviest edges (co-occurrence weight):")
            for row in conn.execute(
                "SELECT subject, predicate, object, COALESCE(weight,1) w FROM triples "
                "WHERE valid_to IS NULL ORDER BY w DESC LIMIT 10"):
                print(f"    {row[3]:>4}x  {row[0]} —{row[1]}→ {row[2]}")

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
        rebuild = "--rebuild" in args
        if rebuild:
            print("Rebuilding knowledge graph from scratch (entities, triples, fact links wiped)...")
            conn.execute("DELETE FROM triples")
            conn.execute("DELETE FROM entities")
            conn.execute("DELETE FROM fact_entities")
            conn.execute("UPDATE facts SET kg_indexed_at = NULL")
            conn.commit()
        print("Indexing knowledge graph from facts.db (un-indexed facts only)...")
        facts = conn.execute(
            "SELECT id, fact_text, domain, first_seen FROM facts "
            "WHERE tombstoned_at IS NULL AND kg_indexed_at IS NULL").fetchall()
        before_e = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        before_t = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        capped_slots = 0
        capped_facts = 0
        for fact in facts:
            skipped = kg_index_fact(conn, fact[0], fact[1], fact[2], timestamp=fact[3])
            if skipped:
                capped_facts += 1
                capped_slots += skipped
        refresh_entity_domains(conn)
        conn.commit()
        after_e = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        after_t = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        n_fe = conn.execute("SELECT COUNT(*) FROM fact_entities").fetchone()[0]
        print(f"Done. {len(facts)} facts indexed. Entities: {before_e} → {after_e} (+{after_e - before_e}). "
              f"Triples: {before_t} → {after_t} (+{after_t - before_t}). Fact links: {n_fe}.")
        if capped_facts:
            print(f"  Coverage note: {capped_facts} facts hit the {_MAX_COOCCUR_ENTITIES}-entity "
                  f"co-occurrence cap ({capped_slots} entity slots not paired).")
        # Invariant: every entity should be reachable from at least one fact
        orphans = conn.execute(
            "SELECT COUNT(*) FROM entities e WHERE NOT EXISTS "
            "(SELECT 1 FROM fact_entities fe WHERE fe.entity_id = e.id)").fetchone()[0]
        if orphans:
            print(f"  ⚠ invariant: {orphans} entities have no fact_entities link")

    elif subcmd == "invalidate":
        if len(args) < 4:
            print("Usage: gaius kg invalidate <subject-id> <predicate> <object-id>")
            return
        invalidate_triple(conn, args[1], args[2], args[3])
        print(f"✓ Invalidated: {args[1]} {args[2]} {args[3]}")

    else:
        print(f"Unknown kg subcommand: {subcmd}")
        print("Available: stats, query, timeline, index, export-links, invalidate")


# ── export-links: materialize derived edges into the vault markdown ──────────
#
# The Obsidian graph draws edges only from links in the markdown itself, but the
# corpus's connective tissue lives in entity mentions. export-links derives
# related-note links from SHARED ENTITIES (IDF-weighted, so rare specific
# entities count more than ubiquitous ones) and writes a one-line, marker-
# delimited footer. Deterministic + idempotent: same files + patterns → same
# footers. Edges are evidence-derived, never invented.

# Directories whose files RECEIVE a Related footer. (malint/ deliberately
# absent: its md files are per-session detonation logs in subdirs — logs don't
# get links, same rule as handoffs/.)
_EXPORT_RECIPIENT_DIRS = ["feedback", "project", "troubleshooting", "reference", "user"]
# Directories whose files are link TARGETS only (hand-curated hubs / byte-gated
# domain files gain inbound edges without being rewritten).
_EXPORT_TARGET_ONLY_DIRS = ["domain", "specs"]
# Entity types that never count as link evidence between FILES (agent/skill
# names appear everywhere). Incident words are WEAK evidence: they support a
# link but can't establish one alone — "quorum loss" joins DRBD notes to etcd
# notes across unrelated subsystems (measured 30% spurious-link rate), yet
# "split-brain" between two MySQL-failover notes is exactly right when a
# concrete entity co-occurs. All types still live in the KG proper.
_EXPORT_LINK_TYPES_EXCLUDED = {"agent", "skill"}
_EXPORT_LINK_TYPES_WEAK = {"incident"}
# Never write into: INDEX.md (feedback/INDEX.md is injected at session start —
# every byte there costs every session), underscore-prefixed files, generated files.
_EXPORT_EXCLUDE_NAMES = {"INDEX.md", "MEMORY.md"}
_EXPORT_MAX_BYTES = 15000   # stay clear of the mnemosyne 16KB byte-warn gate
_EXPORT_MAX_DF = 0.20       # entities in >20% of files carry no link signal

_RELATED_BEGIN = "<!-- gaius:related begin -->"
_RELATED_END = "<!-- gaius:related end -->"
_RELATED_BLOCK_RE = re.compile(
    re.escape(_RELATED_BEGIN) + r".*?" + re.escape(_RELATED_END) + r"\n?",
    re.DOTALL)


def _is_generated_file(text: str) -> bool:
    head = text[:2000]
    return "AUTO-GENERATED" in head or "Do not edit" in head


def _vault_link_target(path: Path, root: Path, stem_counts: dict) -> str:
    """Wikilink target: bare stem when unique vault-wide, else vault-relative path."""
    if stem_counts.get(path.stem, 0) <= 1:
        return path.stem
    rel = path.relative_to(root)
    return str(rel.with_suffix(""))


def cmd_kg_export_links(args):
    """Derive related-note links from shared entities and write vault footers.

    Usage: gaius kg export-links [--root DIR] [--max-links N] [--min-shared N] [--dry-run]
    """
    p = argparse.ArgumentParser(prog="gaius kg export-links")
    p.add_argument("--root", default=None, help="vault root (default: memory_dir)")
    p.add_argument("--max-links", type=int, default=4)
    p.add_argument("--min-shared", type=int, default=2,
                   help="min shared entities for a link (1 rare entity also qualifies)")
    p.add_argument("--dry-run", action="store_true")
    ns = p.parse_args(args)

    root = Path(ns.root).expanduser() if ns.root else MEMORY_DIR
    if not root or not Path(root).is_dir():
        print(f"export-links: vault root not found ({root}) — set memory_dir or --root", file=sys.stderr)
        return
    root = Path(root).resolve()

    # Collect corpus files: recipients (get footers) + target-only (inbound edges only)
    recipients, targets_only = [], []
    for d in _EXPORT_RECIPIENT_DIRS:
        recipients.extend(sorted((root / d).glob("*.md")) if (root / d).is_dir() else [])
    for d in _EXPORT_TARGET_ONLY_DIRS:
        targets_only.extend(sorted((root / d).glob("*.md")) if (root / d).is_dir() else [])

    corpus = {}     # path → set(entity_id)
    texts = {}      # path → current text (recipients only)
    skipped_size, skipped_generated = [], []
    for path in recipients + targets_only:
        if path.name in _EXPORT_EXCLUDE_NAMES or path.name.startswith("_"):
            continue
        try:
            text = path.read_text()
        except Exception:
            continue
        is_recipient = path in set(recipients)
        if is_recipient and _is_generated_file(text):
            skipped_generated.append(path)
            continue
        if is_recipient and len(text.encode()) > _EXPORT_MAX_BYTES:
            skipped_size.append(path)
            # oversized files still act as link targets
        body = _RELATED_BLOCK_RE.sub("", text)  # never let our own footer feed extraction
        ents = {eid for eid, _n, etype in extract_entities(body)
                if etype not in _EXPORT_LINK_TYPES_EXCLUDED}
        if ents:
            corpus[path] = ents
        if is_recipient:
            texts[path] = text

    n_files = len(corpus)
    if n_files < 2:
        print("export-links: fewer than 2 files with entities — nothing to link")
        return

    # Entity → files inverted index; document-frequency filter
    entity_files = {}
    for path, ents in corpus.items():
        for e in ents:
            entity_files.setdefault(e, set()).add(path)
    max_df = max(2, int(n_files * _EXPORT_MAX_DF))
    idf = {e: math.log(n_files / len(files))
           for e, files in entity_files.items() if 1 < len(files) <= max_df}

    # Wikilink targets resolve by basename vault-wide; disambiguate collisions
    # (e.g. INDEX.md exists in several dirs) with vault-relative paths.
    stem_counts = {}
    for f in root.rglob("*.md"):
        if any(part in (".obsidian", ".git", ".venv", "__pycache__") for part in f.parts):
            continue
        stem_counts[f.stem] = stem_counts.get(f.stem, 0) + 1

    written, unchanged, no_links = [], [], []
    skipped_set = set(skipped_size)
    for path, text in sorted(texts.items()):
        if path in skipped_set:
            continue
        my_ents = {e for e in corpus.get(path, set()) if e in idf}
        scores = {}   # other_path → [score, shared_count, strong_shared_count]
        for e in my_ents:
            weak = e.split(":", 1)[0] in _EXPORT_LINK_TYPES_WEAK
            for other in entity_files[e]:
                if other == path:
                    continue
                s = scores.setdefault(other, [0.0, 0, 0])
                s[0] += idf[e]
                s[1] += 1
                if not weak:
                    s[2] += 1
        # Hub damping: a target dense with entities (domain hubs, overview
        # specs) matches everything cheaply — normalize by its entity count so
        # links prefer specifically-related notes over generic hubs. min_shared
        # is strict (no single-entity exception — one shared token as a file's
        # ONLY link measured worse than no link at all) and at least one shared
        # entity must be a strong type (weak incident words can't stand alone).
        candidates = [(other, s[0] / math.log2(2 + len(corpus.get(other, ()))))
                      for other, s in scores.items()
                      if s[1] >= ns.min_shared and s[2] >= 1]
        candidates.sort(key=lambda x: (-x[1], x[0].name))
        top = [c[0] for c in candidates[:ns.max_links]]

        if top:
            links = " · ".join(f"[[{_vault_link_target(t, root, stem_counts)}]]" for t in top)
            block = f"{_RELATED_BEGIN}\n**Related:** {links}\n{_RELATED_END}\n"
            if _RELATED_BEGIN in text:
                new_text = _RELATED_BLOCK_RE.sub(block, text)
            else:
                new_text = text.rstrip("\n") + "\n\n" + block
        else:
            no_links.append(path)
            new_text = _RELATED_BLOCK_RE.sub("", text) if _RELATED_BEGIN in text else text

        if new_text == text:
            unchanged.append(path)
            continue
        if ns.dry_run:
            targets = ", ".join(t.stem for t in top) if top else "(footer removed)"
            print(f"  would write {path.relative_to(root)}: {targets}")
            written.append(path)
            continue
        path.write_text(new_text)
        written.append(path)

    print(f"export-links: {len(written)} written, {len(unchanged)} unchanged, "
          f"{len(no_links)} without qualifying links, {len(skipped_size)} skipped (> {_EXPORT_MAX_BYTES}B), "
          f"{len(skipped_generated)} skipped (generated)")
    print(f"  corpus: {n_files} files ({len(recipients)} recipient-dir, {len(targets_only)} target-only); "
          f"dirs not scanned by design: handoffs/ daily/ malint/ skills/ skills-grok/ juleis/ gaius/")
    if skipped_size:
        for path in skipped_size:
            print(f"  size-skipped: {path.relative_to(root)}")
