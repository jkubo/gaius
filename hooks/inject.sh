#!/usr/bin/env bash
# gaius-inject — PreToolUse hook: auto-inject corpus + skills on session start
#
# Fires once per session when CLAUDE.md is Read (session start signal).
# Outputs gaius inject result to stdout → Claude Code injects it as context.
#
# Usage: called by Claude Code PreToolUse hook (receives JSON on stdin)
#
# Design:
#   - Sentinel file /tmp/gaius-inject-{session_id[:16]} prevents re-injection
#   - Context terms built from git diff (changed files signal what we're working on)
#   - Timeout 10s; silent on failure (never blocks the tool call)

TOOL_INPUT=$(cat)

# Parse fields from hook JSON
if command -v jq &>/dev/null; then
    TOOL_NAME=$(printf '%s' "$TOOL_INPUT" | jq -r '.tool_name // ""'      2>/dev/null)
    FILE_PATH=$(printf '%s' "$TOOL_INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null)
    SESSION_ID=$(printf '%s' "$TOOL_INPUT" | jq -r '.session_id // "unknown"'   2>/dev/null)
    CWD=$(printf '%s'        "$TOOL_INPUT" | jq -r '.cwd // ""'           2>/dev/null)
else
    TOOL_NAME=$(printf '%s' "$TOOL_INPUT"  | grep -o '"tool_name":"[^"]*"' | cut -d'"' -f4)
    FILE_PATH=$(printf '%s' "$TOOL_INPUT"  | grep -o '"file_path":"[^"]*"' | cut -d'"' -f4)
    SESSION_ID=$(printf '%s' "$TOOL_INPUT" | grep -o '"session_id":"[^"]*"' | cut -d'"' -f4)
    CWD=$(printf '%s'        "$TOOL_INPUT" | grep -o '"cwd":"[^"]*"'       | cut -d'"' -f4)
fi

# Only fire on Read of CLAUDE.md
[[ "$TOOL_NAME" != "Read" ]]        && exit 0
[[ "$FILE_PATH" != *"CLAUDE.md" ]]  && exit 0

# Session sentinel — inject exactly once per session
SENTINEL="/tmp/gaius-inject-${SESSION_ID:0:16}"
[[ -f "$SENTINEL" ]] && exit 0
touch "$SENTINEL"

# Build context terms from git working-tree diff in cwd
# Changed/staged files are the strongest signal for what skill is needed
CONTEXT_TERMS=""
if [[ -n "$CWD" && -d "$CWD" ]]; then
    DIFF_FILES=$(git -C "$CWD" diff --name-only HEAD 2>/dev/null | head -10)
    STAGED_FILES=$(git -C "$CWD" diff --name-only --cached 2>/dev/null | head -10)
    COMBINED=$(printf '%s\n%s' "$DIFF_FILES" "$STAGED_FILES" | sort -u | tr '\n' ' ')
    # Add cwd directory name as a weak signal (e.g. "ansible", "cctv-api")
    COMBINED="$COMBINED $(basename "${CWD:-/}")"
    CONTEXT_TERMS=$(printf '%s' "$COMBINED" | tr '/' ' ')
fi

# Run gaius inject (10s timeout, errors suppressed)
# stdout goes to Claude Code as pre-tool context
timeout 10 gaius inject \
    --budget 3000 \
    --skills-budget 2000 \
    ${CONTEXT_TERMS:+--skills-context "$CONTEXT_TERMS"} \
    2>/dev/null

exit 0
