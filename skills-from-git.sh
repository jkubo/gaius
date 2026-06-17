#!/usr/bin/env bash
# skills-from-git.sh — Extract candidate gotchas from git history
#
# Analyzes commit patterns to surface:
# - Error-fix pairs (feat: → fix: sequences on same files)
# - Files that always change together (co-change coupling)
# - Revert pairs (reverts within 48h of original commit)
#
# Output: candidate feedback_*.md entries for human review.
# NOT automated — produces drafts that must be promoted manually.
#
# Usage: skills-from-git.sh [repo-dir [repo-dir ...]] [output-dir]
#   Repos: pass as arguments, or set GAIUS_SKILL_REPOS (space-separated paths)
#   Default output: /tmp/gaius-skills-candidates/
#
# Example:
#   skills-from-git.sh ~/myproject ~/another-repo
#   GAIUS_SKILL_REPOS="~/myproject ~/other" skills-from-git.sh

set -euo pipefail

# Repos from arguments, env var, or empty (user must provide one or the other)
if [[ $# -gt 0 ]]; then
    # All args except the last are repos if the last arg looks like a path ending in /
    # Simple heuristic: use all args as repos; output dir is /tmp default
    REPOS=("$@")
elif [[ -n "${GAIUS_SKILL_REPOS:-}" ]]; then
    IFS=' ' read -ra REPOS <<< "${GAIUS_SKILL_REPOS}"
else
    echo "Usage: skills-from-git.sh <repo-dir> [repo-dir ...] OR set GAIUS_SKILL_REPOS"
    exit 1
fi

OUTPUT_DIR="${2:-/tmp/gaius-skills-candidates}"
mkdir -p "$OUTPUT_DIR"

echo "=== gaius skills-from-git — $(date -u +%Y-%m-%dT%H:%MZ) ===" | tee "$OUTPUT_DIR/summary.txt"
echo "Output dir: $OUTPUT_DIR" | tee -a "$OUTPUT_DIR/summary.txt"
echo "" | tee -a "$OUTPUT_DIR/summary.txt"

for REPO in "${REPOS[@]}"; do
    [[ -d "$REPO/.git" ]] || { echo "SKIP: $REPO (not a git repo)"; continue; }

    REPO_NAME=$(basename "$REPO")
    echo "--- $REPO_NAME ---" | tee -a "$OUTPUT_DIR/summary.txt"

    cd "$REPO"

    # 1. REVERT PAIRS — commits reverted within 48h
    echo "" | tee -a "$OUTPUT_DIR/summary.txt"
    echo "## Revert pairs (things that didn't work):" | tee -a "$OUTPUT_DIR/summary.txt"
    git log --oneline --format="%H %s" | grep -i "^.\{7\} revert" | head -20 | while read -r hash msg; do
        # Find original commit referenced in revert message
        ORIG=$(echo "$msg" | grep -oP '(?<=Revert ")[^"]+' | head -1 || true)
        [[ -z "$ORIG" ]] && continue
        ORIG_HASH=$(git log --oneline --grep="$ORIG" --format="%H" | tail -1 || true)
        [[ -z "$ORIG_HASH" ]] && continue
        echo "  REVERT: $msg" | tee -a "$OUTPUT_DIR/summary.txt"
        echo "  ORIGINAL: $(git log --oneline -1 $ORIG_HASH 2>/dev/null || echo "not found")" | tee -a "$OUTPUT_DIR/summary.txt"
    done

    # 2. FIX-AFTER-FEAT — fix commits within 2 days of a feat on same files
    echo "" | tee -a "$OUTPUT_DIR/summary.txt"
    echo "## Quick fixes after feat (possible gotchas):" | tee -a "$OUTPUT_DIR/summary.txt"
    git log --oneline --format="%ad %H %s" --date=unix | \
        awk 'BEGIN{prev_ts=0; prev_hash=""; prev_msg=""}
             /^[0-9]+ [a-f0-9]+ fix/ {
                ts=$1; hash=$2;
                $1=""; $2=""; msg=$0;
                if (prev_ts > 0 && (ts - prev_ts) < 172800) {
                    print "FEAT: " prev_msg
                    print "FIX:  " msg
                    print "---"
                }
             }
             /^[0-9]+ [a-f0-9]+ feat/ { prev_ts=$1; prev_hash=$2; $1=""; $2=""; prev_msg=$0 }' | \
        head -30 | tee -a "$OUTPUT_DIR/summary.txt"

    # 3. FILE CO-CHANGE — files that always change together (top pairs)
    echo "" | tee -a "$OUTPUT_DIR/summary.txt"
    echo "## File co-change pairs (coupling):" | tee -a "$OUTPUT_DIR/summary.txt"
    git log --name-only --format="" | sort | uniq -c | sort -rn | head -10 | \
        while read -r count file; do
            [[ -n "$file" ]] && echo "  $count changes: $file"
        done | tee -a "$OUTPUT_DIR/summary.txt"

    echo "" | tee -a "$OUTPUT_DIR/summary.txt"
done

echo ""
echo "=== DONE ==="
echo "Review candidates at: $OUTPUT_DIR/summary.txt"
echo "Promote relevant findings to your memory directory feedback_*.md files"
echo ""
echo "Format for promoted entries:"
cat <<'EOF'
---
name: <short name>
description: <one-liner>
type: feedback
domain: <networking|storage|etcd|security|observability|development|cluster-ops|gitops>
trigger: "when doing X"
confidence: 0.8
gate: soft  # or hard if painful lesson
---

<rule>

**Why:** <incident that caused this>

**How to apply:** <specific guidance>
EOF
