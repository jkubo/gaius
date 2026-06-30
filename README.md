# gaius

**Ops memory lifecycle manager for AI coding agents.**

Not another RAG chatbot memory — a production-grade system that extracts facts from Claude Code, Gemini CLI, Grok, and Codex sessions, routes them through human review, enforces behavioral gates, and prevents you from breaking prod at 3am.

```
gaius retire      # scan sessions → stage summaries
gaius batch       # review staged summaries
gaius inject      # inject context into active session
```

---

## What It Does

Engineers running Claude Code all day generate enormous amounts of institutional knowledge that vanishes when the context window closes. gaius captures it:

1. **Extract** — scans Claude Code (and Gemini CLI) session JSONLs, extracts compact summaries with typed signals (knowledge, patterns, errors)
2. **Review** — queues summaries for human review before promoting facts. Bad facts don't silently enter the corpus.
3. **Index** — promotes reviewed facts into a hybrid keyword + semantic SQLite database (`facts.db`)
4. **Inject** — at task start, retrieves relevant facts and loads skills context into the session

---

## Benchmark

The demo below is a **regression / smoke check, NOT a quality score**: it confirms retrieval still
works on a fresh clone. A perfect score on a hand-built 27-fact corpus (same author wrote the facts
and the queries) proves the pipeline runs, nothing more. Real quality is the **External evaluation**
section below, on a benchmark gaius didn't build.

`bench_inject.py` builds a throwaway SQLite corpus from `benchmarks/demo_corpus/`:

```
$ python3 benchmarks/bench_inject.py

gaius Injection Benchmark (25 queries, bundled demo corpus)
════════════════════════════════════════════════════════════
Recall:    25/25 (100%)  regression check, NOT a quality score
Prec-warn: 3/25
Corpus:    27 demo facts  (throwaway SQLite, no daemon, ~0.08s/query)

Semantic mode (embed daemon, real corpus):
Cold start:  ~7s    (first query, model load)
Daemon warm: ~8ms   (Unix socket, model kept in memory)

Storage: sqlite-vec (384-dim, all-MiniLM-L6-v2) + BM25 in a single SQLite file
No API keys, no cloud, runs entirely offline.
```

## External evaluation (LongMemEval-S)

The demo above only proves the pipeline runs. For an *unbiased* measure, gaius's retrieval is scored on
[LongMemEval-S](https://github.com/xiaowu0162/LongMemEval) (ICLR 2025): a third-party benchmark whose
500 questions, ~48-session haystacks, and gold labels gaius has no hand in choosing (independent by
construction, not a self-graded demo). Reproducible: `python3 benchmarks/bench_longmemeval.py --matrix`.

Two metrics, not the same: **R@k** = per-question fractional recall (stricter; 65% of questions are
multi-gold); **hit@k** = recall_any@k ("at least one gold session in top-k"), the metric published
baselines report, so compare hit@k to those, not R@k.

gaius retrieval (all-MiniLM-L6-v2), 500 questions:

| config | MRR | R@5 | R@10 | hit@10 |
|--------|-----|-----|------|--------|
| session-semantic | 0.827 | 85.7 | 92.7 | 96.8 |
| turn-semantic | 0.906 | 92.8 | 96.6 | 98.8 |
| **turn-hybrid** (real inject path) | **0.912** | 92.8 | 95.2 | **98.6** |

On the matched metric (hit@k), gaius at its real regime (short-fact units + the hybrid inject ranking)
scores **hit@10 98.6%**, at par with published same-model (all-MiniLM-L6-v2) session-level retrieval
baselines (~96-99% recall_any@10; protocols differ; retrieval-only, not an end-to-end QA metric).
Larger embedding models beat all-MiniLM outright, a deliberate tradeoff for gaius's 384-dim, no-GPU,
fully-offline footprint. Weakest category: temporal-reasoning (~94% R@10 at turn-semantic).

This measures the retrieval engine. Whether *proactive injection* improves agent outcomes is a
separate evaluation (in progress), and is **not** claimed here.

---

## What's Genuinely Novel

| Feature | Description |
|---------|-------------|
| **Human review gate** | `retire → stage → review → promote`. Bad facts can't silently enter the corpus. |
| **Multi-agent corroboration** | Facts confirmed by multiple AI agents (Claude + Gemini) get a 1.5× score boost. Cross-model verification for higher confidence. |
| **Session-type behavioral priming** | Load different skill sets based on what you're doing: ops, trading, security, code review. |
| **Hard enforcement gates** | Memory that *prevents actions*. `exit:2` blocks force-push, live trading without confirmation, critical resource deletion. |
| **Mnemosyne health monitoring** | Automated memory bloat prevention. Line-count thresholds, misclassification audit, split/prune proposals. |
| **Live state injection** | Domain files with `kubectl`/`curl` commands in frontmatter, TTL-cached. Memory that knows what's happening right now. |
| **Hybrid sqlite-vec search** | Keyword TF-IDF/BM25 + semantic embeddings in a single SQLite file. |
| **Temporal knowledge graph** | Entity-relationship triples with validity windows. `gaius kg timeline node` shows what changed and when. |
| **MCP server** | 5 tools for mid-session memory access without leaving Claude Code. |

---

## Quick Start

### Install

```bash
# Option 1: development install (editable, reads from repo)
git clone https://github.com/jkubo/gaius
cd gaius
pip install -e ".[semantic]"   # includes sentence-transformers + sqlite-vec

# Option 2: script install (no pip, uses system/venv python)
install -m 755 gaius_cli ~/.local/bin/gaius
```

### Initialize

```bash
# Create config dir
mkdir -p ~/.gaius

# Copy example config
cp presets/k8s.yaml ~/.gaius/config.yaml   # for K8s clusters
# or: cp presets/default.yaml ~/.gaius/config.yaml

# Edit to set your sessions_dir and domain_dir
$EDITOR ~/.gaius/config.yaml
```

### First Run

```bash
# Scan local sessions and stage summaries.
# Plain `retire` also auto-sweeps Grok (~/.grok/sessions) and Codex
# (~/.codex/sessions) when those CLI dirs exist; `--format <name>` scopes to one.
gaius retire

# Review staged summaries (read each, promote facts manually to domain/*.md)
gaius batch          # print all unreviewed in sequence
gaius next           # print one at a time
gaius done <uuid>    # mark reviewed after reading

# Check corpus stats
gaius stats

# Inject context at session start
gaius inject --task "debug flannel networking" --budget 4000
```

### MCP Server (Claude Code)

```bash
# Register the MCP server in Claude Code
claude mcp add gaius -- python3 -m gaius.mcp_server

# Or if using the script install:
claude mcp add gaius -- /path/to/gaius/gaius/mcp_server.py
```

5 tools available mid-session: `gaius_search`, `gaius_kg_query`, `gaius_kg_timeline`, `gaius_stats`, `gaius_fact_add`.

---

## Architecture

```
gaius/
├── gaius/
│   ├── _core.py          # core logic (extraction, search, inject, dedup, decay)
│   ├── kg.py             # temporal knowledge-graph commands
│   ├── parsers.py        # session / domain-file parsers
│   ├── record.py         # session recorder (OpenAI-compatible endpoints)
│   ├── telemetry.py      # prompt / injection event logging
│   ├── mcp_server.py     # MCP server (5 tools)
│   ├── __init__.py       # Public API surface
│   └── __main__.py       # python -m gaius
├── http_adapter.py       # FastAPI REST adapter (optional, for remote access)
├── presets/
│   ├── k8s.yaml          # Entity patterns for Kubernetes clusters
│   └── default.yaml      # Minimal defaults for any project
├── benchmarks/
│   ├── bench_inject.py        # injection regression check (bundled demo corpus)
│   ├── bench_longmemeval.py   # LongMemEval-S external retrieval eval (--matrix)
│   └── bench_retrieval.py     # legacy Recall@k/MRR harness (not for public numbers)
├── tests/
│   └── *.py              # unit + integration suites (test_core.py, ...)
├── pyproject.toml
└── LICENSE               # Apache 2.0
```

**Storage**: single `~/.gaius/facts.db` SQLite file — facts table with BM25 virtual table + sqlite-vec embedding index. No external services required.

**Embed daemon**: optional systemd user service (`gaius-embed-daemon`) keeps `all-MiniLM-L6-v2` loaded in memory (~8ms/query warm vs ~7s cold).

---

## Configuration

Config file: `~/.gaius/config.yaml`

```yaml
# Sessions directory (Claude Code project JSONLs)
sessions_dir: ~/.claude/projects

# Domain memory directory (your *.md knowledge files)
domain_dir: ~/my-memory/domain

# Skills directory (prospective how-to guides)
skills_dir: ~/my-memory/skills

# Principal mapping — agents grouped for cross-agent scoring
principals:
  default: operator

# Entity extraction — extend the built-in K8s baseline
entities:
  preset: k8s        # use built-in K8s patterns; set to "none" to disable
  patterns:
    service: '\b(?:my-api|my-worker|my-scheduler)\b'
    namespace: '\b(?:prod|staging|dev)\b'
```

See `presets/k8s.yaml` for a full annotated example.

---

## Commands

| Command | Description |
|---------|-------------|
| `gaius retire` | Scan local sessions → stage new summaries (auto-sweeps Claude/Grok/Codex; `--format` to scope) |
| `gaius record` | Capture chat sessions into gaius JSONL (vLLM, any OpenAI-compatible endpoint) |
| `gaius s3-retire <agent>` | Retire from S3-archived agent sessions (rclone) |
| `gaius harvest` | Scan cold Gemini CLI sessions (`.json` format) |
| `gaius grok-retire` | Scan Grok CLI sessions (`~/.grok/sessions/`) → stage decision events |
| `gaius codex-retire` | Scan Codex CLI rollouts (`~/.codex/sessions/`) → stage decision events |
| `gaius next` | Print oldest unreviewed summary |
| `gaius batch` | Print all unreviewed summaries in sequence |
| `gaius done <uuid>` | Mark summary as reviewed |
| `gaius show` | List all staged summaries |
| `gaius stats` | Extraction and corpus statistics |
| `gaius inject` | Inject ranked corpus + skills into active session |
| `gaius kg query <entity>` | Query knowledge graph for an entity |
| `gaius kg timeline <entity>` | Show temporal changes for an entity |
| `gaius governor` | Cross-agent knowledge gap analysis |
| `gaius embed` | Build/rebuild semantic embedding index |
| `gaius index` | Rebuild memory index |
| `gaius landscape` | Show memory system landscape |
| `gaius skills` | List available skills with scores |

---

## Dependencies

**Core** (no extras): `pyyaml>=6.0` — pure Python, no binary deps.

**Semantic search** (`pip install "gaius-memory[semantic]"`):
- `sentence-transformers>=2.7` — local embedding model (`all-MiniLM-L6-v2`, 384-dim)
- `sqlite-vec>=0.1` — vector search extension for SQLite

**MCP server** (`pip install "gaius-memory[mcp]"`):
- `mcp[server]>=1.0`

**HTTP adapter** (`pip install "gaius-memory[http]"`):
- `fastapi>=0.100`, `uvicorn[standard]>=0.23`

Without `[semantic]`, gaius falls back to keyword-only BM25 search (no embeddings required).

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
