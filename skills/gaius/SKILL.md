---
name: gaius
description: Use gaius memory mid-session — search past sessions for a fact, record a durable one, query the knowledge graph, or run the retire/review loop that promotes session output into the corpus. Use when the user asks what was learned before, why something is configured a way, or asks you to remember something.
---

# gaius — session memory

gaius stores facts mined from past coding sessions in a local SQLite corpus (`~/.gaius/`)
and injects the relevant ones at session start. This skill is for reaching into it
mid-session, when the automatic injection didn't carry what you need.

Nothing here leaves the machine.

## Recall — before you guess

Search before asserting how something was set up. The corpus is the record of what was
actually done, and it is usually cheaper than re-deriving it from the code.

Search and fact-recording are **MCP tools, not CLI subcommands** — they need the plugin's
bundled MCP server (or `gaius-mcp`) running:

| Tool | Use |
|------|-----|
| `gaius_search` | hybrid BM25 + vector search over the corpus |
| `gaius_kg_query` | relationships for an entity |
| `gaius_kg_timeline` | what changed about an entity, in order |
| `gaius_stats` | corpus overview — counts, domains, embeddings |
| `gaius_fact_add` | record a fact straight into the corpus |

Without the MCP server, the CLI equivalent for recall is a targeted injection:

```bash
gaius inject --task "drbd quorum eviction" --budget 1500
gaius kg query <entity>
gaius kg timeline <entity>
```

Two rules that keep this honest:

- **A fact is a claim, not a guarantee.** It was true when written. If it names a file,
  flag, or host, verify that it still exists before acting on it.
- **Absence is not evidence.** "Not in the corpus" means nobody wrote it down, not that
  it isn't so.

## Record — when something was hard to learn

Use `gaius_fact_add` when the session produced knowledge that isn't recoverable from the
code: a failure mode and its cause, a decision and its reasoning, a constraint discovered
the hard way. Do not record what `git log` already says.

Facts carry a volatility type — `structural` (doesn't decay), `operational`, `live`
(decays fast). Pick honestly; a live fact filed as structural is how a corpus goes stale
without anyone noticing.

## The review loop

Mined facts are staged, not trusted. They enter the corpus only after review:

```bash
gaius retire         # scan session logs, stage new summaries
gaius next           # oldest unreviewed summary
gaius confirm <id>   # promote  (also: reject <id>, defer <id>)
gaius show           # everything staged
gaius stats          # corpus health
```

Run `gaius retire` at the end of a session that produced something worth keeping.
Review is where corpus quality is actually decided — a fact confirmed without checking is
worse than no fact, because the next session will believe it.

## Health

`mnemosyne health` reports bloated or misclassified memory files. The plugin runs it
automatically after edits inside the memory directory and surfaces anything RED or YELLOW.

First run: `gaius init` creates `~/.gaius/config.yaml`, picks a session backend
(Claude Code / Gemini CLI / vLLM) and an entity preset, then `gaius retire` mines your
existing session history.
