"""gaius.facts — the SQLite facts store: schema, upsert, dedup, confidence.

Owns init_db (schema + migrations + vec0 virtual table), fact upsert with
semantic dedup (_find_semantic_duplicate; _SEM_DEDUP_WARNED module state stays
here), confidence scoring + hedge/observed patterns, contradiction checks,
per-fact embedding writes, session registration, and clarified-intent
distillation upserts.

Facade convention (see ARCHITECTURE.md): hub constants are read at call time as
`_core.NAME` (DB_PATH is monkeypatched on gaius._core by 8 test files — a
from-import would freeze the unpatched value). kg_index_fact is imported at the
call site inside its guard: KG import/indexing failure must never block fact
ingestion, and the lazy import keeps facade ordering irrelevant.
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import sqlite_vec  # noqa: F401  (loaded into connections by init_db)
except ImportError:  # pragma: no cover
    sqlite_vec = None

import gaius._core as _core
# imports from gaius._core (shared hub) — circular-by-design, see ARCHITECTURE.md
from gaius._core import HAS_SQLITE_VEC, agent_to_principal
from gaius.embed import _embed_text, _chunk_text, _EMBED_DIM

# ── v2: SQLite facts index ────────────────────────────────────────────────────

def _dedup_live_fact_keys(conn: sqlite3.Connection) -> int:
    """One-time merge of live rows sharing a fact_key (cross-domain + race dupes).

    Keeps the oldest row, sums confirmation counts, unions the provenance
    arrays, tombstones the rest, and drops their embeddings. Idempotent.
    Returns the number of rows tombstoned."""
    now = datetime.now(timezone.utc).isoformat()
    groups = conn.execute(
        "SELECT fact_key FROM facts WHERE tombstoned_at IS NULL "
        "GROUP BY fact_key HAVING COUNT(*) > 1"
    ).fetchall()
    merged = 0
    for g in groups:
        rows = conn.execute(
            "SELECT * FROM facts WHERE fact_key = ? AND tombstoned_at IS NULL ORDER BY id",
            (g["fact_key"],)
        ).fetchall()
        keeper, losers = rows[0], rows[1:]
        total_conf = sum(r["confirmation_count"] or 1 for r in rows)
        last_seen = max((r["last_seen"] or "") for r in rows)

        def _union(col):
            out = []
            for r in rows:
                try:
                    for v in json.loads(r[col] or "[]"):
                        if v not in out:
                            out.append(v)
                except (TypeError, ValueError):
                    pass
            return json.dumps(out)

        conn.execute(
            "UPDATE facts SET confirmation_count=?, last_seen=?, agents=?, "
            "sessions=?, model_families=?, principals=?, model_versions=? WHERE id=?",
            (total_conf, last_seen, _union("agents"), _union("sessions"),
             _union("model_families"), _union("principals"), _union("model_versions"),
             keeper["id"]))
        for r in losers:
            conn.execute("UPDATE facts SET tombstoned_at=? WHERE id=?", (now, r["id"]))
            try:
                conn.execute("DELETE FROM fact_embeddings WHERE fact_id=?", (r["id"],))
            except sqlite3.Error:
                pass
        merged += len(losers)
    conn.commit()
    return merged


def init_db(db_path: Path = None) -> sqlite3.Connection:
    """Create or open the facts database. Returns open connection."""
    if db_path is None:
        db_path = _core.DB_PATH
    # Test isolation guard: if running under pytest, refuse to open the real DB
    if "PYTEST_CURRENT_TEST" in os.environ and str(db_path) == str(Path.home() / ".gaius" / "facts.db"):
        raise RuntimeError(f"Test attempted to open live DB at {db_path}. Use a tmp_path fixture.")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    # WAL mode: survives concurrent readers and is more resilient to crashes
    # than the default rollback journal. Critical because multiple writers
    # (session-stop hook, nightly sync, K8s CronJob) touch this file.
    conn.execute("PRAGMA journal_mode=WAL")
    # Wait out concurrent writers instead of failing with 'database is locked'
    # (stop hook vs nightly ~03:00 overlap produced race duplicates on 05-18).
    conn.execute("PRAGMA busy_timeout=15000")
    # Load sqlite-vec extension if available
    if HAS_SQLITE_VEC:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS facts (
            id                  INTEGER PRIMARY KEY,
            domain              TEXT NOT NULL,
            fact_key            TEXT NOT NULL,
            fact_text           TEXT NOT NULL,
            first_seen          TEXT,
            last_seen           TEXT,
            confirmation_count  INTEGER DEFAULT 1,
            agents              TEXT DEFAULT '[]',
            sessions            TEXT DEFAULT '[]',
            provenance          TEXT,
            score               REAL DEFAULT 0.0,
            outcome             TEXT,
            source_agent        TEXT,
            principals          TEXT DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS sessions (
            uuid                TEXT PRIMARY KEY,
            origin              TEXT,
            agent               TEXT,
            project             TEXT,
            size_bytes          INTEGER,
            processed_at        TEXT,
            compaction_present  INTEGER DEFAULT 0,
            fact_count          INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS domains (
            name                TEXT PRIMARY KEY,
            spec_path           TEXT,
            keywords            TEXT DEFAULT '[]',
            maturity_score      REAL DEFAULT 0.0,
            last_computed       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_facts_domain_score ON facts(domain, score DESC);
        CREATE INDEX IF NOT EXISTS idx_facts_fact_key ON facts(fact_key);

        -- Knowledge Graph: temporal entity-relationship triples
        -- Temporal entity-relationship schema with validity windows.
        -- Entity types: node, service, storage-pool, namespace, agent, model, incident
        CREATE TABLE IF NOT EXISTS entities (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            type        TEXT DEFAULT 'unknown',
            domain      TEXT,
            properties  TEXT DEFAULT '{}',
            created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE TABLE IF NOT EXISTS triples (
            id              INTEGER PRIMARY KEY,
            subject         TEXT NOT NULL,
            predicate       TEXT NOT NULL,
            object          TEXT NOT NULL,
            valid_from      TEXT,
            valid_to        TEXT,
            confidence      REAL DEFAULT 1.0,
            source_session  TEXT,
            source_agent    TEXT,
            source_fact_id  INTEGER,
            extracted_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (subject) REFERENCES entities(id),
            FOREIGN KEY (object) REFERENCES entities(id)
        );
        CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject);
        CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object);
        CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate);
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);

        -- Fact ↔ entity membership (which facts mention which entities).
        -- Powers entity-grounded routing (route_suggest) and entity domain
        -- majority votes (kg.refresh_entity_domains).
        CREATE TABLE IF NOT EXISTS fact_entities (
            fact_id     INTEGER NOT NULL,
            entity_id   TEXT NOT NULL,
            PRIMARY KEY (fact_id, entity_id)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_fact_entities_entity ON fact_entities(entity_id);
    """)
    # Schema migrations — safe to run on existing DBs (ignore duplicate column errors)
    for _migration in [
        "ALTER TABLE facts ADD COLUMN fact_type TEXT DEFAULT 'operational'",
        "ALTER TABLE facts ADD COLUMN verification_expected TEXT",
        "ALTER TABLE facts ADD COLUMN verification_type TEXT DEFAULT 'contains'",
        "ALTER TABLE facts ADD COLUMN last_verified_at TEXT",
        "ALTER TABLE facts ADD COLUMN last_verification_result TEXT",
        "ALTER TABLE facts ADD COLUMN tombstoned_at TEXT",
        "ALTER TABLE facts ADD COLUMN tombstone_reason TEXT",
        "ALTER TABLE facts ADD COLUMN injection_weight REAL DEFAULT 1.0",

        "ALTER TABLE facts ADD COLUMN model_family TEXT DEFAULT 'claude'",
        "ALTER TABLE facts ADD COLUMN model_families TEXT DEFAULT '[\"claude\"]'",
        "ALTER TABLE facts ADD COLUMN source_agent TEXT",
        "ALTER TABLE facts ADD COLUMN principals TEXT DEFAULT '[]'",
        "ALTER TABLE facts ADD COLUMN model_version TEXT DEFAULT ''",
        "ALTER TABLE facts ADD COLUMN model_versions TEXT DEFAULT '[]'",
        "ALTER TABLE facts ADD COLUMN source TEXT DEFAULT 'human'",
        "ALTER TABLE facts ADD COLUMN verification_cmd TEXT DEFAULT ''",
        "ALTER TABLE facts ADD COLUMN fact_type TEXT DEFAULT 'operational'",
        "ALTER TABLE facts ADD COLUMN injection_weight REAL DEFAULT 1.0",

        # Confidence scoring + human review loop (2026-04-26)
        "ALTER TABLE facts ADD COLUMN confidence REAL DEFAULT 0.5",
        "ALTER TABLE facts ADD COLUMN confidence_source TEXT DEFAULT 'inferred'",
        "ALTER TABLE facts ADD COLUMN review_state TEXT DEFAULT 'auto'",
        "ALTER TABLE facts ADD COLUMN conflict_with TEXT",

        # Knowledge graph Gap-13 rebuild (2026-07-03): weight = co-occurrence
        # aggregation counter; kg_indexed_at = incremental-index watermark
        "ALTER TABLE triples ADD COLUMN weight INTEGER DEFAULT 1",
        "ALTER TABLE facts ADD COLUMN kg_indexed_at TEXT",
    ]:
        try:
            conn.execute(_migration)
        except sqlite3.OperationalError:
            pass  # column already exists

    # Index for fast review queue queries
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_review ON facts(review_state, confidence)")
    except sqlite3.OperationalError:
        pass

    # Race-proof upsert target: one live row per fact_key. Pre-06-10 DBs carry
    # duplicate live keys (cross-domain + race dupes) — auto-merge them once,
    # then index. Self-healing so stale S3/cluster copies fix themselves.
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_facts_live_key "
                     "ON facts(fact_key) WHERE tombstoned_at IS NULL")
    except sqlite3.IntegrityError:
        merged = _dedup_live_fact_keys(conn)
        print(f"facts.db migration: merged {merged} duplicate live fact rows",
              file=sys.stderr)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_facts_live_key "
                     "ON facts(fact_key) WHERE tombstoned_at IS NULL")

    # Vector embedding table (sqlite-vec) — stores 384-dim float vectors alongside fact IDs.
    # Used for semantic search and corroboration merge (dedup by cosine similarity).
    if HAS_SQLITE_VEC:
        try:
            conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS fact_embeddings USING vec0(embedding float[{_EMBED_DIM}], fact_id integer)")
        except sqlite3.OperationalError:
            pass  # already exists or vec0 not available

    conn.commit()
    return conn




#   ~/.grok/sessions/<urlencoded-cwd>/<uuid>/chat_history.jsonl  (+ summary.json)
#   ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl


def register_session(conn: sqlite3.Connection, uuid: str, origin: str, agent: str,
                     project: str, size_bytes: int, compaction_present: bool = False):
    """Insert session into dedup table. INSERT OR IGNORE — one row per UUID."""
    conn.execute("""
        INSERT OR IGNORE INTO sessions (uuid, origin, agent, project, size_bytes, processed_at, compaction_present)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (uuid, origin, agent, project, size_bytes,
          datetime.now(timezone.utc).isoformat(), 1 if compaction_present else 0))
    conn.commit()


_SEM_DEDUP_WARNED = False

def _find_semantic_duplicate(conn: sqlite3.Connection, domain: str, fact_text: str, threshold: float = 0.92) -> dict | None:
    """Find an existing fact that is semantically similar (cosine sim > threshold).
    Returns the matching fact row as a dict, or None.
    Uses sqlite-vec for efficient vector search."""
    if not HAS_SQLITE_VEC:
        return None
    # Multi-vector safety: a long (chunked) incoming fact compared chunk-wise against
    # the corpus risks a false-merge that silently discards distinct information, so
    # never auto-dedup when the incoming fact would be chunked.
    if len(_chunk_text(fact_text)) > 1:
        return None
    embedding = _embed_text(fact_text)
    if embedding is None:
        return None
    try:
        # Query vec0 for nearest neighbors, join with facts table.
        # Global (no domain filter): exact-key dedup is global now, so semantic
        # dedup must be too — a cross-domain near-duplicate is a corroboration,
        # not a new fact. Tombstoned rows excluded (vec slots are wasted on
        # them post-join; k=10 compensates).
        import struct
        vec_blob = struct.pack(f'{_EMBED_DIM}f', *embedding)
        rows = conn.execute("""
            SELECT f.*, fe.distance
            FROM fact_embeddings fe
            JOIN facts f ON f.id = fe.fact_id
            WHERE fe.embedding MATCH ?
              AND k = 10
              AND f.tombstoned_at IS NULL
        """, (vec_blob,)).fetchall()
        for row in rows:
            # sqlite-vec returns L2 distance; convert to cosine similarity
            # For normalized vectors: cosine_sim = 1 - (L2_dist^2 / 2)
            l2_dist = row["distance"]
            cosine_sim = 1.0 - (l2_dist ** 2 / 2.0)
            if cosine_sim >= threshold and row["fact_key"] != hashlib.sha256(fact_text.encode()).hexdigest()[:32]:
                # Only merge into a single-chunk (short) fact -- matching one chunk of a
                # long multi-chunk fact is not a whole-fact duplicate (false-merge guard).
                n_chunks = conn.execute(
                    "SELECT COUNT(*) FROM fact_embeddings WHERE fact_id = ?", (row["id"],)).fetchone()[0]
                if n_chunks == 1:
                    return dict(row)
    except Exception as e:
        # A broken vec0 extension or dimension mismatch silently disabling
        # dedup was invisible for weeks — make it loud once per process.
        global _SEM_DEDUP_WARNED
        if not _SEM_DEDUP_WARNED:
            print(f"Warning: semantic dedup unavailable ({e})", file=sys.stderr)
            _SEM_DEDUP_WARNED = True
    return None


def _store_embedding(conn: sqlite3.Connection, fact_id: int, fact_text: str):
    """Compute and store embedding(s) for a fact. No-op if dependencies unavailable.

    Long facts are split into <=256-token chunks, each embedded and stored as its own
    row (same fact_id), so retrieval can MAX over chunks instead of seeing only the
    first ~256 tokens. Short facts produce exactly one chunk == prior behavior."""
    if not HAS_SQLITE_VEC:
        return
    import struct
    try:
        # Upsert: delete all old chunk rows for this fact, then insert one per chunk.
        conn.execute("DELETE FROM fact_embeddings WHERE fact_id = ?", (fact_id,))
        for chunk in _chunk_text(fact_text):
            embedding = _embed_text(chunk)
            if embedding is None:
                continue
            vec_blob = struct.pack(f'{_EMBED_DIM}f', *embedding)
            conn.execute("INSERT INTO fact_embeddings (embedding, fact_id) VALUES (?, ?)", (vec_blob, fact_id))
    except Exception:
        pass


# ── Confidence Scoring + Human Review Loop ───────────────────────────────────

_HEDGE_PATTERNS = [
    r'\bappears? to\b', r'\bmight be\b', r'\bI think\b', r'\bI assumed?\b',
    r'\bprobably\b', r'\bseems? to\b', r'\bnot sure\b', r'\bmaybe\b',
    r'\bI believe\b', r'\bcould be\b',
]

_OBSERVED_PATTERNS = [
    r'kubectl get', r'kubectl describe', r'kubectl logs',
    r'\$ ', r'```',
    r'confirmed via', r'verified by', r'checked:',
]

_HARDWARE_STATE_DOMAINS = {'nodes', 'storage', 'networking'}


def _score_confidence(text: str, domain: str) -> tuple:
    """Return (confidence_score: float, source_label: str).

    Heuristic scoring based on language patterns:
    - Live observation evidence → 0.85
    - Hedged language (2+ hedges) → 0.25, (1 hedge) → 0.40
    - Hardware/state domain with no live evidence → 0.45
    - Default → 0.50
    """
    text_lower = text.lower()

    if any(re.search(p, text) for p in _OBSERVED_PATTERNS):
        return 0.85, 'inferred'

    hedge_count = sum(1 for p in _HEDGE_PATTERNS if re.search(p, text_lower))
    if hedge_count >= 2:
        return 0.25, 'inferred'
    if hedge_count == 1:
        return 0.40, 'inferred'

    if domain in _HARDWARE_STATE_DOMAINS:
        return 0.45, 'inferred'

    return 0.50, 'inferred'


_CONTRADICTION_STOP_WORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'it', 'its', 'this', 'that', 'these', 'those', 'has', 'have', 'had',
    'will', 'would', 'can', 'could', 'should', 'may', 'might', 'must',
    'from', 'by', 'as', 'if', 'into', 'through', 'after', 'before',
    'all', 'any', 'each', 'both', 'more', 'most', 'other', 'some',
    'than', 'so', 'yet', 'now', 'also', 'when', 'where', 'which', 'who',
}

# Operational verbs that signal a state change — not common English words
_OPERATIONAL_NEGATION = {
    'removed', 'disabled', 'replaced', 'migrated', 'deprecated',
    'deleted', 'uninstalled', 'reverted', 'rolled-back', 'decommissioned',
    'retired', 'offline', 'broken', 'failed',
}


def _check_contradiction(new_fact: str, domain: str, conn) -> int | None:
    """Return row id of a conflicting existing fact, or None.

    Uses meaningful-token overlap (>=5 tokens, stop-words excluded) +
    operational-negation asymmetry as a contradiction signal. Much more
    conservative than naive token overlap to avoid false positives.

    Replace with embedding similarity when sqlite-vec is available.
    """
    existing = conn.execute(
        "SELECT id, fact_text FROM facts WHERE domain = ? AND review_state != 'rejected'",
        (domain,)
    ).fetchall()

    def _meaningful_tokens(text: str) -> set:
        tokens = set(re.sub(r'[^\w\s-]', '', text.lower()).split())
        return tokens - _CONTRADICTION_STOP_WORDS

    new_tokens = _meaningful_tokens(new_fact)

    for row in existing:
        fact_id, content = row['id'], row['fact_text']
        old_tokens = _meaningful_tokens(content)
        shared = new_tokens & old_tokens
        if len(shared) < 5:
            continue

        new_negated = bool(new_tokens & _OPERATIONAL_NEGATION)
        old_negated = bool(old_tokens & _OPERATIONAL_NEGATION)
        if new_negated != old_negated:
            return fact_id

    return None


def upsert_fact(conn: sqlite3.Connection, domain: str, fact_key: str, fact_text: str,
                agent: str, session_uuid: str, provenance: str, score: float = 0.5,
                outcome: str = None, model_family: str = 'claude',
                model_version: str = '', source: str = 'human', verification_cmd: str = '', fact_type: str = 'operational', injection_weight: float = 1.0):
    """Insert new fact or increment confirmation_count for existing fact_key.

    Dedup is GLOBAL on fact_key (fact_key ↔ fact_text is 1:1): a re-extraction
    in a different domain corroborates the existing row instead of creating a
    cross-domain duplicate (514 of those split confirmation counts pre-06-10).
    Also performs semantic dedup (cosine > 0.92) and is race-safe: the insert
    uses ON CONFLICT against the partial unique index uq_facts_live_key, so a
    concurrent writer that wins the check-then-insert window turns this call
    into a corroboration instead of a duplicate."""
    now = datetime.now(timezone.utc).isoformat()
    principal = agent_to_principal(agent)
    mv_tag = f"{model_family}:{model_version}" if model_version else model_family

    _EXISTING_COLS = ("SELECT id, agents, sessions, confirmation_count, model_families, "
                      "principals, model_versions, verification_cmd FROM facts ")

    def _corroborate(existing_row):
        agents = json.loads(existing_row["agents"] or "[]")
        sessions = json.loads(existing_row["sessions"] or "[]")
        model_families = json.loads(existing_row["model_families"] or '["claude"]')
        principals = json.loads(existing_row["principals"] or "[]")
        model_versions = json.loads(existing_row["model_versions"] or "[]")
        if agent not in agents:
            agents.append(agent)
        if session_uuid not in sessions:
            sessions.append(session_uuid)
        if model_family not in model_families:
            model_families.append(model_family)
        if principal not in principals:
            principals.append(principal)
        if mv_tag not in model_versions:
            model_versions.append(mv_tag)
        conn.execute("""
            UPDATE facts SET
                last_seen = ?,
                confirmation_count = confirmation_count + 1,
                agents = ?,
                sessions = ?,
                model_families = ?,
                principals = ?,
                model_version = ?,
                model_versions = ?
            WHERE id = ?
        """, (now, json.dumps(agents), json.dumps(sessions),
              json.dumps(model_families), json.dumps(principals),
              model_version, json.dumps(model_versions), existing_row["id"]))

    existing = conn.execute(
        _EXISTING_COLS + "WHERE fact_key = ? AND tombstoned_at IS NULL",
        (fact_key,)
    ).fetchone()

    # Semantic dedup: if no exact key match, check for semantically similar facts
    if not existing and HAS_SQLITE_VEC:
        sem_match = _find_semantic_duplicate(conn, domain, fact_text)
        if sem_match:
            # Corroboration merge: don't delete old fact, just bump its confirmation
            existing = conn.execute(
                _EXISTING_COLS + "WHERE id = ?",
                (sem_match["id"],)
            ).fetchone()

    if existing:
        _corroborate(existing)
    else:
        # Score confidence and check for contradictions before inserting
        confidence, conf_source = _score_confidence(fact_text, domain)
        conflict_id = _check_contradiction(fact_text, domain, conn)
        review_state = 'auto'
        if confidence < 0.5 or conflict_id is not None:
            review_state = 'pending'
            if conflict_id is not None:
                confidence = min(confidence, 0.30)
                conf_source = 'contradiction'

        cur = conn.execute("""
            INSERT INTO facts (domain, fact_key, fact_text, first_seen, last_seen,
                               agents, sessions, provenance, score, outcome,
                               model_family, model_families,
                               source_agent, principals,
                               model_version, model_versions, source, verification_cmd, fact_type, injection_weight,
                               confidence, confidence_source, review_state, conflict_with)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fact_key) WHERE tombstoned_at IS NULL DO NOTHING
        """, (domain, fact_key, fact_text, now, now,
              json.dumps([agent]), json.dumps([session_uuid]),
              provenance, score, outcome,
              model_family, json.dumps([model_family]),
              principal, json.dumps([principal]),
              model_version, json.dumps([mv_tag]), source, verification_cmd, fact_type, injection_weight,
              confidence, conf_source, review_state, str(conflict_id) if conflict_id else None))
        if cur.rowcount == 0:
            # Lost the check-then-insert race — a concurrent writer inserted
            # this fact_key between our SELECT and INSERT. Corroborate theirs.
            existing = conn.execute(
                _EXISTING_COLS + "WHERE fact_key = ? AND tombstoned_at IS NULL",
                (fact_key,)
            ).fetchone()
            if existing:
                _corroborate(existing)
        else:
            # Store embedding for new fact
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            _store_embedding(conn, new_id, fact_text)
            # Keep the KG current (Gap 13): index entities/relations at insert
            # time — previously only manual `kg index` runs populated the graph.
            # Guarded: KG failure must never block fact ingestion. SAVEPOINT so
            # a partial failure rolls back ALL its KG writes — otherwise the
            # fact commits un-stamped (kg_indexed_at is the last statement) and
            # the nightly re-index double-counts its co-occurrence weights.
            try:
                from gaius.kg import kg_index_fact  # lazy: KG failure must not block ingestion
                conn.execute("SAVEPOINT kg_index")
                kg_index_fact(conn, new_id, fact_text, domain,
                              session_uuid=session_uuid, agent=agent, timestamp=now)
                conn.execute("RELEASE SAVEPOINT kg_index")
            except Exception:
                try:
                    conn.execute("ROLLBACK TO SAVEPOINT kg_index")
                    conn.execute("RELEASE SAVEPOINT kg_index")
                except Exception:
                    pass
            # Flag the conflicting existing fact now that we have our new_id
            if conflict_id is not None:
                conn.execute(
                    "UPDATE facts SET review_state='pending', confidence=0.30, "
                    "confidence_source='contradiction', conflict_with=? WHERE id=?",
                    (str(new_id), conflict_id)
                )
    conn.commit()


def _upsert_distillations(conn: sqlite3.Connection, distillations: list[dict],
                           session_uuid: str) -> int:
    """Upsert clarified_intent blocks from extra sessions into facts.db.

    Each distillation's objective is the primary fact key. Domain is derived from
    context_refs (e.g. "domain/networking.md" → "networking"), falling back to "extra".

    Uses provenance="distillation" (weight 0.85 in maturity scoring) — above
    automated/structured_reasoning, below human_reviewed. These are validated
    strategic outputs from the intent relay, not raw session observations.

    Returns count of upserted distillations.
    """
    count = 0
    for d in distillations:
        objective = d.get("objective", "").strip()
        if not objective:
            continue

        # Derive domain from context_refs: only accept explicit "domain/*.md" refs.
        # Other refs (issue refs, session memory files) are not domain pointers.
        domain = "extra"
        for ref in d.get("context_refs", []):
            if ref.startswith("domain/"):
                candidate = ref[len("domain/"):].replace(".md", "").split("#")[0].strip()
                if candidate and "/" not in candidate:
                    domain = candidate
                    break

        # Build fact_text: objective with constraints summary
        constraints = d.get("constraints", [])
        text_parts = [f"[distillation] {objective}"]
        if constraints:
            text_parts.append("constraints: " + "; ".join(str(c) for c in constraints[:3]))
        fact_text = "\n".join(text_parts)

        fact_key = hashlib.sha256(objective.lower().encode()).hexdigest()[:16]

        upsert_fact(
            conn,
            domain=domain,
            fact_key=fact_key,
            fact_text=fact_text,
            agent="extra",
            session_uuid=session_uuid,
            provenance="distillation",
            score=0.75,
            model_family="claude",
        )
        count += 1

    return count
