#!/usr/bin/env bash
# gaius plugin — UserPromptSubmit hook: inject facts relevant to THIS prompt.
#
# SessionStart injects on cwd + git state alone; this narrows on what was
# actually asked. Off by default because it bills tokens on every turn —
# set GAIUS_PROMPT_INJECT=1 to enable.
#
# Tunables (env):
#   GAIUS_PROMPT_INJECT         1 to enable (default off)
#   GAIUS_PROMPT_BUDGET         corpus token budget per prompt (default 1200)
#   GAIUS_PROMPT_SKILLS_BUDGET  skills token budget per prompt (default 0)
#   GAIUS_INJECT_TIMEOUT        seconds (default 8)
set -u

[[ "${GAIUS_PROMPT_INJECT:-0}" == "1" ]] || exit 0

# Yield to a standalone install's own hooks (see session-start.sh).
if [[ "${GAIUS_PLUGIN_HOOKS:-}" != "force" && -x "$HOME/.local/bin/gaius-inject-prompt" ]]; then
    exit 0
fi

HOOK_JSON=$(cat 2>/dev/null || true)

# The prompt is free text — jq is the only safe way to pull it out of the JSON.
command -v jq >/dev/null 2>&1 || exit 0
PROMPT=$(printf '%s' "$HOOK_JSON" | jq -r '.prompt // ""' 2>/dev/null | head -c 500)
[[ -n "$PROMPT" ]] || exit 0

if command -v gaius >/dev/null 2>&1; then
    GAIUS=(gaius)
elif [[ -x "$HOME/.local/bin/gaius" ]]; then
    GAIUS=("$HOME/.local/bin/gaius")
else
    exit 0
fi

timeout "${GAIUS_INJECT_TIMEOUT:-8}" "${GAIUS[@]}" inject \
    --task "$PROMPT" \
    --budget "${GAIUS_PROMPT_BUDGET:-1200}" \
    --skills-budget "${GAIUS_PROMPT_SKILLS_BUDGET:-0}" \
    2>/dev/null

exit 0
