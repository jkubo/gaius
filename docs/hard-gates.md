# hard gates — safety rules that don't rely on recall

A hard gate is a behavioral rule enforced by **mechanism, not retrieval luck**. Ordinary
memory competes for context: it gets scored, ranked, capped — and sometimes loses. A safety
rule that surfaces 80% of the time is not a safety rule; the one session it misses is the
one that force-pushes over main. gaius marks a rule as a hard gate and changes how every
shipped stage treats it: **selection is deterministic** (a triggered gate cannot lose its
context slot to a higher-scoring generic fact), and **telemetry closes the loop** (did the
gate fire, was it violated, has it gone stale). Blocking the tool call itself is a thin
hook you wire into your agent harness — gaius ships the audit trail for it (last section).

A gate lives in one of two places:

| Where | File | Granularity |
|-------|------|-------------|
| Feedback rule | `<memory_dir>/feedback/*.md` | one incident-derived rule |
| Skill gate | `<skills_dir>/*.md` with `gate:` frontmatter | a whole session-mode guide |

---

## Feedback rules — per-incident gates

Feedback rules are markdown files in the `feedback/` subdirectory of your memory dir
(`memory_dir` in `~/.gaius/config.yaml`, or `GAIUS_MEMORY_DIR`; if unset, gaius
auto-discovers the agent project memory dir). They are human-curated and live outside
`facts.db` — gaius reads them at inject time, never rewrites them.

A rule becomes a hard gate two ways:

- `description:` frontmatter contains `hard gate` (case-insensitive), or
- the literal string `HARD GATE` appears in the body — honored only for files in
  `feedback/` and `domain/`, so an auto-generated file *quoting* a gate cannot
  inherit its privileges.

Structure convention: the rule first, then `**Why:**` (the incident), then
`**How to apply:**` (the procedure). At inject time gaius keeps the rule and the
How-to-apply section and drops the Why narrative — the model needs the instruction,
not the war story.

### What "hard" buys at inject time

Gates are **trigger-scoped, then guaranteed**. The rule must still match the task text
(keyword or semantic overlap) — a gate about prod databases stays out of your CSS session.
Once triggered, selection is deterministic:

- `feedback/` is scanned first, at the highest priority of any memory type.
- 1.5× keyword score multiplier.
- Relaxed semantic bar: a triggered gate survives below the per-type cosine threshold;
  only the "truly irrelevant" floor (cosine < 0.20) drops it.
- **Count-cap exemption**: ordinary feedback rules take the top 3 slots — every triggered
  hard gate injects, competing only against other gates.
- Budget honesty: gates share a pool capped at 40% of the inject budget, so one
  5K-token rule cannot starve the corpus out of the session.

Feedback scanning keys on the task text, so pass `--task`:

```bash
gaius inject --task "clean out stale rows from the orders database"
```

The plugin's per-prompt hook (`GAIUS_PROMPT_INJECT=1`) passes each prompt as `--task`
automatically. `--budget` is optional (default 2000 tokens).

---

## Skill gates — session-mode guides

Skills are prospective how-to files in `skills_dir` (config key, or `GAIUS_SKILLS_DIR`;
default: a `skills/` directory next to your domain dir). The `gate:` frontmatter field
sets the enforcement tier:

| `gate:` | Behavior |
|---------|----------|
| `always` | Injected unconditionally, outside the token budget. Never ranked. |
| `hard` / `mandate` | Score floor + 1.5× multiplier — never excluded for lack of signal; injected even when zero keywords match, budget permitting. |
| `reference` (default) | Normal ranking; excluded entirely when no keyword or path signal. |

```yaml
---
name: deploy-safety
description: "Deploy procedure and rollback rules"
domain: deploy
gate: mandate
trigger: "deploy, rollout, release, rollback"
paths: "deploy/**,manifests/**/*.yaml"
also_load: verification-gate
---
```

`paths:` globs match the files you're touching (ground truth beats keywords);
`also_load` pulls dependency skills in with the winner.

Inspect the gate landscape:

```bash
gaius skills                                  # every skill: domain, gate, staleness
gaius skills --score "rollback the deploy"    # ranked, with score-per-token
gaius suggest                                 # domains with many facts but NO mandate skill
```

`gaius suggest` is the coverage check: a domain accumulating facts without a
`mandate`-gated skill is a gap — incidents are being remembered but no rule guards them.

---

## Worked example

```bash
# One-time setup (non-interactive)
gaius init --backend claude --yes

# Define a gate from a real incident
mkdir -p ~/my-memory/feedback
cat > ~/my-memory/feedback/no-prod-delete.md <<'EOF'
---
name: no-prod-delete
description: "HARD GATE: destructive prod SQL requires a same-session verified backup"
---
Never run DROP, TRUNCATE, or DELETE-without-WHERE against the production database
unless a backup was taken AND verified in this session.

**Why:** 2025-11-03 — a cleanup script truncated the live orders table; the most
recent backup was 26 hours old and silently corrupt.

**How to apply:**
- Before any destructive SQL: take a backup, restore it to a scratch database,
  and row-count the restored table.
- Paste both outputs before proceeding. No verified backup, no destructive statement.
EOF
```

Trigger it:

```bash
$ gaius inject --task "clean out stale rows from the orders database"
### Feedback: no-prod-delete
_HARD GATE: destructive prod SQL requires a same-session verified backup_

Never run DROP, TRUNCATE, or DELETE-without-WHERE against the production database
unless a backup was taken AND verified in this session.

**How to apply:**
- Before any destructive SQL: take a backup, restore it to a scratch database, ...
```

The Why section stayed home; the rule and procedure injected. An unrelated task
(`--task "write unit tests for the parser"`) leaves the gate out — gates are scoped,
not spam.

---

## What happens on violation

Injection puts the rule in front of the model every relevant session; the harness decides
whether the model obeys. gaius ships the accounting in `~/.gaius/telemetry.db`
(`GAIUS_TELEMETRY_DB` to override): an `enforcement_events` table plus event types the
review loop surfaces —

| Event | Meaning |
|-------|---------|
| `hard_gate_fired` | a gate was injected for this session |
| `violation_detected` | the model acted contrary to an injected gate — logged for review |
| `rule_never_fires` | a gate hasn't matched any prompt in 30+ days — stale or too narrow |
| `enforcement_bypass` | a blocked pattern proceeded anyway (e.g. indirect execution) |

Log from your own tooling via `gaius.telemetry.log_enforcement_event(session_id,
command_hash, check_name, result, message=...)`.

## Mechanical blocking — bring your own hook

The shipped plugin registers three hooks: SessionStart (inject), UserPromptSubmit
(inject), PostToolUse (memory health check). **None of them block tool calls.** Most
agent harnesses expose a pre-tool-use hook where a non-zero exit vetoes the call — in
Claude Code, exit code 2 blocks the tool and feeds stderr back to the model. Upgrading a
gate from "always in context" to "physically enforced" is a script you own:

```bash
#!/usr/bin/env bash
# PreToolUse hook: veto force-push. Claude Code: exit 2 = block, stderr -> model.
INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // ""')
if [[ "$CMD" == *"push --force"* || "$CMD" == *"push -f"* ]]; then
    HASH=$(printf '%s' "$CMD" | sha256sum | cut -c1-12)
    python3 -c "from gaius.telemetry import log_enforcement_event as log; \
log('hook', '$HASH', 'no-force-push', 'block', message='vetoed by pre-tool hook')"
    echo "BLOCKED by hard gate no-force-push: force-push requires operator confirmation" >&2
    exit 2
fi
exit 0
```

Register it under `PreToolUse` in your harness settings with a `Bash` matcher. Pair every
blocking pattern with a feedback rule: the injection tells the model *why* before it acts,
the hook makes disobedience mechanical, and the telemetry table tells you which layer did
the work.
