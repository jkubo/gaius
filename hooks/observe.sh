#!/usr/bin/env bash
# gaius-observe — Lightweight PreToolUse observation capture
# Appends one JSONL line per tool-use event to ~/.claude/observations/{project-hash}/sessions.jsonl
#
# Usage: gaius-observe pre  (called by Claude Code PreToolUse hook)
#        gaius-observe post (called by Claude Code PostToolUse hook)
#
# Receives JSON from Claude Code on stdin:
#   { "session_id": "...", "cwd": "...", "tool_name": "...", "tool_input": {...} }
#
# Design goals: complete in <10ms, never fail the hook (|| true everywhere),
# background the write so hook returns immediately.

EVENT="${1:-pre}"

# Read stdin (tool call JSON from Claude Code)
TOOL_INPUT=$(cat)

# Extract fields (jq preferred — fast C binary; grep fallback for minimal envs)
if command -v jq &>/dev/null 2>&1; then
    TOOL_NAME=$(printf '%s' "$TOOL_INPUT" | jq -r '.tool_name // "unknown"' 2>/dev/null || echo "unknown")
    SESSION_ID=$(printf '%s' "$TOOL_INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null || echo "unknown")
    CWD=$(printf '%s' "$TOOL_INPUT" | jq -r '.cwd // ""' 2>/dev/null || echo "")
else
    TOOL_NAME=$(printf '%s' "$TOOL_INPUT" | grep -o '"tool_name":"[^"]*"' | cut -d'"' -f4 || echo "unknown")
    SESSION_ID=$(printf '%s' "$TOOL_INPUT" | grep -o '"session_id":"[^"]*"' | cut -d'"' -f4 || echo "unknown")
    CWD=$(printf '%s' "$TOOL_INPUT" | grep -o '"cwd":"[^"]*"' | cut -d'"' -f4 || echo "")
fi

# Background the write to return immediately (non-blocking)
{
    # Detect project hash from git remote in cwd
    PROJECT_HASH=$(cd "${CWD:-$PWD}" 2>/dev/null && \
        git remote get-url origin 2>/dev/null | sha256sum | cut -c1-12) || \
        PROJECT_HASH="no-git"
    PROJECT_HASH="${PROJECT_HASH:-no-git}"

    OBS_DIR="$HOME/.claude/observations/$PROJECT_HASH"
    mkdir -p "$OBS_DIR" 2>/dev/null || true

    TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    printf '{"ts":"%s","event":"%s","tool":"%s","session":"%s","project":"%s"}\n' \
        "$TIMESTAMP" "$EVENT" "${TOOL_NAME:-unknown}" \
        "${SESSION_ID:-unknown}" "$PROJECT_HASH" \
        >> "$OBS_DIR/sessions.jsonl" 2>/dev/null || true
} &

exit 0
