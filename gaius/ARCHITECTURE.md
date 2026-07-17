# gaius — package architecture

`gaius` began as a single `_core.py`. As it grew, cohesive concerns were split
into their own modules while keeping a **facade** so no importer had to change.
This doc is the map: what lives where, and the one convention you must follow
when extracting more.

## Module map

| Module | Concern | Notes |
|--------|---------|-------|
| `_core.py` | **Shared hub.** Config discovery, ANSI, agent-type thresholds, the SQLite facts index (`init_db`, `upsert_fact`), embeddings + embed daemon, semantic dedup, confidence scoring + contradiction checks, credential redaction patterns, TF-IDF / BM25 / decay scoring helpers, the retire/index/harvest command family, session mining, skill loading, the `COMMANDS` dispatch dict, and `main()`. | Everything that reads the runtime-mutable globals (`PROJECT_DIR`, `STAGING_DIR`, `EXTRA_SESSIONS_DIR`, rebound in `main()`) stays here. |
| `parsers.py` | Session-format adapters (claude / gemini / ollama / pentagi / grok / codex): `detect_format`, `parse_*_events`, session discovery. | |
| `kg.py` | Knowledge graph: entity/relation patterns, `extract_entities`, triples, `kg_index_fact`, `cmd_kg`. | |
| `record.py` | `gaius record` — capture AI chat sessions into gaius JSONL. | |
| `telemetry.py` | Prompt/injection event logging (`log_prompt_event`, `log_injection_fact`). | Imported function-locally by hot paths to avoid import cost. |
| `mcp_server.py` | MCP server exposing gaius over the Model Context Protocol. | Imports from `gaius._core`. |
| `raft.py` | Blog-post → RAFT training-sidecar YAML. Owns `_parse_frontmatter` and the failure-class / domain keyword maps. | `cmd_raft`. |
| `maturity.py` | Fact-maturity / training-readiness scoring + `maturity`/`readiness`/`snapshot`/`governor`/`route`. Owns the scoring weight tables (`PROVENANCE_WEIGHT`, `OUTCOME_MODIFIER`, …). | `cmd_decay`/`cmd_rescore` stay in `_core` but consume these tables via the re-export. |
| `sync.py` | Council-log + recurring-alerts sync into domain files. | `cmd_sync_council`, `cmd_sync_alerts`. |
| `outcomes.py` | Orchestrator task-outcome ingestion (`task_outcomes` table, win-rates). | `cmd_ingest_outcomes`. |
| `corpus_audit.py` | Read-only corpus integrity (repetition/prune, self-poison audit) + `route_suggest`. | `cmd_corpus_audit`, `cmd_route_suggest`. |
| `reconcile.py` | Source-of-truth reconciler: registry, dev↔mirror fingerprint divergence, remote HEAD divergence, curated-fact promotion. | `cmd_reconcile`. `_remote_head` lives here — monkeypatch `gaius.reconcile._remote_head`, not `_core`. |
| `landscape.py` | The Landscape Protocol + context-injection engine: live-state probes with TTL cache (`_run_landscape`, `cmd_landscape`) and `cmd_inject` (BM25 + semantic + decay ranking within a token budget). | Reads no runtime globals; the retire/index family it sat next to stays in `_core`. |

## The facade convention (read before extracting a new module)

The goal: move code out of `_core.py` **without changing a single importer**. Every
`from gaius._core import X` and the `COMMANDS` dict must keep working.

1. **New module imports shared helpers from `gaius._core` at its top.**
   ```python
   from gaius._core import init_db, DB_PATH   # shared hub
   ```
2. **`_core.py` re-imports the module's public symbols at its END** — in the
   `FACADE RE-EXPORTS` block, which sits just above the `COMMANDS` dict:
   ```python
   from gaius.mymod import (  # noqa: E402,F401  re-export (mymod split YYYY-MM-DD)
       public_fn, PUBLIC_CONST, cmd_mymod,
   )
   ```
   The end-of-file placement is what breaks the circular import: by the time this
   line runs, `_core`'s own definitions exist, so `mymod`'s top-level
   `from gaius._core import …` resolves.

3. **Re-export every moved symbol that is either** (a) in the public contract
   (imported by `__init__.py`, `http_adapter.py`, `mcp_server.py`, or any test),
   **or** (b) referenced by the `COMMANDS` dict (all `cmd_*`), **or** (c) read by
   any function that stays in `_core`. Miss one and you get an import-time
   `NameError` when `COMMANDS` is built.

4. **Ordering matters.** The re-export block runs top-to-bottom; a module that
   imports a symbol another extracted module owns must be re-imported *after* that
   owner. Current invariant: **raft before landscape** (`_parse_frontmatter`).

5. **Never move a function that reads a runtime-mutable global**
   (`PROJECT_DIR` / `STAGING_DIR` / `EXTRA_SESSIONS_DIR`, rebound in `main()`)
   unless it references it as `_core.NAME`. A bare imported name binds the stale
   import-time value. When in doubt, leave it in `_core`.

6. **New modules declare their own stdlib imports.** They do NOT inherit
   `_core`'s top-level `import` lines. Verify free names statically (`symtable`)
   rather than trusting an import smoke test — names used only inside function
   bodies aren't checked until the function runs.

7. **Verify green after each extraction:** `pytest` + `python -c 'import gaius._core'`
   + a live `gaius <cmd> --help` for any moved command.
