#!/bin/bash
# gaius loop-sync — refresh the closed self-improvement loop's signals (2026-06-24).
#
# Runs the loop's read/additive commands so outcome + corpus-integrity signals stay fresh:
#   1. ingest-outcomes : pull completed-task outcomes from the orchestrator into task_outcomes
#                        (additive table, idempotent by key — never touches the facts corpus)
#   2. corpus-audit    : snapshot corpus integrity (READ-ONLY) for the health trend log
#
# SAFE: no SeaweedFS-filer interaction, no corpus mutation, no billing. Versioned so the
# nightly wiring is reviewable (commit-each-step). Wire it in via either:
#   - a line in ~/.local/bin/gaius-nightly-sync  (append: bash <this>/loop-sync.sh), or
#   - a user systemd timer (gaius-loop-sync.timer) calling this script.
#
# Override the CLI with GAIUS_BIN (defaults to the installed `gaius`).
set -uo pipefail

GAIUS="${GAIUS_BIN:-gaius}"
ORCH="${AGENT_ORCH_URL:-http://localhost:8080}"
LOG="${HOME}/.gaius/logs/loop-sync.log"
mkdir -p "$(dirname "$LOG")"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

echo "[$(ts)] loop-sync start (orch=$ORCH)" >>"$LOG"

# 1. Pull outcomes (additive, idempotent). Non-fatal on failure.
if "$GAIUS" ingest-outcomes --orch "$ORCH" --limit 500 >>"$LOG" 2>&1; then
  echo "[$(ts)] ingest-outcomes ok" >>"$LOG"
else
  echo "[$(ts)] ingest-outcomes FAILED (non-fatal)" >>"$LOG"
fi

# 2. Corpus integrity snapshot (read-only) for the health trend.
if "$GAIUS" corpus-audit --json >>"$LOG" 2>&1; then
  echo "[$(ts)] corpus-audit ok" >>"$LOG"
else
  echo "[$(ts)] corpus-audit FAILED (non-fatal)" >>"$LOG"
fi

# 3. Source reconcile: promote curated source-of-truth facts (insert-once, flagged-unverified)
#    + log dev<->mirror divergence. Closes the promotion gap; mechanical, no LLM, no billing.
if "$GAIUS" reconcile --promote >>"$LOG" 2>&1; then
  echo "[$(ts)] reconcile ok" >>"$LOG"
else
  echo "[$(ts)] reconcile FAILED (non-fatal)" >>"$LOG"
fi

echo "[$(ts)] loop-sync done" >>"$LOG"
