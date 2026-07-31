# kg — temporal knowledge graph

`gaius kg` is a navigation layer over `facts.db`. As facts are ingested, gaius mines
them for **entities** (nodes, services, namespaces, CVEs, incidents, models) and
**relations** between them, stored as triples with temporal validity windows
(`valid_from` / `valid_to`). The graph never invents knowledge — every entity is
linked back to at least one source fact, and every edge carries provenance (source
fact, session, agent). Use it to answer "what touches X?" and "what happened to X,
in order?" without grepping the whole corpus.

Everything lives in the same SQLite file as the facts (`~/.gaius/facts.db`), in three
tables: `entities`, `triples`, and `fact_entities` (which facts mention which
entities). No extra services.

---

## What gets extracted

**Entities** come from regex patterns. The built-in `k8s` preset ships six types:

| Type | Matches |
|------|---------|
| `node` | K8s-style node names (`k8s-r1-web-gpu-01`) |
| `service` | ~70 common infra products (traefik, postgresql, redis, etcd, loki, …) |
| `namespace` | High-precision anchors only: `kubectl … -n X`, `--namespace X`, `in namespace X` |
| `incident` | Failure vocabulary: cascade, outage, split-brain, crashloop, oomkill, … |
| `cve` | `CVE-YYYY-NNNN` identifiers |
| `model` | LLM naming convention (`llama-3-70b`, `qwen2-72b`) |

Customize in `~/.gaius/config.yaml` — add patterns, override built-ins, or disable
the preset entirely. Aliases merge spelling variants into one entity
(`postgres` → `postgresql`); without them one real thing splits into twin entities
and every query silently misses half its facts:

```yaml
entities:
  preset: k8s          # or "none" to disable built-ins
  patterns:
    service: '\b(?:my-service|other-service)\b'
    node: '\bmy-node-prefix-\d+\b'
  aliases:
    web-01: k8s-r1-web-gpu-01
```

**Relations** come in two strengths, deliberately kept apart:

- **Strong predicates** (`runs_on`, `uses_storage`, `in_namespace`) require an actual
  verb phrase in the fact text ("X runs on Y", "X backed by block-nvme"). Temporal:
  each fact produces its own dated triple.
- **Weak co-occurrence** (`co_occurs_with` same-type, `mentioned_with` cross-type)
  means only "these appeared near each other" — first mentions within 300 chars, max
  8 entities per fact, symmetric edges aggregated by `weight` (how many facts paired
  them). Co-occurrence never emits strong predicates: two entities sharing a fact is
  not evidence that one *affected* the other.

## How the graph is built

Three paths, all idempotent:

1. **At insert time** — every fact added to `facts.db` is indexed into the KG in the
   same transaction (guarded: a KG failure never blocks fact ingestion). The graph
   stays current without any scheduled job.
2. **`gaius kg index`** — incremental catch-up. Indexes only facts not yet stamped
   with `kg_indexed_at`, so repeat runs never double-count co-occurrence weights.
   Safe to put in a nightly cron next to `gaius decay`.
3. **Full rebuild** — `gaius kg index --rebuild` (or `gaius rescore --rebuild-kg`)
   wipes entities, triples, and fact links, then re-extracts from every live fact.
   Run after changing entity patterns or aliases in config.

Tombstoned facts are excluded. After indexing, each entity's domain is set by
majority vote of its linked facts, and an invariant check warns if any entity has no
`fact_entities` link.

## Commands

### stats

```bash
$ gaius kg stats
Knowledge Graph Statistics:
  Entities:       412
  Triples:        1893 (1859 active, 34 ended)
  Fact links:     3241 (fact ↔ entity memberships)

  By entity type:
    service           214
    namespace          88
    node               61
  By predicate:
    mentioned_with   1102
    co_occurs_with    655
    runs_on            71
  Heaviest edges (co-occurrence weight):
      41x  service:postgresql —co_occurs_with→ service:redis
```

### query — everything connected to an entity

Substring-matches entity names/ids, then prints outgoing (`→`) and incoming (`←`)
edges with validity windows:

```bash
$ gaius kg query redis
redis (service, domain: storage)
  → co_occurs_with service:traefik since 2026-05-02
  → mentioned_with namespace:cache since 2026-05-14
  ← service:postgresql co_occurs_with since 2026-04-19 → ended 2026-06-01
```

### timeline — chronological story of an entity

Merges both edge directions into one dated sequence. This is the "what happened to
this node, in order?" view:

```bash
$ gaius kg timeline k8s-r1-web-gpu-01
Timeline for k8s-r1-web-gpu-01 (node):

  2026-04-12  ← runs_on service:build-api
  2026-05-03  → mentioned_with incident:oomkill [claude]
  2026-06-01  ← runs_on service:build-api (ended 2026-06-01)
```

### index — build / rebuild

```bash
$ gaius kg index
Indexing knowledge graph from facts.db (un-indexed facts only)...
Done. 37 facts indexed. Entities: 405 → 412 (+7). Triples: 1851 → 1893 (+42). Fact links: 3241.

$ gaius kg index --rebuild      # wipe + full pass over all live facts
```

Reports when a fact hit the 8-entity co-occurrence cap, so truncation is never
silent.

### invalidate — end a triple without deleting it

State changes are recorded, not erased. `invalidate` stamps `valid_to` on the active
triple; `query` and `timeline` then show it as ended:

```bash
$ gaius kg invalidate service:build-api runs_on node:k8s-r1-web-gpu-01
✓ Invalidated: service:build-api runs_on node:k8s-r1-web-gpu-01
```

### export-links — Obsidian wikilinks from shared entities

Derives "Related" footers between your memory-vault markdown files from shared
entities (IDF-weighted — rare entities count, ubiquitous ones don't) and writes a
marker-delimited block so the corpus browses as an Obsidian graph. Deterministic and
idempotent; edges are evidence-derived, never invented.

```bash
$ gaius kg export-links --dry-run
$ gaius kg export-links [--root DIR] [--max-links N] [--min-shared N]
```

Only curated-note directories receive footers; hub/index files are link targets
only, and generated or oversized files are skipped.

## Relationship to facts

The KG is an index, not a second source of truth. Facts hold the full text and
scoring; the graph holds *which things* a fact is about and *how those things
relate*. `fact_entities` powers entity-grounded retrieval — `gaius inject` boosts
facts that mention the infrastructure entities named in your task query — and MCP
exposes the graph mid-session as `gaius_kg_query` and `gaius_kg_timeline`. If a
graph edge looks wrong, the fix is upstream: correct or tombstone the fact, adjust
patterns/aliases, then `gaius kg index --rebuild`.
