"""gaius - Session memory lifecycle manager for Claude Code projects.

Extracts compact summaries from Claude Code session JSONLs and stages
them for review, enabling facts from past sessions to be promoted to
persistent memory files (domain/*.md are READ-ONLY from gaius — never
written by gaius index; all index output goes to ~/.gaius/corpus/).

INVARIANT (enforced by _guard_write_path):
  gaius index must ONLY write inside CORPUS_DIR (~/.gaius/corpus/).
  domain/*.md and troubleshooting.md are human+agent-curated. gaius reads
  them for context but never writes to them.
  If you add a new write path to process_session or any function it calls,
  you MUST call _guard_write_path(path) before opening the file.
  Bypassing this guard is a bug, not a shortcut.

Usage:
  gaius [--sessions-dir DIR] [--staging-dir DIR] [--format FMT] <command> [args]

Options:
  --sessions-dir DIR  Override session JSONL directory
                      (env: GAIUS_SESSIONS_DIR, default: ~/.claude/projects/...)
  --staging-dir DIR   Override staging output directory
                      (env: GAIUS_STAGING_DIR, default: ~/.gaius/staged)
  --format FMT        Session format: claude, gemini, ollama (default: claude)

Commands:
  retire      Scan JSONL files and stage new compact summaries
  s3-retire   Scan session JSONLs from S3/rclone remote for a given agent
  harvest     Scan cold Gemini CLI sessions (.json), stage events for review
  inject      Inject ranked corpus entries into context (--budget N tokens, default 2000, --skills-budget N, --landscape DOMAIN)
  landscape   Run live landscape commands for a domain (cached by TTL)
  skills      List all skills with domain/trigger/gate/line-count
  index       Parse JSONL, build domain index, write deltas and corpus
  migrate     Migrate agent memory: corpus, S3 paths, and attribution
  show        List all staged summaries (unreviewed first)
  next        Print the oldest unreviewed summary (Gemini staged facts reviewed first)
  done ID     Mark a summary as reviewed (ID = uuid prefix, min 4 chars)
  rescan ID   Force re-extraction for a session (ID = uuid prefix, min 4 chars)
  stats       Show extraction and corpus statistics (includes facts.db)
  batch         Show unreviewed summaries by section (bulk scan mode)
"""

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

try:
    import sqlite_vec
    HAS_SQLITE_VEC = True
except ImportError:
    HAS_SQLITE_VEC = False

# ── Config file ──────────────────────────────────────────────────────────────
# Loaded from ~/.gaius/config.yaml (or GAIUS_CONFIG env var).
# All values here are optional overrides; built-in defaults apply when absent.
_GAIUS_CONFIG_FILE = Path(os.environ.get(
    "GAIUS_CONFIG",
    Path.home() / ".gaius" / "config.yaml"
))

def _load_gaius_config() -> dict:
    if _GAIUS_CONFIG_FILE.exists() and HAS_YAML:
        try:
            with open(_GAIUS_CONFIG_FILE) as _f:
                return yaml.safe_load(_f) or {}
        except Exception:
            pass
    return {}

_gaius_cfg = _load_gaius_config()

# ── ANSI colors ───────────────────────────────────────────────────────────────
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

# ── Configuration ────────────────────────────────────────────────────────────

# Operator identity — configurable via operator.name in ~/.gaius/config.yaml.
# Used in output/stats to identify the human principal.
OPERATOR_NAME: str = _gaius_cfg.get("operator", {}).get("name", "operator")

# Defaults — overridden by --sessions-dir / --staging-dir / env vars in main()
# sessions_dir in config should be set to your Claude Code project directory.
# Default: scan all project dirs under ~/.claude/projects/ (auto-discovery).
_cfg_sessions_dir = _gaius_cfg.get("sessions_dir")
PROJECT_DIR = (
    Path(_cfg_sessions_dir).expanduser() if _cfg_sessions_dir
    else Path.home() / ".claude" / "projects"
)
STAGING_DIR = Path.home() / ".gaius" / "staged"
CORPUS_DIR = Path.home() / ".gaius" / "corpus"
DB_PATH = Path.home() / ".gaius" / "facts.db"
if os.environ.get("GAIUS_DB_PATH"):
    DB_PATH = Path(os.environ["GAIUS_DB_PATH"])

# Memory directory — where curated memory files (feedback, domain, project, etc.) live.
# Auto-discovery: scan ~/.claude/projects/*/memory/ for dirs containing MEMORY.md.
# Override: memory_dir in config.yaml, or GAIUS_MEMORY_DIR env var.
def _discover_memory_dir() -> Path | None:
    """Find the Claude Code project memory dir with the most files (primary project)."""
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.is_dir():
        return None
    best, best_count = None, 0
    for d in sorted(claude_projects.iterdir()):
        candidate = d / "memory"
        if (candidate / "MEMORY.md").is_file():
            count = sum(1 for _ in candidate.rglob("*.md"))
            if count > best_count:
                best, best_count = candidate, count
    return best

_cfg_memory_dir = (
    os.environ.get("GAIUS_MEMORY_DIR")
    or _gaius_cfg.get("memory_dir")
)
MEMORY_DIR: Path | None = (
    Path(_cfg_memory_dir).expanduser() if _cfg_memory_dir
    else _discover_memory_dir()
)

def _guard_write_path(path):
    """Invariant: gaius index must only write inside CORPUS_DIR.

    Call before any open(..., 'w'|'a') in the index call stack.
    Hard abort if the path escapes corpus/ — no recovery, no workaround.
    This is a correctness invariant, not a permissions check.
    """
    p = Path(path).resolve()
    corpus = CORPUS_DIR.resolve()
    if not str(p).startswith(str(corpus) + "/") and p != corpus:
        print(f"\n\033[1;31m🚨 SAFETY ABORT\033[0m: gaius attempted write outside corpus/", file=sys.stderr)
        print(f"   Target : {p}", file=sys.stderr)
        print(f"   Corpus : {corpus}", file=sys.stderr)
        print(f"   This is a bug in gaius. Do NOT add a workaround — fix the write path.", file=sys.stderr)
        sys.exit(1)
    return p



# Alias blocklist — aliases that should never be promoted to corpus.
# Extend via alias_blocklist in ~/.gaius/config.yaml.
_DEFAULT_ALIAS_BLOCKLIST: frozenset = frozenset()
ALIAS_BLOCKLIST: frozenset = _DEFAULT_ALIAS_BLOCKLIST | frozenset(
    _gaius_cfg.get("alias_blocklist", [])
)

SPECS_DIR = Path(__file__).resolve().parent.parent / "domain" / "specs"
GEMINI_DIR = Path.home() / ".gemini" / "tmp"
GEMINI_COLD_THRESHOLD_HOURS = 4
SIGNAL_THRESHOLD = 0.55   # entries above this always go to corpus

# Extra session directory — optional second Claude Code project to scan (e.g. an advisor agent).
# Set via --extra-sessions-dir or GAIUS_EXTRA_SESSIONS_DIR env var.
# Also configurable via extra_sessions_dir in ~/.gaius/config.yaml.
_cfg_extra_dir = _gaius_cfg.get("extra_sessions_dir")
_default_extra_sessions = (
    Path(_cfg_extra_dir).expanduser() if _cfg_extra_dir
    else None
)
EXTRA_SESSIONS_DIR: "Path | None" = (
    _default_extra_sessions if _default_extra_sessions and _default_extra_sessions.exists() else None
)



# Agent name → session format mapping for S3 retire.
# Configurable via principals.formats in ~/.gaius/config.yaml.
# Unknown agents default to "claude" format.
# Example config:
#   principals:
#     formats:
#       my-gemini-agent: gemini
#       my-ollama-agent: ollama
_DEFAULT_FORMAT_BY_AGENT: dict = {
    # "pentagi" is a known open-source agent framework; its format is included as a default.
    "pentagi": "pentagi",
}

# Model identity for each format — used by parsers and stats.
MODEL_INFO: dict = {
    "claude":  {"family": "claude",  "default_version": "claude-4"},
    "gemini":  {"family": "gemini",  "default_version": "2.5-pro"},
    "pentagi": {"family": "qwen",    "default_version": "2.5-32b"},
    "ollama":  {"family": "ollama",  "default_version": "unknown"},
    "grok":    {"family": "grok",    "default_version": "grok-composer-2.5"},
    "codex":   {"family": "codex",   "default_version": "unknown"},
}
FORMAT_BY_AGENT: dict = {
    **_DEFAULT_FORMAT_BY_AGENT,
    **_gaius_cfg.get("principals", {}).get("formats", {}),
}

# Agent name → named principal mapping for governor layer.
# Principals group agents by model family and role for threshold tuning,
# corpus weighting, and session tracking.
#
# Configurable via principals.mapping in ~/.gaius/config.yaml.
# Unknown agents fall back to the default principal ("operator" unless overridden).
# Example config:
#   principals:
#     default: operator
#     mapping:
#       my-claude-agent: operator
#       my-gemini-agent: researcher
# Gap 48: peer coding agents (Grok CLI / Codex CLI) are self-principals so their
# facts do not collapse into the shared "operator" bucket by default. Claude and
# Gemini stay on the operator default unless config overrides.
_DEFAULT_PRINCIPAL_BY_AGENT: dict = {
    "grok": "grok",
    "codex": "codex",
}
PRINCIPAL_BY_AGENT: dict = {
    **_DEFAULT_PRINCIPAL_BY_AGENT,
    **_gaius_cfg.get("principals", {}).get("mapping", {}),
}


_DEFAULT_PRINCIPAL = _gaius_cfg.get("principals", {}).get("default", "operator")

def agent_to_principal(agent: str) -> str:
    """Map raw agent name to a principal group. Unknown agents use default_principal from config."""
    return PRINCIPAL_BY_AGENT.get(agent, _DEFAULT_PRINCIPAL)

# ── Step 7: agent-type aware size thresholds ──────────────────────────────────
# Grounded in corpus audit (500 local + 13 cluster sessions).
# Median ~70KB local, ~180KB cluster; p90 ~1.5MB; max ~13.4MB.
_DEFAULT_AGENT_THRESHOLDS: dict = {
    # Research agents — long multi-hop sessions, high signal density
    "researcher": 10 * 1024 * 1024,  # 10MB
    # Task agents — shorter focused sessions, compact more aggressively
    "dev":        2 * 1024 * 1024,   # 2MB
    "qa":         2 * 1024 * 1024,
}
# Configurable via principals.thresholds in ~/.gaius/config.yaml.
# Values in bytes; user entries merged on top of defaults.
AGENT_THRESHOLDS: dict = {
    **_DEFAULT_AGENT_THRESHOLDS,
    **_gaius_cfg.get("principals", {}).get("thresholds", {}),
}
_thresholds_cfg = _gaius_cfg.get("principals", {})
LOCAL_THRESHOLD   = _thresholds_cfg.get("local_threshold",   5 * 1024 * 1024)
DEFAULT_THRESHOLD = _thresholds_cfg.get("default_threshold", 2 * 1024 * 1024)


def get_session_threshold(origin: str, agent_name: str) -> int:
    """Return minimum session size in bytes to include in corpus.

    Returns 0 (no filter) for unknown origins.  Strips '-agent' suffix so
    'my-agent' maps to the same threshold as 'my'.
    """
    if origin == "local":
        return LOCAL_THRESHOLD
    base = agent_name.replace("-agent", "").lower()
    return AGENT_THRESHOLDS.get(base, DEFAULT_THRESHOLD)


# Training readiness thresholds per domain.
# A domain is "ready" if score >= threshold AND facts >= min_facts.
#
# Only universal defaults live here. Project-specific domains are either:
#   (a) defined in readiness_thresholds in ~/.gaius/config.yaml, or
#   (b) auto-discovered from domain/*.md files and assigned DEFAULT_READINESS.
# Users never need to register domains — just create domain/<name>.md.
_DEFAULT_READINESS_THRESHOLDS: dict = {
    "quality":  {"score": 0.70, "min_facts": 100},
    "security": {"score": 0.70, "min_facts": 50},
}
DEFAULT_READINESS = _gaius_cfg.get("default_readiness", {"score": 0.60, "min_facts": 50})

# Minimum priority (score/token) for corpus facts injection.
# Facts below this threshold are noise — BM25 residuals with no real signal.
# Default 0.0 (backward compat). Set inject_min_priority: 0.05 in config to filter noise.
INJECT_MIN_PRIORITY: float = _gaius_cfg.get("inject_min_priority", 0.04)

# Explicit overrides from config (highest priority)
_cfg_readiness: dict = _gaius_cfg.get("readiness_thresholds", {})

# Domain directory — resolved now so auto-discovery can run at startup
DOMAIN_DIR = Path(
    os.environ.get("GAIUS_DOMAIN_DIR")
    or _gaius_cfg.get("domain_dir", Path.home() / ".gaius" / "memory" / "domain")
).expanduser()

def _discover_domain_thresholds(domain_dir: Path) -> dict:
    """Auto-populate thresholds for every domain/*.md file not already configured."""
    if not domain_dir.is_dir():
        return {}
    return {
        p.stem: DEFAULT_READINESS
        for p in domain_dir.glob("*.md")
        if p.stem not in _DEFAULT_READINESS_THRESHOLDS and p.stem not in _cfg_readiness
    }

# Final merged table: defaults < auto-discovered < explicit config
READINESS_THRESHOLDS: dict = {
    **_DEFAULT_READINESS_THRESHOLDS,
    **_discover_domain_thresholds(DOMAIN_DIR),
    **_cfg_readiness,
}

# SOP directory — Standard Operating Procedures
SOP_DIR = Path(__file__).resolve().parent.parent / "sop"
if os.environ.get("GAIUS_SOP_DIR"):
    SOP_DIR = Path(os.environ["GAIUS_SOP_DIR"])

# Skills directory — prospective how-to guides (Claude Code native skill files)
# Defaults to sibling of DOMAIN_DIR (i.e., memory_root/skills)
SKILLS_DIR = Path(
    os.environ.get("GAIUS_SKILLS_DIR")
    or _gaius_cfg.get("skills_dir", DOMAIN_DIR.parent / "skills")
).expanduser()


# ── Helpers ───────────────────────────────────────────────────────────────────







# Domains that have loadable context files (subset of DOMAIN_KEYWORDS).
# Maps domain name → filename in the domain context directory.
ROUTABLE_DOMAINS = {
    "networking", "security", "storage", "services", "observability", "gitops",
}


def route_domains(query: str, primary_hint: str = None,
                  max_files: int = 3, max_chars: int = 10000,
                  primary_budget: int = 4000) -> list[dict]:
    """Route a query to the most relevant domain files.

    Keyword-based bootstrap router. Scores each domain by counting keyword
    hits in the query, optionally boosted by a primary hint (e.g. the question's
    domain tag). Returns the top domains with char budget allocations.

    Args:
        query: The question or prompt text to route.
        primary_hint: Domain name to prioritize (e.g. from question metadata).
        max_files: Maximum number of domain files to return.
        max_chars: Total character budget across all files.
        primary_budget: Max chars allocated to the primary (highest-scoring) file.

    Returns:
        List of dicts: [{"domain": str, "score": float, "budget": int}, ...]
        Ordered by score descending. Budget sums to <= max_chars.
    """
    query_lower = query.lower()
    # Split on word boundaries for whole-word matching on short keywords
    query_words = set(re.findall(r'[a-z0-9][-a-z0-9_.]*', query_lower))

    scores: dict[str, float] = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        if domain not in ROUTABLE_DOMAINS:
            continue
        hits = 0
        for kw in keywords:
            # Substring match for multi-word/hyphenated keywords,
            # word match for short ones to avoid false positives
            if len(kw) <= 3:
                if kw in query_words:
                    hits += 1
            else:
                if kw in query_lower:
                    hits += 1
        if hits > 0:
            # Normalize by keyword count to avoid bias toward domains with more keywords
            scores[domain] = hits / len(keywords)

    # Boost primary hint
    if primary_hint and primary_hint in ROUTABLE_DOMAINS:
        scores.setdefault(primary_hint, 0)
        scores[primary_hint] += 0.3  # hint boost

    if not scores:
        # No keyword matches — fall back to primary hint only
        if primary_hint and primary_hint in ROUTABLE_DOMAINS:
            return [{"domain": primary_hint, "score": 0.3, "budget": min(primary_budget, max_chars)}]
        return []

    # Sort by score descending, take top max_files
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:max_files]

    # Allocate budgets: primary gets up to primary_budget, rest split evenly
    results = []
    remaining = max_chars
    for i, (domain, score) in enumerate(ranked):
        if i == 0:
            budget = min(primary_budget, remaining)
        else:
            # Split remaining budget evenly among secondaries
            secondaries_left = len(ranked) - i
            budget = min(remaining // secondaries_left, primary_budget)
        remaining -= budget
        results.append({"domain": domain, "score": round(score, 3), "budget": budget})

    return results








# ── Knowledge Graph: Entity Extraction & Triple Management ───────────────────

# Built-in entity patterns — generic K8s / infrastructure baseline.
# Extend or replace via entities.patterns in ~/.gaius/config.yaml.
# Set entities.preset: none to disable built-in patterns entirely.
# ── Knowledge Graph (entities/relations/triples) ────────────────────────────
# Extracted to gaius/kg.py (2026-06-28); re-imported at the bottom of this file
# so `from gaius._core import kg_index_fact` (etc.) keeps working. See gaius/kg.py.




# ── Commands ──────────────────────────────────────────────────────────────────

def content_hash(content: str) -> str:
    """SHA-256 hex digest of compaction content for change detection."""
    return hashlib.sha256(content.encode()).hexdigest()


# ── Landscape Protocol → extracted to gaius/landscape.py (facade re-import below) ──
def cmd_migrate(args):
    """Migrate agent memory: corpus, S3 paths, and domain attribution."""
    if len(args) < 2:
        print("Usage: gaius migrate <old_name> <new_name>", file=sys.stderr)
        sys.exit(1)

    old_name = args[0]
    new_name = args[1]
    repo_root = Path(__file__).resolve().parent.parent

    print(f"🚀 Migrating agent memory: {old_name} → {new_name}")

    # 1. Rename directories (e.g. old-agent/ -> new_name/)
    old_dir = repo_root / old_name
    new_dir = repo_root / new_name
    if old_dir.is_dir() and old_name != "domain":
        print(f"  📁 Renaming directory {old_dir.relative_to(repo_root)} → {new_dir.name}")
        old_dir.rename(new_dir)

    # 2. Rename files in domain/
    old_domain_file = repo_root / "domain" / f"{old_name}.md"
    new_domain_file = repo_root / "domain" / f"{new_name}.md"
    if old_domain_file.exists():
        print(f"  📄 Renaming domain file domain/{old_domain_file.name} → {new_domain_file.name}")
        old_domain_file.rename(new_domain_file)

    # 3. Update occurrences in all files
    updated_files = 0
    # Include both lowercase and capitalized versions
    patterns = [
        (old_name, new_name),
        (old_name.capitalize(), new_name.capitalize())
    ]

    for path in repo_root.rglob("*"):
        if path.is_dir() or ".git" in path.parts:
            continue
        # Only touch text files
        if path.suffix not in [".md", ".json", ".jsonl", ".yml", ".yaml", ".sh", ""]:
            if path.name not in ["gaius", "mnemosyne"]:
                continue

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

            new_content = content
            for old, new in patterns:
                new_content = new_content.replace(old, new)

            if new_content != content:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"  ✍️  Updated {path.relative_to(repo_root)}")
                updated_files += 1
        except Exception as e:
            # Skip binary files or permission issues
            pass

    print(f"\n✅ Migration complete. {updated_files} files updated.")
    print("\nS3 Manual Steps (if using s3-retire):")
    print(f"  rclone moveto <remote>:sessions/cluster/{old_name}/ <remote>:sessions/cluster/{new_name}/")




# ── Step 6: maturity scoring → extracted to gaius/maturity.py (facade re-import below) ──
# ── RAFT Sidecar Extraction → extracted to gaius/raft.py (facade re-import below) ──
# ── Claude Code command stubs ─────────────────────────────────────────────────

# Modern format: ~/.claude/skills/<name>/SKILL.md (v2.1+)
# Legacy format: ~/.claude/commands/<name>.md (kept for backwards compat)
CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"
CLAUDE_COMMANDS_DIR = Path.home() / ".claude" / "commands"

# Frontmatter keys the wired copy must NOT carry. `paths:` makes Claude Code treat
# the skill as file-glob-conditional, so it drops out of the always-available Skill
# list (verified 2026-07-25). The source keeps `paths:` — gaius injection reads it.
# ── Dispatch ──────────────────────────────────────────────────────────────────

def cmd_init(args):
    """Guided first-run setup: create ~/.gaius/config.yaml and memory directories."""
    import shutil

    parser = argparse.ArgumentParser(prog="gaius init")
    parser.add_argument("--backend", type=str, default=None,
                        choices=["claude", "gemini", "vllm"],
                        help="Agent backend to configure (skips the interactive backend prompt)")
    parser.add_argument("--yes", action="store_true",
                        help="Accept the default answer for every prompt (non-interactive setup)")
    parsed = parser.parse_args(args)

    def ask(prompt, default=""):
        """input() wrapper: --yes takes the default; closed stdin exits cleanly."""
        if parsed.yes:
            return default
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\ngaius init: no interactive input available — "
                  "use `gaius init --backend <name> --yes` for non-interactive setup.")
            sys.exit(1)

    gaius_dir = Path.home() / ".gaius"
    config_path = gaius_dir / "config.yaml"

    print("gaius init — first-run setup")
    print("─" * 40)

    # Check for existing config
    if config_path.exists():
        ans = ask(f"\nConfig already exists at {config_path}\nOverwrite? [y/N] ").lower()
        if ans != "y":
            print("Aborted.")
            return

    # Choose backend
    if parsed.backend:
        backend = parsed.backend
    else:
        if not parsed.yes:
            print("\nChoose your AI coding agent backend:")
            print("  1) claude   — Claude Code (JSONL sessions in ~/.claude/projects/)")
            print("  2) gemini   — Gemini CLI (JSON sessions in ~/.gemini/tmp/)")
            print("  3) vllm     — vLLM-served models (Gemma, Nemotron, etc. — requires chat TUI that writes JSONL)")
        backend_choice = ask("\nBackend [1]: ") or "1"
        backend_map = {"1": "claude", "2": "gemini", "3": "vllm",
                       "claude": "claude", "gemini": "gemini", "vllm": "vllm"}
        backend = backend_map.get(backend_choice, "claude")

    # Choose preset
    if not parsed.yes:
        print("\nChoose a starting preset:")
        print("  1) default  — minimal, any software project (no K8s patterns)")
        print("  2) k8s      — Kubernetes cluster ops (includes service/namespace/incident patterns)")
    preset_choice = ask("\nPreset [1]: ") or "1"
    preset_name = "k8s" if preset_choice == "2" else "default"

    # Find preset file
    _script_dir = Path(__file__).parent  # gaius package dir (ships presets/ + skill/)
    preset_src = _script_dir / "presets" / f"{preset_name}.yaml"
    if not preset_src.exists():
        # Fallback: un-migrated repo checkout (presets/ still at repo root)
        preset_src = Path(__file__).parent.parent / "presets" / f"{preset_name}.yaml"
    if not preset_src.exists():
        print(f"ERROR: preset file not found: {preset_src}")
        print("Run from the gaius repo directory or install with pip install gaius-memory.")
        return

    # Sessions dir (backend-specific default)
    sessions_defaults = {
        "claude": str(Path.home() / ".claude" / "projects"),
        "gemini": str(Path.home() / ".gemini" / "tmp"),
        "vllm":   str(Path.home() / ".gaius" / "sessions"),
    }
    default_sessions = sessions_defaults[backend]
    sessions_input = ask(f"\nSessions directory [{default_sessions}]: ")
    sessions_dir = sessions_input or default_sessions

    # Memory directory
    default_memory = str(Path.home() / ".gaius" / "memory")
    memory_input = ask(f"Memory directory (domain/*.md files) [{default_memory}]: ")
    memory_dir = memory_input or default_memory

    # Create dirs
    gaius_dir.mkdir(parents=True, exist_ok=True)
    Path(memory_dir).mkdir(parents=True, exist_ok=True)
    (Path(memory_dir) / "domain").mkdir(parents=True, exist_ok=True)

    # Write config
    shutil.copy(preset_src, config_path)

    # Patch sessions_dir and domain_dir into the config
    with open(config_path) as f:
        content = f.read()

    # Uncomment/set sessions_dir
    import re as _re
    content = _re.sub(
        r'^#?\s*sessions_dir:.*$',
        f'sessions_dir: {sessions_dir}',
        content, flags=_re.MULTILINE
    )
    # Uncomment/set domain_dir
    content = _re.sub(
        r'^#\s*domain_dir:.*$',
        f'domain_dir: {memory_dir}/domain',
        content, flags=_re.MULTILINE
    )

    with open(config_path, "w") as f:
        f.write(content)

    # Install /gaius skill (Claude Code only — Gemini uses system prompt)
    skill_installed = False
    skill_src = _script_dir / "skill" / "SKILL.md"
    if backend == "claude" and skill_src.exists():
        skill_dest = Path.home() / ".claude" / "skills" / "gaius"
        skill_dest.mkdir(parents=True, exist_ok=True)
        shutil.copy(skill_src, skill_dest / "SKILL.md")
        skill_installed = True
    elif backend == "gemini" and skill_src.exists():
        # Generate a system prompt file from SKILL.md for Gemini
        gemini_prompt = Path.home() / ".gaius" / "skill-prompt.md"
        shutil.copy(skill_src, gemini_prompt)
        print(f"✓  Gemini system prompt: {gemini_prompt}")
        print("   Add to .gemini/config.yaml: system_prompt_file: ~/.gaius/skill-prompt.md")

    # Write backend to config
    with open(config_path) as f:
        content = f.read()
    if "backend:" not in content:
        content = f"backend: {backend}\n" + content
        with open(config_path, "w") as f:
            f.write(content)

    print(f"\n✓  Config written to {config_path}")
    print(f"✓  Memory dir: {memory_dir}/domain/")
    if skill_installed:
        print(f"✓  Skill installed: ~/.claude/skills/gaius/SKILL.md")
        print(f"   Use /gaius in Claude Code to enter memory maintenance mode")
    if backend == "vllm":
        print(f"✓  Sessions dir: {sessions_dir}")
        print(f"   Your chat TUI must write JSONL here. See: gaius schema --format session")
    print()
    print("Next steps:")
    print(f"  gaius retire              # scan sessions → stage summaries")
    print(f"  gaius stats               # show corpus statistics")
    print(f"  gaius batch               # review staged summaries")
    print(f"  gaius scaffold-skill      # have your agent build a memory-maintenance skill for this system")
    print()
    if backend == "claude":
        print("To add the MCP server to Claude Code:")
        print("  claude mcp add gaius -- python3 -m gaius.mcp_server")
        print()
    print(f"Edit {config_path} to customize entity patterns and principal mappings.")


def cmd_sync_memory(args):
    """Write top facts from facts.db into Claude Code auto-memory reference file.

    Creates a read-only cache of high-value facts, grouped by domain, for passive
    loading by Claude Code's native auto-memory system.

    Output: <MEMORY_DIR>/reference/corpus-highlights.md
    Hard cap: 180 lines (under mnemosyne RED threshold of 200).
    """
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=100,
                   help="Max facts to consider per domain")
    p.add_argument("--max-lines", type=int, default=180,
                   help="Hard line cap for output file")
    p.add_argument("--dry-run", action="store_true",
                   help="Print to stdout instead of writing file")
    opts = p.parse_args(args)

    conn = init_db()

    # Query top facts: active, non-tombstoned, sorted by composite score
    # Composite: base score * (1 + 0.1 * confirmation_count) * recency_boost
    facts = conn.execute("""
        SELECT domain, fact_text, score, confirmation_count, last_seen, first_seen
        FROM facts
        WHERE tombstoned_at IS NULL AND (outcome IS NULL OR outcome != 'rejected')
          AND score > 0.15
        ORDER BY score * (1.0 + 0.1 * COALESCE(confirmation_count, 0)) DESC
        LIMIT ?
    """, (opts.limit * 10,)).fetchall()

    if not facts:
        print("No qualifying facts in facts.db")
        return

    # Group by domain, take top N per domain
    by_domain = {}
    for f in facts:
        d = f[0] or "general"
        if d not in by_domain:
            by_domain[d] = []
        if len(by_domain[d]) < opts.limit // max(len(set(r[0] for r in facts)), 1) + 5:
            by_domain[d].append(f)

    # Build output
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Corpus Highlights",
        f"",
        f"> Auto-generated by `gaius sync-memory` on {now}. Do not edit manually.",
        f"> Source: facts.db ({len(facts)} qualifying facts). Regenerated nightly.",
        "",
    ]

    for domain in sorted(by_domain.keys()):
        domain_facts = by_domain[domain]
        lines.append(f"## {domain}")
        lines.append("")
        for f in domain_facts:
            text = (f[1] or "").strip().replace("\n", " ")[:200]
            conf = f[3] or 0
            corr = f" [x{conf}]" if conf > 1 else ""
            lines.append(f"- {text}{corr}")
            if len(lines) >= opts.max_lines - 2:
                lines.append(f"\n_Truncated at {opts.max_lines} lines._")
                break
        lines.append("")
        if len(lines) >= opts.max_lines - 2:
            break

    output = "\n".join(lines[:opts.max_lines])

    if opts.dry_run:
        print(output)
        print(f"\n--- {len(lines)} lines ({opts.max_lines} max) ---")
        return

    # Write to Claude Code auto-memory directory
    memory_dir = (MEMORY_DIR / "reference") if MEMORY_DIR else (Path.home() / ".gaius" / "reference")
    memory_dir.mkdir(parents=True, exist_ok=True)
    out_path = memory_dir / "corpus-highlights.md"
    out_path.write_text(output)
    print(f"Wrote {len(lines)} lines to {out_path}")


def cmd_record(args):
    """Capture AI chat sessions into gaius-compatible JSONL."""
    from gaius.record import main as record_main
    record_main(args)


# ═════════════════════════════════════════════════════════════════════════════
# FACADE RE-EXPORTS (extracted modules) — see gaius/ARCHITECTURE.md § facade convention
# ═════════════════════════════════════════════════════════════════════════════
# Each extracted module imports shared helpers from gaius._core at ITS top; these
# re-imports run at module END (after _core's own definitions) so the extracted
# modules can import back without a circular-import error, and so every existing
# `from gaius._core import X` and the COMMANDS dict below keep resolving unchanged.
# ORDER MATTERS: leaf modules first — raft before landscape (landscape imports the
# _parse_frontmatter that raft owns and _core re-exports here). This whole block
# MUST precede the COMMANDS dict, which references the re-exported cmd_* by name.
#
# ── Phase-A split modules (2026-08-13) ───────────────────────────────────────
# These re-imports run FIRST inside the block: older extracted modules below
# (parsers, kg, maturity, …) top-level `from gaius._core import` symbols whose
# implementation moved into the Phase-A modules, so those names must be bound
# back into _core before the older modules' own re-import lines execute.
# Phase-A modules import moved symbols from their new homes directly, never
# via _core; runtime-only deps go through lazy in-body `from gaius._core import`.
from gaius.embed import (  # noqa: E402,F401  re-export (embed split 2026-08-13)
    _get_embed_model, _embed_via_daemon, _embed_text, _embed_texts,
    _chunk_text, _EMBED_DIM, _EMBED_DAEMON_SOCK, cmd_embed,
)
from gaius.extract import (  # noqa: E402,F401  re-export (extract split 2026-08-13)
    SECRET_KEYS_RE, GEMINI_NOISE_SUBJECTS, GEMINI_CREDENTIAL_PATTERNS,
    CREDENTIAL_PATTERNS, DECISION_KEYWORDS,
    FINDING_PATTERNS, FINDING_BASE_SCORE,
    PROCEDURE_INDICATORS, PROCEDURE_FAILURE_INDICATORS, PROCEDURE_MIN_STEPS,
    PROCEDURE_BASE_SCORE, PROCEDURE_INCOMPLETE_SCORE,
    SECTION_HEADERS, SIGNAL_SECTIONS, _DOMAIN_KEYWORDS_DEFAULT, DOMAIN_KEYWORDS,
    extract_section, has_signal, count_domain_hits, classify_entry, boost_score,
    classify_finding, extract_procedure, classify_procedure, sample_entry,
    tag_domains, extract_delta_lines, load_domain_specs, is_gemini_cold,
    tag_domains_from_specs,
    _BASE64_PREFIX_RE, _HTML_START_RE, _HTML_BODY_RE, _ERROR_KEYWORDS,
    _is_html, _is_binary, _strip_string_content, _strip_content_block,
    _strip_tool_result, strip_bloat,
    NOISE_PATTERNS, _NARRATION_ANCHOR_CHARS, _NARRATION_CLAUSE,
    _REVIEWER_VERDICT, _REVIEWER_VERDICT_OPENER, _REVIEWER_VERDICT_TERM,
    _is_noise, _SEEDED_SCORE_PATTERNS, _seeded_score, _extract_clarified_intent,
)
from gaius.scoring import (  # noqa: E402,F401  re-export (scoring split 2026-08-13)
    DECAY_HALF_LIFE, CROSS_AGENT_MULTIPLIER, BOOTSTRAP_THRESHOLD, DOMAIN_STATS_FILE,
    extract_quoted_phrases, quoted_phrase_boost, infra_entity_boost,
    compute_tfidf, decay_factor, estimate_tokens, compute_entry_tfidf_score,
    build_doc_freq, bm25_score, _build_bm25_doc_freq,
    load_domain_stats, save_domain_stats, update_domain_stats,
)
from gaius.facts import (  # noqa: E402,F401  re-export (facts split 2026-08-13)
    init_db, _dedup_live_fact_keys, register_session,
    _find_semantic_duplicate, _store_embedding,
    _HEDGE_PATTERNS, _OBSERVED_PATTERNS, _HARDWARE_STATE_DOMAINS,
    _score_confidence, _CONTRADICTION_STOP_WORDS, _OPERATIONAL_NEGATION,
    _check_contradiction, upsert_fact, _upsert_distillations,
)
from gaius.review import (  # noqa: E402,F401  re-export (review split 2026-08-13)
    load_staged, _STATE_CHANGE_RE, save_staged, display_uid,
    cmd_show, _promote_event, _rewrite_staging,
    cmd_next_staged_facts, cmd_next_gemini, _cmd_next_pending_fact,
    cmd_next, cmd_done, _resolve_fact_id,
    cmd_confirm, cmd_reject, cmd_defer, cmd_agent_review,
    cmd_stats, cmd_rescan, cmd_batch,
)
from gaius.retire import (  # noqa: E402,F401  re-export (retire split 2026-08-13)
    MINE_MIN_BYTES, MINE_REGROW_FACTOR, MINE_MIN_TEXT_LEN,
    MINE_SCORE_THRESHOLD, MINE_MAX_BLOCKS_PER_SECTION,
    _mine_session, _mine_uncompacted_sessions, _promote_mined_to_facts,
    cmd_retire, _scan_extra_sessions, cmd_s3_retire,
    cmd_index, process_session, write_domain_deltas, write_procedure_deltas,
    archive_session, cmd_harvest,
    _retire_event_sessions, cmd_pentagi_retire, cmd_ollama_retire,
    cmd_grok_retire, cmd_codex_retire,
)
from gaius.skills import (  # noqa: E402,F401  re-export (skills split 2026-08-13)
    SKILL_STALE_DAYS, SKILL_PATH_WEIGHT, get_skill_git_date, load_skills,
    _skill_path_match, _glob_specificity, compute_skill_score, cmd_skills,
    STUB_STRIP_FM_KEYS, _strip_frontmatter_key, _stub_content, cmd_commands,
    cmd_scaffold_skill,
    _SUGGEST_DISMISSED_PATH, _load_dismissed, _save_dismissed,
    cmd_suggest, _generate_skill_draft,
)
from gaius.drift import (  # noqa: E402,F401  re-export (drift split 2026-08-13)
    _drift_live, cmd_drift,
)
from gaius.ingest import (  # noqa: E402,F401  re-export (ingest split 2026-08-13)
    cmd_ansible, cmd_aliases,
)
#
# ── Session-format adapters (extracted to gaius/parsers.py) ──────────────────
from gaius.parsers import (  # noqa: E402
    detect_format,
    parse_claude_events,
    parse_gemini_events,
    parse_pentagi_flow,
    parse_pentagi_flow_from_jsonl,
    parse_ollama_events,
    _content_blocks_to_text,
    _grok_operator_query,
    parse_grok_events,
    parse_codex_events,
    _discover_grok_sessions,
    _discover_codex_sessions,
    PEER_AGENT_MIN_RESPONSE,
    _CODEX_CONTEXT_MARKERS,
)
from gaius.kg import (  # noqa: E402,F401  re-export (kg split 2026-06-28)
    _BUILTIN_ENTITY_PATTERNS, _load_entity_patterns, _ENTITY_PATTERNS,
    _RELATION_PATTERNS, extract_entities, extract_relations, upsert_entity,
    add_triple, invalidate_triple, kg_index_fact, cmd_kg,
    add_cooccurrence, refresh_entity_domains, cmd_kg_export_links,
)

from gaius.raft import (  # noqa: E402,F401  re-export (raft split 2026-07-01)
    _parse_frontmatter, _FAILURE_CLASS_MAP, _DOMAIN_MAP,
    _FAILURE_CLASS_MAP_DEFAULT, _DOMAIN_MAP_DEFAULT, cmd_raft,
)


# --- Phase 1b: production-outcome ingestion → extracted to gaius/outcomes.py (facade re-import below) ---
# --- Phase 2/3: corpus integrity + router → extracted to gaius/corpus_audit.py (facade re-import below) ---
# ── Source-of-truth reconciler → extracted to gaius/reconcile.py (facade re-import below) ──

from gaius.maturity import (  # noqa: E402,F401  re-export (maturity split 2026-07-01)
    _maturity_score, PROVENANCE_WEIGHT, NO_DECAY_PROVENANCES, OUTCOME_MODIFIER,
    MATURITY_BOOTSTRAP_MIN, CROSS_MODEL_MULTIPLIER, SOURCE_RELIABILITY,
    REVIEW_STATE_WEIGHT,
    cmd_maturity, cmd_readiness, cmd_snapshot, cmd_governor, cmd_route,
    volatility_recency, cmd_decay, cmd_rescore,  # moved home 2026-08-13 (Phase A)
)

from gaius.outcomes import (  # noqa: E402,F401  re-export (outcomes split 2026-07-01)
    _ensure_outcomes_table, ingest_outcomes, outcome_winrates, cmd_ingest_outcomes,
)

from gaius.degradation import (  # noqa: E402,F401  re-export (degradation split 2026-07-24)
    detect_events, store as store_degradation, report as degradation_report,
    band_for, cmd_degradation,
)

from gaius.corpus_audit import (  # noqa: E402,F401  re-export (corpus_audit split 2026-07-01)
    REPETITION_THRESHOLD, CONTRADICTION_ENFORCE_MIN_CC, repetition_candidates,
    corpus_audit_stats, enforce_demote, route_suggest, cmd_corpus_audit, cmd_route_suggest,
)

from gaius.reconcile import (  # noqa: E402,F401  re-export (reconcile split 2026-07-01)
    load_source_registry, source_divergence, reconcile_source,
    _reconcile_excluded, _dir_fingerprint, remote_divergence, cmd_reconcile,
)

from gaius.landscape import (  # noqa: E402,F401  re-export (landscape split 2026-07-01)
    cmd_landscape,
)
from gaius.landscape import cmd_inject as _landscape_cmd_inject  # noqa: E402

# Default --budget for `gaius inject` when the flag is omitted: the documented
# per-session corpus budget ("2000 corpus + 1500 skills", README) — also the
# session-start hook default (hooks/session-start.sh, GAIUS_INJECT_BUDGET).
DEFAULT_INJECT_BUDGET = 2000


def cmd_inject(args):
    """Inject ranked corpus entries into context (gaius.landscape.cmd_inject).

    Thin wrapper supplying the standard --budget default so plain
    `gaius inject --task ...` works without an explicit budget. The default is
    PREPENDED, so an explicit --budget anywhere in args still wins (argparse
    keeps the last value for a repeated flag).
    """
    args = list(args)
    if not any(a == "--budget" or a.startswith("--budget=") for a in args):
        args = ["--budget", str(DEFAULT_INJECT_BUDGET)] + args
    return _landscape_cmd_inject(args)

from gaius.recentstate import (  # noqa: E402,F401  re-export (Recent State auto-roll 2026-07-21)
    cmd_recent_roll, roll_recent_state, should_evict,
)

from gaius.concord import (  # noqa: E402,F401  re-export (cross-session coordination, 2026-07-17 — OSS-included)
    cmd_concord, init_concord,
)


def cmd_completion(args):
    """Emit a shell completion script for gaius to stdout.

    Completes the top-level command names (generated from the live COMMANDS
    registry, so OSS/publish-stripped builds get the right subset automatically)
    and the global flags. Pure and offline: no DB access, no session scanning.

    Install:
      gaius completion bash >> ~/.bashrc
      gaius completion zsh  >> ~/.zshrc
      gaius completion fish >  ~/.config/fish/completions/gaius.fish
    """
    parser = argparse.ArgumentParser(
        prog="gaius completion",
        description="Print a shell completion script to stdout.",
        epilog=(
            "Install:\n"
            "  gaius completion bash >> ~/.bashrc\n"
            "  gaius completion zsh  >> ~/.zshrc\n"
            "  gaius completion fish >  ~/.config/fish/completions/gaius.fish"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "shell", choices=["bash", "zsh", "fish"],
        help="Shell to emit completion for (bash, zsh, or fish)",
    )
    parsed = parser.parse_args(args)

    # Command list tracks the live registry (auto-reflects OSS-stripped subsets).
    cmd_words = " ".join(sorted(COMMANDS.keys()))
    fmt_words = " ".join(sorted(SUPPORTED_FORMATS))
    global_flags_meta = [
        ("--sessions-dir", "Override session JSONL directory"),
        ("--staging-dir", "Override staging output directory"),
        ("--extra-sessions-dir", "Additional session JSONL directory to scan"),
        ("--format", "Session format"),
    ]
    global_flags = " ".join(flag for flag, _ in global_flags_meta)

    if parsed.shell == "bash":
        script = f'''# gaius bash completion
# Install: gaius completion bash >> ~/.bashrc
_gaius_complete() {{
    local cur cmds globals
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    cmds="{cmd_words}"
    globals="{global_flags}"
    COMPREPLY=( $(compgen -W "${{cmds}} ${{globals}}" -- "${{cur}}") )
}}
complete -F _gaius_complete gaius
'''
        sys.stdout.write(script)
        return

    if parsed.shell == "zsh":
        script = r'''#compdef gaius
# gaius zsh completion
# Install: gaius completion zsh >> ~/.zshrc  (or drop on a dir in $fpath)
_gaius() {
    local -a _gaius_commands _gaius_globals
    _gaius_commands=(__CMDS__)
    _gaius_globals=(
        '--sessions-dir[Override session JSONL directory]:dir:_files -/'
        '--staging-dir[Override staging output directory]:dir:_files -/'
        '--extra-sessions-dir[Additional session JSONL directory to scan]:dir:_files -/'
        '--format[Session format]:format:(__FMTS__)'
    )
    _arguments -C \
        "${_gaius_globals[@]}" \
        '1:command:->command' \
        '*::args:->args'
    case "$state" in
        command) _describe -t commands 'gaius command' _gaius_commands ;;
    esac
}
compdef _gaius gaius
'''
        script = script.replace("__CMDS__", cmd_words).replace("__FMTS__", fmt_words)
        sys.stdout.write(script)
        return

    if parsed.shell == "fish":
        lines = [
            "# gaius fish completion",
            "# Install: gaius completion fish > ~/.config/fish/completions/gaius.fish",
            "complete -c gaius -f",
            f"complete -c gaius -n '__fish_use_subcommand' -a '{cmd_words}' -d 'gaius command'",
        ]
        for flag, desc in global_flags_meta:
            lines.append(f"complete -c gaius -l {flag.lstrip('-')} -d '{desc}'")
        sys.stdout.write("\n".join(lines) + "\n")
        return


COMMANDS = {
    "init":       cmd_init,
    "scaffold-skill": cmd_scaffold_skill,
    "retire":     cmd_retire,
    "s3-retire":  cmd_s3_retire,
    "harvest":    cmd_harvest,
    "ansible":    cmd_ansible,
    "aliases":    cmd_aliases,
    "inject":     cmd_inject,
    "show":       cmd_show,
    "next":       cmd_next,
    "done":       cmd_done,
    "confirm":    cmd_confirm,
    "reject":     cmd_reject,
    "defer":      cmd_defer,
    "agent-review": cmd_agent_review,
    "rescan":     cmd_rescan,
    "stats":      cmd_stats,
    "batch":      cmd_batch,
    "migrate":    cmd_migrate,
    "index":      cmd_index,
    "maturity":   cmd_maturity,
    "readiness":  cmd_readiness,
    "snapshot":   cmd_snapshot,

    "governor":        cmd_governor,
    "route":           cmd_route,
    "raft":            cmd_raft,
    "pentagi-retire":  cmd_pentagi_retire,
    "ollama-retire":   cmd_ollama_retire,
    "grok-retire":     cmd_grok_retire,
    "codex-retire":    cmd_codex_retire,
    "skills":          cmd_skills,
    "commands":        cmd_commands,
    "landscape":       cmd_landscape,
    "recent-roll":     cmd_recent_roll,
    "embed":           cmd_embed,
    "kg":              cmd_kg,
    "decay":           cmd_decay,
    "sync-memory":     cmd_sync_memory,
    "suggest":         cmd_suggest,
    "drift":           cmd_drift,
    "record":          cmd_record,
    "rescore":         cmd_rescore,
    "ingest-outcomes": cmd_ingest_outcomes,
    "degradation":     cmd_degradation,
    "corpus-audit":    cmd_corpus_audit,
    "route-suggest":   cmd_route_suggest,
    "reconcile":       cmd_reconcile,
    "concord":         cmd_concord,
    "completion":      cmd_completion,
}


SUPPORTED_FORMATS = {"claude", "gemini", "ollama", "vllm", "pentagi", "grok", "codex"}


def main():
    global PROJECT_DIR, STAGING_DIR, EXTRA_SESSIONS_DIR

    # Split argv: find the command name, everything after it is passed to the subcommand
    argv = sys.argv[1:]
    cmd_names = set(COMMANDS.keys())

    # Find where the command name is in argv
    cmd_index = None
    for i, arg in enumerate(argv):
        if arg in cmd_names:
            cmd_index = i
            break

    if cmd_index is None:
        # No command found — let argparse handle the error/help
        parser = argparse.ArgumentParser(
            description="Session memory lifecycle manager",
            usage="gaius [--sessions-dir DIR] [--staging-dir DIR] [--format FMT] <command> [args]",
        )
        parser.add_argument("--sessions-dir", type=str, default=None)
        parser.add_argument("--staging-dir", type=str, default=None)
        parser.add_argument("--format", type=str, default="claude", choices=SUPPORTED_FORMATS)
        parser.add_argument("command", choices=list(COMMANDS.keys()), help="Command to run")
        parser.parse_args(argv)
        return

    # Parse only the global flags (everything before the command)
    global_argv = argv[:cmd_index]
    command = argv[cmd_index]
    cmd_argv = argv[cmd_index + 1:]

    parser = argparse.ArgumentParser(
        description="Session memory lifecycle manager",
        usage="gaius [--sessions-dir DIR] [--staging-dir DIR] [--extra-sessions-dir DIR] [--format FMT] <command> [args]",
    )
    parser.add_argument("--sessions-dir", type=str, default=None,
                        help="Override session JSONL directory (env: GAIUS_SESSIONS_DIR)")
    parser.add_argument("--staging-dir", type=str, default=None,
                        help="Override staging output directory (env: GAIUS_STAGING_DIR)")
    parser.add_argument("--extra-sessions-dir", type=str, default=None,
                        help="Additional Claude Code session JSONL directory to scan (env: GAIUS_EXTRA_SESSIONS_DIR). "
                             "When set, retire also scans this directory.")
    parser.add_argument("--format", type=str, default="claude", choices=SUPPORTED_FORMATS,
                        help="Session format (default: claude)")
    parsed = parser.parse_args(global_argv)

    # Resolve sessions directory: flag > env > default
    if parsed.sessions_dir:
        PROJECT_DIR = Path(parsed.sessions_dir)
    elif os.environ.get("GAIUS_SESSIONS_DIR"):
        PROJECT_DIR = Path(os.environ["GAIUS_SESSIONS_DIR"])

    # Resolve staging directory: flag > env > default
    if parsed.staging_dir:
        STAGING_DIR = Path(parsed.staging_dir)
    elif os.environ.get("GAIUS_STAGING_DIR"):
        STAGING_DIR = Path(os.environ["GAIUS_STAGING_DIR"])

    # Resolve extra sessions directory: flag > env > auto-detected default
    if parsed.extra_sessions_dir:
        EXTRA_SESSIONS_DIR = Path(parsed.extra_sessions_dir)
    elif os.environ.get("GAIUS_EXTRA_SESSIONS_DIR"):
        EXTRA_SESSIONS_DIR = Path(os.environ["GAIUS_EXTRA_SESSIONS_DIR"])
    # else: keep EXTRA_SESSIONS_DIR as None (not set)

    # `completion` emits a static script from the in-memory COMMANDS registry —
    # no DB or session scan needed. Skip init_db() to keep it fast/side-effect-free.
    if command == "completion":
        COMMANDS[command](cmd_argv)
        return

    # Ensure facts.db is initialized on every run
    init_db()

    COMMANDS[command](cmd_argv)


if __name__ == "__main__":
    main()
