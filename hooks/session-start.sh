#!/usr/bin/env bash
# gaius plugin — SessionStart hook: inject the relevant slice of the corpus.
#
# stdout becomes session context, so this must print facts and nothing else:
# no banners, no progress, no errors. Any failure is silent and exits 0 —
# a memory tool that can't answer must never stop the session from starting.
#
# Tunables (env):
#   GAIUS_INJECT_BUDGET   corpus token budget   (default 2000)
#   GAIUS_SKILLS_BUDGET   skills token budget   (default 1500)
#   GAIUS_INJECT_TIMEOUT  seconds               (default 12)
#   GAIUS_PLUGIN_HOOKS    set to "force" to inject even when standalone hooks exist
set -u

HOOK_JSON=$(cat 2>/dev/null || true)

# A standalone install wires its own hooks into settings.json. Running both
# would inject the same corpus twice and bill the tokens twice, so the plugin
# yields to the standalone install unless explicitly forced.
if [[ "${GAIUS_PLUGIN_HOOKS:-}" != "force" && -x "$HOME/.local/bin/gaius-inject-session-start" ]]; then
    exit 0
fi

# Resume already carries the earlier injection in its context.
if command -v jq >/dev/null 2>&1; then
    SOURCE=$(printf '%s' "$HOOK_JSON" | jq -r '.source // ""' 2>/dev/null)
    [[ "$SOURCE" == "resume" ]] && exit 0
fi

# Resolve gaius: PATH, then the usual script-install location, then uv's
# ephemeral env (which is how the bundled MCP server runs too).
if command -v gaius >/dev/null 2>&1; then
    GAIUS=(gaius)
elif [[ -x "$HOME/.local/bin/gaius" ]]; then
    GAIUS=("$HOME/.local/bin/gaius")
elif command -v uvx >/dev/null 2>&1; then
    GAIUS=(uvx --from "gaius-memory @ git+https://github.com/jkubo/gaius" gaius)
else
    exit 0
fi

timeout "${GAIUS_INJECT_TIMEOUT:-12}" "${GAIUS[@]}" inject \
    --budget "${GAIUS_INJECT_BUDGET:-2000}" \
    --skills-budget "${GAIUS_SKILLS_BUDGET:-1500}" \
    2>/dev/null

exit 0
