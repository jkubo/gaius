# review lifecycle — retire → review → corpus

gaius turns finished coding sessions into a curated fact corpus in three stages:
**retire** parses session JSONLs and stages compact summaries (auto-promoting
high-signal content into `facts.db` as it goes), **review** is a human pass over
the staged queue (`next` / `batch` / `show` + `done`), and **verdicts**
(`confirm` / `reject` / `defer`) settle individual facts the extractor was not
confident about. Nothing waits on review — facts flow to `inject` immediately
with an `auto` review state; the review loop exists to catch what automation
gets wrong, not to gate what it gets right.

```
session JSONLs ──retire──▶ staged summaries (~/.gaius/staged/)
                     │                 │
                     │           next / batch / show ──▶ done   (summary bookkeeping)
                     ▼
                 facts.db ──▶ pending facts ──▶ confirm / reject / defer
                     │
                     ▼
               gaius inject  (rejected facts excluded)
```

---

## Stage 1: retire — extract and stage

```bash
gaius retire                     # scan local sessions (default: ~/.claude/projects)
gaius retire --format gemini     # format-specific parsers: gemini, ollama,
                                 #   pentagi, grok, codex
gaius retire --all               # run every retire path in sequence
```

What a plain `retire` does:

- Scans every `*.jsonl` session file under the sessions dir (see
  [session-jsonl-schema.md](session-jsonl-schema.md) for the format), deduped by
  session UUID so a session that exists at both a live and an archive path is
  processed once.
- Extracts **compact summaries** and splits them into sections: Primary Request
  and Intent, Key Technical Concepts, Files and Code Sections, Errors and Fixes,
  Pending Tasks, Current Work. Each summary is staged as one JSON file in
  `~/.gaius/staged/`.
- **Mines uncompacted sessions** for signal too — those summaries carry a
  `[mined]` tag in the review UI.
- **Auto-promotes** bullets from Key Technical Concepts and Errors and Fixes
  into `facts.db`, domain-tagged by keyword. Most new facts land with
  `review_state='auto'`; a fact drops to `'pending'` when its scored confidence
  is below 0.5 or it contradicts an existing fact (contradictions also flag the
  *older* fact back to pending, so both sides surface for review).
- Re-runs are cheap: unchanged summaries are skipped by content hash; a summary
  whose content changed is updated and **re-queued for review**.
- Peer coding-agent sessions (Grok, Codex) are swept automatically on every
  plain `retire` when those CLIs are installed.
- A TF-IDF pass scores all staged entries so the highest-signal summaries sort
  first.

---

## Stage 2: review the staged queue

```bash
gaius next           # oldest unreviewed item, highest-priority queue first
gaius batch          # all unreviewed summaries with signal, in sequence
gaius show           # full queue listing (unreviewed first)
```

`next` works through three queues in priority order: staged event facts from
format-specific retires, then **pending facts** in `facts.db`, then session
summaries. `--facts` shows only pending facts; `--summaries` skips straight to
summaries.

`show` marks summaries with `★` when they contain signal sections and tags which
are present: `K` = key concepts, `E` = errors/fixes, `P` = pending tasks.

### The `state_change` flag

At staging time each summary is scanned for operational-transition verbs
(*deleted, decommissioned, migrated, completed, shipped, torn down, removed,
deprecated, cutover, flipped, promoted, scaled down, terminated*). Matches set
`state_change: true`, and `batch` lists those summaries **first**, tagged:

```
⚡STATE-CHANGE — verify project files reflect this
```

Why it matters: a state transition ("the old queue consumer was torn down")
often exists *only* in session history. If review is delayed, every durable
memory file that still describes the old state is now wrong. Review
state-change summaries first and update your long-lived notes before marking
them done.

### done — summary bookkeeping

```bash
gaius done a1b2c3d4      # UUID prefix, min 4 chars
gaius rescan a1b2c3d4    # force re-extraction from the source JSONL, re-queue
```

`done` marks a summary reviewed and drops it from the queue. It does **not**
touch facts — promotion already happened at retire time. The job during review
is to read the summary, make sure durable memory reflects it (especially
state changes), then mark it done. If a prefix matches several summaries, `done`
auto-resolves to the single unreviewed match and errors only when that is
ambiguous too.

---

## Stage 3: verdicts on pending facts

Verdicts operate on **facts** (integer id, shown by `gaius next`), not summaries
(UUID prefix). That is the practical difference from `done`.

| Command | Effect on the fact |
|---|---|
| `gaius confirm <id>` | `confidence=1.0`, `confidence_source='human'`, state `confirmed` — full inject weight |
| `gaius reject <id>` | state `rejected` **and** `outcome='rejected'` — excluded from inject, retained in `facts.db` for audit |
| `gaius defer <id>` | state `deferred` — `gaius next` re-surfaces it after 7 days |

`reject` is the only way to remove a bad fact from circulation; `confirm` is the
only way a fact reaches human-grade confidence.

---

## How facts reach inject

`gaius inject --task "..." [--budget N]` selects from `facts.db` excluding
tombstoned and rejected facts (`outcome != 'rejected'`), ranks by relevance to
the task, and fills the token budget. `--budget` is optional and has a sensible
default. Review state shapes rank, not membership:

- `auto` and `confirmed` facts rank at full weight (1.0)
- `pending` and `deferred` facts are penalized (0.6) until settled

So an unreviewed corpus is still useful on day one, and every verdict you issue
sharpens it: confirm boosts, reject removes, defer parks.

---

## Worked session

```
$ gaius retire
Scanning 212 JSONL session files in ~/.claude/projects...
Scored:    9 entries (TF-IDF)
New:       2
Mined:     1 (from uncompacted sessions)
Updated:   0 (content changed)
Skipped:   205 (unchanged)
Deduped:   4 (duplicate UUID paths skipped)
Total:     208  (3 unreviewed)
Staging:   ~/.gaius/staged

$ gaius batch
Batch mode: 3 summaries with signal (1 ⚡state-change, listed first)

====================================================================
[1/3]  9f3ab2c1  2026-07-30 ⚡STATE-CHANGE — verify project files reflect this
====================================================================

── Key Technical Concepts ──
- The legacy /v1/orders poller was torn down; the API now receives
  webhook callbacks on /hooks/orders with HMAC verification

── Pending Tasks ──
- Add a replay endpoint for missed webhook deliveries
...

$ gaius done 9f3ab2c1
✓ Marked reviewed: 9f3ab2c1  |  2 remaining

$ gaius next
====================================================================
[PENDING FACT]  id=214  domain=services
Confidence:     30%  (contradiction)
First seen:     2026-07-30T22:10:03
Pending queue:  2
====================================================================

The API health-check path is /healthz (returns build sha since v2.3)
Conflicts with: [187] The API health-check path is /status

====================================================================
Confirm:  gaius confirm 214
Reject:   gaius reject 214
Defer:    gaius defer 214

$ gaius confirm 214
✓ Confirmed: [214] The API health-check path is /healthz (returns build sha since v2.3)  |  1 pending remaining

$ gaius next --facts       # surfaces the conflicting fact [187] next
$ gaius reject 187
✗ Rejected: [187] The API health-check path is /status  |  0 pending remaining
```

From here, `gaius inject --task "debug the orders webhook"` can pull the
confirmed fact at full weight; the rejected one is gone from the pool.
