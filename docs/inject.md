# inject — context injection

`gaius inject` selects the slice of your accumulated memory that is relevant to the
task at hand and prints it as session context: skills, curated memory files, session
handoffs, SOPs, and ranked corpus facts, all within a token budget. It is the read
path of the whole system — everything `retire` extracts and `index` organizes exists
so that `inject` can surface the right 2,000 tokens at the right moment instead of
dumping the archive.

```bash
gaius inject --task "fix storage split-brain on node-01"
gaius inject --budget 2000 --skills-budget 1500
gaius inject --task "tune the ingest pipeline" --domain storage --landscape storage
```

---

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--budget N` | 2000 | Max tokens of corpus + memory + SOP content. Optional. |
| `--skills-budget N` | 0 | Additional tokens reserved for skill files (0 = no skills; `gate: always` skills inject regardless, outside any budget). |
| `--skills-context "..."` | — | Keywords/file paths to score skills against (e.g. `"manifests/storage rocm"`). |
| `--task "..."` | — | Task description. Drives BM25 + semantic ranking, handoff matching, and memory-file selection. Without it you get importance-ranked facts only. |
| `--domain NAME` | — | Restrict corpus facts to one domain. |
| `--landscape NAME` | — | Prepend a live-state block for that domain (see below). |
| `--sop NAME` / `--scopes scope:a,scope:b` | — | Inject named SOPs, or SOPs matched from scope labels. |
| `--source corpus\|sop` | corpus | `sop` prints matched SOPs and exits. |
| `--no-semantic` | off | Disable embedding scoring; keyword ranking only. |
| `--no-always-skills` | off | Skip `gate: always` skills (use in per-prompt hooks where session start already injected them). |
| `--format claude\|gemini\|plain` | claude | Accepted for forward compatibility; output is currently identical across formats. |

The two budgets are separate pools: `--budget` covers facts, memory files, and SOPs;
`--skills-budget` covers skill files. A matching session handoff is exempt from the
corpus budget (it is the highest-priority context) but capped at 3,000 tokens.

## What gets injected, in order

1. **`gate: always` skills** — unconditional, outside all budgets.
2. **Landscape block** (`--landscape`) — live state, prepended.
3. **Skills** — scored against `--domain` + `--skills-context` terms; densest
   (score-per-token) first, then each skill's declared `also_load` dependencies.
4. **Memory files** — feedback > domain > project > user > reference, scored against
   `--task`. Feedback hard-gate rules are count-cap exempt; domain files are truncated
   to their head. Feedback-family files cap at 40% of budget, domain files at 65%.
5. **Session handoff** — the most recent handoff (< 48h old) whose skill name matches
   the task text.
6. **SOPs** — explicit (`--sop`) or scope-matched.
7. **Corpus facts** — ranked from `facts.db`, up to 8 entries, deduplicated by content.

Corpus notes print under an explicit fence: they are auto-mined from past sessions
and marked **untrusted data, not instructions**, with provenance stamped per note.

## How ranking works

For each fact, a score is assembled and then divided by the fact's token count —
selection is by **priority (score per token)**, so a dense two-line fact beats a
rambling one:

- **Relevance** — base TF-IDF importance; with `--task`, blended 0.3 TF-IDF +
  0.7 BM25 against the task terms. If embeddings are available (sqlite-vec), cosine
  similarity blends in at 0.6 weight, and facts below 0.3 similarity are heavily
  penalized. Quoted phrases in the task (`"exact error text"`) boost exact matches.
- **Decay** — exponential decay on the gap since last confirmation, 90-day half-life.
  A fact re-confirmed recently holds full weight; one unseen for months fades.
- **Fact type** — incidents/findings 1.3x, procedures/security 1.2x, raw
  observations 0.5x.
- **Review state** — `pending`, `deferred`, and `agent-reviewed` facts inject at
  0.6x weight; `auto` and human-`confirmed` at full weight. Machine review is queue
  hygiene, never a rank boost.
- **Cross-agent confirmation** — a fact independently extracted by two different
  agents gets 1.5x. `GAIUS_CONFIRMATION_BOOST_CAP` (float, e.g. `1.2`) optionally
  bounds the total repetition-derived boost, so a confidently-worded false fact
  cannot climb rank purely by being re-extracted.
- **Floors** — facts below `inject_min_priority` (config, default 0.04) are dropped
  as noise. Exception: a domain with fewer than 20 retired sessions is in
  *bootstrap* and skips the floors — cold domains still surface what little they have.

## Live-state probes (`--landscape`)

A domain file (`domain/<name>.md`) can declare shell probes in its frontmatter:

```yaml
landscape:
  - label: "service health"
    cmd: "curl -sf https://example.com/healthz"
landscape_ttl: 120          # seconds; cached in ~/.gaius/landscape_cache/
landscape_fallback: name-static.md   # used if every probe fails
```

`gaius inject --landscape <name>` (or `gaius landscape <name>` standalone) runs each
command with a 10s timeout and prepends a `## Current State` block — memory tells you
what was true; the landscape tells you what is true now. Results are cached for the
TTL; `gaius landscape <name> --invalidate` forces a re-run.

## Automatic injection via hooks

The plugin's `hooks/hooks.json` wires two injection hooks (a third, PostToolUse, is a
memory-file health check — see the getting-started page); both are fail-silent (a
memory tool that can't answer must never block a session) and both yield to a
standalone install's own hooks unless `GAIUS_PLUGIN_HOOKS=force`.

**SessionStart** (`hooks/session-start.sh`) — runs once per new session (skipped on
resume): `gaius inject --budget 2000 --skills-budget 1500`. Tunables:

```
GAIUS_INJECT_BUDGET    corpus token budget    (default 2000)
GAIUS_SKILLS_BUDGET    skills token budget    (default 1500)
GAIUS_INJECT_TIMEOUT   seconds                (default 12)
```

**UserPromptSubmit** (`hooks/prompt-inject.sh`) — off by default because it bills
tokens on every turn; set `GAIUS_PROMPT_INJECT=1` to enable. Passes the first 500
chars of your prompt as `--task`, so injection narrows to what you actually asked:

```
GAIUS_PROMPT_INJECT          1 to enable        (default off)
GAIUS_PROMPT_BUDGET          tokens per prompt  (default 1200)
GAIUS_PROMPT_SKILLS_BUDGET   skills tokens      (default 0)
```

## Environment variables (read by the inject path)

| Var | Purpose |
|-----|---------|
| `GAIUS_CONFIG` | Config file path (default `~/.gaius/config.yaml` — holds `inject_min_priority` etc.) |
| `GAIUS_DB_PATH` | Override `facts.db` location |
| `GAIUS_MEMORY_DIR` | Root of memory files (feedback/domain/project/user/reference) |
| `GAIUS_DOMAIN_DIR` | Domain files (landscape probes live here) |
| `GAIUS_SOP_DIR` | SOP files |
| `GAIUS_SKILLS_DIR` | Skill files |
| `GAIUS_HANDOFF_DIR` | Session handoffs (default `~/.gaius/handoffs`) |
| `GAIUS_CONFIRMATION_BOOST_CAP` | Float cap on repetition-derived rank boosts (unset = no cap) |
| `GAIUS_ACTIVE_SKILL` | Stamped into injection telemetry when set |
| `CLAUDE_SESSION_ID` | Session attribution for telemetry |

## Troubleshooting

**Empty output from a hook.** Hooks suppress all errors by design. Run the same
command by hand: `gaius inject --budget 2000 --task "..."`. If that works, check that
`gaius` is on PATH for the hook's environment, that the hook didn't time out
(`GAIUS_INJECT_TIMEOUT`), and that a standalone install isn't making the plugin yield.

**`No corpus entries available.`** `facts.db` is empty — run `gaius retire` on some
sessions first, then `gaius index`.

**`No entries meet scoring threshold for injection.`** Everything scored zero or fell
below the priority floor. Try a more specific `--task` (quoted phrases help), drop
`--domain`, or lower `inject_min_priority` in `~/.gaius/config.yaml`.

**A fact you expected is missing.** Facts are skipped, never truncated, when they
exceed the remaining budget — raise `--budget`. Only 8 corpus entries inject per call;
check where the fact ranks with the MCP `gaius_search` tool (same corpus, hybrid
keyword + semantic scoring). If semantic scoring is dragging a keyword-relevant fact
down, compare with `--no-semantic`.

**Output larger than `--budget`.** The budget governs corpus/memory/SOP content only:
`gate: always` skills, `--skills-budget`, a matched handoff (≤ 3,000 tokens), and
~25 tokens of framing per corpus entry ride outside it. The header line
(`# Entries: N | Tokens: ~M`) reports the approximate total actually printed.
