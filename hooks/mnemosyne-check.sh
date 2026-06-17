#!/usr/bin/env bash
# gaius-mnemosyne-check — PostToolUse hook: health check after memory directory edits
#
# Fires after Edit or Write tool calls. If the file touched is inside
# the configured memory directory, runs mnemosyne health and surfaces any RED/YELLOW
# files to Claude's context so they can be fixed in the same session.
#
# Exits 0 always (PostToolUse hooks that exit non-zero block the tool result).

TOOL_INPUT=$(cat)

TOOL_NAME=$(printf '%s' "$TOOL_INPUT" | jq -r '.tool_name // ""' 2>/dev/null)
FILE_PATH=$(printf '%s' "$TOOL_INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null)

# Only fire on Edit or Write
case "$TOOL_NAME" in
  Edit|Write) ;;
  *) exit 0 ;;
esac

# Only fire if the file is inside the configured memory directory
MEMORY_DIR="${MNEMOSYNE_MEMORY_DIR:-$HOME/.gaius/memory}"
case "$FILE_PATH" in
  "$MEMORY_DIR"/*) ;;
  *) exit 0 ;;
esac

MNEMOSYNE="$HOME/.local/bin/mnemosyne"
[[ -x "$MNEMOSYNE" ]] || exit 0

# Run health — only print if there's something to report
OUTPUT=$("$MNEMOSYNE" health 2>/dev/null)

# Surface RED and YELLOW files only (suppress full table in clean state)
if echo "$OUTPUT" | grep -qE 'RED|YELLOW'; then
  echo ""
  echo "── mnemosyne health (post-edit) ──"
  echo "$OUTPUT" | grep -E 'RED|YELLOW|⚠|⚡|✓'
  echo ""
fi

exit 0
