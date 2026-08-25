# gaius — package architecture

`gaius` began as a single `_core.py`. As it grew, cohesive concerns were split
into their own modules while keeping a **facade** so no importer had to change.
This doc is the map: what lives where, and the one convention you must follow
when extracting more.

## Module map

| Module | Concern | Notes |
|--------|---------|-------|
| `_core.py` | **Shared hub + dispatch only** (post Phase-A split, 2026-08-13). Config cascade (`_gaius_cfg` + every path/threshold constant), ANSI, `_guard_write_path`, `route_domains`, `content_hash`, `cmd_init`/`cmd_migrate`/`cmd_sync_memory`, thin wrappers (`cmd_record`, `cmd_inject`, `cmd_completion`), the FACADE RE-EXPORTS block, `COMMANDS`, `main()`. | ALL runtime-rebindable (`PROJECT_DIR`/`STAGING_DIR`/`EXTRA_SESSIONS_DIR`, rebound in `main()`) and test-patched constants (`DB_PATH`, `MEMORY_DIR`, `SKILLS_DIR`, `CLAUDE_SKILLS_DIR`, `CLAUDE_COMMANDS_DIR`, `_gaius_cfg`, …) live HERE; split modules read them at call time as `_core.NAME`. |
| `embed.py` | Embedding model + warm-daemon fast path, `_chunk_text`, `cmd_embed`. | `_EMBED_MODEL` module state is deliberately NOT re-exported. Phase-A split. |
| `extract.py` | Classification vocabulary (DECISION/FINDING/PROCEDURE patterns, `SECTION_HEADERS`, `DOMAIN_KEYWORDS` + config merge), `classify_*`, `strip_bloat` family, noise/narration/reviewer-verdict filters (`_is_noise`), seeded scoring, clarified-intent parsing. | `parsers.py` resolves `_is_noise`/`CREDENTIAL_PATTERNS` through the facade — extract's re-export line must stay ahead of the parsers re-import. Phase-A split. |
| `scoring.py` | TF-IDF, BM25, `decay_factor`, query/entity boosts, `estimate_tokens`, `domain_stats.json` trio. | Phase-A split. |
| `facts.py` | The facts store: `init_db` (schema/vec0), `upsert_fact` + semantic dedup + contradiction checks, confidence scoring, `register_session`, distillation upserts. | Reads `_core.DB_PATH` at call time (monkeypatched by 8 test files). `kg_index_fact` is a call-site import inside its SAVEPOINT guard. Phase-A split. |
| `review.py` | The human review queue: `load_staged`/`save_staged`, show/next/done/batch/rescan, verdict verbs (`confirm` HUMAN-ONLY), `cmd_quiz`, `cmd_stats`. | `gaius quiz` is the v0.2 Leitner HITL loop (weighted draw; first confirm writes `confidence_source='human'`; repeats only move the box). |
| `leitner.py` | Pure spaced-repetition scheduler: `qid`, `grade`, `draw`, `item_weight`. No I/O. | Shared by `gaius quiz` and any card deck. Box policy = shipped prep-console weights. |
| `cards.py` | MCQ card schema + validator (`validate_cards`) + JS renderer. Content-agnostic. | Question text is caller-owned and does not ship. |
| `retire.py` | Session mining (`MINE_*`, `_mine_session`), `cmd_retire`/`cmd_s3_retire`, the index pipeline (`cmd_index`, `process_session`, `write_*_deltas`, `archive_session`), `cmd_harvest`, event-based peer retires (pentagi/ollama/grok/codex). | Every index write path still calls `_core._guard_write_path`. Phase-A split. |
| `skills.py` | Skill loading/scoring (`load_skills`, `compute_skill_score`), `cmd_skills`, Claude Code stub wiring (`cmd_commands`), `cmd_scaffold_skill`, suggest pipeline. | Phase-A split. |
| `drift.py` | `cmd_drift` + `_drift_live` — registry drift, live-claims probes, council posting. | Phase-A split. |
| `ingest.py` | Infrastructure source ingesters: `cmd_ansible`, `cmd_aliases`. | Phase-A split. |
| `parsers.py` | Session-format adapters (claude / gemini / ollama / pentagi / grok / codex): `detect_format`, `parse_*_events`, session discovery. | |
| `kg.py` | Knowledge graph: entity/relation patterns, `extract_entities`, triples, `kg_index_fact`, `cmd_kg`. | |
| `record.py` | `gaius record` — capture AI chat sessions into gaius JSONL. | |
| `telemetry.py` | Prompt/injection event logging (`log_prompt_event`, `log_injection_fact`). | Imported function-locally by hot paths to avoid import cost. |
| `mcp_server.py` | MCP server exposing gaius over the Model Context Protocol. | Imports from `gaius._core`. |
| `raft.py` | Blog-post → RAFT training-sidecar YAML. Owns `_parse_frontmatter` and the failure-class / domain keyword maps. | `cmd_raft`. |
| `maturity.py` | Fact-maturity / training-readiness scoring + `maturity`/`readiness`/`snapshot`/`governor`/`route`. Owns the scoring weight tables (`PROVENANCE_WEIGHT`, `OUTCOME_MODIFIER`, …) **and, since the Phase-A split, `cmd_decay`/`cmd_rescore`/`volatility_recency`** — the commands moved home to the tables they consume. | |
| `outcomes.py` | Orchestrator task-outcome ingestion (`task_outcomes` table, win-rates). | `cmd_ingest_outcomes`. |
| `corpus_audit.py` | Read-only corpus integrity (repetition/prune, self-poison audit) + `route_suggest`. | `cmd_corpus_audit`, `cmd_route_suggest`. |
| `reconcile.py` | Source-of-truth reconciler: registry, dev↔mirror fingerprint divergence, remote HEAD divergence, curated-fact promotion. | `cmd_reconcile`. `_remote_head` lives here — monkeypatch `gaius.reconcile._remote_head`, not `_core`. |
| `landscape.py` | The Landscape Protocol + context-injection engine: live-state probes with TTL cache (`_run_landscape`, `cmd_landscape`) and `cmd_inject` (BM25 + semantic + decay ranking within a token budget). | Reads no runtime globals; the retire/index family it sat next to stays in `_core`. |
| `concord.py` | Local, offline-first cross-session coordination: advisory claims (TTL + holder-pid liveness), findings with an adversarial review loop (open → reviewing → confirmed/refuted), claimable task pool, live roster — sidecar SQLite at `~/.gaius/concord.db`. | `cmd_concord`. Advisory by design: surfaced by hooks, never auto-enforced. |
| `degradation.py` | Intra-session degradation detection over Claude Code transcripts: raw `turn_fuel` + `degradation_events` tables in `~/.gaius/telemetry.db`; the report joins them into an event rate per fuel band. Also carries **prompt-cache health** (`turn_fuel.cache_read`/`cache_creation`, added 2026-08-13) — `_fill` always parsed both and discarded the split, so cache invalidation was invisible at an identical `total`. | `cmd_degradation`. Bands are derived at read, never stored. ⚠️ A low aggregate hit rate is usually COMPACTION, not a bug — read it against `comp_trig`. |
| `recentstate.py` | `gaius recent-roll` — evicts pointered, signature-homed, INDEX-reachable `## Recent State` bullets from the always-injected MEMORY.md into a non-injected archive changelog. | `cmd_recent_roll`. Fail-closed: pin (unless `--ignore-pins`), no pointer, unhomed, or an INDEX_TREE target unlisted in its `INDEX.md` (Gap 60) → KEEP. ⚠️ is not a veto. |


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
   (imported by `__init__.py`, `mcp_server.py`, or any test),
   **or** (b) referenced by the `COMMANDS` dict (all `cmd_*`), **or** (c) read by
   any function that stays in `_core`. Miss one and you get an import-time
   `NameError` when `COMMANDS` is built.

4. **Ordering matters.** The re-export block runs top-to-bottom; a module that
   imports a symbol another extracted module owns must be re-imported *after* that
   owner. Current invariants: **the Phase-A zone (embed → extract → scoring →
   facts → review → retire → skills → drift → ingest) runs FIRST** — the older
   modules below it (parsers, kg, maturity, …) top-level `from gaius._core
   import` symbols whose implementation moved into Phase-A modules — and
   **raft before landscape** (`_parse_frontmatter`). Phase-A modules import
   moved symbols from their new homes DIRECTLY (`from gaius.extract import …`),
   never via `_core`; runtime-only deps use lazy in-body `from gaius._core
   import` (resolved through the fully-built facade at command time).

5. **Never move a function that reads a runtime-mutable OR test-patched global**
   (`PROJECT_DIR` / `STAGING_DIR` / `EXTRA_SESSIONS_DIR`, rebound in `main()`;
   `DB_PATH` / `MEMORY_DIR` / `SKILLS_DIR` / `CLAUDE_SKILLS_DIR` /
   `CLAUDE_COMMANDS_DIR` / `_gaius_cfg`, monkeypatched on `gaius._core` by tests)
   unless it references it as `_core.NAME` at call time. A bare imported name
   binds the stale import-time value — a downstream consumer that imports a hub
   global has to double-patch its own copy for exactly this reason. When in
   doubt, leave it in `_core`.

6. **New modules declare their own stdlib imports.** They do NOT inherit
   `_core`'s top-level `import` lines. Verify free names statically (`symtable`)
   rather than trusting an import smoke test — names used only inside function
   bodies aren't checked until the function runs.

7. **Verify green after each extraction:** `pytest` + `python -c 'import gaius._core'`
   + a live `gaius <cmd> --help` for any moved command.
