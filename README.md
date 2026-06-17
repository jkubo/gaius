# gaius

**Ops memory lifecycle manager for AI coding agents.**

Not another RAG chatbot memory — a production-grade system that extracts facts from Claude Code and Gemini CLI sessions, routes them through human review, enforces behavioral gates, and prevents you from breaking prod at 3am.

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

## Evaluating retrieval

gaius ships a functional smoke test (`benchmarks/bench_inject.py`) that runs `inject`
against a small bundled demo corpus (`benchmarks/demo_corpus/`) to confirm the pipeline
works end to end. It is **not** a quality benchmark: the demo corpus and its queries were
authored together, so passing only proves the plumbing runs, not that retrieval ranks well.

A credible benchmark needs an independently-authored, held-out corpus with distractor
facts and a ranking metric (MRR / NDCG@k) rather than keyword presence. That eval is not
built yet, so gaius makes no recall claim here. Run it on your own corpus instead:

```
python benchmarks/bench_inject.py   # CI smoke test: builds a temp DB from demo_corpus
```

Storage: sqlite-vec (384-dim, all-MiniLM-L6-v2) + BM25 in a single SQLite file. No API keys, no cloud, offline.

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
git clone https://github.com/kub0-ai/gaius
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
# Scan your Claude Code sessions and stage summaries
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
claude mcp add gaius -- /path/to/gaius/mcp_server.py
```

5 tools available mid-session: `gaius_search`, `gaius_kg_query`, `gaius_kg_timeline`, `gaius_stats`, `gaius_fact_add`.

---

## Architecture

```
gaius/
├── gaius/
│   ├── _core.py          # All logic (extraction, search, KG, inject, MCP)
│   ├── __init__.py       # Public API surface
│   └── __main__.py       # python -m gaius
├── mcp_server.py         # MCP server (5 tools)
├── presets/
│   ├── k8s.yaml          # Entity patterns for Kubernetes clusters
│   └── default.yaml      # Minimal defaults for any project
├── benchmarks/
│   ├── bench_inject.py   # Canonical injection benchmark (self-contained)
│   ├── bench_retrieval.py
│   └── demo_corpus/      # Generic ops facts the benchmark runs against
├── tests/                # pytest suite (test_core, test_mnemosyne, ...)
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
| `gaius retire` | Scan local sessions → stage new summaries (Claude/Gemini/vLLM) |
| `gaius record` | Capture chat sessions into gaius JSONL (vLLM, any OpenAI-compatible endpoint) |
| `gaius s3-retire <agent>` | Retire from S3-archived agent sessions (rclone) |
| `gaius harvest` | Scan cold Gemini CLI sessions (`.json` format) |
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

Without `[semantic]`, gaius falls back to keyword-only BM25 search (no embeddings required).

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
