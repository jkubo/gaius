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
  inject      Inject ranked corpus entries into context (--budget N tokens, --skills-budget N, --landscape DOMAIN)
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

# Lazy-loaded embedding model (sentence-transformers)
_EMBED_MODEL = None
_EMBED_DIM = 384

def _get_embed_model():
    """Load the embedding model lazily (first call takes ~2s)."""
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer
            _EMBED_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        except ImportError:
            return None
    return _EMBED_MODEL

_EMBED_DAEMON_SOCK = Path.home() / ".gaius" / "embed.sock"

def _embed_via_daemon(text: str) -> list[float] | None:
    """Fast path: embed via the resident gaius-embed-daemon (avoids 6s model cold-load).
    Returns None if daemon is not running or errors — caller falls back to inline load."""
    sock_path = str(_EMBED_DAEMON_SOCK)
    if not os.path.exists(sock_path):
        return None
    try:
        import socket as _socket
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(sock_path)
            s.sendall((json.dumps({"text": text}) + "\n").encode())
            resp = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                resp += chunk
                if b"\n" in resp:
                    break
        data = json.loads(resp.split(b"\n")[0])
        if "vector" in data:
            return data["vector"]
        return None
    except Exception:
        return None


def _embed_text(text: str) -> list[float] | None:
    """Embed text into a 384-dim vector. Returns None if model unavailable.
    Tries warm daemon first (~5ms), falls back to inline model load (~6s)."""
    vec = _embed_via_daemon(text)
    if vec is not None:
        return vec
    model = _get_embed_model()
    if model is None:
        return None
    return model.encode(text, normalize_embeddings=True).tolist()

def _embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Batch embed multiple texts. Returns None if model unavailable."""
    model = _get_embed_model()
    if model is None:
        return None
    return model.encode(texts, normalize_embeddings=True).tolist()


_CHUNK_WORDS = 170  # ~220 wordpieces, safely under all-MiniLM-L6-v2's 256-token cap


def _chunk_text(text: str, max_words: int = _CHUNK_WORDS) -> list[str]:
    """Split text into <=max_words word-windows so each fits the embedder's 256-token
    cap instead of being silently truncated to its first ~256 tokens. Short text (the
    common case) returns a single chunk == the original string byte-for-byte, so
    short-fact embeddings are unchanged."""
    words = text.split()
    if len(words) <= max_words:
        return [text]
    return [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]

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


# Secret exclusion regex (follow sentinel.go pattern)
SECRET_KEYS_RE = re.compile(r'(password|secret|token|key|vault|crypt|private)', re.IGNORECASE)

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

# Gemini thought subjects that are navigation/orientation noise, not domain knowledge.
# These are a secondary agent figuring out where it is and how to use tools — not facts worth keeping.
GEMINI_NOISE_SUBJECTS = frozenset([
    "reporting workspace error",
    "reporting path error",
    "reporting workspace restriction",
    "checking repository access",
    "examining repository access",
    "exploring repo visibility",
    "initiating system exploration",
    "investigating task locations",
    "searching task locations",
    "exploring task availability",
    "re-evaluating the url",
    "orchestrating task completion",
    "contemplating environment access",
    "accessing document data",
    "investigating file access",
    "revisiting synchronization details",
    "identifying core file locations",
    "reviewing scope availability",
    "reassessing file access",
    "evaluating possible solutions",
    "considering hidden elements",
    "re-evaluating directory paths",
    "prioritizing file exploration",
])

# Patterns in discovery outputs that indicate credential/secret leakage.
GEMINI_CREDENTIAL_PATTERNS = ("FORGEJO_TOKEN=", "forgejo_token=", "_TOKEN=", "password=", "secret=")

DECISION_KEYWORDS = frozenset([
    "decided", "fixed", "mistake", "discovered", "gotcha",
    "warning", "architecture", "pattern", "never", "always", "critical"
])

FINDING_PATTERNS = [
    # Credential leakage
    r"exposed in", r"visible in kubectl describe", r"plaintext in pod args",
    r"ghp_[A-Za-z0-9]", r"token.*plaintext", r"secret.*leaked",
    # Infrastructure incidents
    r"CrashLoopBackOff", r"LMDB corruption", r"quorum lost",
    r"OOMKill", r"ImagePullBackOff", r"node NotReady",
    # Security actions
    r"rotated", r"revoked", r"incident",
    r"CVE-\d{4}", r"RBAC.*overly broad", r"privilege escalation",
]

FINDING_BASE_SCORE = 0.85

PROCEDURE_INDICATORS = [
    # Explicit step sequences
    r"step \d+[:\.]",
    r"^\d+\.\s+(?:check|run|try|verify|restart|delete|apply|inspect|look)",
    # Diagnostic branching
    r"tried .+?, (?:but |failed |didn't |error)",
    r"correct approach is",
    r"the fix (?:is|was|turned out)",
    r"root cause.+?was",
    r"workaround:",
    # Multi-attempt resolution
    r"attempt \d+",
    r"finally.+?(?:worked|resolved|fixed)",
]

PROCEDURE_FAILURE_INDICATORS = [
    r"failed", r"error", r"didn't work", r"timed out",
    r"crash", r"not found", r"refused", r"degraded",
]

PROCEDURE_MIN_STEPS = 3
PROCEDURE_BASE_SCORE = 0.70
PROCEDURE_INCOMPLETE_SCORE = 0.50

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
_DEFAULT_PRINCIPAL_BY_AGENT: dict = {}
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

# TF-IDF scoring configuration
DECAY_HALF_LIFE = 90.0          # days — confidence halves without reconfirmation
CROSS_AGENT_MULTIPLIER = 1.5    # bonus when both claude and gemini confirm
BOOTSTRAP_THRESHOLD = 20        # sessions per domain before scoring applies
DOMAIN_STATS_FILE = "domain_stats.json"

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

# Numbered sections in compaction summaries, in the order they appear.
# Each tuple: (storage_key, display_header)
SECTION_HEADERS = [
    ("primary_request", "Primary Request and Intent"),
    ("key_concepts",    "Key Technical Concepts"),
    ("files_changed",   "Files and Code Sections"),
    ("errors_fixes",    "Errors and Fixes"),
    ("pending_tasks",   "Pending Tasks"),
    ("current_work",    "Current Work"),
]

# Sections most likely to contain promotable memory facts
SIGNAL_SECTIONS = {"key_concepts", "errors_fixes", "pending_tasks"}

# Domain file stems → keywords that signal relevance.
# Used by stats to count per-domain fact density.
# Generic defaults — extend via domain_keywords in ~/.gaius/config.yaml.
_DOMAIN_KEYWORDS_DEFAULT = {
    "networking":    ["flannel", "cilium", "wireguard", "dns", "mtu", "cidr", "ingress",
                      "tunnel", "cloudflare", "traefik", "proxy", "coredns", "route"],
    "security":      ["vault", "tls", "cert", "token", "secret", "oauth", "rbac",
                      "incident", "leaked", "rotated", "cve", "apparmor", "osquery"],
    "storage":       ["drbd", "pvc", "persistent", "csi", "nfs", "storage", "s3",
                      "archive", "volume", "raft"],
    "services":      ["helm", "deployment", "rollout", "oauth2-proxy", "otel", "cronjob"],
    "observability": ["alert", "metric", "dashboard", "scrape", "collector",
                      "node-exporter", "grafana", "prometheus", "loki"],
    "gitops":        ["flux", "helmrelease", "kustomization", "gitops", "reconcile", "deploy"],
    "quality":       ["test", "lint", "ci", "pipeline", "review", "coverage"],
    "finint":        ["polymarket", "trading", "executor", "portfolio", "equity", "philosopher",
                      "kelly", "odds", "autotrade", "alpaca", "schwab", "ibkr", "finint",
                      "updown", "maker", "taker", "adverse selection", "fill rate"],
    "malint":        ["malware", "detonate", "sandbox", "vigiles", "yara", "tetragon",
                      "tracingpolicy", "sigkill", "ebpf", "bpf_lsm", "malint", "assay",
                      "bazaar", "mwdb", "triage", "corpus", "verdict", "sample"],
}
DOMAIN_KEYWORDS: dict = {
    **_DOMAIN_KEYWORDS_DEFAULT,
    **_gaius_cfg.get("domain_keywords", {}),
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_section(text: str, header: str) -> str:
    """Pull content of a numbered section from a compaction summary."""
    pattern = rf'\d+\.\s+{re.escape(header)}[^\n]*\n(.*?)(?=\n\d+\.\s+[A-Z]|\Z)'
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else ""


def load_staged() -> dict:
    """Return all staged summaries keyed by uuid."""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    result = {}
    for f in sorted(STAGING_DIR.glob("*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
                result[d["uuid"]] = d
        except Exception:
            pass
    return result


# Operational state transitions only exist in session history — dismissing
# them at review as "derivable from code" is the failure mode that rotted the
# JDT project files twice (2026-05-18, 05-20). Keyword list is the agreed spec
# from project_gaius_promotion_gap.md; false positives just surface earlier.
_STATE_CHANGE_RE = re.compile(
    r'\b(deleted|decommissioned|migrated|completed|shipped|torn down|removed|'
    r'deprecated|cutover|flipped|promoted|scaled down|terminated)\b',
    re.IGNORECASE)


def save_staged(entry: dict):
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    if not entry.get("reviewed") and "state_change" not in entry:
        section_text = " ".join(
            str(v) for v in (entry.get("sections") or {}).values() if v)
        entry["state_change"] = bool(_STATE_CHANGE_RE.search(section_text))
    ts = entry.get("timestamp", "unknown")[:19].replace(":", "-")
    # Path-traversal hardening: `timestamp`/`uuid` come from session JSONLs that,
    # via `gaius s3-retire`, may be authored on another agent's host. The only
    # prior sanitization was colon->dash, so `/` and `..` survived and a crafted
    # timestamp="../../../../tmp/" escaped STAGING_DIR. Restrict both fields to
    # safe filename chars and assert containment before writing.
    ts = re.sub(r"[^0-9A-Za-z_-]", "-", ts)
    uuid8 = re.sub(r"[^0-9A-Za-z]", "", str(entry.get("uuid", ""))[:8]) or "nouuid"
    fname = f"{ts}_{uuid8}.json"
    dest = (STAGING_DIR / fname).resolve()
    if dest.parent != STAGING_DIR.resolve():
        raise ValueError(f"staged filename escapes STAGING_DIR: {fname!r}")
    with open(dest, "w") as f:
        json.dump(entry, f, indent=2)


def has_signal(entry: dict) -> bool:
    """True if the summary has any of the high-value sections."""
    return any(entry["sections"].get(k) for k in SIGNAL_SECTIONS)


def count_domain_hits(entries: list) -> dict:
    """Count how many summaries mention each domain's keywords."""
    counts = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        count = 0
        for e in entries:
            text = " ".join(
                (e["sections"].get(k, "") or "").lower()
                for k, _ in SECTION_HEADERS
            )
            if any(kw in text for kw in keywords):
                count += 1
        counts[domain] = count
    return counts


def classify_entry(entry: dict) -> tuple[str, float]:
    """Return (entry_type, base_signal_score)."""
    if entry.get("isCompactSummary"):
        return "compaction_summary", 1.0

    etype = entry.get("type")
    msg = entry.get("message", {})
    content_list = msg.get("content", [])
    if not isinstance(content_list, list):
        content_list = []

    if etype == "assistant":
        has_text = any(c.get("type") == "text" and len(c.get("text", "")) > 100 for c in content_list)
        if has_text:
            return "assistant_reasoning", 0.65
        has_tool = any(c.get("type") == "tool_use" for c in content_list)
        if has_tool:
            return "assistant_tool_call", 0.25

    if etype == "tool_result":
        content = str(entry.get("content", ""))
        if any(w in content.lower() for w in ["error", "failed", "exception"]):
            return "tool_result_error", 0.80
        if len(content) > 500:
            return "tool_result_success_large", 0.20
        return "tool_result_success_small", 0.10

    if etype == "user":
        has_text = any(c.get("type") == "text" for c in content_list)
        if has_text:
            return "user_instruction", 0.55

    return "other", 0.0


def boost_score(text: str, base: float) -> float:
    """Add 0.15 if text contains decision keywords."""
    text_lower = text.lower()
    if any(kw in text_lower for kw in DECISION_KEYWORDS):
        return min(1.0, base + 0.15)
    return base


def classify_finding(text, base_type, base_score):
    """Upgrade entry to finding type if text matches finding patterns.

    Runs after classify_entry() and boost_score(). If any FINDING_PATTERNS
    regex matches the text, returns ("finding", max(base_score, FINDING_BASE_SCORE)).
    Otherwise returns (base_type, base_score) unchanged.
    """
    if not text:
        return base_type, base_score
    for pat in FINDING_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return "finding", max(base_score, FINDING_BASE_SCORE)
    return base_type, base_score


def extract_procedure(text):
    """Extract a procedure from narrative text.

    Returns None if text doesn't contain a valid procedure (>= PROCEDURE_MIN_STEPS
    numbered steps and at least one failure indicator).
    Returns dict with trigger, steps, resolution, step_count if found.
    """
    if not text:
        return None

    # Find numbered steps (1. ..., 2. ..., 3. ...)
    steps = re.findall(r'^\s*\d+\.\s+(.+?)$', text, re.MULTILINE)
    if len(steps) < PROCEDURE_MIN_STEPS:
        return None

    # Require at least one failure indicator to avoid false positives
    # (e.g. installation instructions vs diagnostic procedures)
    text_lower = text.lower()
    has_failure = any(re.search(pat, text_lower) for pat in PROCEDURE_FAILURE_INDICATORS)
    if not has_failure:
        return None

    # Extract trigger (symptom/error that starts the sequence)
    trigger = None
    trigger_patterns = [
        r'(?:symptom|error|issue|problem|failure)[:;]\s*(.+?)$',
        r'(?:when|after)\s+(.+?)(?:,|$)',
    ]
    for pat in trigger_patterns:
        m = re.search(pat, text[:500], re.IGNORECASE | re.MULTILINE)
        if m:
            trigger = m.group(1).strip()
            break

    # Check if there's a clear resolution
    has_resolution = any(
        re.search(pat, text, re.IGNORECASE)
        for pat in [r"the fix (?:is|was)", r"finally.+?(?:worked|resolved|fixed)",
                    r"correct approach", r"resolution:", r"solved by"]
    )

    return {
        "trigger": trigger or "Unknown trigger",
        "steps": steps,
        "resolution": steps[-1] if steps else None,
        "step_count": len(steps),
        "complete": has_resolution,
    }


def classify_procedure(text, base_type, base_score):
    """Upgrade entry to procedure type if it contains a diagnostic sequence.

    Runs after classify_entry() and boost_score(). If the text contains
    a multi-step diagnostic procedure (>= PROCEDURE_MIN_STEPS steps with
    failure indicators), returns ("procedure", PROCEDURE_BASE_SCORE).
    Incomplete procedures (no clear resolution) get PROCEDURE_INCOMPLETE_SCORE.
    """
    proc = extract_procedure(text)
    if proc is None:
        return base_type, base_score
    if proc["complete"]:
        return "procedure", max(base_score, PROCEDURE_BASE_SCORE)
    return "procedure", max(base_score, PROCEDURE_INCOMPLETE_SCORE)


def sample_entry(uuid: str, sample_rate: float) -> bool:
    """Deterministic sampling by UUID hash."""
    if not uuid:
        return False
    threshold_pct = sample_rate * 100
    return int(hashlib.md5(uuid.encode()).hexdigest(), 16) % 100 < threshold_pct


def tag_domains(text: str) -> list[str]:
    """Return list of domains matching keywords in text."""
    text_lower = text.lower()
    return [
        domain
        for domain, keywords in DOMAIN_KEYWORDS.items()
        if any(kw in text_lower for kw in keywords)
    ]


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


_BASE64_PREFIX_RE = re.compile(r"data:image/[a-z]+;base64,", re.IGNORECASE)
_HTML_START_RE = re.compile(r"^\s*(<(!DOCTYPE|html)\b)", re.IGNORECASE)
_HTML_BODY_RE = re.compile(r"<head\b.*<body\b", re.IGNORECASE | re.DOTALL)
_ERROR_KEYWORDS = ("error", "failed", "exception", "panic", "traceback")


def _is_html(s):
    """Return True if string looks like a full HTML document."""
    if len(s) < 500:
        return False
    return bool(_HTML_START_RE.match(s)) or bool(_HTML_BODY_RE.search(s[:2000]))


def _is_binary(s):
    """Return True if >30% of characters are non-printable."""
    if not s:
        return False
    sample = s[:4096]
    non_print = sum(1 for c in sample if not (c.isprintable() or c in "\n\r\t"))
    return non_print / len(sample) > 0.30


def _strip_string_content(s, max_bytes):
    """Strip bloat from a string content value. Returns stripped string or original."""
    if not isinstance(s, str) or not s:
        return s

    # Base64 image data
    if _BASE64_PREFIX_RE.search(s[:200]):
        return f"[image stripped: {len(s)} bytes]"

    # HTML documents
    if _is_html(s):
        return f"[HTML response stripped: {len(s)} bytes]"

    # Binary content
    if _is_binary(s):
        return f"[binary content stripped: {len(s)} bytes]"

    # Large content truncation
    if len(s) > max_bytes:
        head = max_bytes // 2
        tail = max_bytes // 2
        stripped = len(s) - head - tail
        return s[:head] + f"\n...\n[stripped {stripped} bytes]\n...\n" + s[-tail:]

    return s


def _strip_content_block(block, max_bytes):
    """Strip bloat from a single content block dict."""
    if not isinstance(block, dict):
        return block

    btype = block.get("type", "")

    # Image content blocks (Claude API format)
    if btype == "image":
        source = block.get("source", {})
        if isinstance(source, dict) and source.get("type") == "base64":
            data_len = len(source.get("data", ""))
            stripped = copy.deepcopy(block)
            stripped["source"] = {"type": "base64", "media_type": source.get("media_type", ""),
                                  "data": f"[stripped {data_len} bytes]"}
            return stripped
        return block

    # Text blocks — check for base64/HTML/binary inside text
    if btype == "text":
        text = block.get("text", "")
        new_text = _strip_string_content(text, max_bytes)
        if new_text is not text:
            stripped = copy.deepcopy(block)
            stripped["text"] = new_text
            return stripped

    # Tool result blocks
    if btype == "tool_result":
        return _strip_tool_result(block, max_bytes)

    return block


def _strip_tool_result(entry, max_bytes):
    """Strip bloat from a tool_result entry. Preserves error content."""
    content = entry.get("content", "")

    # String content
    if isinstance(content, str):
        lower = content.lower()
        if any(kw in lower for kw in _ERROR_KEYWORDS):
            return entry  # preserve error signal
        new_content = _strip_string_content(content, max_bytes)
        if new_content is not content:
            stripped = copy.deepcopy(entry)
            stripped["content"] = new_content
            return stripped
        return entry

    # List of content blocks
    if isinstance(content, list):
        new_blocks = [_strip_content_block(b, max_bytes) for b in content]
        if any(nb is not ob for nb, ob in zip(new_blocks, content)):
            stripped = copy.deepcopy(entry)
            stripped["content"] = new_blocks
            return stripped

    return entry


def strip_bloat(entry, max_tool_result_bytes=4096):
    """Return a pruned copy of a JSONL entry with bloat removed.

    Strips:
    - Base64 image data (data:image/... or content blocks with type='image')
    - Tool result content exceeding max_tool_result_bytes (keep first/last half)
    - Raw HTML responses (detect via <html or <!DOCTYPE, replace with placeholder)
    - Binary-looking content (high ratio of non-printable characters)

    Preserves:
    - Compaction summaries (isCompactSummary=true) — returned unchanged
    - Error messages in tool results — returned unchanged
    - User instruction text
    - Assistant reasoning text blocks
    - All metadata fields (uuid, timestamp, type, etc.)
    """
    if entry.get("isCompactSummary"):
        return entry

    etype = entry.get("type", "")

    # tool_result entries — delegate to tool result stripper
    if etype == "tool_result":
        return _strip_tool_result(entry, max_tool_result_bytes)

    # assistant/user entries with message.content list
    msg = entry.get("message", {})
    if isinstance(msg, dict):
        content_list = msg.get("content", [])
        if isinstance(content_list, list) and content_list:
            new_blocks = [_strip_content_block(b, max_tool_result_bytes) for b in content_list]
            if any(nb is not ob for nb, ob in zip(new_blocks, content_list)):
                pruned = copy.deepcopy(entry)
                pruned["message"]["content"] = new_blocks
                return pruned

    # Fallback: if entry has a top-level string "content" field
    content = entry.get("content")
    if isinstance(content, str) and len(content) > max_tool_result_bytes:
        new_content = _strip_string_content(content, max_tool_result_bytes)
        if new_content is not content:
            pruned = copy.deepcopy(entry)
            pruned["content"] = new_content
            return pruned

    return entry


def extract_delta_lines(text: str, domains: list[str]) -> dict[str, list[str]]:
    """Return {domain: [lines]} from text where lines match domain keywords."""
    lines = text.splitlines()
    result = {}
    for domain in domains:
        keywords = DOMAIN_KEYWORDS.get(domain, [])
        matching_lines = [
            line.strip()
            for line in lines
            if any(kw in line.lower() for kw in keywords)
        ]
        if matching_lines:
            result[domain] = matching_lines
    return result


# ── v2: SQLite facts index ────────────────────────────────────────────────────

def _dedup_live_fact_keys(conn: sqlite3.Connection) -> int:
    """One-time merge of live rows sharing a fact_key (cross-domain + race dupes).

    Keeps the oldest row, sums confirmation counts, unions the provenance
    arrays, tombstones the rest, and drops their embeddings. Idempotent.
    Returns the number of rows tombstoned."""
    now = datetime.now(timezone.utc).isoformat()
    groups = conn.execute(
        "SELECT fact_key FROM facts WHERE tombstoned_at IS NULL "
        "GROUP BY fact_key HAVING COUNT(*) > 1"
    ).fetchall()
    merged = 0
    for g in groups:
        rows = conn.execute(
            "SELECT * FROM facts WHERE fact_key = ? AND tombstoned_at IS NULL ORDER BY id",
            (g["fact_key"],)
        ).fetchall()
        keeper, losers = rows[0], rows[1:]
        total_conf = sum(r["confirmation_count"] or 1 for r in rows)
        last_seen = max((r["last_seen"] or "") for r in rows)

        def _union(col):
            out = []
            for r in rows:
                try:
                    for v in json.loads(r[col] or "[]"):
                        if v not in out:
                            out.append(v)
                except (TypeError, ValueError):
                    pass
            return json.dumps(out)

        conn.execute(
            "UPDATE facts SET confirmation_count=?, last_seen=?, agents=?, "
            "sessions=?, model_families=?, principals=?, model_versions=? WHERE id=?",
            (total_conf, last_seen, _union("agents"), _union("sessions"),
             _union("model_families"), _union("principals"), _union("model_versions"),
             keeper["id"]))
        for r in losers:
            conn.execute("UPDATE facts SET tombstoned_at=? WHERE id=?", (now, r["id"]))
            try:
                conn.execute("DELETE FROM fact_embeddings WHERE fact_id=?", (r["id"],))
            except sqlite3.Error:
                pass
        merged += len(losers)
    conn.commit()
    return merged


def init_db(db_path: Path = None) -> sqlite3.Connection:
    """Create or open the facts database. Returns open connection."""
    if db_path is None:
        db_path = DB_PATH
    # Test isolation guard: if running under pytest, refuse to open the real DB
    if "PYTEST_CURRENT_TEST" in os.environ and str(db_path) == str(Path.home() / ".gaius" / "facts.db"):
        raise RuntimeError(f"Test attempted to open live DB at {db_path}. Use a tmp_path fixture.")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    # WAL mode: survives concurrent readers and is more resilient to crashes
    # than the default rollback journal. Critical because multiple writers
    # (session-stop hook, nightly sync, K8s CronJob) touch this file.
    conn.execute("PRAGMA journal_mode=WAL")
    # Wait out concurrent writers instead of failing with 'database is locked'
    # (stop hook vs nightly ~03:00 overlap produced race duplicates on 05-18).
    conn.execute("PRAGMA busy_timeout=15000")
    # Load sqlite-vec extension if available
    if HAS_SQLITE_VEC:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS facts (
            id                  INTEGER PRIMARY KEY,
            domain              TEXT NOT NULL,
            fact_key            TEXT NOT NULL,
            fact_text           TEXT NOT NULL,
            first_seen          TEXT,
            last_seen           TEXT,
            confirmation_count  INTEGER DEFAULT 1,
            agents              TEXT DEFAULT '[]',
            sessions            TEXT DEFAULT '[]',
            provenance          TEXT,
            score               REAL DEFAULT 0.0,
            outcome             TEXT,
            source_agent        TEXT,
            principals          TEXT DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS sessions (
            uuid                TEXT PRIMARY KEY,
            origin              TEXT,
            agent               TEXT,
            project             TEXT,
            size_bytes          INTEGER,
            processed_at        TEXT,
            compaction_present  INTEGER DEFAULT 0,
            fact_count          INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS domains (
            name                TEXT PRIMARY KEY,
            spec_path           TEXT,
            keywords            TEXT DEFAULT '[]',
            maturity_score      REAL DEFAULT 0.0,
            last_computed       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_facts_domain_score ON facts(domain, score DESC);
        CREATE INDEX IF NOT EXISTS idx_facts_fact_key ON facts(fact_key);

        -- Knowledge Graph: temporal entity-relationship triples
        -- Temporal entity-relationship schema with validity windows.
        -- Entity types: node, service, storage-pool, namespace, agent, model, incident
        CREATE TABLE IF NOT EXISTS entities (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            type        TEXT DEFAULT 'unknown',
            domain      TEXT,
            properties  TEXT DEFAULT '{}',
            created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE TABLE IF NOT EXISTS triples (
            id              INTEGER PRIMARY KEY,
            subject         TEXT NOT NULL,
            predicate       TEXT NOT NULL,
            object          TEXT NOT NULL,
            valid_from      TEXT,
            valid_to        TEXT,
            confidence      REAL DEFAULT 1.0,
            source_session  TEXT,
            source_agent    TEXT,
            source_fact_id  INTEGER,
            extracted_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (subject) REFERENCES entities(id),
            FOREIGN KEY (object) REFERENCES entities(id)
        );
        CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject);
        CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object);
        CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate);
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);

        -- Fact ↔ entity membership (which facts mention which entities).
        -- Powers entity-grounded routing (route_suggest) and entity domain
        -- majority votes (kg.refresh_entity_domains).
        CREATE TABLE IF NOT EXISTS fact_entities (
            fact_id     INTEGER NOT NULL,
            entity_id   TEXT NOT NULL,
            PRIMARY KEY (fact_id, entity_id)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_fact_entities_entity ON fact_entities(entity_id);
    """)
    # Schema migrations — safe to run on existing DBs (ignore duplicate column errors)
    for _migration in [
        "ALTER TABLE facts ADD COLUMN fact_type TEXT DEFAULT 'operational'",
        "ALTER TABLE facts ADD COLUMN verification_expected TEXT",
        "ALTER TABLE facts ADD COLUMN verification_type TEXT DEFAULT 'contains'",
        "ALTER TABLE facts ADD COLUMN last_verified_at TEXT",
        "ALTER TABLE facts ADD COLUMN last_verification_result TEXT",
        "ALTER TABLE facts ADD COLUMN tombstoned_at TEXT",
        "ALTER TABLE facts ADD COLUMN tombstone_reason TEXT",
        "ALTER TABLE facts ADD COLUMN injection_weight REAL DEFAULT 1.0",

        "ALTER TABLE facts ADD COLUMN model_family TEXT DEFAULT 'claude'",
        "ALTER TABLE facts ADD COLUMN model_families TEXT DEFAULT '[\"claude\"]'",
        "ALTER TABLE facts ADD COLUMN source_agent TEXT",
        "ALTER TABLE facts ADD COLUMN principals TEXT DEFAULT '[]'",
        "ALTER TABLE facts ADD COLUMN model_version TEXT DEFAULT ''",
        "ALTER TABLE facts ADD COLUMN model_versions TEXT DEFAULT '[]'",
        "ALTER TABLE facts ADD COLUMN source TEXT DEFAULT 'human'",
        "ALTER TABLE facts ADD COLUMN verification_cmd TEXT DEFAULT ''",
        "ALTER TABLE facts ADD COLUMN fact_type TEXT DEFAULT 'operational'",
        "ALTER TABLE facts ADD COLUMN injection_weight REAL DEFAULT 1.0",

        # Confidence scoring + human review loop (2026-04-26)
        "ALTER TABLE facts ADD COLUMN confidence REAL DEFAULT 0.5",
        "ALTER TABLE facts ADD COLUMN confidence_source TEXT DEFAULT 'inferred'",
        "ALTER TABLE facts ADD COLUMN review_state TEXT DEFAULT 'auto'",
        "ALTER TABLE facts ADD COLUMN conflict_with TEXT",

        # Knowledge graph Gap-13 rebuild (2026-07-03): weight = co-occurrence
        # aggregation counter; kg_indexed_at = incremental-index watermark
        "ALTER TABLE triples ADD COLUMN weight INTEGER DEFAULT 1",
        "ALTER TABLE facts ADD COLUMN kg_indexed_at TEXT",
    ]:
        try:
            conn.execute(_migration)
        except sqlite3.OperationalError:
            pass  # column already exists

    # Index for fast review queue queries
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_review ON facts(review_state, confidence)")
    except sqlite3.OperationalError:
        pass

    # Race-proof upsert target: one live row per fact_key. Pre-06-10 DBs carry
    # duplicate live keys (cross-domain + race dupes) — auto-merge them once,
    # then index. Self-healing so stale S3/cluster copies fix themselves.
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_facts_live_key "
                     "ON facts(fact_key) WHERE tombstoned_at IS NULL")
    except sqlite3.IntegrityError:
        merged = _dedup_live_fact_keys(conn)
        print(f"facts.db migration: merged {merged} duplicate live fact rows",
              file=sys.stderr)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_facts_live_key "
                     "ON facts(fact_key) WHERE tombstoned_at IS NULL")

    # Vector embedding table (sqlite-vec) — stores 384-dim float vectors alongside fact IDs.
    # Used for semantic search and corroboration merge (dedup by cosine similarity).
    if HAS_SQLITE_VEC:
        try:
            conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS fact_embeddings USING vec0(embedding float[{_EMBED_DIM}], fact_id integer)")
        except sqlite3.OperationalError:
            pass  # already exists or vec0 not available

    conn.commit()
    return conn


def load_domain_specs() -> dict:
    """Load domain YAML specs from domain/specs/*.yaml.
    Falls back to DOMAIN_KEYWORDS for domains without spec files."""
    if not HAS_YAML or not SPECS_DIR.exists():
        return DOMAIN_KEYWORDS
    specs = {}
    for spec_file in sorted(SPECS_DIR.glob("*.yaml")):
        try:
            with open(spec_file) as f:
                spec = yaml.safe_load(f)
            if not isinstance(spec, dict):
                continue
            domain = spec.get("domain", spec_file.stem)
            keywords = spec.get("keywords", [])
            if keywords:
                specs[domain] = [str(k).lower() for k in keywords]
        except Exception as e:
            print(f"  warning: failed to load spec {spec_file.name}: {e}", file=sys.stderr)
    # Domains not in specs fall back to DOMAIN_KEYWORDS
    merged = dict(DOMAIN_KEYWORDS)
    merged.update(specs)
    return merged


def is_gemini_cold(path: Path, threshold_hours: float = GEMINI_COLD_THRESHOLD_HOURS) -> bool:
    """True if the Gemini session file hasn't been modified in threshold_hours."""
    mtime = path.stat().st_mtime
    age_hours = (time.time() - mtime) / 3600
    return age_hours >= threshold_hours


# ── Credential patterns (shared across parsers) ─────────────────────────────
CREDENTIAL_PATTERNS = GEMINI_CREDENTIAL_PATTERNS  # alias for use by all parsers


#   ~/.grok/sessions/<urlencoded-cwd>/<uuid>/chat_history.jsonl  (+ summary.json)
#   ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl


def register_session(conn: sqlite3.Connection, uuid: str, origin: str, agent: str,
                     project: str, size_bytes: int, compaction_present: bool = False):
    """Insert session into dedup table. INSERT OR IGNORE — one row per UUID."""
    conn.execute("""
        INSERT OR IGNORE INTO sessions (uuid, origin, agent, project, size_bytes, processed_at, compaction_present)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (uuid, origin, agent, project, size_bytes,
          datetime.now(timezone.utc).isoformat(), 1 if compaction_present else 0))
    conn.commit()


_SEM_DEDUP_WARNED = False

def _find_semantic_duplicate(conn: sqlite3.Connection, domain: str, fact_text: str, threshold: float = 0.92) -> dict | None:
    """Find an existing fact that is semantically similar (cosine sim > threshold).
    Returns the matching fact row as a dict, or None.
    Uses sqlite-vec for efficient vector search."""
    if not HAS_SQLITE_VEC:
        return None
    # Multi-vector safety: a long (chunked) incoming fact compared chunk-wise against
    # the corpus risks a false-merge that silently discards distinct information, so
    # never auto-dedup when the incoming fact would be chunked.
    if len(_chunk_text(fact_text)) > 1:
        return None
    embedding = _embed_text(fact_text)
    if embedding is None:
        return None
    try:
        # Query vec0 for nearest neighbors, join with facts table.
        # Global (no domain filter): exact-key dedup is global now, so semantic
        # dedup must be too — a cross-domain near-duplicate is a corroboration,
        # not a new fact. Tombstoned rows excluded (vec slots are wasted on
        # them post-join; k=10 compensates).
        import struct
        vec_blob = struct.pack(f'{_EMBED_DIM}f', *embedding)
        rows = conn.execute("""
            SELECT f.*, fe.distance
            FROM fact_embeddings fe
            JOIN facts f ON f.id = fe.fact_id
            WHERE fe.embedding MATCH ?
              AND k = 10
              AND f.tombstoned_at IS NULL
        """, (vec_blob,)).fetchall()
        for row in rows:
            # sqlite-vec returns L2 distance; convert to cosine similarity
            # For normalized vectors: cosine_sim = 1 - (L2_dist^2 / 2)
            l2_dist = row["distance"]
            cosine_sim = 1.0 - (l2_dist ** 2 / 2.0)
            if cosine_sim >= threshold and row["fact_key"] != hashlib.sha256(fact_text.encode()).hexdigest()[:32]:
                # Only merge into a single-chunk (short) fact -- matching one chunk of a
                # long multi-chunk fact is not a whole-fact duplicate (false-merge guard).
                n_chunks = conn.execute(
                    "SELECT COUNT(*) FROM fact_embeddings WHERE fact_id = ?", (row["id"],)).fetchone()[0]
                if n_chunks == 1:
                    return dict(row)
    except Exception as e:
        # A broken vec0 extension or dimension mismatch silently disabling
        # dedup was invisible for weeks — make it loud once per process.
        global _SEM_DEDUP_WARNED
        if not _SEM_DEDUP_WARNED:
            print(f"Warning: semantic dedup unavailable ({e})", file=sys.stderr)
            _SEM_DEDUP_WARNED = True
    return None


def _store_embedding(conn: sqlite3.Connection, fact_id: int, fact_text: str):
    """Compute and store embedding(s) for a fact. No-op if dependencies unavailable.

    Long facts are split into <=256-token chunks, each embedded and stored as its own
    row (same fact_id), so retrieval can MAX over chunks instead of seeing only the
    first ~256 tokens. Short facts produce exactly one chunk == prior behavior."""
    if not HAS_SQLITE_VEC:
        return
    import struct
    try:
        # Upsert: delete all old chunk rows for this fact, then insert one per chunk.
        conn.execute("DELETE FROM fact_embeddings WHERE fact_id = ?", (fact_id,))
        for chunk in _chunk_text(fact_text):
            embedding = _embed_text(chunk)
            if embedding is None:
                continue
            vec_blob = struct.pack(f'{_EMBED_DIM}f', *embedding)
            conn.execute("INSERT INTO fact_embeddings (embedding, fact_id) VALUES (?, ?)", (vec_blob, fact_id))
    except Exception:
        pass


# ── Confidence Scoring + Human Review Loop ───────────────────────────────────

_HEDGE_PATTERNS = [
    r'\bappears? to\b', r'\bmight be\b', r'\bI think\b', r'\bI assumed?\b',
    r'\bprobably\b', r'\bseems? to\b', r'\bnot sure\b', r'\bmaybe\b',
    r'\bI believe\b', r'\bcould be\b',
]

_OBSERVED_PATTERNS = [
    r'kubectl get', r'kubectl describe', r'kubectl logs',
    r'\$ ', r'```',
    r'confirmed via', r'verified by', r'checked:',
]

_HARDWARE_STATE_DOMAINS = {'nodes', 'storage', 'networking'}


def _score_confidence(text: str, domain: str) -> tuple:
    """Return (confidence_score: float, source_label: str).

    Heuristic scoring based on language patterns:
    - Live observation evidence → 0.85
    - Hedged language (2+ hedges) → 0.25, (1 hedge) → 0.40
    - Hardware/state domain with no live evidence → 0.45
    - Default → 0.50
    """
    text_lower = text.lower()

    if any(re.search(p, text) for p in _OBSERVED_PATTERNS):
        return 0.85, 'inferred'

    hedge_count = sum(1 for p in _HEDGE_PATTERNS if re.search(p, text_lower))
    if hedge_count >= 2:
        return 0.25, 'inferred'
    if hedge_count == 1:
        return 0.40, 'inferred'

    if domain in _HARDWARE_STATE_DOMAINS:
        return 0.45, 'inferred'

    return 0.50, 'inferred'


_CONTRADICTION_STOP_WORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'it', 'its', 'this', 'that', 'these', 'those', 'has', 'have', 'had',
    'will', 'would', 'can', 'could', 'should', 'may', 'might', 'must',
    'from', 'by', 'as', 'if', 'into', 'through', 'after', 'before',
    'all', 'any', 'each', 'both', 'more', 'most', 'other', 'some',
    'than', 'so', 'yet', 'now', 'also', 'when', 'where', 'which', 'who',
}

# Operational verbs that signal a state change — not common English words
_OPERATIONAL_NEGATION = {
    'removed', 'disabled', 'replaced', 'migrated', 'deprecated',
    'deleted', 'uninstalled', 'reverted', 'rolled-back', 'decommissioned',
    'retired', 'offline', 'broken', 'failed',
}


def _check_contradiction(new_fact: str, domain: str, conn) -> int | None:
    """Return row id of a conflicting existing fact, or None.

    Uses meaningful-token overlap (>=5 tokens, stop-words excluded) +
    operational-negation asymmetry as a contradiction signal. Much more
    conservative than naive token overlap to avoid false positives.

    Replace with embedding similarity when sqlite-vec is available.
    """
    existing = conn.execute(
        "SELECT id, fact_text FROM facts WHERE domain = ? AND review_state != 'rejected'",
        (domain,)
    ).fetchall()

    def _meaningful_tokens(text: str) -> set:
        tokens = set(re.sub(r'[^\w\s-]', '', text.lower()).split())
        return tokens - _CONTRADICTION_STOP_WORDS

    new_tokens = _meaningful_tokens(new_fact)

    for row in existing:
        fact_id, content = row['id'], row['fact_text']
        old_tokens = _meaningful_tokens(content)
        shared = new_tokens & old_tokens
        if len(shared) < 5:
            continue

        new_negated = bool(new_tokens & _OPERATIONAL_NEGATION)
        old_negated = bool(old_tokens & _OPERATIONAL_NEGATION)
        if new_negated != old_negated:
            return fact_id

    return None


def upsert_fact(conn: sqlite3.Connection, domain: str, fact_key: str, fact_text: str,
                agent: str, session_uuid: str, provenance: str, score: float = 0.5,
                outcome: str = None, model_family: str = 'claude',
                model_version: str = '', source: str = 'human', verification_cmd: str = '', fact_type: str = 'operational', injection_weight: float = 1.0):
    """Insert new fact or increment confirmation_count for existing fact_key.

    Dedup is GLOBAL on fact_key (fact_key ↔ fact_text is 1:1): a re-extraction
    in a different domain corroborates the existing row instead of creating a
    cross-domain duplicate (514 of those split confirmation counts pre-06-10).
    Also performs semantic dedup (cosine > 0.92) and is race-safe: the insert
    uses ON CONFLICT against the partial unique index uq_facts_live_key, so a
    concurrent writer that wins the check-then-insert window turns this call
    into a corroboration instead of a duplicate."""
    now = datetime.now(timezone.utc).isoformat()
    principal = agent_to_principal(agent)
    mv_tag = f"{model_family}:{model_version}" if model_version else model_family

    _EXISTING_COLS = ("SELECT id, agents, sessions, confirmation_count, model_families, "
                      "principals, model_versions, verification_cmd FROM facts ")

    def _corroborate(existing_row):
        agents = json.loads(existing_row["agents"] or "[]")
        sessions = json.loads(existing_row["sessions"] or "[]")
        model_families = json.loads(existing_row["model_families"] or '["claude"]')
        principals = json.loads(existing_row["principals"] or "[]")
        model_versions = json.loads(existing_row["model_versions"] or "[]")
        if agent not in agents:
            agents.append(agent)
        if session_uuid not in sessions:
            sessions.append(session_uuid)
        if model_family not in model_families:
            model_families.append(model_family)
        if principal not in principals:
            principals.append(principal)
        if mv_tag not in model_versions:
            model_versions.append(mv_tag)
        conn.execute("""
            UPDATE facts SET
                last_seen = ?,
                confirmation_count = confirmation_count + 1,
                agents = ?,
                sessions = ?,
                model_families = ?,
                principals = ?,
                model_version = ?,
                model_versions = ?
            WHERE id = ?
        """, (now, json.dumps(agents), json.dumps(sessions),
              json.dumps(model_families), json.dumps(principals),
              model_version, json.dumps(model_versions), existing_row["id"]))

    existing = conn.execute(
        _EXISTING_COLS + "WHERE fact_key = ? AND tombstoned_at IS NULL",
        (fact_key,)
    ).fetchone()

    # Semantic dedup: if no exact key match, check for semantically similar facts
    if not existing and HAS_SQLITE_VEC:
        sem_match = _find_semantic_duplicate(conn, domain, fact_text)
        if sem_match:
            # Corroboration merge: don't delete old fact, just bump its confirmation
            existing = conn.execute(
                _EXISTING_COLS + "WHERE id = ?",
                (sem_match["id"],)
            ).fetchone()

    if existing:
        _corroborate(existing)
    else:
        # Score confidence and check for contradictions before inserting
        confidence, conf_source = _score_confidence(fact_text, domain)
        conflict_id = _check_contradiction(fact_text, domain, conn)
        review_state = 'auto'
        if confidence < 0.5 or conflict_id is not None:
            review_state = 'pending'
            if conflict_id is not None:
                confidence = min(confidence, 0.30)
                conf_source = 'contradiction'

        cur = conn.execute("""
            INSERT INTO facts (domain, fact_key, fact_text, first_seen, last_seen,
                               agents, sessions, provenance, score, outcome,
                               model_family, model_families,
                               source_agent, principals,
                               model_version, model_versions, source, verification_cmd, fact_type, injection_weight,
                               confidence, confidence_source, review_state, conflict_with)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fact_key) WHERE tombstoned_at IS NULL DO NOTHING
        """, (domain, fact_key, fact_text, now, now,
              json.dumps([agent]), json.dumps([session_uuid]),
              provenance, score, outcome,
              model_family, json.dumps([model_family]),
              principal, json.dumps([principal]),
              model_version, json.dumps([mv_tag]), source, verification_cmd, fact_type, injection_weight,
              confidence, conf_source, review_state, str(conflict_id) if conflict_id else None))
        if cur.rowcount == 0:
            # Lost the check-then-insert race — a concurrent writer inserted
            # this fact_key between our SELECT and INSERT. Corroborate theirs.
            existing = conn.execute(
                _EXISTING_COLS + "WHERE fact_key = ? AND tombstoned_at IS NULL",
                (fact_key,)
            ).fetchone()
            if existing:
                _corroborate(existing)
        else:
            # Store embedding for new fact
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            _store_embedding(conn, new_id, fact_text)
            # Keep the KG current (Gap 13): index entities/relations at insert
            # time — previously only manual `kg index` runs populated the graph.
            # Guarded: KG failure must never block fact ingestion. SAVEPOINT so
            # a partial failure rolls back ALL its KG writes — otherwise the
            # fact commits un-stamped (kg_indexed_at is the last statement) and
            # the nightly re-index double-counts its co-occurrence weights.
            try:
                conn.execute("SAVEPOINT kg_index")
                kg_index_fact(conn, new_id, fact_text, domain,
                              session_uuid=session_uuid, agent=agent, timestamp=now)
                conn.execute("RELEASE SAVEPOINT kg_index")
            except Exception:
                try:
                    conn.execute("ROLLBACK TO SAVEPOINT kg_index")
                    conn.execute("RELEASE SAVEPOINT kg_index")
                except Exception:
                    pass
            # Flag the conflicting existing fact now that we have our new_id
            if conflict_id is not None:
                conn.execute(
                    "UPDATE facts SET review_state='pending', confidence=0.30, "
                    "confidence_source='contradiction', conflict_with=? WHERE id=?",
                    (str(new_id), conflict_id)
                )
    conn.commit()


def tag_domains_from_specs(text: str, domain_specs: dict) -> list[str]:
    """Return domains whose keywords appear in text, ranked best-match-first.

    Ranking = number of distinct keyword hits (desc), ties broken by spec
    order (earlier wins). Callers that take ``domains[0]`` get the DOMINANT
    topic instead of whichever domain merely happens to be first in the dict.
    The old boolean-OR-in-dict-order behaviour let a single incidental keyword
    (e.g. 'dns' in a malware C2 description) hijack threat-intel facts to
    'networking'; a fact with 3 'security' hits now beats it. Single-match
    and no-match results are unchanged, so no correctly-formed (two-arg)
    caller regresses: the domains[0] consumers get the dominant topic and
    order-insensitive aggregating callers are unaffected.
    """
    text_lower = text.lower()
    scored = []
    for pos, (domain, keywords) in enumerate(domain_specs.items()):
        hits = sum(1 for kw in keywords if kw in text_lower)
        if hits:
            scored.append((hits, -pos, domain))  # hits desc, then earlier spec pos
    scored.sort(reverse=True)
    return [domain for _hits, _pos, domain in scored]


# ── Knowledge Graph: Entity Extraction & Triple Management ───────────────────

# Built-in entity patterns — generic K8s / infrastructure baseline.
# Extend or replace via entities.patterns in ~/.gaius/config.yaml.
# Set entities.preset: none to disable built-in patterns entirely.
# ── Knowledge Graph (entities/relations/triples) ────────────────────────────
# Extracted to gaius/kg.py (2026-06-28); re-imported at the bottom of this file
# so `from gaius._core import kg_index_fact` (etc.) keeps working. See gaius/kg.py.


# ── Query Boosting (hybrid BM25 + TF-IDF) ────────────────────────

def extract_quoted_phrases(text: str) -> list[str]:
    """Extract 'quoted' and "double-quoted" phrases from query text."""
    phrases = []
    for pat in [r"'([^']{3,60})'", r'"([^"]{3,60})"']:
        phrases.extend(re.findall(pat, text))
    return [p.strip().lower() for p in phrases if len(p.strip()) >= 3]


def quoted_phrase_boost(phrases: list[str], fact_text: str) -> float:
    """Boost score if quoted phrases appear verbatim in fact. Returns 0.0-1.0."""
    if not phrases:
        return 0.0
    text_lower = fact_text.lower()
    hits = sum(1 for p in phrases if p in text_lower)
    return min(hits / len(phrases), 1.0)


def infra_entity_boost(query: str, fact_text: str) -> float:
    """Boost if infrastructure entity names (k8s-*, *-api, namespaces) from query appear in fact."""
    # Extract k8s node names, service names, namespace-like tokens
    entities = re.findall(r'k8s-[\w-]+|[\w]+-api|[\w]+-fwd-[\w-]+', query.lower())
    if not entities:
        return 0.0
    text_lower = fact_text.lower()
    hits = sum(1 for e in entities if e in text_lower)
    return min(hits / len(entities), 1.0)


# ── TF-IDF Scoring ───────────────────────────────────────────────────────────

def compute_tfidf(term_freq: int, doc_freq: int, total_docs: int) -> float:
    """Standard TF-IDF: tf * log(N / df)."""
    if doc_freq == 0 or total_docs == 0:
        return 0.0
    return term_freq * math.log(total_docs / doc_freq)


def decay_factor(age_days: float, last_confirmed_days: float, half_life: float = DECAY_HALF_LIFE) -> float:
    """Exponential decay since last confirmation. Clamped to [0, 1]."""
    gap = age_days - last_confirmed_days
    if gap <= 0:
        return 1.0
    lam = math.log(2) / half_life
    return math.exp(-lam * gap)


def estimate_tokens(text: str) -> int:
    """Approximate token count (chars / 4)."""
    return max(1, len(text) // 4)


def compute_entry_tfidf_score(entry: dict, doc_freq: dict, total_docs: int) -> float:
    """Compute TF-IDF score for a staged summary entry using decision keywords."""
    text = " ".join(
        (entry.get("sections", {}).get(k, "") or "").lower()
        for k, _ in SECTION_HEADERS
    )
    if not text.strip():
        return 0.0

    # TF: count of decision keywords in this entry
    words = text.split()
    tf = Counter(w for w in words if w in DECISION_KEYWORDS)

    score = 0.0
    for term, freq in tf.items():
        df = doc_freq.get(term, 0)
        score += compute_tfidf(freq, df, total_docs)
    return score


def build_doc_freq(entries: list) -> dict:
    """Build document frequency counts for decision keywords across all entries."""
    doc_freq = Counter()
    for entry in entries:
        text = " ".join(
            (entry.get("sections", {}).get(k, "") or "").lower()
            for k, _ in SECTION_HEADERS
        )
        words_in_doc = set(text.split())
        for kw in DECISION_KEYWORDS:
            if kw in words_in_doc:
                doc_freq[kw] += 1
    return dict(doc_freq)


def bm25_score(query_terms: list[str], entry: dict, doc_freq: dict, total_docs: int, avg_len: float,
               k1: float = 1.5, b: float = 0.75) -> float:
    """BM25 relevance score for an entry against a query.

    Uses query terms from --task description to rank entries by task relevance.
    Returns score >= 0.0 (higher = more relevant to the query).
    """
    if not query_terms:
        return 0.0

    text = " ".join(
        (entry.get("sections", {}).get(k, "") or "").lower()
        for k, _ in SECTION_HEADERS
    )
    if not text.strip():
        return 0.0

    words = text.split()
    doc_len = len(words)
    tf_counts = Counter(words)

    score = 0.0
    for term in query_terms:
        tf = tf_counts.get(term, 0)
        df = doc_freq.get(term, 0)
        if tf == 0:
            continue
        # IDF with smoothing — positive even when df == total_docs
        idf = math.log((total_docs - df + 0.5) / (df + 0.5) + 1.0)
        # BM25 TF normalization
        tf_norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * doc_len / max(avg_len, 1)))
        score += idf * tf_norm

    return score


def _build_bm25_doc_freq(entries: list, query_terms: set) -> tuple[dict, float]:
    """Build doc-frequency counts and avg doc length for BM25 query terms."""
    df: dict[str, int] = Counter()
    total_len = 0
    for entry in entries:
        text = " ".join(
            (entry.get("sections", {}).get(k, "") or "").lower()
            for k, _ in SECTION_HEADERS
        )
        words = text.split()
        total_len += len(words)
        words_in_doc = set(words)
        for term in query_terms:
            if term in words_in_doc:
                df[term] += 1
    avg_len = total_len / len(entries) if entries else 1.0
    return dict(df), avg_len


def load_domain_stats() -> dict:
    """Load per-domain session counts from corpus directory."""
    stats_path = CORPUS_DIR / DOMAIN_STATS_FILE
    if stats_path.exists():
        try:
            with open(stats_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_domain_stats(stats: dict):
    """Save per-domain session counts to corpus directory."""
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    stats_path = CORPUS_DIR / DOMAIN_STATS_FILE
    with open(stats_path, "w") as f:
        json.dump(stats, f, indent=2)


def update_domain_stats(entries: list) -> dict:
    """Recompute per-domain session counts from staged entries."""
    stats = {}
    seen_sessions = {}  # domain -> set of session_ids
    for entry in entries:
        domains = tag_domains_from_specs(" ".join(
            (entry.get("sections", {}).get(k, "") or "")
            for k, _ in SECTION_HEADERS
        ), DOMAIN_KEYWORDS)
        sid = entry.get("session_id", "")
        for dom in domains:
            if dom not in seen_sessions:
                seen_sessions[dom] = set()
            seen_sessions[dom].add(sid)
    for dom, sids in seen_sessions.items():
        stats[dom] = {"session_count": len(sids)}
    save_domain_stats(stats)
    return stats


# ── Commands ──────────────────────────────────────────────────────────────────

def content_hash(content: str) -> str:
    """SHA-256 hex digest of compaction content for change detection."""
    return hashlib.sha256(content.encode()).hexdigest()


# ── Uncompacted session mining ────────────────────────────────────────────────

# Minimum session file size to attempt mining (skip abandoned/empty sessions)
MINE_MIN_BYTES = 10_000  # 10 KB

# Re-mine threshold: re-process if file grew >3x since last mine
MINE_REGROW_FACTOR = 3

# Minimum text length for an assistant block to be worth keeping
MINE_MIN_TEXT_LEN = 150

# Score threshold — only keep blocks above this after classification
MINE_SCORE_THRESHOLD = 0.50

# Max blocks to keep per section to avoid staged entries becoming walls of text
MINE_MAX_BLOCKS_PER_SECTION = 12

# ── Noise filter: patterns that should never become facts ───────────────────
# These match text blocks that are navigation/boilerplate, not domain knowledge.
NOISE_PATTERNS = [
    re.compile(r'^(Let me |I\'ll |I will |I need to )(read|check|look|search|find|open|examine)', re.IGNORECASE),
    re.compile(r'^(Reading|Checking|Searching|Looking|Opening|Examining) ', re.IGNORECASE),
    re.compile(r'^Tool (loaded|result|call)', re.IGNORECASE),
    re.compile(r'^(Here\'s|Here is) (the|a) (summary|recap|overview of what)', re.IGNORECASE),
    re.compile(r'this (session|conversation) (is being |was )continued from', re.IGNORECASE),
    re.compile(r'^(I\'ve |I have )?(successfully |now )?(completed|finished|done|updated|made)', re.IGNORECASE),
    re.compile(r'^(Great|Perfect|Excellent|Wonderful)[!.,]', re.IGNORECASE),
    re.compile(r'^\s*Co-Authored-By:', re.IGNORECASE),
    # User quotes that aren't domain knowledge
    re.compile(r'^"(so |yeah|yes |no |ok |go |do it|check |fix |sure|implement|u |we |can we|could you|while we|meanwhile)', re.IGNORECASE),
    re.compile(r'^(User (noted|asked|said|explicitly|confirmed))', re.IGNORECASE),
    # Remaining from #, blocked on, task pending
    re.compile(r'^(Remaining from|Blocked on|blocked on you)', re.IGNORECASE),
]

def _is_noise(text: str) -> bool:
    """Return True if text matches known boilerplate/navigation patterns."""
    for pat in NOISE_PATTERNS:
        if pat.search(text[:200]):  # only check first 200 chars
            return True
    return False

# ── Seeded scores: content-type → initial score ────────────────────────────
# Higher scores for content that's genuinely useful in future sessions.
_SEEDED_SCORE_PATTERNS = [
    (re.compile(r'(outage|incident|postmortem|cascade|failure|broke|down|crash)', re.IGNORECASE), 0.80),
    (re.compile(r'(architecture|design|pattern|three.layer|migration|refactor)', re.IGNORECASE), 0.70),
    (re.compile(r'(config|manifest|helm|values|deployment|cronjob|daemonset)', re.IGNORECASE), 0.60),
    (re.compile(r'(procedure|steps|how.to|runbook|playbook|troubleshoot)', re.IGNORECASE), 0.70),
    (re.compile(r'(security|vuln|cve|exploit|rbac|auth|tls|cert)', re.IGNORECASE), 0.70),
]

def _seeded_score(text: str) -> float:
    """Return a content-type-based initial score. Higher = more valuable content."""
    best = 0.4  # default for general content
    text_sample = text[:500].lower()
    for pat, score in _SEEDED_SCORE_PATTERNS:
        if pat.search(text_sample):
            best = max(best, score)
    return best


def _mine_session(path: Path) -> dict | None:
    """Extract signal from a non-compacted session JSONL.

    Parses assistant text blocks, user messages, and error tool results.
    Classifies each using the existing scoring pipeline and synthesizes
    high-signal blocks into the standard sections format.

    Returns a sections dict compatible with staged entries, or None if
    the session has insufficient signal.
    """
    entries = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:
        return None

    if not entries:
        return None

    # Collect classified blocks
    concepts = []     # high-signal assistant reasoning
    errors = []       # error tool results + fix reasoning
    user_context = [] # user messages for intent

    for entry in entries:
        etype = entry.get("type", "")
        entry_type, base_score = classify_entry(entry)

        if etype == "assistant":
            msg = entry.get("message", {})
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = block.get("text", "").strip()
                if len(text) < MINE_MIN_TEXT_LEN:
                    continue

                # Noise filter — skip boilerplate before scoring
                if _is_noise(text):
                    continue

                # Score the block
                score = boost_score(text, base_score)
                _, score = classify_finding(text, entry_type, score)
                _, score = classify_procedure(text, entry_type, score)

                if score >= MINE_SCORE_THRESHOLD:
                    # Classify into concepts vs errors
                    text_lower = text.lower()
                    if any(kw in text_lower for kw in _ERROR_KEYWORDS):
                        errors.append(text)
                    else:
                        concepts.append(text)

        elif etype == "tool_result":
            content = str(entry.get("content", ""))
            if len(content) < 100:
                continue
            content_lower = content.lower()
            if any(kw in content_lower for kw in _ERROR_KEYWORDS):
                # Truncate long error outputs
                errors.append(content[:800])

        elif etype == "user":
            msg = entry.get("message", {})
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if len(text) > 30:
                            user_context.append(text)
            elif isinstance(content, str) and len(content) > 30:
                user_context.append(content.strip())

    # Need minimum signal to stage
    if not concepts and not errors:
        return None

    # Trim to max blocks
    concepts = concepts[:MINE_MAX_BLOCKS_PER_SECTION]
    errors = errors[:MINE_MAX_BLOCKS_PER_SECTION]
    user_context = user_context[:8]

    # Build sections matching SECTION_HEADERS format
    # Derive primary request from first few user messages
    primary_request = ""
    if user_context:
        first_msgs = user_context[:3]
        primary_request = "\n".join(f"- {msg[:200]}" for msg in first_msgs)

    sections = {
        "primary_request": primary_request,
        "key_concepts": "\n".join(f"- {c[:300]}" for c in concepts) if concepts else "",
        "files_changed": "",  # not reliably extractable without compaction
        "errors_fixes": "\n".join(f"- {e[:300]}" for e in errors) if errors else "",
        "pending_tasks": "",  # not reliably extractable without compaction
        "current_work": "",
    }

    return sections


def _mine_uncompacted_sessions(conn: sqlite3.Connection, staged: dict,
                                compacted_stems: set[str],
                                all_jsonl: list[Path]) -> int:
    """Mine signal from sessions that were never compacted.

    Scans session JSONLs that have no isCompactSummary entry, extracts
    high-signal assistant reasoning and error blocks, stages them as
    mined summaries, AND auto-promotes high-signal content to facts.db.

    Re-mines sessions that have grown significantly (>MINE_REGROW_FACTOR)
    since last processing.

    Returns count of newly staged/updated mined entries.
    """
    mined_count = 0

    for path in all_jsonl:
        # Skip if already compacted
        if path.stem in compacted_stems:
            continue

        # Skip tiny files (abandoned/empty sessions)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < MINE_MIN_BYTES:
            continue

        # Use a synthetic UUID for mined entries: "mined-" + session stem
        mined_uuid = f"mined-{path.stem}"

        if mined_uuid in staged:
            # Already mined — check if session has grown significantly
            prev_size = staged[mined_uuid].get("_mined_size", 0)
            if prev_size > 0 and size < prev_size * MINE_REGROW_FACTOR:
                continue  # not grown enough to re-mine
            # Session grew significantly — re-mine it

        sections = _mine_session(path)
        if sections is None:
            # No signal — register as processed (size tracked for regrow check)
            register_session(conn, path.stem, "local", _DEFAULT_PRINCIPAL,
                             PROJECT_DIR.name, size, compaction_present=False)
            continue

        # Check if sections have real content
        if not has_signal({"sections": sections}):
            register_session(conn, path.stem, "local", _DEFAULT_PRINCIPAL,
                             PROJECT_DIR.name, size, compaction_present=False)
            continue

        # Stage the mined entry (or update if re-mining)
        is_update = mined_uuid in staged
        record = {
            "uuid": mined_uuid,
            "session_id": path.stem,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reviewed": False,
            "sections": sections,
            "content_hash": content_hash(json.dumps(sections, sort_keys=True)),
            "agent_source": "claude",
            "source": "mined",
            "_mined_size": size,  # track for regrow detection
        }
        save_staged(record)
        staged[mined_uuid] = record
        mined_count += 1

        register_session(conn, path.stem, "local", _DEFAULT_PRINCIPAL,
                         PROJECT_DIR.name, size, compaction_present=False)

        # Auto-promote: insert high-signal blocks directly into facts.db
        _promote_mined_to_facts(conn, path.stem, sections)

    return mined_count


def _promote_mined_to_facts(conn: sqlite3.Connection, session_stem: str,
                             sections: dict) -> int:
    """Insert high-signal mined content as facts in facts.db.

    Takes the concepts and errors from a mined session and inserts them
    as individual facts with seeded scores. Returns count of new facts.
    """
    count = 0
    now = datetime.now(timezone.utc).isoformat()

    # Collect text blocks from key_concepts and errors_fixes
    blocks = []
    for section_key in ("key_concepts", "errors_fixes"):
        text = sections.get(section_key, "")
        if not text:
            continue
        # Split on bullet points
        for line in text.split("\n"):
            line = line.strip().lstrip("- ").strip()
            if len(line) < 80:
                continue
            # Skip noise one more time at promotion boundary
            if _is_noise(line):
                continue
            blocks.append(line)

    for block in blocks:
        fact_key = hashlib.sha256(f"{session_stem}:{block[:200]}".encode()).hexdigest()[:16]

        # Assign domain from keywords
        domain = "general"
        block_lower = block.lower()
        for d, kws in DOMAIN_KEYWORDS.items():
            if any(kw in block_lower for kw in kws):
                domain = d
                break

        # Use seeded score based on content type
        score = _seeded_score(block)

        upsert_fact(conn, domain, fact_key, block[:500],
                    _DEFAULT_PRINCIPAL, session_stem, "auto-mined",
                    score=score, source="autonomous", fact_type="structural",
                    injection_weight=0.7)
        count += 1

    return count


def cmd_retire(args):


    """Scan JSONL files and stage new compact summaries.

    Supports --format to dispatch to format-specific parsers:
      claude (default): scan for isCompactSummary in project JSONL
      gemini: delegate to harvest
      ollama: parse ~/.ollama/sessions/ JSONL
      pentagi: parse ~/.pentagi/sessions/ JSONL
      grok: parse ~/.grok/sessions/<cwd>/<uuid>/chat_history.jsonl
      codex: parse ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

    Plain `retire` (no --format) also auto-sweeps local Grok + Codex sessions
    when those CLIs are installed — they are peers of Claude Code.
    """
    # --all: run all retire paths in sequence
    clean_args = list(args)

    if "--claude-shim" in clean_args:
        clean_args.remove("--claude-shim")
        print("Running Claude session retirement shim (#89)...")
        staged = load_staged()
        all_files = list(PROJECT_DIR.iterdir()) if PROJECT_DIR.exists() else []
        jsonl_files = sorted((f for f in all_files if f.suffix == '.jsonl'), 
                            key=lambda x: x.stat().st_mtime, reverse=True)
        
        batch = jsonl_files[:100]  # last 100 sessions
        print(f"Processing last {len(batch)} sessions...")
        
        conn = init_db()
        new_facts = 0
        for path in batch:
            events = parse_claude_events(path)
            for ev in events:
                # Map to facts.db fields
                # Use signal as fact_text, source as provenance
                fact_text = ev["signal"]
                provenance = ev["source"]
                outcome = ev["outcome"]
                
                # Derive a deterministic key from the signal
                fact_key = hashlib.sha256(f"{path.stem}:{fact_text}".encode()).hexdigest()[:16]
                
                # Assign domain using config-driven DOMAIN_KEYWORDS
                domain = "general"
                _ft_lower = fact_text.lower()
                for _d, _kws in DOMAIN_KEYWORDS.items():
                    if any(_kw in _ft_lower for _kw in _kws):
                        domain = _d
                        break
                
                upsert_fact(conn, domain, fact_key, fact_text,
                            _DEFAULT_PRINCIPAL, path.stem, provenance,
                            outcome=outcome, source="autonomous", fact_type="structural",
                            injection_weight=0.7, score=_seeded_score(fact_text))
                new_facts += 1
        print(f"Imported {new_facts} facts from Claude sessions.")
        return

    if "--all" in clean_args:
        clean_args.remove("--all")
        print("=" * 68)
        print("gaius retire --all")
        print("=" * 68)

        print("\n── Claude ──")
        cmd_retire(clean_args)

        print("\n── Gemini ──")
        try:
            cmd_harvest(clean_args)
        except SystemExit:
            pass

        print("\n── Ollama ──")
        try:
            cmd_ollama_retire(clean_args)
        except SystemExit:
            pass

        print("\n── PentAGI ──")
        try:
            cmd_pentagi_retire(["--parse-only"] + clean_args)
        except SystemExit:
            pass

        print(f"\n{'=' * 68}")
        print("All formats processed. Run `gaius next` to review staged facts.")
        return

    # Format dispatch — non-claude formats use event-based parsers
    fmt_flag = None
    if "--format" in clean_args:
        idx = clean_args.index("--format")
        if idx + 1 < len(clean_args):
            fmt_flag = clean_args[idx + 1]
            clean_args = clean_args[:idx] + clean_args[idx + 2:]

    if fmt_flag and fmt_flag != "claude":
        if fmt_flag == "gemini":
            cmd_harvest(clean_args)
            return
        elif fmt_flag in ("ollama", "vllm"):
            cmd_ollama_retire(clean_args)
            return
        elif fmt_flag == "pentagi":
            cmd_pentagi_retire(clean_args)
            return
        elif fmt_flag == "grok":
            cmd_grok_retire(clean_args)
            return
        elif fmt_flag == "codex":
            cmd_codex_retire(clean_args)
            return
        else:
            print(f"Unknown format: {fmt_flag}. Supported: {', '.join(sorted(SUPPORTED_FORMATS))}", file=sys.stderr)
            sys.exit(1)

    staged = load_staged()
    new_count = skip_count = updated_count = dedup_skip = 0

    # Explicit .jsonl filter — non-JSONL files (tool cache .json, .txt, .jpg) silently skipped
    all_files = list(PROJECT_DIR.iterdir()) if PROJECT_DIR.exists() else []
    jsonl_files = sorted(f for f in all_files if f.suffix == '.jsonl')
    non_jsonl = len(all_files) - len(jsonl_files)
    print(f"Scanning {len(jsonl_files)} JSONL session files in {PROJECT_DIR}...")
    if non_jsonl:
        print(f"  (skipping {non_jsonl} non-JSONL files)")

    conn = init_db()
    # UUID dedup: same session may appear at live path AND archive path (same stem).
    # Build a seen set; only process the first occurrence of each stem.
    seen_stems: set[str] = set()
    dedup_filtered: list[Path] = []
    for f in jsonl_files:
        if f.stem in seen_stems:
            dedup_skip += 1
        else:
            seen_stems.add(f.stem)
            dedup_filtered.append(f)

    compacted_stems: set[str] = set()  # track which sessions have compaction

    for path in dedup_filtered:
        try:
            has_compaction = False
            with open(path) as f:
                for line in f:
                    if "isCompactSummary" not in line:
                        continue
                    entry = json.loads(line)
                    if not entry.get("isCompactSummary"):
                        continue
                    has_compaction = True
                    uuid = entry.get("uuid", "")
                    if not uuid:
                        continue

                    content = entry.get("message", {}).get("content", "")
                    if not content:
                        continue

                    chash = content_hash(content)

                    # Already staged — check if content has changed
                    if uuid in staged:
                        if staged[uuid].get("content_hash") == chash:
                            skip_count += 1
                            continue
                        # Content changed — update the staged entry
                        sections = {
                            key: extract_section(content, header)
                            for key, header in SECTION_HEADERS
                        }
                        staged[uuid]["sections"] = sections
                        staged[uuid]["content_hash"] = chash
                        staged[uuid]["updated_at"] = datetime.now(timezone.utc).isoformat()
                        staged[uuid]["reviewed"] = False  # re-queue for review
                        save_staged(staged[uuid])
                        updated_count += 1
                        # Re-promote updated content to facts.db
                        _promote_mined_to_facts(conn, path.stem, sections)
                        continue

                    sections = {
                        key: extract_section(content, header)
                        for key, header in SECTION_HEADERS
                    }

                    record = {
                        "uuid": uuid,
                        "session_id": path.stem,
                        "timestamp": entry.get("timestamp", ""),
                        "reviewed": False,
                        "sections": sections,
                        "content_hash": chash,
                        "agent_source": "claude",
                        "last_confirmed": entry.get("timestamp", ""),
                    }
                    save_staged(record)
                    staged[uuid] = record
                    new_count += 1
                    # Register in DB for dedup tracking
                    register_session(conn, path.stem, "local", _DEFAULT_PRINCIPAL,
                                     PROJECT_DIR.name, path.stat().st_size,
                                     compaction_present=True)
                    # Auto-promote compacted content to facts.db
                    _promote_mined_to_facts(conn, path.stem, sections)

            if has_compaction:
                compacted_stems.add(path.stem)

        except Exception as e:
            print(f"  warning: {path.name}: {e}", file=sys.stderr)

    # Mine uncompacted sessions for signal
    mined_count = _mine_uncompacted_sessions(conn, staged, compacted_stems, dedup_filtered)

    # Compute TF-IDF scores across all staged entries
    all_entries = list(staged.values())
    if all_entries:
        doc_freq = build_doc_freq(all_entries)
        total_docs = len(all_entries)
        scored_count = 0
        for entry in all_entries:
            score = compute_entry_tfidf_score(entry, doc_freq, total_docs)
            if score != entry.get("score", 0):
                entry["score"] = round(score, 4)
                save_staged(entry)
                scored_count += 1
        update_domain_stats(all_entries)
        print(f"Scored:    {scored_count} entries (TF-IDF)")

    total = len(staged)
    unreviewed = sum(1 for e in staged.values() if not e.get("reviewed"))
    print(f"New:       {new_count}")
    print(f"Mined:     {mined_count} (from uncompacted sessions)")
    print(f"Updated:   {updated_count} (content changed)")
    print(f"Skipped:   {skip_count} (unchanged)")
    print(f"Deduped:   {dedup_skip} (duplicate UUID paths skipped)")
    print(f"Total:     {total}  ({unreviewed} unreviewed)")
    print(f"Staging:   {STAGING_DIR}")

    # Extra session scan — optional second project dir (advisor, relay agent, etc.)
    if EXTRA_SESSIONS_DIR and EXTRA_SESSIONS_DIR.exists():
        extra_staged, extra_distillations, extra_mined = _scan_extra_sessions(conn, staged)
        if extra_staged > 0 or extra_distillations > 0 or extra_mined > 0:
            parts = []
            if extra_staged:
                parts.append(f"{extra_staged} summaries staged")
            if extra_distillations:
                parts.append(f"{extra_distillations} distillation facts upserted")
            if extra_mined:
                parts.append(f"{extra_mined} sessions mined")
            print(f"\nExtra:     {', '.join(parts)} from {EXTRA_SESSIONS_DIR}")

    # Peer coding-agent sessions (Grok, Codex) — first-class local sessions,
    # swept on every retire like Claude. Deduped by session UUID, so re-scans
    # are cheap. Silently skipped for users without these CLIs installed.
    for _pname, _pdir, _pparser, _pdiscover, _psub in (
        ("Grok",  Path.home() / ".grok"  / "sessions", parse_grok_events,  _discover_grok_sessions,  "grok-facts"),
        ("Codex", Path.home() / ".codex" / "sessions", parse_codex_events, _discover_codex_sessions, "codex-facts"),
    ):
        if _pdir.exists():
            _pcount = _retire_event_sessions(_pdir, _pparser, _psub, _pname.lower(),
                                             conn, discover_fn=_pdiscover)
            if _pcount:
                print(f"{_pname + ':':<10} {_pcount} events staged from {_pdir}")


def _scan_extra_sessions(conn: sqlite3.Connection, staged: dict) -> tuple[int, int, int]:
    """Scan an extra session directory for compact summaries, distillations, and mined signal.

    Three scan paths:
    1. Compact summary entries (isCompactSummary=True) — staged for gaius review UI.
    2. Assistant messages — scanned for clarified_intent JSON, upserted directly into
       facts.db as provenance='distillation'. Useful for short sessions that never
       reach context compaction (no compact summaries).
    3. Mining — sessions with no compaction and no distillations get the same
       _mine_session treatment as uncompacted primary sessions.

    Records sessions as agent='extra' in the DB for separate tracking.
    Returns (newly_staged_count, distillation_facts_upserted_count, mined_count).
    """
    if not EXTRA_SESSIONS_DIR or not EXTRA_SESSIONS_DIR.exists():
        return (0, 0, 0)

    jsonl_files = sorted(f for f in EXTRA_SESSIONS_DIR.iterdir() if f.suffix == '.jsonl')
    if not jsonl_files:
        return (0, 0, 0)

    # Check which sessions are already in the sessions table (assistant-message scan dedup)
    already_registered: set[str] = set(
        row[0] for row in conn.execute(
            "SELECT uuid FROM sessions WHERE agent = 'extra'"
        ).fetchall()
    )

    new_count = 0
    distillation_count = 0
    mined_count = 0
    seen_stems: set[str] = set()
    compacted_stems: set[str] = set()
    dedup_filtered: list[Path] = []

    for f in jsonl_files:
        if f.stem in seen_stems:
            continue
        seen_stems.add(f.stem)
        dedup_filtered.append(f)

    for path in dedup_filtered:
        try:
            entries = []
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

            # --- Path 1: compact summary staging ---
            has_compaction = False
            for entry in entries:
                if not entry.get("isCompactSummary"):
                    continue
                has_compaction = True
                uuid = entry.get("uuid", "")
                if not uuid:
                    continue
                content = entry.get("message", {}).get("content", "")
                if not content:
                    continue

                distillations = _extract_clarified_intent(content)
                if distillations:
                    _upsert_distillations(conn, distillations, path.stem)

                if uuid in staged:
                    continue

                chash = content_hash(content)
                sections = {
                    key: extract_section(content, header)
                    for key, header in SECTION_HEADERS
                }
                if distillations:
                    sections["distillations"] = distillations

                record = {
                    "uuid": uuid,
                    "session_id": path.stem,
                    "timestamp": entry.get("timestamp", ""),
                    "reviewed": False,
                    "sections": sections,
                    "content_hash": chash,
                    "agent": "extra",
                }
                save_staged(record)
                staged[uuid] = record
                new_count += 1
                register_session(conn, path.stem, "local", "extra",
                                 EXTRA_SESSIONS_DIR.name, path.stat().st_size,
                                 compaction_present=True)
                already_registered.add(path.stem)

            if has_compaction:
                compacted_stems.add(path.stem)

            # --- Path 2: assistant message distillation scan ---
            # Run for sessions not yet registered (avoids re-scanning on every retire).
            if path.stem not in already_registered:
                session_distillations: list[dict] = []
                for entry in entries:
                    if entry.get("type") != "assistant":
                        continue
                    msg_content = entry.get("message", {}).get("content", [])
                    if isinstance(msg_content, list):
                        for block in msg_content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "")
                                if text:
                                    session_distillations.extend(
                                        _extract_clarified_intent(text)
                                    )
                    elif isinstance(msg_content, str) and msg_content:
                        session_distillations.extend(
                            _extract_clarified_intent(msg_content)
                        )

                if session_distillations:
                    upserted = _upsert_distillations(conn, session_distillations, path.stem)
                    distillation_count += upserted
                    register_session(conn, path.stem, "local", "extra",
                                     EXTRA_SESSIONS_DIR.name, path.stat().st_size,
                                     compaction_present=False)
                    already_registered.add(path.stem)
                else:
                    # No distillations found; register anyway so we skip on next run.
                    register_session(conn, path.stem, "local", "extra",
                                     EXTRA_SESSIONS_DIR.name, path.stat().st_size,
                                     compaction_present=False)
                    already_registered.add(path.stem)

        except Exception as e:
            print(f"  warning (extra-sessions): {path.name}: {e}", file=sys.stderr)

    # --- Path 3: mine uncompacted extra sessions ---
    # Reuse the same _mine_session function used for primary sessions.
    # Only process sessions that had no compaction summary AND are large enough.
    for path in dedup_filtered:
        if path.stem in compacted_stems:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < MINE_MIN_BYTES:
            continue

        mined_uuid = f"mined-extra-{path.stem}"
        if mined_uuid in staged:
            continue

        sections = _mine_session(path)
        if sections is None or not has_signal({"sections": sections}):
            continue

        record = {
            "uuid": mined_uuid,
            "session_id": path.stem,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reviewed": False,
            "sections": sections,
            "content_hash": content_hash(json.dumps(sections, sort_keys=True)),
            "agent": "extra",
            "source": "mined",
        }
        save_staged(record)
        staged[mined_uuid] = record
        mined_count += 1

    return (new_count, distillation_count, mined_count)


def _extract_clarified_intent(content: str) -> list[dict]:
    """Extract clarified_intent JSON blocks from session content.

    A clarifying relay agent produces these via the distillation schema:
      { "objective": ..., "constraints": [...], "open_questions": [...],
        "escalate_if": [...], "context_refs": [...], "continuation_of": ... }

    Handles both single objects and arrays (bundle format for multi-objective sessions).
    Searches for JSON objects/arrays containing 'objective' in code fences or inline.
    Returns list of parsed distillation dicts (may be empty).
    """
    distillations = []

    def _accept(obj):
        """Return list of valid clarified_intent dicts from a parsed JSON value."""
        if isinstance(obj, list):
            results = []
            for item in obj:
                if isinstance(item, dict) and "objective" in item:
                    if any(k in item for k in ("constraints", "open_questions", "escalate_if")):
                        results.append(item)
            return results
        if isinstance(obj, dict) and "objective" in obj:
            if any(k in obj for k in ("constraints", "open_questions", "escalate_if")):
                return [obj]
        return []

    # Match JSON code blocks (```json ... ```) — objects and arrays
    fence_pattern = re.compile(r'```(?:json)?\s*([\[{][^`]+?[\]}])\s*```', re.DOTALL)
    for match in fence_pattern.finditer(content):
        try:
            obj = json.loads(match.group(1))
            distillations.extend(_accept(obj))
        except (json.JSONDecodeError, ValueError):
            pass

    # Also check for bare JSON (not in fences)
    if not distillations:
        bare_pattern = re.compile(r'\{[^{}]*"objective"\s*:[^{}]+\}', re.DOTALL)
        for match in bare_pattern.finditer(content):
            try:
                obj = json.loads(match.group(0))
                distillations.extend(_accept(obj))
            except (json.JSONDecodeError, ValueError):
                pass

    return distillations


def _upsert_distillations(conn: sqlite3.Connection, distillations: list[dict],
                           session_uuid: str) -> int:
    """Upsert clarified_intent blocks from extra sessions into facts.db.

    Each distillation's objective is the primary fact key. Domain is derived from
    context_refs (e.g. "domain/networking.md" → "networking"), falling back to "extra".

    Uses provenance="distillation" (weight 0.85 in maturity scoring) — above
    automated/structured_reasoning, below human_reviewed. These are validated
    strategic outputs from the intent relay, not raw session observations.

    Returns count of upserted distillations.
    """
    count = 0
    for d in distillations:
        objective = d.get("objective", "").strip()
        if not objective:
            continue

        # Derive domain from context_refs: only accept explicit "domain/*.md" refs.
        # Other refs (issue refs, session memory files) are not domain pointers.
        domain = "extra"
        for ref in d.get("context_refs", []):
            if ref.startswith("domain/"):
                candidate = ref[len("domain/"):].replace(".md", "").split("#")[0].strip()
                if candidate and "/" not in candidate:
                    domain = candidate
                    break

        # Build fact_text: objective with constraints summary
        constraints = d.get("constraints", [])
        text_parts = [f"[distillation] {objective}"]
        if constraints:
            text_parts.append("constraints: " + "; ".join(str(c) for c in constraints[:3]))
        fact_text = "\n".join(text_parts)

        fact_key = hashlib.sha256(objective.lower().encode()).hexdigest()[:16]

        upsert_fact(
            conn,
            domain=domain,
            fact_key=fact_key,
            fact_text=fact_text,
            agent="extra",
            session_uuid=session_uuid,
            provenance="distillation",
            score=0.75,
            model_family="claude",
        )
        count += 1

    return count


def cmd_s3_retire(args):
    """Scan session JSONLs from S3/rclone remote and stage new compact summaries.

    Requires s3.remote (and optionally s3.prefix) in ~/.gaius/config.yaml.
    """
    parser = argparse.ArgumentParser(prog="gaius s3-retire")
    parser.add_argument("agent_name", help="Agent name (e.g. gemini-agent)")
    parser.add_argument("--format", type=str, default=None,
                        help="Session format: claude, gemini, ollama (auto-detected from agent name)")
    parser.add_argument("--s3-path", type=str, default=None,
                        help="Override full rclone path (e.g. my-remote:bucket/path/)")
    parsed = parser.parse_args(args)

    agent_name = parsed.agent_name
    fmt = parsed.format or FORMAT_BY_AGENT.get(agent_name, "claude")

    # Verify rclone is available
    try:
        subprocess.run(["rclone", "version"], capture_output=True, check=True)
    except FileNotFoundError:
        print("Error: rclone is not installed. Install it first.", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"Error: rclone failed: {e}", file=sys.stderr)
        sys.exit(1)

    if parsed.s3_path:
        s3_path = parsed.s3_path.rstrip("/") + "/"
    else:
        s3_cfg = _gaius_cfg.get("s3", {})
        remote = s3_cfg.get("remote", "")
        prefix = s3_cfg.get("prefix", "sessions").strip("/")
        if not remote:
            print("Error: s3.remote not set in ~/.gaius/config.yaml\n"
                  "  Add: s3:\n    remote: my-rclone-remote\n    prefix: sessions",
                  file=sys.stderr)
            sys.exit(1)
        # Agent dir root, not <agent>/sessions/ — uploads exist under both
        # sessions/ and projects/ subtrees depending on uploader generation.
        s3_path = f"{remote}:{prefix}/{agent_name}/"
    threshold_bytes = get_session_threshold("cluster", agent_name)
    threshold_mb = threshold_bytes / (1024 * 1024)

    # One bulk rclone copy into a persistent local mirror, then process
    # locally. A per-file size+copyto loop spawns two rclone processes per
    # session — minutes of pure round-trip overhead on high-latency links.
    # The mirror also makes re-runs incremental.
    local_dir = Path.home() / ".gaius" / "s3-sessions" / agent_name
    local_dir.mkdir(parents=True, exist_ok=True)
    print(f"[s3] syncing {s3_path} -> {local_dir} (format: {fmt}, threshold: {threshold_mb:.0f}MB)...", flush=True)
    copy_cmd = ["rclone", "copy", s3_path, str(local_dir),
                "--include", "*.jsonl", "--transfers", "8",
                "--retries", "3", "--low-level-retries", "3",
                "--timeout", "90s", "--contimeout", "30s",
                "--log-level", "ERROR"]
    if threshold_bytes > 0:
        copy_cmd += ["--min-size", f"{threshold_bytes}B"]
    copy_result = subprocess.run(copy_cmd, capture_output=True, text=True)
    if copy_result.returncode != 0:
        # Degraded objects (e.g. ghost chunks after a volume loss) must not
        # abort the sweep — mine whatever synced. Hard-fail only when
        # nothing is available locally at all.
        stderr = copy_result.stderr.strip() if copy_result.stderr else ""
        print(f"warning: rclone copy from {s3_path} incomplete — mining what synced: {stderr}",
              file=sys.stderr)

    local_files = sorted(local_dir.rglob("*.jsonl"))
    if not local_files:
        if copy_result.returncode != 0:
            print(f"Error: rclone copy from {s3_path} failed and no local mirror exists",
                  file=sys.stderr)
            sys.exit(1)
        print(f"[s3] no .jsonl files found in {s3_path}")
        return

    print(f"[s3] found {len(local_files)} session file(s)")

    staged = load_staged()
    # Build set of existing content hashes for dedup
    existing_hashes = {e.get("content_hash") for e in staged.values() if e.get("content_hash")}

    new_count = 0
    skip_count = 0

    for local_file in local_files:
        try:
            # Format-aware dispatch: event-based formats use their parsers
            if fmt in ("gemini", "ollama", "pentagi"):
                parser_map = {
                    "gemini":  parse_gemini_events,
                    "ollama":  parse_ollama_events,
                    "pentagi": parse_pentagi_flow_from_jsonl,
                }
                agent_map = {
                    "gemini":  "gemini",
                    "ollama":  "ollama",
                    "pentagi": "pentagi",
                }
                staging_map = {
                    "gemini":  "gemini-facts",
                    "ollama":  "ollama-facts",
                    "pentagi": "pentagi-facts",
                }
                session_id = local_file.stem
                conn_s3 = init_db()
                existing_s3 = conn_s3.execute(
                    "SELECT uuid FROM sessions WHERE uuid = ?", (session_id,)
                ).fetchone()
                if existing_s3:
                    skip_count += 1
                    continue

                events = parser_map[fmt](local_file)
                if events:
                    for ev in events:
                        text = " ".join(filter(None, [
                            ev.get("subject", ""), ev.get("description", ""),
                            ev.get("output", ""), str(ev.get("tool", "")),
                        ]))
                        domains = tag_domains_from_specs(text, load_domain_specs())
                        ev["domain"] = domains[0] if domains else "general"

                    staging_dir = STAGING_DIR / staging_map[fmt]
                    staging_dir.mkdir(parents=True, exist_ok=True)
                    out_path = staging_dir / f"{session_id}.jsonl"
                    with open(out_path, "w") as outf:
                        for ev in events:
                            outf.write(json.dumps(ev) + "\n")

                    register_session(conn_s3, session_id, f"s3:{agent_name}",
                                     agent_map[fmt], "cluster",
                                     local_file.stat().st_size)
                    new_count += len(events)
                    print(f"  {session_id}: {len(events)} events staged")
                continue

            # Claude format: scan for isCompactSummary
            with open(local_file) as f:
                for line in f:
                    if "isCompactSummary" not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not entry.get("isCompactSummary"):
                        continue

                    uuid = entry.get("uuid", "")
                    if not uuid:
                        continue

                    content = entry.get("message", {}).get("content", "")
                    if not content:
                        continue

                    chash = content_hash(content)

                    # Skip if already staged with same content
                    if uuid in staged and staged[uuid].get("content_hash") == chash:
                        skip_count += 1
                        continue
                    if chash in existing_hashes:
                        skip_count += 1
                        continue

                    sections = {
                        key: extract_section(content, header)
                        for key, header in SECTION_HEADERS
                    }

                    record = {
                        "uuid": uuid,
                        "session_id": local_file.stem,
                        "timestamp": entry.get("timestamp", ""),
                        "reviewed": False,
                        "sections": sections,
                        "content_hash": chash,
                        "source": f"s3:{agent_name}",
                        "format": fmt,
                    }
                    save_staged(record)
                    staged[uuid] = record
                    existing_hashes.add(chash)
                    new_count += 1

        except Exception as e:
            print(f"  warning: failed to process {local_file.name}: {e}", file=sys.stderr)

    print(f"[s3] processed {new_count} new sessions for {agent_name} ({skip_count} skipped)")


SKILL_STALE_DAYS = 90  # flag skills not touched in git for this many days


def get_skill_git_date(skill_path: Path) -> str | None:
    """Return last git commit date for a skill file as 'YYYY-MM-DD', or None."""
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%ai", "--", str(skill_path.name)],
            capture_output=True, text=True, cwd=str(skill_path.parent), timeout=5,
        )
        date = r.stdout.strip()[:10]
        return date if len(date) == 10 else None
    except Exception:
        return None


def load_skills(domain_filter=None):
    """Load skill files from SKILLS_DIR.

    Returns list of dicts:
      {name, fm, body, full_text, tokens, domain, gate, also_load, git_date, is_stale}
    body is stored separately for scoring. also_load lists dependency skill names.
    git_date is the last commit date; is_stale flags files unchanged for SKILL_STALE_DAYS.
    """
    if not SKILLS_DIR.is_dir():
        return []

    now_ts = datetime.now(timezone.utc)
    skills = []
    for p in sorted(SKILLS_DIR.glob("*.md")):
        try:
            text = p.read_text()
        except Exception:
            continue
        fm, body = _parse_frontmatter(text)

        git_date = get_skill_git_date(p)
        is_stale = False
        if git_date:
            try:
                age = (now_ts - datetime.fromisoformat(git_date + "T00:00:00+00:00")).days
                is_stale = age >= SKILL_STALE_DAYS
            except Exception:
                pass

        also_load_raw = fm.get("also_load", [])
        if isinstance(also_load_raw, str):
            also_load_raw = [s.strip() for s in also_load_raw.split(",") if s.strip()]

        skills.append({
            "name":      p.stem,
            "fm":        fm,
            "body":      body,
            "full_text": text,
            "tokens":    estimate_tokens(text),
            "domain":    fm.get("domain", ""),
            "gate":      fm.get("gate", "reference"),
            "also_load": also_load_raw,
            "git_date":  git_date or "unknown",
            "is_stale":  is_stale,
            "path":      p,
        })

    return skills


def compute_skill_score(skill: dict, context_terms: set) -> float:
    """Score a skill against context terms. Returns score-per-token (density).

    Scoring:
    - gate:always → float('inf') sentinel; injected outside budget unconditionally
    - gate:mandate and gate:hard → floor score + 1.5x multiplier (always beats reference)
    - gate:reference → score 0 if no context terms (excluded when no signal)
    - Frontmatter signal (trigger + description + domain) weighted 3x body keywords
    - Returns score-per-token so dense high-signal skills beat long diffuse ones
    """
    if skill["gate"] == "always":
        return float("inf")

    is_hard = skill["gate"] in ("hard", "mandate")

    if not context_terms:
        # No context signal — only inject hard gates, everything else excluded
        raw = 0.5 if is_hard else 0.0
        return (raw * 1.5 if is_hard else 0.0) / skill["tokens"]

    # Build term sets from frontmatter (high signal) and body (low signal)
    def _terms(text: str) -> set:
        return set(re.sub(r'[^\w\s]', ' ', text.lower()).split())

    fm_signal = (
        _terms(skill["fm"].get("trigger", ""))
        | _terms(skill["fm"].get("description", ""))
        | _terms(skill["domain"].replace("-", " "))
    )
    body_signal = _terms(skill["body"])

    overlap_fm   = len(context_terms & fm_signal)
    overlap_body = len(context_terms & body_signal)

    score = (overlap_fm * 3.0) + (overlap_body * 0.5)

    # Hard gate floor — injected even with weak context match
    if is_hard:
        score = max(score, 0.5)
        score *= 1.5

    return score / skill["tokens"] if skill["tokens"] > 0 else 0.0


# ── Landscape Protocol → extracted to gaius/landscape.py (facade re-import below) ──
def cmd_index(args):
    """Parse JSONL, build domain index, write deltas and corpus."""
    parser = argparse.ArgumentParser(prog="gaius index")
    parser.add_argument("session_id", nargs="?", help="Session ID prefix to index")
    parser.add_argument("--threshold-mb", type=int, default=0, help="Min size in MB to index (default: 0)")
    parser.add_argument("--sample-rate", type=float, default=0.25, help="Sample rate for low-signal entries (default: 0.25)")
    parser.add_argument("--no-archive", action="store_true", help="Skip S3 archival (faster, local-only)")
    parsed_args = parser.parse_args(args)

    # 1. Load index of already indexed sessions
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    index_path = CORPUS_DIR / "index.jsonl"
    indexed_sessions = set()
    if index_path.exists():
        with open(index_path, "r") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if "session_id" in d:
                        indexed_sessions.add(d["session_id"])
                except Exception:
                    pass

    # 2. Identify sessions to process
    jsonl_files = sorted(PROJECT_DIR.glob("*.jsonl"))
    targets = []
    if parsed_args.session_id:
        targets = [f for f in jsonl_files if f.stem.startswith(parsed_args.session_id)]
        if not targets:
            print(f"No session matching {parsed_args.session_id} found in {PROJECT_DIR}", file=sys.stderr)
            sys.exit(1)
    else:
        for f in jsonl_files:
            if f.stem in indexed_sessions:
                continue
            if parsed_args.threshold_mb > 0:
                size_mb = f.stat().st_size / (1024 * 1024)
                if size_mb < parsed_args.threshold_mb:
                    continue
            targets.append(f)

    if not targets:
        print("No new sessions to index.")
        return

    print(f"Indexing {len(targets)} sessions...")

    for path in targets:
        process_session(path, parsed_args.sample_rate, index_path,
                        archive=not parsed_args.no_archive)


def process_session(path, sample_rate, index_path, archive=True):
    session_id = path.stem
    print(f"🚀 Processing {session_id} ({path.stat().st_size / (1024*1024):.1f} MB)...")

    total_entries = 0
    corpus_entries = 0
    domain_counts = {}
    domain_deltas = {}  # {domain: [lines]}
    procedures = []     # extracted procedure dicts

    corpus_subdir = CORPUS_DIR / datetime.now().strftime("%Y-%m")
    corpus_subdir.mkdir(parents=True, exist_ok=True)
    corpus_path = corpus_subdir / f"{session_id}.jsonl"

    with open(path, "r") as f, open(_guard_write_path(corpus_path), "w") as out_f:
        for line in f:
            total_entries += 1
            try:
                entry = json.loads(line)
            except Exception:
                continue

            uuid = entry.get("uuid", "")
            timestamp = entry.get("timestamp", "")

            # Classification and Scoring
            etype, base_score = classify_entry(entry)

            # Extract text for scoring and tagging
            text = ""
            if etype == "compaction_summary":
                text = entry.get("message", {}).get("content", "")
            elif etype == "assistant_reasoning" or etype == "user_instruction":
                content_list = entry.get("message", {}).get("content", [])
                text = " ".join(c.get("text", "") for c in content_list if c.get("type") == "text")
            elif etype.startswith("tool_result"):
                text = str(entry.get("content", ""))

            score = boost_score(text, base_score)
            etype, score = classify_finding(text, etype, score)
            etype, score = classify_procedure(text, etype, score)
            domains = tag_domains(text)

            # Procedure Extraction
            if etype == "procedure":
                proc = extract_procedure(text)
                if proc:
                    procedures.append(proc)

            # Domain Delta Extraction
            if etype == "compaction_summary":
                # Handle summary sections
                for key, header in SECTION_HEADERS:
                    section_text = extract_section(text, header)
                    if not section_text:
                        continue
                    lines_by_domain = extract_delta_lines(section_text, domains)
                    for dom, dlines in lines_by_domain.items():
                        if dom not in domain_deltas:
                            domain_deltas[dom] = []
                        for dl in dlines:
                            domain_deltas[dom].append(f"- **[{key}]** {dl}")
            elif score >= SIGNAL_THRESHOLD and etype == "assistant_reasoning":
                # Extract lines with decision keywords
                lines = text.splitlines()
                matching_lines = [l.strip() for l in lines if any(kw in l.lower() for kw in DECISION_KEYWORDS)]
                if matching_lines:
                    date_str = timestamp[:10] if timestamp else datetime.now().strftime("%Y-%m-%d")
                    for dom in domains:
                        if dom not in domain_deltas:
                            domain_deltas[dom] = []
                        for ml in matching_lines:
                            domain_deltas[dom].append(f"- [{date_str}] {ml} (session: {uuid[:8]})")

            # Update stats
            for dom in domains:
                domain_counts[dom] = domain_counts.get(dom, 0) + 1

            # Corpus Sampling
            included = (score >= SIGNAL_THRESHOLD) or sample_entry(uuid, sample_rate)
            if included:
                corpus_entries += 1
                record = {
                    "session_id": session_id,
                    "uuid": uuid,
                    "timestamp": timestamp,
                    "entry_type": etype,
                    "signal_score": round(score, 3),
                    "domains": domains,
                    "included": True,
                    "content": strip_bloat(entry)
                }
                out_f.write(json.dumps(record) + "\n")

    # Write Domain Deltas
    write_domain_deltas(session_id, domain_deltas)

    # Write Procedure Deltas
    write_procedure_deltas(session_id, procedures)

    # S3 Archival
    archived_to = archive_session(path) if archive else None

    # Update Index
    summary = {
        "session_id": session_id,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "total_entries": total_entries,
        "corpus_entries": corpus_entries,
        "domain_counts": domain_counts,
        "archived_to": archived_to
    }
    with open(_guard_write_path(index_path), "a") as f:
        f.write(json.dumps(summary) + "\n")

    print(f"  ✅ Indexed: {corpus_entries}/{total_entries} entries in corpus. Deltas written to {len(domain_deltas)} domains.")


def write_domain_deltas(session_id, deltas):
    """Write domain deltas to corpus/deltas/ — NOT to human-maintained domain/*.md files.

    Domain files are hand-curated gotchas/incidents. Raw index extracts belong in
    corpus/deltas/{domain}/{session_id[:8]}.md for gaius inject consumption only.
    """
    if not deltas:
        return
    date_str = datetime.now().strftime("%Y-%m-%d")
    delta_root = CORPUS_DIR / "deltas"
    delta_root.mkdir(parents=True, exist_ok=True)

    for domain, lines in deltas.items():
        if not lines:
            continue
        domain_delta_dir = delta_root / domain
        domain_delta_dir.mkdir(parents=True, exist_ok=True)
        delta_file = domain_delta_dir / f"{session_id[:8]}.md"
        with open(_guard_write_path(delta_file), "w") as f:
            f.write(f"# Delta: {domain} / {session_id[:8]} ({date_str})\n\n")
            for line in lines:
                f.write(f"{line}\n")
        print(f"  📝 Delta → corpus/deltas/{domain}/{session_id[:8]}.md")


def write_procedure_deltas(session_id, procedures):
    """Write extracted procedures to corpus/deltas/troubleshooting/ — NOT troubleshooting.md.

    troubleshooting.md is hand-curated. Raw extracted procedures go to corpus/deltas/
    for gaius inject consumption only.
    """
    if not procedures:
        return
    delta_dir = CORPUS_DIR / "deltas" / "troubleshooting"
    delta_dir.mkdir(parents=True, exist_ok=True)
    delta_file = delta_dir / f"{session_id[:8]}.md"
    date_str = datetime.now().strftime("%Y-%m-%d")

    with open(_guard_write_path(delta_file), "w") as f:
        for proc in procedures:
            f.write(f"\n## {proc['trigger']}\n\n")
            f.write(f"**Symptom**: {proc['trigger']}\n\n")
            for i, step in enumerate(proc["steps"], 1):
                f.write(f"{i}. {step}\n")
            f.write(f"\n**Resolution**: {proc.get('resolution', 'See final step above')}\n")
            if not proc.get("complete"):
                f.write(f"**Status**: Incomplete — no clear resolution identified\n")
            f.write(f"\n*Extracted from session {session_id[:8]} on {date_str}*\n")
    print(f"  📋 Procedures → corpus/deltas/troubleshooting/{session_id[:8]}.md")


def archive_session(path, strip_before_archive=True):
    """Archive a session JSONL to S3/rclone remote. Requires s3.remote in config."""
    s3_cfg = _gaius_cfg.get("s3", {})
    remote = s3_cfg.get("remote", "")
    prefix = s3_cfg.get("prefix", "sessions").strip("/")
    if not remote:
        print("  ⚠️  s3.remote not set in config — skipping archive", file=sys.stderr)
        return None

    project = PROJECT_DIR.name
    month = datetime.now().strftime("%Y-%m")
    target = f"{remote}:{prefix}/local/{project}/archive/{month}/{path.name}"

    upload_path = str(path)
    tmp_path = None

    if strip_before_archive:
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
                tmp_path = tmp.name
                with open(path) as src:
                    for line in src:
                        try:
                            entry = json.loads(line)
                            stripped = strip_bloat(entry)
                            tmp.write(json.dumps(stripped) + "\n")
                        except json.JSONDecodeError:
                            tmp.write(line)
            upload_path = tmp_path
        except Exception as e:
            print(f"  ⚠️  Bloat strip failed, archiving raw: {e}", file=sys.stderr)
            upload_path = str(path)
            tmp_path = None

    try:
        subprocess.run([
            "rclone", "copyto", upload_path,
            target,
            "--s3-upload-cutoff", "200M", "--s3-disable-checksum",
            "--retries", "3", "--log-level", "INFO"
        ], check=True, capture_output=True)
        return target
    except Exception as e:
        print(f"  ⚠️  S3 archive failed: {e}", file=sys.stderr)
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def cmd_harvest(args):
    """Scan cold Gemini CLI sessions, extract events, stage for review.

    Gemini sessions are .json files (single object, not line-delimited).
    A session is 'cold' if lastModified > GEMINI_COLD_THRESHOLD_HOURS ago.
    Events are grouped by domain keyword and written to staging/gemini-facts/.
    """
    parser = argparse.ArgumentParser(prog="gaius harvest")
    parser.add_argument("--gemini-dir", type=str, default=None,
                        help=f"Gemini sessions directory (default: {GEMINI_DIR})")
    parser.add_argument("--threshold-hours", type=float, default=GEMINI_COLD_THRESHOLD_HOURS,
                        help=f"Cold threshold in hours (default: {GEMINI_COLD_THRESHOLD_HOURS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be harvested without writing anything")
    parsed_args = parser.parse_args(args)

    gemini_dir = Path(parsed_args.gemini_dir) if parsed_args.gemini_dir else GEMINI_DIR
    if not gemini_dir.exists():
        print(f"Gemini sessions directory not found: {gemini_dir}", file=sys.stderr)
        sys.exit(1)

    conn = init_db()
    domain_specs = load_domain_specs()

    # Find all .json files recursively (Gemini CLI session files)
    all_json = list(gemini_dir.rglob("*.json"))
    # Filter to cold sessions only
    cold = [p for p in all_json if is_gemini_cold(p, parsed_args.threshold_hours)]
    # Get already-processed session UUIDs from DB
    processed_uuids = {
        row[0] for row in conn.execute("SELECT uuid FROM sessions").fetchall()
    }

    print(f"Gemini dir:   {gemini_dir}")
    print(f"Total .json:  {len(all_json)}")
    print(f"Cold (>{parsed_args.threshold_hours}h): {len(cold)}")

    new_count = skip_count = event_count = 0
    gemini_staging = STAGING_DIR / "gemini-facts"
    if not parsed_args.dry_run:
        gemini_staging.mkdir(parents=True, exist_ok=True)

    for path in sorted(cold):
        # Peek at sessionId for dedup check
        try:
            with open(path) as f:
                first_chunk = f.read(256)
            # Quick extract sessionId without full parse
            import re as _re
            sid_match = _re.search(r'"sessionId"\s*:\s*"([^"]+)"', first_chunk)
            session_uuid = sid_match.group(1) if sid_match else path.stem
        except Exception:
            session_uuid = path.stem

        if session_uuid in processed_uuids:
            skip_count += 1
            continue

        events = parse_gemini_events(path)
        if not events:
            skip_count += 1
            continue

        # Tag events with domain keywords
        domain_groups: dict[str, list[dict]] = {}
        for ev in events:
            ev_text = " ".join([
                ev.get("subject", ""),
                ev.get("description", ""),
                ev.get("tool", ""),
                ev.get("output", "") or "",
            ])
            domains = tag_domains_from_specs(ev_text, domain_specs)
            if not domains:
                domains = ["general"]
            for dom in domains:
                domain_groups.setdefault(dom, []).append(ev)

        if parsed_args.dry_run:
            print(f"\n  [dry-run] {path.name} ({session_uuid[:8]})")
            for dom, evs in sorted(domain_groups.items()):
                print(f"    {dom}: {len(evs)} events")
            event_count += len(events)
            new_count += 1
            continue

        # Write staged facts file: one per session
        staged_file = gemini_staging / f"{session_uuid[:8]}_{path.stem[-8:]}.jsonl"
        with open(staged_file, "w") as f:
            for dom, evs in sorted(domain_groups.items()):
                for ev in evs:
                    record = {"session_uuid": session_uuid, "domain": dom, **ev}
                    f.write(json.dumps(record) + "\n")

        # Register session in DB (dedup key)
        register_session(conn, session_uuid, "cluster", "gemini",
                         path.parent.name, path.stat().st_size)
        processed_uuids.add(session_uuid)
        event_count += len(events)
        new_count += 1
        print(f"  harvested {path.name}: {len(events)} events → {staged_file.name}")

    print(f"\nNew:          {new_count}")
    print(f"Skipped:      {skip_count} (already processed or empty)")
    print(f"Events:       {event_count}")
    if not parsed_args.dry_run and new_count:
        print(f"Staged:       {gemini_staging}")
        print(f"\nNext:  gaius next   (topic-grouped review → promote staged facts to facts.db)")


def cmd_ansible(args):
    """Scan Ansible inventory and manifests, extract operational facts."""
    parser = argparse.ArgumentParser(prog="gaius ansible")
    parser.add_argument("--path", type=str, default=str(Path.home() / "ansible"),
                        help="Path to ansible repo root (default: ~/ansible)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be extracted without writing to facts.db")
    parser.add_argument("--max-chars", type=int, default=50000,
                        help="Maximum total chars of extracted facts (default: 50000)")
    parsed_args = parser.parse_args(args)

    if not HAS_YAML:
        print("Error: ansible source requires PyYAML: pip install pyyaml", file=sys.stderr)
        sys.exit(1)

    ansible_root = Path(parsed_args.path).expanduser()
    if not ansible_root.exists():
        print(f"Error: path not found: {ansible_root}", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning Ansible repo: {ansible_root}")
    conn = init_db()
    extracted_chars = 0
    fact_count = 0

    def extract_from_yaml(path: Path, domain: str = "infra"):
        nonlocal extracted_chars, fact_count
        if extracted_chars >= parsed_args.max_chars:
            return

        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except Exception as e:
            print(f"  warning: could not parse {path.relative_to(ansible_root)}: {e}")
            return

        if not isinstance(data, (dict, list)):
            return

        # Simplified extraction logic: convert to string, filter secrets/templates
        def process_node(node, prefix=""):
            nonlocal extracted_chars, fact_count
            if extracted_chars >= parsed_args.max_chars:
                return

            if isinstance(node, dict):
                for k, v in node.items():
                    if SECRET_KEYS_RE.search(str(k)):
                        continue
                    process_node(v, f"{prefix}{k}: ")
            elif isinstance(node, list):
                for item in node:
                    process_node(item, prefix)
            else:
                val = str(node)
                if "{{" in val:  # Skip templates
                    return

                fact_text = f"{prefix}{val}"
                if len(fact_text) > 500:  # Cap individual fact length
                    fact_text = fact_text[:500] + "..."

                if not parsed_args.dry_run:
                    # Auto-tag domain (best-match; fall back to the section domain)
                    _doms = tag_domains_from_specs(fact_text, load_domain_specs())
                    derived_domain = _doms[0] if _doms else domain
                    # Distillation pattern: SHA256 first 16 chars
                    fk = hashlib.sha256(fact_text.lower().encode()).hexdigest()[:16]

                    upsert_fact(
                        conn,
                        domain=derived_domain,
                        fact_key=fk,
                        fact_text=fact_text,
                        agent="gaius-ansible",
                        provenance="ansible",
                        score=0.7,
                        model_family="human",
                    )
                else:
                    print(f"  [dry-run] {fact_text}")

                extracted_chars += len(fact_text)
                fact_count += 1

        process_node(data)

    # 1. Inventory: hosts.yml
    hosts_path = ansible_root / "inventory" / "hosts.yml"
    if hosts_path.exists():
        extract_from_yaml(hosts_path, domain="infra")

    # 2. Group Vars
    gv_dir = ansible_root / "inventory" / "group_vars"
    if gv_dir.exists():
        for yml in sorted(gv_dir.glob("*.yml")):
            if yml.name == "vault.yml":
                continue  # Skip encrypted vault
            domain_map = {
                "storage.yml": "storage",
                "k3s_cluster.yml": "infra",
                "all.yml": "infra",
            }
            extract_from_yaml(yml, domain=domain_map.get(yml.name, "infra"))

    # 3. Playbooks (summaries)
    pb_dir = ansible_root / "playbooks"
    if pb_dir.exists():
        for yml in sorted(pb_dir.glob("*.yml")):
            # Just extract the 'name' and purpose
            try:
                with open(yml) as f:
                    content = f.read()
                    # Look for the first 'name:' in the playbook
                    match = re.search(r'^\s*-\s*name:\s*(.*)$', content, re.MULTILINE)
                    if match:
                        purpose = match.group(1).strip()
                        fact_text = f"Playbook {yml.name}: {purpose}"
                        if not parsed_args.dry_run:
                            fk = hashlib.sha256(fact_text.lower().encode()).hexdigest()[:16]
                            upsert_fact(
                                conn,
                                domain="infra",
                                fact_key=fk,
                                fact_text=fact_text,
                                agent="gaius-ansible",
                                provenance="ansible",
                                score=0.75,
                                model_family="human",
                            )
                            fact_count += 1
                        else:
                            print(f"  [dry-run] {fact_text}")
            except Exception:
                pass

    if not parsed_args.dry_run:
        conn.commit()
    print(f"Extracted {fact_count} facts ({extracted_chars} chars) from Ansible.")


def cmd_aliases(args):
    """Scan shell alias files, extract operational cluster facts."""
    parser = argparse.ArgumentParser(prog="gaius aliases")
    parser.add_argument("--path", type=str, default=str(Path.home() / ".aliases"),
                        help="Path to aliases file (default: ~/.aliases)")
    parser.add_argument("--dry-run", action="store_true")
    parsed_args = parser.parse_args(args)

    alias_path = Path(parsed_args.path).expanduser()
    if not alias_path.exists():
        # Fallback to .bashrc if .aliases not found
        alias_path = Path.home() / ".bashrc"

    if not alias_path.exists():
        print(f"Error: alias source not found: {alias_path}", file=sys.stderr)
        return

    print(f"Scanning Aliases: {alias_path}")
    conn = init_db()
    fact_count = 0

    def parse_file(path: Path, visited=None):
        nonlocal fact_count
        if visited is None:
            visited = set()
        if path in visited:
            return
        visited.add(path)

        try:
            with open(path) as f:
                lines = f.readlines()
        except Exception:
            return

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Handle source directives
            source_match = re.match(r'^(source|\.)\s+(.*)$', line)
            if source_match:
                sub_path_str = source_match.group(2).replace("$HOME", str(Path.home())).replace("~", str(Path.home()))
                sub_path = Path(sub_path_str).expanduser()
                if not sub_path.is_absolute():
                    sub_path = path.parent / sub_path
                parse_file(sub_path, visited)
                continue

            # Handle alias name='command'
            alias_match = re.match(r'^alias\s+([^=]+)=[\'"]?([^\'"]+)[\'"]?$', line)
            if alias_match:
                name = alias_match.group(1).strip()
                cmd = alias_match.group(2).strip()

                if name in ALIAS_BLOCKLIST:
                    continue

                fact_text = f"Alias '{name}' executes: {cmd}"

                # Special IP extraction from ping aliases or similar
                ip_match = re.search(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', cmd)
                if ip_match:
                    fact_text += f" (IP: {ip_match.group(0)})"

                if not parsed_args.dry_run:
                    fk = hashlib.sha256(fact_text.lower().encode()).hexdigest()[:16]
                    _doms = tag_domains_from_specs(fact_text, load_domain_specs())
                    upsert_fact(
                        conn,
                        domain=_doms[0] if _doms else "general",
                        fact_key=fk,
                        fact_text=fact_text,
                        agent="gaius-aliases",
                        provenance="aliases",
                        score=0.65,
                        model_family="human",
                    )
                    fact_count += 1
                else:
                    print(f"  [dry-run] {fact_text}")
                continue

            # Handle simple functions: name() { ... }
            func_match = re.match(r'^([a-zA-Z0-9_-]+)\s*\(\)\s*\{', line)
            if func_match:
                name = func_match.group(1)
                if name in ALIAS_BLOCKLIST:
                    continue

                fact_text = f"Function '{name}' is defined in {path.name}"
                if not parsed_args.dry_run:
                    fk = hashlib.sha256(fact_text.lower().encode()).hexdigest()[:16]
                    upsert_fact(
                        conn,
                        domain="general",
                        fact_key=fk,
                        fact_text=fact_text,
                        agent="gaius-aliases",
                        provenance="aliases",
                        score=0.6,
                        model_family="human",
                    )
                    fact_count += 1
                else:
                    print(f"  [dry-run] {fact_text}")

    parse_file(alias_path)

    # Also check a gen_corpus.py alongside the alias file (optional, user-specific)
    corpus_gen = alias_path.parent / "gen_corpus.py"
    if corpus_gen.exists():
        try:
            with open(corpus_gen) as f:
                content = f.read()
                # Extract qa("...", "...") pairs in the ALIASES section
                alias_section = re.search(r'# ALIASES.*?(?=# PLAYBOOK|$)', content, re.DOTALL)
                if alias_section:
                    qa_pairs = re.findall(r'qa\("(.*?)",\s*"(.*?)"\)', alias_section.group(0), re.DOTALL)
                    for q, a in qa_pairs:
                        # Clean up strings
                        q = q.replace('\\"', '"').strip()
                        a = a.replace('\\"', '"').strip()
                        fact_text = f"Fact: {q} Answer: {a}"
                        if not parsed_args.dry_run:
                            fk = hashlib.sha256(fact_text.lower().encode()).hexdigest()[:16]
                            upsert_fact(
                                conn,
                                domain="infra",
                                fact_key=fk,
                                fact_text=fact_text,
                                agent="gaius-aliases",
                                provenance="aliases",
                                score=0.8,  # Very high confidence from documented corpus
                                model_family="human",
                            )
                            fact_count += 1
                        else:
                            print(f"  [dry-run] {fact_text}")
        except Exception:
            pass

    if not parsed_args.dry_run:
        conn.commit()
    print(f"Extracted {fact_count} facts from Aliases.")


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


def cmd_show(args):
    """List all staged summaries, unreviewed first."""
    staged = load_staged()
    entries = sorted(staged.values(), key=lambda e: e.get("timestamp", ""))

    unreviewed = [e for e in entries if not e.get("reviewed")]
    reviewed   = [e for e in entries if     e.get("reviewed")]

    # Signal = has key_concepts or errors_fixes
    sig_unrev = [e for e in unreviewed if has_signal(e)]

    print(f"Total: {len(entries)}  |  Unreviewed: {len(unreviewed)}  |  "
          f"With signal: {len(sig_unrev)}  |  Reviewed: {len(reviewed)}")
    print()

    if unreviewed:
        print("── UNREVIEWED ──────────────────────────────────────────────────")
        for e in unreviewed:
            ts  = e.get("timestamp", "")[:10]
            sid = e.get("session_id", "?")[:8]
            uid = e.get("uuid", "?")[:8]
            sig = "★" if has_signal(e) else " "
            sections_present = [k.split("_")[0][0].upper()
                                 for k in SIGNAL_SECTIONS if e["sections"].get(k)]
            tags = "".join(sections_present) if sections_present else "-"
            print(f"  {sig} {uid}  {ts}  session:{sid}  [{tags}]")
        print()
        print("  ★ = has key concepts / errors / pending tasks")
        print("  Tags: K=key_concepts  E=errors_fixes  P=pending_tasks")

    if reviewed:
        print(f"\n── REVIEWED ({len(reviewed)}) "
              "──────────────────────────────────────────────")
        for e in reviewed[-5:]:
            ts  = e.get("timestamp", "")[:10]
            uid = e.get("uuid", "?")[:8]
            print(f"    {uid}  {ts}  ✓")
        if len(reviewed) > 5:
            print(f"    ... and {len(reviewed)-5} more")


def _promote_event(conn: sqlite3.Connection, ev: dict, outcome: str = None) -> None:
    """Build fact_text from a staged event and upsert into facts.db.

    Works for all format types — reads model_family/model_version from the event
    dict rather than hardcoding. Falls back to gemini defaults for backward compat.
    """
    ev_type = ev.get("type", "discovery")
    if ev_type == "decision":
        subject = ev.get("subject", "").strip()
        description = ev.get("description", "").strip()
        fact_text = f"[decision] {subject}: {description}" if description else f"[decision] {subject}"
    else:
        tool = ev.get("tool", "unknown")
        output = (ev.get("output") or "")[:300].strip()
        fact_text = f"[tool:{tool}] {output}" if output else f"[tool:{tool}]"

    final_outcome = outcome if outcome is not None else ev.get("outcome")
    upsert_fact(
        conn,
        domain=ev.get("domain", "general"),
        fact_key=ev.get("fact_key", hashlib.sha256(fact_text.encode()).hexdigest()[:16]),
        fact_text=fact_text,
        agent=ev.get("agent", "gemini"),
        session_uuid=ev.get("session_uuid", ""),
        provenance=ev.get("provenance", "automated"),
        score=0.6 if ev_type == "decision" else 0.4,
        outcome=final_outcome,
        model_family=ev.get("model_family", "gemini"),
        model_version=ev.get("model_version", ""),
        source=ev.get("source", "human"),
    )


def _rewrite_staging(all_events_by_file: dict, promoted_keys: set) -> int:
    """Rewrite staged gemini-facts files, removing promoted events.

    Args:
        all_events_by_file: {Path: [event_dict, ...]}
        promoted_keys: set of fact_key values that were promoted

    Returns:
        Number of files deleted (were empty after removing promoted events).
    """
    deleted = 0
    for staged_path, events in all_events_by_file.items():
        remaining = [ev for ev in events if ev.get("fact_key") not in promoted_keys]
        if not remaining:
            staged_path.unlink(missing_ok=True)
            deleted += 1
        else:
            with open(staged_path, "w") as f:
                for ev in remaining:
                    f.write(json.dumps(ev) + "\n")
    return deleted


def cmd_next_staged_facts(conn: sqlite3.Connection, staging_dir: Path, label: str) -> None:
    """Topic-grouped review UI for staged event-based facts (any format).

    Thin wrapper — delegates to the review loop with a parameterized staging dir.
    """
    cmd_next_gemini(conn, staging_dir=staging_dir, label=label)


def cmd_next_gemini(conn: sqlite3.Connection, staging_dir: Path = None,
                    label: str = "gemini-facts") -> None:
    """Topic-grouped review UI for staged facts.

    Presents staged events clustered by domain. Reviewer approves or rejects
    at cluster level. Individual review mode available per domain.

    Flow:
      [Y] approve all in cluster → promote with outcome=null
      [n] skip cluster → events stay staged for next review
      [r] review individually → k/c/x/?/s per event
      [q] quit → rewrite files, print summary

    Individual review keys:
      k = keep (promote, outcome=null)
      c = confirmed (promote, outcome='confirmed')
      x = refuted   (promote, outcome='refuted')
      ? = open_question (promote, outcome='open_question')
      s = skip (leave staged)
      q = quit individual mode (remaining in cluster auto-skipped)
    """
    gemini_staging = staging_dir or (STAGING_DIR / "gemini-facts")

    # Load all staged events grouped by source file
    all_events_by_file: dict = {}
    for staged_path in sorted(gemini_staging.glob("*.jsonl")):
        events = []
        try:
            with open(staged_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        except Exception as e:
            print(f"  warning: could not read {staged_path.name}: {e}", file=sys.stderr)
            continue
        if events:
            all_events_by_file[staged_path] = events

    if not all_events_by_file:
        return  # caller handles "nothing to review" message

    # Flatten and group by domain
    domain_groups: dict[str, list[dict]] = {}
    for events in all_events_by_file.values():
        for ev in events:
            dom = ev.get("domain", "general")
            domain_groups.setdefault(dom, []).append(ev)

    total_events = sum(len(evs) for evs in domain_groups.values())
    total_files = len(all_events_by_file)
    decisions_total = sum(1 for evs in domain_groups.values()
                          for ev in evs if ev.get("type") == "decision")
    discoveries_total = total_events - decisions_total

    print("=" * 68)
    print(f"{label.replace('-', ' ').title()} Review — {total_events} events across {len(domain_groups)} domains")
    print(f"  {decisions_total} decisions (structured_reasoning)  "
          f"{discoveries_total} discoveries (automated)")
    print(f"  Source files: {total_files}")
    print(f"  [Y]es all / [n]o skip / [r]eview individually / [q]uit")
    print("=" * 68)

    promoted_keys: set[str] = set()
    promoted_count = 0
    quit_requested = False

    for domain, events in sorted(domain_groups.items()):
        if quit_requested:
            break

        decisions = [ev for ev in events if ev.get("type") == "decision"]
        discoveries = [ev for ev in events if ev.get("type") != "decision"]

        print(f"\n── {domain} ── {len(events)} facts "
              f"({len(decisions)} decisions, {len(discoveries)} discoveries) ──")

        # Show sample decisions
        if decisions:
            print("  Decisions:")
            for ev in decisions[:5]:
                subj = ev.get("subject", "")[:70]
                print(f"    • {subj}")
            if len(decisions) > 5:
                print(f"    … and {len(decisions) - 5} more")

        # Show sample discoveries
        if discoveries:
            print("  Discoveries:")
            for ev in discoveries[:5]:
                tool = ev.get("tool", "?")
                out = (ev.get("output") or "")[:60].replace("\n", " ")
                print(f"    • [{tool}] {out}")
            if len(discoveries) > 5:
                print(f"    … and {len(discoveries) - 5} more")

        # Prompt
        try:
            choice = input("\n  [Y/n/r/q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            quit_requested = True
            break

        if choice in ("", "y"):
            # Approve all — promote with outcome=null
            for ev in events:
                fk = ev.get("fact_key")
                if fk and fk not in promoted_keys:
                    _promote_event(conn, ev, outcome=None)
                    promoted_keys.add(fk)
                    promoted_count += 1
            print(f"  ✓ Promoted {len(events)} facts from {domain}")

        elif choice == "n":
            print(f"  ↷ Skipped {domain}")

        elif choice == "r":
            # Individual review mode
            for ev in events:
                fk = ev.get("fact_key")
                if not fk:
                    continue

                ev_type = ev.get("type", "discovery")
                print(f"\n  ┌─ {ev_type.upper()} ─────────────────────────────────────────")
                if ev_type == "decision":
                    print(f"  │  Subject:     {ev.get('subject', '')}")
                    print(f"  │  Description: {ev.get('description', '')[:200]}")
                else:
                    print(f"  │  Tool:    {ev.get('tool', '')}")
                    print(f"  │  Output:  {(ev.get('output') or '')[:200]}")
                print(f"  │  Domain:  {ev.get('domain', '')} | "
                      f"Provenance: {ev.get('provenance', '')}")
                print(f"  └─ [k]eep / [c]onfirmed / [x]refuted / [?]open / [s]kip / [q]uit")

                try:
                    sub_choice = input("     > ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    quit_requested = True
                    break

                if sub_choice == "q":
                    break  # exit individual mode, rest of cluster skipped
                elif sub_choice == "s":
                    continue  # skip this event
                else:
                    outcome_map = {
                        "k": None,
                        "c": "confirmed",
                        "x": "refuted",
                        "?": "open_question",
                    }
                    outcome = outcome_map.get(sub_choice, None)
                    _promote_event(conn, ev, outcome=outcome)
                    promoted_keys.add(fk)
                    promoted_count += 1
                    tag = f"outcome={outcome}" if outcome else "outcome=null"
                    print(f"     ✓ {tag}")

        elif choice == "q":
            quit_requested = True

    # Rewrite staging files — remove promoted events
    deleted_files = _rewrite_staging(all_events_by_file, promoted_keys)

    remaining_events = total_events - promoted_count
    print(f"\n{'=' * 68}")
    print(f"Review complete: promoted {promoted_count} facts, "
          f"{remaining_events} remaining staged")
    if deleted_files:
        print(f"  Cleaned up {deleted_files} fully-reviewed staging file(s)")
    if promoted_count:
        print(f"  Facts now in: {DB_PATH}")
        print(f"  Query:  sqlite3 {DB_PATH} \"SELECT domain, fact_text FROM facts ORDER BY domain, score DESC\"")


def _cmd_next_pending_fact(conn) -> bool:
    """Show the highest-priority pending fact. Returns True if one was shown.

    Also re-queues deferred facts whose reopen date has passed.
    """
    # Re-queue deferred facts past their reopen date (conflict_with holds ISO date)
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE facts SET review_state='pending' "
        "WHERE review_state='deferred' AND conflict_with IS NOT NULL AND conflict_with < ?",
        (now_iso,)
    )
    conn.commit()

    row = conn.execute("""
        SELECT id, fact_text, domain, confidence, confidence_source, conflict_with, first_seen
        FROM facts
        WHERE review_state = 'pending'
        ORDER BY (1.0 - confidence) * score DESC
        LIMIT 1
    """).fetchone()

    if not row:
        return False

    pending_count = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE review_state = 'pending'"
    ).fetchone()[0]

    conflict_info = ""
    if row['conflict_with']:
        cf = conn.execute(
            "SELECT fact_text FROM facts WHERE id = ?", (row['conflict_with'],)
        ).fetchone()
        if cf:
            conflict_info = f"\nConflicts with: [{row['conflict_with']}] {cf['fact_text'][:120]}"

    conf_pct = int((row['confidence'] or 0.5) * 100)
    print("=" * 68)
    print(f"[PENDING FACT]  id={row['id']}  domain={row['domain']}")
    print(f"Confidence:     {conf_pct}%  ({row['confidence_source']})")
    print(f"First seen:     {(row['first_seen'] or '')[:19]}")
    print(f"Pending queue:  {pending_count}")
    print("=" * 68)
    print(f"\n{row['fact_text']}{conflict_info}")
    print(f"\n{'=' * 68}")
    print(f"Confirm:  gaius confirm {row['id']}")
    print(f"Reject:   gaius reject {row['id']}")
    print(f"Defer:    gaius defer {row['id']}")
    return True


def cmd_next(args):
    """Print the oldest unreviewed summary with signal.

    Priority order:
    1. Staged event facts (pentagi/ollama/gemini)
    2. Pending facts in facts.db (low-confidence or contradicted)
    3. Session compaction summaries

    Pass --facts to show only pending facts. Pass --summaries to skip to summaries.
    """
    show_facts_only = '--facts' in (args or [])
    skip_facts = '--summaries' in (args or [])

    # Event-based staged facts take priority — they need merge before facts.db is useful
    conn = init_db()
    if not show_facts_only:
        for staging_label in ("pentagi-facts", "ollama-facts", "gemini-facts"):
            staging_dir = STAGING_DIR / staging_label
            if staging_dir.exists() and list(staging_dir.glob("*.jsonl")):
                cmd_next_staged_facts(conn, staging_dir, staging_label)
                return

    # Pending facts (low-confidence / contradicted) — second priority
    if not skip_facts:
        if _cmd_next_pending_fact(conn):
            return

    if show_facts_only:
        print("No pending facts in review queue.")
        return

    staged = load_staged()

    # Prefer summaries with signal; fall back to any unreviewed
    unreviewed = sorted(
        [e for e in staged.values() if not e.get("reviewed")],
        key=lambda e: (not has_signal(e), e.get("timestamp", ""))
    )

    if not unreviewed:
        print("All summaries reviewed. Nothing left in queue.")
        return

    e = unreviewed[0]
    remaining = len(unreviewed)

    source_tag = " [mined]" if e.get("source") == "mined" else ""
    print("=" * 68)
    print(f"UUID:      {e['uuid']}")
    print(f"Session:   {e['session_id']}")
    print(f"Date:      {e.get('timestamp','')[:19]}")
    print(f"Signal:    {'yes (★)' if has_signal(e) else 'no'}{source_tag}")
    print(f"Remaining: {remaining}")
    print("=" * 68)

    for key, header in SECTION_HEADERS:
        text = e["sections"].get(key, "").strip()
        if text:
            print(f"\n── {header} ──────────────────────────────────────────")
            print(text)

    print(f"\n{'=' * 68}")
    print(f"Mark done:  gaius done {e['uuid'][:8]}")
    print(f"Skip:       gaius next  (after gaius done {e['uuid'][:8]})")


def cmd_done(args):
    """Mark a summary as reviewed by UUID prefix."""
    if not args:
        print("Usage: gaius done <uuid-prefix>  (min 4 chars)", file=sys.stderr)
        sys.exit(1)

    prefix = args[0].lower()
    if len(prefix) < 4:
        print("UUID prefix must be at least 4 characters", file=sys.stderr)
        sys.exit(1)

    staged = load_staged()
    matches = [(uid, e) for uid, e in staged.items()
               if uid.lower().startswith(prefix)]

    if not matches:
        print(f"No summary matching prefix: {prefix}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        # Short labels (e.g. `mined-XX`) collide across sessions, but the intent
        # is almost always the one still UNREVIEWED. Auto-disambiguate to it;
        # only error if the collision is itself ambiguous (0 or >=2 unreviewed).
        unreviewed = [(uid, e) for uid, e in matches if not e.get("reviewed")]
        if len(unreviewed) == 1:
            matches = unreviewed
        else:
            print(f"Ambiguous prefix '{prefix}' matches {len(matches)} summaries "
                  f"({len(unreviewed)} unreviewed):", file=sys.stderr)
            for uid, e in matches:
                state = "reviewed" if e.get("reviewed") else "UNREVIEWED"
                print(f"  {uid}  [{state}]", file=sys.stderr)
            sys.exit(1)

    uid, e = matches[0]
    if e.get("reviewed"):
        print(f"Already marked reviewed: {uid[:8]}")
        return

    e["reviewed"] = True
    e["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    save_staged(e)

    remaining = sum(1 for x in staged.values()
                    if not x.get("reviewed") and x["uuid"] != uid)
    print(f"✓ Marked reviewed: {uid[:8]}  |  {remaining} remaining")


def _resolve_fact_id(args, cmd_name: str) -> int:
    """Parse and validate a numeric fact ID from args. Exits on error."""
    if not args:
        print(f"Usage: gaius {cmd_name} <fact-id>", file=sys.stderr)
        sys.exit(1)
    try:
        return int(args[0])
    except ValueError:
        print(f"fact-id must be an integer, got: {args[0]}", file=sys.stderr)
        sys.exit(1)


def cmd_confirm(args):
    """Mark a pending fact as confirmed by a human reviewer.

    Sets confidence=1.0, confidence_source='human', review_state='confirmed'.
    Usage: gaius confirm <fact-id>
    """
    fact_id = _resolve_fact_id(args, 'confirm')
    conn = init_db()
    row = conn.execute("SELECT id, fact_text, domain FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if not row:
        print(f"No fact with id={fact_id}", file=sys.stderr)
        sys.exit(1)
    conn.execute(
        "UPDATE facts SET review_state='confirmed', confidence=1.0, confidence_source='human' WHERE id=?",
        (fact_id,)
    )
    conn.commit()
    pending = conn.execute("SELECT COUNT(*) FROM facts WHERE review_state='pending'").fetchone()[0]
    print(f"✓ Confirmed: [{fact_id}] {row['fact_text'][:80]}  |  {pending} pending remaining")


def cmd_reject(args):
    """Mark a pending fact as rejected (excluded from inject).

    Sets review_state='rejected'. The fact is retained in facts.db for audit but
    excluded from inject queries.
    Usage: gaius reject <fact-id>
    """
    fact_id = _resolve_fact_id(args, 'reject')
    conn = init_db()
    row = conn.execute("SELECT id, fact_text FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if not row:
        print(f"No fact with id={fact_id}", file=sys.stderr)
        sys.exit(1)
    # outcome='rejected' is the value every inject/search query filters on —
    # review_state alone left rejected facts in the inject candidate pool.
    conn.execute("UPDATE facts SET review_state='rejected', outcome='rejected' WHERE id=?", (fact_id,))
    conn.commit()
    pending = conn.execute("SELECT COUNT(*) FROM facts WHERE review_state='pending'").fetchone()[0]
    print(f"✗ Rejected: [{fact_id}] {row['fact_text'][:80]}  |  {pending} pending remaining")


def cmd_defer(args):
    """Defer a pending fact for re-review in 7 days.

    Sets review_state='deferred'. gaius next will re-surface it after 7 days.
    Usage: gaius defer <fact-id>
    """
    fact_id = _resolve_fact_id(args, 'defer')
    conn = init_db()
    row = conn.execute("SELECT id, fact_text FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if not row:
        print(f"No fact with id={fact_id}", file=sys.stderr)
        sys.exit(1)
    reopen_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    conn.execute(
        "UPDATE facts SET review_state='deferred', conflict_with=? WHERE id=?",
        (reopen_at, fact_id)
    )
    conn.commit()
    pending = conn.execute("SELECT COUNT(*) FROM facts WHERE review_state='pending'").fetchone()[0]
    print(f"⏸  Deferred: [{fact_id}] {row['fact_text'][:80]}  |  re-opens {reopen_at[:10]}")
    print(f"   {pending} pending remaining")


# cmd_kg extracted to gaius/kg.py (2026-06-28); re-imported at bottom of file.


def cmd_embed(args):
    """Backfill embeddings for all facts in facts.db.
    Run once after enabling sqlite-vec, then embeddings are maintained automatically."""
    if not HAS_SQLITE_VEC:
        print("ERROR: sqlite-vec not installed. Run: uv pip install sqlite-vec sentence-transformers")
        sys.exit(1)
    model = _get_embed_model()
    if model is None:
        print("ERROR: sentence-transformers not installed.")
        sys.exit(1)

    conn = init_db()
    facts = conn.execute("SELECT id, fact_text FROM facts WHERE tombstoned_at IS NULL").fetchall()

    # Find facts without embeddings
    existing_ids = set()
    try:
        rows = conn.execute("SELECT fact_id FROM fact_embeddings").fetchall()
        existing_ids = {r[0] for r in rows}
    except Exception:
        pass

    to_embed = [(f["id"], f["fact_text"]) for f in facts if f["id"] not in existing_ids]
    if not to_embed:
        print(f"All {len(facts)} facts already have embeddings.")
        return

    print(f"Embedding {len(to_embed)} facts ({len(existing_ids)} already done)...")

    # Embed each fact via _store_embedding so long facts are chunked consistently
    # with the live write path (one row per <=256-token chunk), instead of one
    # truncated vector per fact.
    embedded = 0
    for fact_id, fact_text in to_embed:
        _store_embedding(conn, fact_id, fact_text)
        embedded += 1
        if embedded % 1000 == 0 or embedded == len(to_embed):
            print(f"  {embedded}/{len(to_embed)} embedded")
    conn.commit()
    print(f"Done. {embedded} facts embedded (chunked as needed).")


def cmd_stats(args):
    """Show extraction statistics."""
    staged = load_staged()
    entries = list(staged.values())

    if not entries:
        print("No staged summaries. Run: gaius retire")
        return

    unreviewed  = [e for e in entries if not e.get("reviewed")]
    with_signal = [e for e in entries if has_signal(e)]
    sessions    = {e.get("session_id") for e in entries}

    oldest = min(e.get("timestamp", "") for e in entries)
    newest = max(e.get("timestamp", "") for e in entries)

    print(f"Sessions dir:  {PROJECT_DIR}")
    print(f"Staging dir:   {STAGING_DIR}")
    print()
    print(f"Sessions with compacts: {len(sessions)}")
    print(f"Total summaries:        {len(entries)}")
    print(f"  With signal:          {len(with_signal)}")
    print(f"  Unreviewed:           {len(unreviewed)}")
    print(f"  Reviewed:             {len(entries) - len(unreviewed)}")
    print(f"Date range:             {oldest[:10]} → {newest[:10]}")
    print()

    # facts.db statistics
    conn = init_db()
    try:
        total_facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        by_source = conn.execute("SELECT provenance, COUNT(*) FROM facts GROUP BY provenance").fetchall()
        by_domain = conn.execute("SELECT domain, COUNT(*) FROM facts GROUP BY domain").fetchall()
        
        print(f"Facts in DB (persistent): {total_facts}")
        if total_facts:
            print("  By source:")
            for src, count in by_source:
                print(f"    {src or 'unknown':<12} {count:>5}")
            print("  By domain:")
            for dom, count in by_domain:
                print(f"    {dom or 'unknown':<12} {count:>5}")
            # Per model family
            by_model = conn.execute(
                "SELECT model_family, COUNT(*) FROM facts GROUP BY model_family"
            ).fetchall()
            if by_model:
                print("  By model family:")
                for fam, count in by_model:
                    print(f"    {fam or 'unknown':<12} {count:>5}")
            # Per model version (family:version)
            by_version = conn.execute(
                "SELECT model_family, model_version, COUNT(*) FROM facts "
                "WHERE model_version != '' GROUP BY model_family, model_version"
            ).fetchall()
            if by_version:
                print("  By model version:")
                for fam, ver, count in by_version:
                    print(f"    {fam}:{ver:<16} {count:>5}")
        # Embedding stats
        if HAS_SQLITE_VEC:
            try:
                # Live facts only: tombstoned facts lose embeddings by design (dedup) —
                # counting them made 100%-embedded corpora read as a backlog.
                live_facts = conn.execute("SELECT COUNT(*) FROM facts WHERE tombstoned_at IS NULL").fetchone()[0]
                embedded_count = conn.execute(
                    "SELECT COUNT(*) FROM facts WHERE tombstoned_at IS NULL "
                    "AND id IN (SELECT fact_id FROM fact_embeddings)").fetchone()[0]
                print(f"  Embeddings: {embedded_count}/{live_facts} ({100*embedded_count//max(live_facts,1)}%)")
                print(f"  Embedding model: all-MiniLM-L6-v2 ({_EMBED_DIM}-dim)")
            except Exception:
                print("  Embeddings: not initialized (run: gaius embed)")
        else:
            print("  Embeddings: sqlite-vec not installed")

    except Exception as e:
        print(f"Warning: could not load facts.db stats: {e}")

    print()
    print("Section extraction rates:")
    for key, header in SECTION_HEADERS:
        count = sum(1 for e in entries if e["sections"].get(key))
        pct = 100 * count // len(entries) if entries else 0
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"  {header:<35} {bar} {count:3}/{len(entries)} ({pct:2d}%)")

    # Per-domain fact density
    domain_hits = count_domain_hits(entries)
    print()
    print("Domain coverage (summaries mentioning domain keywords):")
    for domain, count in sorted(domain_hits.items(), key=lambda x: -x[1]):
        pct = 100 * count // len(entries) if entries else 0
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"  {domain:<20} {bar} {count:3}/{len(entries)} ({pct:2d}%)")

    empty = [d for d, c in domain_hits.items() if c == 0]
    if empty:
        print(f"\n  No coverage: {', '.join(sorted(empty))}")

    # TF-IDF Scoring Stats
    scored = [e for e in entries if e.get("score") is not None and e.get("score", 0) > 0]
    if scored:
        scores = [e["score"] for e in scored]
        print()
        print("TF-IDF Scoring:")
        print(f"  Scored entries:   {len(scored)}/{len(entries)}")
        print(f"  Score range:      {min(scores):.3f} - {max(scores):.3f}")
        print(f"  Mean score:       {sum(scores)/len(scores):.3f}")

    # Agent sources
    sources = Counter(e.get("agent_source", "unknown") for e in entries)
    if any(s != "unknown" for s in sources):
        print()
        print("Agent sources:")
        for source, count in sources.most_common():
            print(f"  {source:<20} {count}")

    # Per-domain bootstrap status
    domain_stats = load_domain_stats()
    if domain_stats:
        print()
        print("Domain bootstrap status:")
        for dom in sorted(domain_stats.keys()):
            info = domain_stats[dom]
            sc = info.get("session_count", 0)
            status = "BOOTSTRAP" if sc < BOOTSTRAP_THRESHOLD else "scoring"
            bar_pct = min(100, 100 * sc // BOOTSTRAP_THRESHOLD)
            bar = "█" * (bar_pct // 5) + "░" * (20 - bar_pct // 5)
            print(f"  {dom:<20} {bar} {sc:3}/{BOOTSTRAP_THRESHOLD} sessions  [{status}]")

    # Corpus Statistics
    index_path = CORPUS_DIR / "index.jsonl"
    if index_path.exists():
        print()
        print("Corpus Statistics:")
        indexed_count = 0
        total_corpus_entries = 0
        with open(index_path, "r") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    indexed_count += 1
                    total_corpus_entries += d.get("corpus_entries", 0)
                except Exception:
                    pass
        print(f"  Sessions indexed:       {indexed_count}")
        print(f"  Total training records: {total_corpus_entries}")

    # Facts DB Statistics
    if DB_PATH.exists():
        print()
        print("Facts DB Statistics (facts.db):")
        try:
            conn = init_db()
            total_facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            active_facts = conn.execute("SELECT COUNT(*) FROM facts WHERE COALESCE(training_excluded, 0) = 0").fetchone()[0]
            excluded_facts = total_facts - active_facts
            by_domain = conn.execute(
                "SELECT domain, COUNT(*) as n FROM facts WHERE COALESCE(training_excluded, 0) = 0 GROUP BY domain ORDER BY n DESC"
            ).fetchall()
            by_prov = conn.execute(
                "SELECT provenance, COUNT(*) as n FROM facts GROUP BY provenance ORDER BY n DESC"
            ).fetchall()
            session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            gemini_sessions = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE agent = 'gemini'"
            ).fetchone()[0]
            print(f"  Total facts:            {total_facts} ({active_facts} active, {excluded_facts} excluded)")
            print(f"  Sessions registered:    {session_count}")
            print(f"  Gemini sessions:        {gemini_sessions}")
            if by_domain:
                print(f"  By domain:")
                for row in by_domain:
                    print(f"    {row[0]:<22} {row[1]}")
            if by_prov:
                print(f"  By provenance:")
                for row in by_prov:
                    print(f"    {row[0]:<22} {row[1]}")
            # Gemini staged facts
            gemini_staging = STAGING_DIR / "gemini-facts"
            if gemini_staging.exists():
                staged_files = list(gemini_staging.glob("*.jsonl"))
                staged_event_count = sum(
                    sum(1 for _ in open(f)) for f in staged_files
                )
                print(f"  Gemini staged (pending merge): {len(staged_files)} sessions, {staged_event_count} events")
        except Exception as e:
            print(f"  (could not read facts.db: {e})")


def cmd_rescan(args):
    """Force re-extraction for a specific session by UUID prefix."""
    if not args:
        print("Usage: gaius rescan <uuid-prefix>  (min 4 chars)", file=sys.stderr)
        sys.exit(1)

    prefix = args[0].lower()
    if len(prefix) < 4:
        print("UUID prefix must be at least 4 characters", file=sys.stderr)
        sys.exit(1)

    staged = load_staged()
    matches = [(uid, e) for uid, e in staged.items()
               if uid.lower().startswith(prefix)]

    if not matches:
        print(f"No staged summary matching prefix: {prefix}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(f"Ambiguous prefix '{prefix}' matches {len(matches)} summaries:",
              file=sys.stderr)
        for uid, _ in matches:
            print(f"  {uid}", file=sys.stderr)
        sys.exit(1)

    uid, existing = matches[0]
    session_id = existing.get("session_id", "")

    # Find the source JSONL file
    jsonl_path = PROJECT_DIR / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        print(f"Source file not found: {jsonl_path}", file=sys.stderr)
        sys.exit(1)

    # Re-scan the file for this UUID
    found = False
    with open(jsonl_path) as f:
        for line in f:
            if "isCompactSummary" not in line:
                continue
            entry = json.loads(line)
            if not entry.get("isCompactSummary"):
                continue
            if entry.get("uuid", "") != uid:
                continue

            content = entry.get("message", {}).get("content", "")
            if not content:
                continue

            sections = {
                key: extract_section(content, header)
                for key, header in SECTION_HEADERS
            }

            existing["sections"] = sections
            existing["content_hash"] = content_hash(content)
            existing["updated_at"] = datetime.now(timezone.utc).isoformat()
            existing["reviewed"] = False
            save_staged(existing)
            found = True
            print(f"✓ Rescanned: {uid[:8]}  (re-queued for review)")
            break

    if not found:
        print(f"No compaction summary with UUID {uid[:8]} found in {jsonl_path.name}",
              file=sys.stderr)
        sys.exit(1)


def cmd_batch(args):
    """Print all unreviewed summaries with signal, one after another."""
    staged = load_staged()
    # State-change summaries first — operational transitions rot project files
    # fastest when their review is delayed.
    unreviewed = sorted(
        [e for e in staged.values() if not e.get("reviewed") and has_signal(e)],
        key=lambda e: (not e.get("state_change"), e.get("timestamp", ""))
    )

    if not unreviewed:
        print("No unreviewed summaries with signal. Run: gaius show")
        return

    sc_count = sum(1 for e in unreviewed if e.get("state_change"))
    print(f"Batch mode: {len(unreviewed)} summaries with signal"
          + (f" ({sc_count} ⚡state-change, listed first)" if sc_count else "") + "\n")

    for i, e in enumerate(unreviewed, 1):
        source_tag = " [mined]" if e.get("source") == "mined" else ""
        sc_tag = " ⚡STATE-CHANGE — verify project files reflect this" if e.get("state_change") else ""
        print("=" * 68)
        print(f"[{i}/{len(unreviewed)}]  {e['uuid'][:8]}  {e.get('timestamp','')[:10]}{source_tag}{sc_tag}")
        print("=" * 68)

        for key, header in SECTION_HEADERS:
            if key not in SIGNAL_SECTIONS:
                continue
            text = e["sections"].get(key, "").strip()
            if text:
                print(f"\n── {header} ──")
                print(text)

        print()


# ── Step 6: maturity scoring → extracted to gaius/maturity.py (facade re-import below) ──
# ── RAFT Sidecar Extraction → extracted to gaius/raft.py (facade re-import below) ──
# ── Event-based session retire (shared by pentagi/ollama) ─────────────────────

def _retire_event_sessions(sessions_dir: Path, parser_fn, staging_subdir: str,
                           agent: str, conn: sqlite3.Connection,
                           dry_run: bool = False, discover_fn=None) -> int:
    """Scan a directory of session files, parse events, domain-tag, stage.

    By default sessions are flat ``*.jsonl`` files. Pass ``discover_fn`` to
    enumerate a non-flat layout (e.g. Grok session dirs, Codex date-nested
    rollouts) — it receives ``sessions_dir`` and yields Path objects (files or
    directories) understood by ``parser_fn``.

    Returns count of new events staged.
    """
    if not sessions_dir.exists():
        print(f"  No sessions directory: {sessions_dir}")
        return 0

    session_paths = (
        sorted(discover_fn(sessions_dir)) if discover_fn
        else sorted(sessions_dir.glob("*.jsonl"))
    )
    if not session_paths:
        print(f"  No sessions found in {sessions_dir}")
        return 0

    staged_dir = STAGING_DIR / staging_subdir
    staged_dir.mkdir(parents=True, exist_ok=True)
    total_events = 0

    for path in session_paths:
        session_id = path.stem
        # Check if already processed
        existing = conn.execute(
            "SELECT uuid FROM sessions WHERE uuid = ?", (session_id,)
        ).fetchone()
        if existing:
            continue

        events = parser_fn(path)
        if not events:
            continue

        # Domain-tag
        for ev in events:
            text = " ".join(filter(None, [
                ev.get("subject", ""), ev.get("description", ""),
                ev.get("output", ""), str(ev.get("tool", "")),
            ]))
            domains = tag_domains_from_specs(text, load_domain_specs())
            ev["domain"] = domains[0] if domains else "general"

        if dry_run:
            print(f"  [dry-run] {path.name}: {len(events)} events")
            total_events += len(events)
            continue

        # Write staged JSONL
        out_path = staged_dir / f"{session_id}.jsonl"
        with open(out_path, "w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        # Promote to the corpus: peer parity with the Claude retire path,
        # which auto-promotes via _promote_mined_to_facts. Without this, peer
        # (Grok/Codex) events stage but never reach facts.db (search/injection).
        # Run BEFORE register_session: upsert_fact is idempotent on fact_key, so
        # a crash mid-loop just re-promotes next run; the session is marked
        # processed only once promotion has run.
        promoted = 0
        for ev in events:
            try:
                upsert_fact(
                    conn, domain=ev.get("domain", "general"),
                    fact_key=ev["fact_key"], fact_text=ev.get("description", ""),
                    agent=agent, session_uuid=ev.get("session_uuid", session_id),
                    provenance=ev.get("provenance", "inference"),
                    model_family=ev.get("model_family", agent),
                    model_version=ev.get("model_version", ""),
                    outcome=ev.get("outcome"), source=agent,
                )
                promoted += 1
            except Exception as e:
                print(f"  warn: promote {ev.get('fact_key', '?')}: {e}", file=sys.stderr)

        register_session(conn, session_id, "local", agent,
                         "cluster", path.stat().st_size)
        total_events += len(events)
        print(f"  Staged {len(events)} events ({promoted} promoted) from {path.name}")

    return total_events


def cmd_pentagi_retire(args):
    """Fetch PentAGI flows via GraphQL, save to local JSONL, then parse and stage."""
    import argparse
    import getpass
    parser = argparse.ArgumentParser(prog="gaius pentagi-retire")
    parser.add_argument("--host", default="localhost:8443")
    parser.add_argument("--mail", default="", help="PentAGI login email (required)")
    parser.add_argument("--password", default=None, help="PentAGI password (prompted if missing)")
    parser.add_argument("--flow-id", type=int, default=None, help="Specific flow (default: all finished)")
    parser.add_argument("--sessions-dir", default=str(Path.home() / ".pentagi" / "sessions"))
    parser.add_argument("--fetch-only", action="store_true", help="Fetch from API, don't parse")
    parser.add_argument("--parse-only", action="store_true", help="Parse existing local files, don't fetch")
    parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(args)

    sessions_dir = Path(parsed.sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    conn = init_db()

    # Phase A — Fetch from GraphQL
    if not parsed.parse_only:
        password = parsed.password or getpass.getpass("PentAGI password: ")
        base_url = f"http://{parsed.host}"

        # Authenticate
        import urllib.request
        import urllib.error
        auth_data = json.dumps({"mail": parsed.mail, "password": password}).encode()
        auth_req = urllib.request.Request(
            f"{base_url}/api/v1/auth/login",
            data=auth_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            auth_resp = urllib.request.urlopen(auth_req)
        except urllib.error.HTTPError as e:
            print(f"Auth failed: {e.code} {e.read().decode()}", file=sys.stderr)
            sys.exit(1)

        # Extract cookie
        cookie = None
        for header in auth_resp.headers.get_all("Set-Cookie") or []:
            if header.startswith("auth="):
                cookie = header.split(";")[0]
                break
        if not cookie:
            print("Auth failed: no auth cookie returned", file=sys.stderr)
            sys.exit(1)

        print(f"[pentagi] Authenticated to {parsed.host}")

        def graphql_query(query: str) -> dict:
            data = json.dumps({"query": query}).encode()
            req = urllib.request.Request(
                f"{base_url}/api/v1/graphql",
                data=data,
                headers={"Content-Type": "application/json", "Cookie": cookie},
                method="POST",
            )
            resp = urllib.request.urlopen(req)
            return json.loads(resp.read().decode())

        # Query flows
        if parsed.flow_id:
            flow_query = f"""{{ flows {{ id status title createdAt updatedAt }} }}"""
        else:
            flow_query = """{ flows { id status title createdAt updatedAt } }"""

        result = graphql_query(flow_query)
        flows = result.get("data", {}).get("flows", [])

        if parsed.flow_id:
            flows = [f for f in flows if int(f.get("id", 0)) == parsed.flow_id]
        else:
            flows = [f for f in flows if f.get("status") == "finished"]

        if not flows:
            print("[pentagi] No matching flows found")
            if not parsed.fetch_only:
                # Fall through to parse phase
                pass
            else:
                return

        print(f"[pentagi] Found {len(flows)} flow(s)")

        for flow in flows:
            fid = flow["id"]
            print(f"  Flow {fid}: {flow.get('title', '?')} ({flow.get('status', '?')})")

            # Fetch logs for this flow
            logs_query = f"""{{
                agentLogs(flowId: {fid}) {{ id initiator executor task result }}
                terminalLogs(flowId: {fid}) {{ id type text }}
                searchLogs(flowId: {fid}) {{ id engine query result }}
                messageLogs(flowId: {fid}) {{ id type message }}
            }}"""

            logs_result = graphql_query(logs_query)
            logs_data = logs_result.get("data", {})

            agent_count = len(logs_data.get("agentLogs", []))
            terminal_count = len(logs_data.get("terminalLogs", []))
            search_count = len(logs_data.get("searchLogs", []))
            message_count = len(logs_data.get("messageLogs", []))
            print(f"    Logs: {agent_count} agent, {terminal_count} terminal, "
                  f"{search_count} search, {message_count} message")

            # Write to local JSONL
            out_path = sessions_dir / f"flow-{fid}.jsonl"
            with open(out_path, "w") as f:
                # Meta header
                f.write(json.dumps({"_meta": flow}) + "\n")
                for log_type in ("agentLogs", "terminalLogs", "searchLogs", "messageLogs"):
                    for entry in logs_data.get(log_type, []):
                        entry["_log_type"] = log_type
                        f.write(json.dumps(entry) + "\n")

            print(f"    Saved to {out_path}")

    # Phase B — Parse local JSONL files
    if not parsed.fetch_only:
        print(f"\n[pentagi] Parsing sessions in {sessions_dir}...")
        count = _retire_event_sessions(
            sessions_dir, parse_pentagi_flow_from_jsonl,
            "pentagi-facts", "pentagi", conn, dry_run=parsed.dry_run,
        )
        print(f"[pentagi] Staged {count} events total")


def cmd_ollama_retire(args):
    """Parse Ollama inference session logs and stage for review."""
    import argparse
    parser = argparse.ArgumentParser(prog="gaius ollama-retire")
    parser.add_argument("--sessions-dir", default=str(Path.home() / ".ollama" / "sessions"))
    parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(args)

    sessions_dir = Path(parsed.sessions_dir)
    conn = init_db()

    print(f"[ollama] Parsing sessions in {sessions_dir}...")
    count = _retire_event_sessions(
        sessions_dir, parse_ollama_events,
        "ollama-facts", "ollama", conn, dry_run=parsed.dry_run,
    )
    print(f"[ollama] Staged {count} events total")


def cmd_grok_retire(args):
    """Parse Grok CLI session directories and stage decision events for review."""
    import argparse
    parser = argparse.ArgumentParser(prog="gaius grok-retire")
    parser.add_argument("--sessions-dir", default=str(Path.home() / ".grok" / "sessions"))
    parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(args)

    sessions_dir = Path(parsed.sessions_dir)
    conn = init_db()

    print(f"[grok] Parsing sessions in {sessions_dir}...")
    count = _retire_event_sessions(
        sessions_dir, parse_grok_events, "grok-facts", "grok", conn,
        dry_run=parsed.dry_run, discover_fn=_discover_grok_sessions,
    )
    print(f"[grok] Staged {count} events total")


def cmd_codex_retire(args):
    """Parse Codex CLI rollout sessions and stage decision events for review."""
    import argparse
    parser = argparse.ArgumentParser(prog="gaius codex-retire")
    parser.add_argument("--sessions-dir", default=str(Path.home() / ".codex" / "sessions"))
    parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(args)

    sessions_dir = Path(parsed.sessions_dir)
    conn = init_db()

    print(f"[codex] Parsing sessions in {sessions_dir}...")
    count = _retire_event_sessions(
        sessions_dir, parse_codex_events, "codex-facts", "codex", conn,
        dry_run=parsed.dry_run, discover_fn=_discover_codex_sessions,
    )
    print(f"[codex] Staged {count} events total")


def cmd_skills(args):
    """List all skills with domain/trigger/gate/line-count/staleness. Analogous to gaius stats."""
    parser = argparse.ArgumentParser(prog="gaius skills")
    parser.add_argument("--domain", type=str, default=None, help="Filter by domain")
    parser.add_argument("--stale", action="store_true", help="Show only stale skills")
    parser.add_argument("--score", type=str, default=None,
                        help="Score skills against this context string and show ranked output")
    parsed = parser.parse_args(args)

    skills = load_skills()

    if parsed.domain:
        skills = [s for s in skills if s["domain"] == parsed.domain]
    if parsed.stale:
        skills = [s for s in skills if s["is_stale"]]

    if not skills:
        print("No skills found.")
        return

    # If --score provided, rank by score descending
    if parsed.score:
        context_terms = set(re.sub(r'[^\w\s]', ' ', parsed.score.lower()).split())
        skills = sorted(skills, key=lambda s: compute_skill_score(s, context_terms), reverse=True)

    col_name   = max(len(s["name"])   for s in skills) + 2
    col_domain = max((len(s["domain"]) for s in skills), default=6) + 2
    col_gate   = 12

    stale_marker = f"  {YELLOW}STALE{RESET}"

    header = f"\n{'Name':<{col_name}} {'Domain':<{col_domain}} {'Gate':<{col_gate}} {'Modified':<12} {'Lines':>5}"
    if parsed.score:
        header += "   Score/tok"
    print(header)
    print("─" * (col_name + col_domain + col_gate + 42))

    stale_count = 0
    for s in skills:
        lines  = len(s["full_text"].splitlines())
        date   = s["git_date"]
        stale  = stale_marker if s["is_stale"] else ""
        if s["is_stale"]:
            stale_count += 1
        row = f"{s['name']:<{col_name}} {s['domain']:<{col_domain}} {s['gate']:<{col_gate}} {date:<12} {lines:>5}"
        if parsed.score:
            context_terms = set(re.sub(r'[^\w\s]', ' ', parsed.score.lower()).split())
            sc = compute_skill_score(s, context_terms)
            row += f"   {sc:.4f}"
        print(row + stale)

    summary = f"\n{len(skills)} skill(s)"
    if stale_count:
        summary += f" | {YELLOW}{stale_count} STALE (>{SKILL_STALE_DAYS}d){RESET}"
    print(summary + "\n")


# ── Claude Code command stubs ─────────────────────────────────────────────────

# Modern format: ~/.claude/skills/<name>/SKILL.md (v2.1+)
# Legacy format: ~/.claude/commands/<name>.md (kept for backwards compat)
CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"
CLAUDE_COMMANDS_DIR = Path.home() / ".claude" / "commands"


def cmd_commands(args):
    """Sync skill files → ~/.claude/skills/ for Claude Code slash commands.

    By default syncs gate:mandate skills only. Use --all to include gate:reference.
    Stubs are idempotent — only written if content changed or missing.
    Stale stubs (no matching skill) are removed with --prune.
    """
    parser = argparse.ArgumentParser(prog="gaius commands")
    parser.add_argument("--all", action="store_true",
                        help="Sync all skills, not just gate:mandate")
    parser.add_argument("--prune", action="store_true",
                        help="Remove stubs whose skill file no longer exists")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without writing")
    parsed = parser.parse_args(args)

    skills = load_skills()
    if not parsed.all:
        skills = [s for s in skills if s["gate"] == "mandate"]

    # Skip meta skills that shouldn't be user-invoked directly
    skip = {"base", "verification-gate"}
    skills = [s for s in skills if s["name"] not in skip]

    CLAUDE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    wrote = 0
    skipped = 0
    unchanged = 0

    for s in skills:
        skill_dir = CLAUDE_SKILLS_DIR / s["name"]
        stub_path = skill_dir / "SKILL.md"
        description = s["fm"].get("description", s["name"])

        # Inline the full skill body so Claude Code gets the content directly
        # Strip frontmatter — Claude Code doesn't need YAML metadata
        stub_content = s["body"].strip() + "\n"

        if stub_path.exists():
            existing = stub_path.read_text()
            if existing == stub_content:
                unchanged += 1
                continue

        if parsed.dry_run:
            print(f"  {'update' if stub_path.exists() else 'create'}: {s['name']}/SKILL.md")
            wrote += 1
            continue

        skill_dir.mkdir(parents=True, exist_ok=True)
        stub_path.write_text(stub_content)
        wrote += 1
        print(f"  {'updated' if skill_dir.exists() else 'created'}: /{s['name']}")

    # Prune stale stubs
    pruned = 0
    if parsed.prune:
        skill_names = {s["name"] for s in load_skills()}
        # Prune modern format (skip symlinks — belong to other tools)
        if CLAUDE_SKILLS_DIR.is_dir():
            for d in CLAUDE_SKILLS_DIR.iterdir():
                if d.is_dir() and not d.is_symlink() and d.name not in skill_names:
                    skill_md = d / "SKILL.md"
                    if skill_md.exists():
                        if parsed.dry_run:
                            print(f"  prune: {d.name}/SKILL.md")
                        else:
                            skill_md.unlink()
                            d.rmdir()
                            print(f"  pruned: /{d.name}")
                        pruned += 1
        # Prune legacy format
        if CLAUDE_COMMANDS_DIR.is_dir():
            for stub in CLAUDE_COMMANDS_DIR.glob("*.md"):
                if parsed.dry_run:
                    print(f"  prune legacy: {stub.name}")
                else:
                    stub.unlink()
                    print(f"  pruned legacy: /{stub.stem}")
                pruned += 1
                pruned += 1

    total = wrote + unchanged
    parts = [f"{total} skill(s)"]
    if wrote:
        parts.append(f"{GREEN}{wrote} written{RESET}")
    if unchanged:
        parts.append(f"{unchanged} unchanged")
    if pruned:
        parts.append(f"{YELLOW}{pruned} pruned{RESET}")
    print(" | ".join(parts))


# ── Dispatch ──────────────────────────────────────────────────────────────────

def cmd_init(args):
    """Guided first-run setup: create ~/.gaius/config.yaml and memory directories."""
    import shutil

    gaius_dir = Path.home() / ".gaius"
    config_path = gaius_dir / "config.yaml"

    print("gaius init — first-run setup")
    print("─" * 40)

    # Check for existing config
    if config_path.exists():
        ans = input(f"\nConfig already exists at {config_path}\nOverwrite? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted.")
            return

    # Choose backend
    print("\nChoose your AI coding agent backend:")
    print("  1) claude   — Claude Code (JSONL sessions in ~/.claude/projects/)")
    print("  2) gemini   — Gemini CLI (JSON sessions in ~/.gemini/tmp/)")
    print("  3) vllm     — vLLM-served models (Gemma, Nemotron, etc. — requires chat TUI that writes JSONL)")
    backend_choice = input("\nBackend [1]: ").strip() or "1"
    backend_map = {"1": "claude", "2": "gemini", "3": "vllm",
                   "claude": "claude", "gemini": "gemini", "vllm": "vllm"}
    backend = backend_map.get(backend_choice, "claude")

    # Choose preset
    print("\nChoose a starting preset:")
    print("  1) default  — minimal, any software project (no K8s patterns)")
    print("  2) k8s      — Kubernetes cluster ops (includes service/namespace/incident patterns)")
    preset_choice = input("\nPreset [1]: ").strip() or "1"
    preset_name = "k8s" if preset_choice == "2" else "default"

    # Find preset file
    _script_dir = Path(__file__).parent.parent  # gaius package root
    preset_src = _script_dir / "presets" / f"{preset_name}.yaml"
    if not preset_src.exists():
        # Try relative to installed package
        preset_src = Path(__file__).parent.parent.parent / "presets" / f"{preset_name}.yaml"
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
    sessions_input = input(f"\nSessions directory [{default_sessions}]: ").strip()
    sessions_dir = sessions_input or default_sessions

    # Memory directory
    default_memory = str(Path.home() / ".gaius" / "memory")
    memory_input = input(f"Memory directory (domain/*.md files) [{default_memory}]: ").strip()
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
    print()
    if backend == "claude":
        print("To add the MCP server to Claude Code:")
        print("  claude mcp add gaius -- python3 -m gaius.mcp_server")
        print()
    print(f"Edit {config_path} to customize entity patterns and principal mappings.")


def volatility_recency(prov_key: str, fact_type: str, age_days: float, rate: float) -> float:
    """Recency factor honoring the fact_type volatility axis (2026-07-03).

    Precedence: an explicit `live` rating wins over provenance — a fact a session
    deliberately marked as volatile state MUST decay fast enough to force
    re-verification (3x rate ≈ half-life ~12 days at the default 0.02/day).
    `structural` facts are design-level and do not decay, same as the no-decay
    provenances. Everything else keeps the standard rate.
    """
    if fact_type == "live":
        return math.exp(-rate * 3.0 * age_days)
    if prov_key in NO_DECAY_PROVENANCES or fact_type == "structural":
        return 1.0
    return math.exp(-rate * age_days)


def cmd_decay(args):
    """Apply time-based score decay to all facts.

    Facts decay based on days since last_seen. High-confirmation and
    recently-seen facts keep high scores. Old single-confirmation facts
    decay toward a floor (never fully zero — historical value persists).

    Designed to run nightly via gaius-nightly-sync.

    Score formula:
        base = max(provenance_weight × source_reliability × cross_model, content_seed) × outcome_modifier
        recency = exp(-decay_rate × days_since_last_seen)
        confirmation_boost = min(log2(confirmation_count + 1), 3.0) / 3.0
        score = clamp(base × (0.3 + 0.7 × recency) × (0.5 + 0.5 × confirmation_boost), FLOOR, 1.0)

    Floor = 0.1 (facts never fully disappear from inject results).
    No-decay provenances (findings, procedures) get recency = 1.0.
    """
    import argparse as _ap
    import math

    parser = _ap.ArgumentParser(prog="gaius decay")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without updating")
    parser.add_argument("--rate", type=float, default=0.02,
                        help="Decay rate per day (default: 0.02 ≈ half-life ~35 days)")
    parser.add_argument("--floor", type=float, default=0.1,
                        help="Minimum score floor (default: 0.1)")
    parsed = parser.parse_args(args)

    conn = init_db()
    now = datetime.now(timezone.utc)
    rate = parsed.rate
    floor = parsed.floor

    facts = conn.execute(
        "SELECT id, domain, fact_key, fact_text, score, confirmation_count, provenance, "
        "outcome, first_seen, last_seen, model_families, source, fact_type "
        "FROM facts WHERE tombstoned_at IS NULL"
    ).fetchall()

    updates = []
    buckets = {"raised": 0, "decayed": 0, "unchanged": 0}

    for fact in facts:
        # Base weights (same as _maturity_score)
        prov_key = fact["provenance"] if fact["provenance"] else "automated"
        prov = PROVENANCE_WEIGHT.get(prov_key, 0.5)
        out = OUTCOME_MODIFIER.get(fact["outcome"], 1.0)
        source_mult = SOURCE_RELIABILITY.get(fact["source"] or "human", 1.0)

        # Cross-model multiplier
        try:
            families = json.loads(fact["model_families"] or '["claude"]')
            cross_mult = CROSS_MODEL_MULTIPLIER if len(set(families)) >= 2 else 1.0
        except (TypeError, ValueError):
            cross_mult = 1.0

        # Content-seeded floor: an incident/postmortem fact must not score like
        # boilerplate just because both are auto-mined. Deterministic (regex on
        # fact_text), so nightly re-runs stay idempotent — no compounding.
        seed = _seeded_score(fact["fact_text"] or "")
        base = max(prov * source_mult * cross_mult, seed) * out

        # Recency decay based on last_seen (not first_seen)
        try:
            last_seen = datetime.fromisoformat(fact["last_seen"])
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            age_days = (now - last_seen).total_seconds() / 86400
        except (TypeError, ValueError):
            age_days = 30.0  # unknown → assume moderately old

        recency = volatility_recency(prov_key, fact["fact_type"], age_days, rate)

        # Confirmation boost (log scale, capped at 3.0)
        conf = max(1, fact["confirmation_count"])
        conf_boost = min(math.log2(conf + 1), 3.0) / 3.0

        # Final score
        new_score = round(max(floor, min(1.0,
            base * (0.3 + 0.7 * recency) * (0.5 + 0.5 * conf_boost)
        )), 4)

        old_score = round(fact["score"] or 0.5, 4)
        if new_score != old_score:
            updates.append((new_score, fact["id"]))
            if new_score > old_score:
                buckets["raised"] += 1
            else:
                buckets["decayed"] += 1
        else:
            buckets["unchanged"] += 1

    if parsed.dry_run:
        print(f"Dry run: {len(facts)} facts analyzed")
        print(f"  Would raise:  {buckets['raised']}")
        print(f"  Would decay:  {buckets['decayed']}")
        print(f"  Unchanged:    {buckets['unchanged']}")
        if updates:
            # Show sample changes
            sample = updates[:10]
            for new_score, fid in sample:
                f = next(r for r in facts if r["id"] == fid)
                print(f"  [{f['domain']}] {f['score']:.4f} → {new_score:.4f}  "
                      f"{(f['fact_key'] or '')[:40]}")
        return

    if not updates:
        print(f"All {len(facts)} facts unchanged.")
        return

    conn.executemany("UPDATE facts SET score = ? WHERE id = ?", updates)
    conn.commit()
    print(f"Decayed {len(facts)} facts: "
          f"↑{buckets['raised']} raised, ↓{buckets['decayed']} decayed, "
          f"={buckets['unchanged']} unchanged")


def cmd_rescore(args):
    """Recompute all fact scores using fact_type-based provenance mapping.

    The original auto-mined provenance gives everything the same weight (0.5).
    fact_types were reclassified in 2026-05-02 but scores were never recomputed.
    This command fixes that by mapping fact_type → provenance, then applying
    the decay formula with updated base scores.

    Mapping:
        finding    → provenance 'finding'              (weight 1.0)
        procedure  → provenance 'procedure'            (weight 0.9)
        security   → provenance 'structured_reasoning' (weight 0.8)
        operational→ provenance 'automated'            (weight 0.7)
        structural → provenance 'automated'            (weight 0.7)
        observation→ default                           (weight 0.5)
    """
    import math

    parser = argparse.ArgumentParser(prog="gaius rescore")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show distribution change without updating")
    parser.add_argument("--rate", type=float, default=0.02,
                        help="Decay rate per day (default: 0.02)")
    parser.add_argument("--floor", type=float, default=0.1,
                        help="Minimum score floor (default: 0.1)")
    parser.add_argument("--update-provenance", action="store_true",
                        help="Also update provenance column based on fact_type")
    parser.add_argument("--rebuild-kg", action="store_true",
                        help="Rebuild knowledge graph from all facts (entity + relation extraction)")
    parsed = parser.parse_args(args)

    # fact_type → provenance mapping
    FACT_TYPE_PROVENANCE = {
        "finding":     "finding",
        "procedure":   "procedure",
        "security":    "structured_reasoning",
        "operational": "automated",
        "structural":  "automated",
        "observation": "automated",
        "live":        "automated",   # volatile state — base weight as operational; decay differentiates
    }

    conn = init_db()
    now = datetime.now(timezone.utc)
    rate = parsed.rate
    floor = parsed.floor

    facts = conn.execute(
        "SELECT id, domain, fact_type, fact_key, fact_text, score, confirmation_count, provenance, "
        "outcome, first_seen, last_seen, model_families, source "
        "FROM facts WHERE tombstoned_at IS NULL"
    ).fetchall()

    updates = []
    prov_updates = []
    old_dist = {}
    new_dist = {}

    for fact in facts:
        old_score = round(fact["score"] or 0.5, 4)
        old_bucket = round(old_score, 1)
        old_dist[old_bucket] = old_dist.get(old_bucket, 0) + 1

        # Map fact_type to effective provenance
        ft = fact["fact_type"] or "operational"
        effective_prov = FACT_TYPE_PROVENANCE.get(ft, "automated")
        prov_weight = PROVENANCE_WEIGHT.get(effective_prov, 0.5)

        # Outcome modifier
        out = OUTCOME_MODIFIER.get(fact["outcome"], 1.0)

        # Source reliability
        source_mult = SOURCE_RELIABILITY.get(fact["source"] or "human", 1.0)

        # Cross-model multiplier
        try:
            families = json.loads(fact["model_families"] or '["claude"]')
            cross_mult = CROSS_MODEL_MULTIPLIER if len(set(families)) >= 2 else 1.0
        except (TypeError, ValueError):
            cross_mult = 1.0

        # Same content-seeded floor as cmd_decay — rescore and decay must agree,
        # or a manual rescore clobbers seeded scores until the next nightly.
        seed = _seeded_score(fact["fact_text"] or "")
        base = max(prov_weight * source_mult * cross_mult, seed) * out

        # Recency decay
        try:
            last_seen = datetime.fromisoformat(fact["last_seen"])
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            age_days = (now - last_seen).total_seconds() / 86400
        except (TypeError, ValueError):
            age_days = 30.0

        recency = volatility_recency(effective_prov, fact["fact_type"], age_days, rate)

        # Confirmation boost
        conf = max(1, fact["confirmation_count"])
        conf_boost = min(math.log2(conf + 1), 3.0) / 3.0

        new_score = round(max(floor, min(1.0,
            base * (0.3 + 0.7 * recency) * (0.5 + 0.5 * conf_boost)
        )), 4)

        new_bucket = round(new_score, 1)
        new_dist[new_bucket] = new_dist.get(new_bucket, 0) + 1

        if new_score != old_score:
            updates.append((new_score, fact["id"]))

        # Track provenance updates
        if parsed.update_provenance and fact["provenance"] == "auto-mined":
            prov_updates.append((effective_prov, fact["id"]))

    # Report
    print(f"Rescore analysis: {len(facts)} active facts")
    print(f"  Would update: {len(updates)} scores")
    if prov_updates:
        print(f"  Would update: {len(prov_updates)} provenances")

    print(f"\n  Score distribution (before → after):")
    all_buckets = sorted(set(list(old_dist.keys()) + list(new_dist.keys())))
    for b in all_buckets:
        old_c = old_dist.get(b, 0)
        new_c = new_dist.get(b, 0)
        delta = new_c - old_c
        bar = "█" * (new_c // 20) if new_c > 0 else ""
        print(f"    {b:.1f}: {old_c:>5} → {new_c:>5} ({delta:+d}) {bar}")

    if parsed.dry_run:
        print("\n  --dry-run: no changes written")
        return

    # Apply updates
    conn.executemany("UPDATE facts SET score = ? WHERE id = ?", updates)
    if prov_updates:
        conn.executemany("UPDATE facts SET provenance = ? WHERE id = ?", prov_updates)
    conn.commit()

    print(f"\n  ✓ Updated {len(updates)} scores" +
          (f", {len(prov_updates)} provenances" if prov_updates else ""))

    # Optional KG rebuild
    if parsed.rebuild_kg:
        print("\n  Rebuilding knowledge graph...")
        # Clear existing KG (incl. fact links + the incremental-index watermark)
        conn.execute("DELETE FROM entities")
        conn.execute("DELETE FROM triples")
        conn.execute("DELETE FROM fact_entities")
        conn.execute("UPDATE facts SET kg_indexed_at = NULL")
        conn.commit()

        kg_count = 0
        for fact in facts:
            text = fact["fact_text"] or ""
            if len(text) < 20:
                continue
            # SAVEPOINT: a partial kg_index_fact failure must roll back all its
            # writes, or the fact stays un-stamped with triples half-written and
            # the next incremental index double-counts co-occurrence weights.
            try:
                conn.execute("SAVEPOINT kg_rebuild")
                kg_index_fact(conn, fact["id"], text, fact["domain"] or "general",
                              timestamp=fact["first_seen"])
                conn.execute("RELEASE SAVEPOINT kg_rebuild")
                kg_count += 1
            except Exception:
                try:
                    conn.execute("ROLLBACK TO SAVEPOINT kg_rebuild")
                    conn.execute("RELEASE SAVEPOINT kg_rebuild")
                except Exception:
                    pass

        refresh_entity_domains(conn)
        conn.commit()
        ent_count = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
        tri_count = conn.execute("SELECT count(*) FROM triples").fetchone()[0]
        print(f"  ✓ KG rebuilt: {ent_count} entities, {tri_count} triples (from {kg_count} facts)")


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


_SUGGEST_DISMISSED_PATH = Path.home() / ".gaius" / "suggest-dismissed.json"


def _load_dismissed() -> set:
    """Load dismissed skill suggestions."""
    if _SUGGEST_DISMISSED_PATH.exists():
        try:
            return set(json.loads(_SUGGEST_DISMISSED_PATH.read_text()))
        except Exception:
            pass
    return set()


def _save_dismissed(dismissed: set):
    """Persist dismissed skill suggestions."""
    _SUGGEST_DISMISSED_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SUGGEST_DISMISSED_PATH.write_text(json.dumps(sorted(dismissed), indent=2))


def cmd_suggest(args):
    """Analyze fact domains and surface skill candidates for human review.

    MVP: scans facts by domain tag, checks if a mandate skill exists for that domain.
    Surfaces uncovered domains with enough facts+sessions as skill candidates.
    """
    parser = argparse.ArgumentParser(prog="gaius suggest")
    parser.add_argument("--threshold", type=int, default=20,
                        help="Min facts to qualify as candidate (default: 20)")
    parser.add_argument("--sessions", type=int, default=3,
                        help="Min unique sessions to qualify (default: 3)")
    parser.add_argument("--output", type=str, default=None,
                        help="Write draft stubs to this directory")
    parser.add_argument("--quiet", action="store_true",
                        help="No stdout, just write drafts (for cron)")
    parser.add_argument("--dismiss", type=str, default=None,
                        help="Dismiss a domain from future suggestions")
    parser.add_argument("--undismiss", type=str, default=None,
                        help="Remove a domain from dismissed list")
    parser.add_argument("--show-dismissed", action="store_true",
                        help="Show dismissed domains")
    parser.add_argument("--include-reference", action="store_true",
                        help="Also flag domains with only reference skills (no mandate)")
    parsed = parser.parse_args(args)

    dismissed = _load_dismissed()

    # Handle dismiss/undismiss/show subcommands
    if parsed.show_dismissed:
        if dismissed:
            print("Dismissed domains:")
            for d in sorted(dismissed):
                print(f"  - {d}")
        else:
            print("No dismissed domains.")
        return

    if parsed.dismiss:
        dismissed.add(parsed.dismiss)
        _save_dismissed(dismissed)
        print(f"Dismissed: {parsed.dismiss}")
        return

    if parsed.undismiss:
        dismissed.discard(parsed.undismiss)
        _save_dismissed(dismissed)
        print(f"Undismissed: {parsed.undismiss}")
        return

    # 1. Load skills and build coverage map: skill_domain → {mandate: [...], reference: [...]}
    skills = load_skills()
    coverage: dict[str, dict[str, list]] = {}
    for s in skills:
        domain = s["domain"]
        if not domain:
            continue
        if domain not in coverage:
            coverage[domain] = {"mandate": [], "reference": []}
        gate = s["gate"]
        if gate in ("mandate", "hard", "always"):
            coverage[domain]["mandate"].append(s["name"])
        else:
            coverage[domain]["reference"].append(s["name"])

    # 2. Query fact domains from facts.db
    conn = init_db()
    try:
        # Two queries: fact counts (simple) and session counts (json_each)
        fact_rows = conn.execute("""
            SELECT domain, COUNT(*) as cnt, MAX(last_seen) as newest
            FROM facts WHERE tombstoned_at IS NULL
            GROUP BY domain ORDER BY cnt DESC
        """).fetchall()
        # Session dedup per domain
        try:
            session_counts = dict(conn.execute("""
                SELECT f.domain, COUNT(DISTINCT j.value)
                FROM facts f, json_each(f.sessions) j
                WHERE f.tombstoned_at IS NULL
                GROUP BY f.domain
            """).fetchall())
        except Exception:
            session_counts = {}
        rows = [(r[0], r[1], session_counts.get(r[0], 0), r[2]) for r in fact_rows]
    finally:
        conn.close()

    # 3. Score each domain
    candidates = []
    covered = []
    for row in rows:
        domain = row[0]
        fact_count = row[1]
        session_count = row[2]
        newest = row[3] or ""

        if domain in dismissed:
            continue

        # Classify coverage
        cov = coverage.get(domain, {"mandate": [], "reference": []})
        has_mandate = bool(cov["mandate"])
        has_reference = bool(cov["reference"])

        if has_mandate:
            covered.append({
                "domain": domain, "facts": fact_count,
                "sessions": session_count, "newest": newest,
                "skills": cov["mandate"] + cov["reference"],
            })
            continue

        # Threshold gate
        if fact_count < parsed.threshold:
            continue

        if session_count > 0 and session_count < parsed.sessions:
            continue

        # Only flag reference-only domains if requested
        if has_reference and not parsed.include_reference:
            continue

        gap_type = "partial" if has_reference else "uncovered"
        nearest = cov["reference"] if has_reference else []

        candidates.append({
            "domain": domain, "facts": fact_count,
            "sessions": session_count, "newest": newest[:10],
            "gap_type": gap_type, "nearest": nearest,
        })

    if parsed.quiet and not parsed.output:
        return

    # 4. Output
    if not candidates:
        if not parsed.quiet:
            print("No skill candidates found. All active domains are covered.")
        return

    # Check for stale skills (candidates for retirement)
    stale_skills = [s for s in skills if s["is_stale"] and s["gate"] != "always"]

    if not parsed.quiet:
        print(f"Skill candidates ({len(candidates)} found, threshold: {parsed.threshold}+ facts, {parsed.sessions}+ sessions):\n")
        for i, c in enumerate(candidates, 1):
            gap_label = "UNCOVERED" if c["gap_type"] == "uncovered" else "PARTIAL (reference only)"
            nearest_str = f" nearest: {', '.join(c['nearest'])}" if c["nearest"] else ""
            sess_str = f" | Sessions: {c['sessions']}" if c["sessions"] > 0 else ""
            print(f"  {i}. {c['domain']} [{gap_label}]")
            print(f"     Facts: {c['facts']}{sess_str} | Newest: {c['newest']}{nearest_str}")

        if stale_skills:
            print(f"\nStale skills (unchanged {SKILL_STALE_DAYS}+ days — consider retiring):")
            for s in stale_skills:
                print(f"  - {s['name']} (gate: {s['gate']}, last commit: {s['git_date']})")

        if parsed.output:
            print(f"\n  Drafts written to: {parsed.output}/")
        print(f"\n  Dismiss: gaius suggest --dismiss <domain>")
        print(f"  Include partial: gaius suggest --include-reference")

    # 5. Write draft stubs if output dir specified
    if parsed.output:
        out_dir = Path(parsed.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        for c in candidates:
            draft = _generate_skill_draft(c)
            draft_path = out_dir / f"{c['domain']}.md"
            draft_path.write_text(draft)
            if not parsed.quiet:
                print(f"  → {draft_path}")


def _generate_skill_draft(candidate: dict) -> str:
    """Generate a draft skill stub for a candidate domain."""
    domain = candidate["domain"]
    # Pull top facts for context
    conn = init_db()
    try:
        top_facts = conn.execute("""
            SELECT fact_text FROM facts
            WHERE domain = ? AND tombstoned_at IS NULL
            ORDER BY score DESC, confirmation_count DESC
            LIMIT 5
        """, (domain,)).fetchall()
    finally:
        conn.close()

    fact_lines = "\n".join(f"- {row[0][:120]}" for row in top_facts) if top_facts else "- (no facts extracted yet)"

    return f"""---
name: {domain}
description: "Auto-suggested skill for {domain} domain ({candidate['facts']} facts)"
origin: kub0
domain: {domain}
gate: mandate
trigger: "{domain} operations, debugging, configuration"
also_load: verification-gate
---

# Session Mode: {domain.replace('-', ' ').title()}

> Auto-generated by `gaius suggest`. Review and edit before promoting.
> Promote: `mv this-file ~/path/to/memory/skills/ && gaius commands`

## Context (top facts from corpus)

{fact_lines}

## Suggested Mindset

(Fill in: what mental model should a session in this domain adopt?)

## Key Patterns

(Fill in: recurring patterns from the facts above)

## Anti-Patterns

(Fill in: what to avoid in this domain)
"""


def _drift_live(parsed):
    """Validate memory-file claims against LIVE cluster state.

    Companion to cmd_drift (which does config-file <-> doc-file matching). Reads a
    live-claims.yaml registry: each claim names a memory file + a regex anchor
    (asserted value = first capture group), a shell probe (live value), and a
    comparator. Three outcomes per claim:
      OK          - asserted matches live.
      STALE       - asserted != live  -> reported, exit 1, --post-council alerts.
      UNCHECKABLE - probe failed or anchor not found -> never alerts (a flaky
                    kubectl must not page); reported in its own bucket.

    Probes run through the registry's 'probe_prefix' (e.g. an ssh-to-genesis
    wrapper); the probe string is shell-quoted so pipes/jsonpath run intact on
    the far side. Usage: gaius drift --live [--registry P] [--post-council] [--quiet]
    """
    import re
    import shlex
    import subprocess
    import urllib.request
    import urllib.error

    GREEN = "\033[0;32m"; YELLOW = "\033[1;33m"; RED = "\033[0;31m"; RESET = "\033[0m"

    # Registry holds cluster topology (node names, genesis IP) -> internal-only,
    # excluded from the OSS mirror exactly like drift-facts.yaml.
    if parsed.registry:
        reg_path = Path(parsed.registry).expanduser()
    else:
        reg_path = Path(__file__).parent.parent / "live-claims.yaml"
        if not reg_path.exists():
            reg_path = Path.home() / ".gaius" / "live-claims.yaml"
    if not reg_path.exists():
        print(f"[drift --live] ERROR: registry not found at {reg_path}", file=sys.stderr)
        print("[drift --live] Create live-claims.yaml or pass --registry PATH", file=sys.stderr)
        sys.exit(1)
    try:
        with open(reg_path) as _f:
            registry = yaml.safe_load(_f) or {}
    except Exception as e:
        print(f"[drift --live] ERROR: cannot load registry: {e}", file=sys.stderr)
        sys.exit(1)

    claims = registry.get("claims", [])
    if not claims:
        print("[drift --live] No claims defined in registry.")
        return
    prefix = (registry.get("probe_prefix") or "").strip()
    timeout = registry.get("probe_timeout", 30)
    mem_base = MEMORY_DIR or (Path.home() / ".claude" / "projects"
                              / "-home-jkubo-ansible" / "memory")

    def _asserted(fil, pattern):
        p = Path(fil).expanduser()
        if not p.is_absolute():
            p = mem_base / fil
        if not p.exists() or not pattern:
            return None, None
        try:
            text = p.read_text(errors="replace")
        except Exception:
            return None, None
        rx = re.compile(pattern, re.IGNORECASE)
        for lineno, line in enumerate(text.splitlines(), 1):
            m = rx.search(line)
            if m:
                for g in m.groups():
                    if g is not None:
                        return g.strip(), lineno
        return None, None

    def _probe(cmd):
        if not cmd:
            return None, "no probe command"
        full = f"{prefix} {shlex.quote(cmd)}" if prefix else cmd
        try:
            r = subprocess.run(full, shell=True, capture_output=True,
                               text=True, timeout=timeout)
            if r.returncode != 0:
                return None, (r.stderr or r.stdout or "nonzero exit").strip()[:100]
            return r.stdout.strip(), None
        except Exception as e:
            return None, str(e)[:100]

    def _match(op, asserted, live):
        a, l = asserted.strip(), live.strip()
        if op == "contains":
            return bool(a) and (a in l or l in a)
        if op == "ge":
            try:
                return float(l) >= float(a)
            except ValueError:
                return False
        return a == l   # default: eq

    ok, stale, uncheck = [], [], []
    for c in claims:
        cid = c.get("id", "?")
        fil = c.get("file", "")
        asserted, lineno = _asserted(fil, c.get("pattern", ""))
        if asserted is None:
            uncheck.append((cid, f"anchor not found in {fil}"))
            continue
        live, err = _probe(c.get("probe", ""))
        if live is None:
            uncheck.append((cid, f"probe failed ({err})"))
            continue
        if _match(c.get("compare", "eq"), asserted, live):
            ok.append((cid, asserted, live))
        else:
            stale.append((cid, fil, lineno, asserted, live, c.get("compare", "eq")))

    print(f"\n  live-claims registry: {reg_path}")
    if not parsed.quiet:
        for cid, a, l in ok:
            print(f"  {GREEN}OK{RESET}    {cid}: memory={a!r} == live={l!r}")
    for cid, msg in uncheck:
        print(f"  {YELLOW}SKIP{RESET}  {cid}: {msg} [uncheckable — not counted]")
    for cid, fil, lineno, a, l, op in stale:
        print(f"  {RED}STALE{RESET} {cid}: {fil}:{lineno} asserts {a!r} but live is {l!r} [{op}]")
    print(f"\n  {len(ok)} ok | {RED}{len(stale)} STALE{RESET} | {len(uncheck)} uncheckable\n")

    if parsed.post_council and stale:
        cfg_council = _gaius_cfg.get("council", {})
        base_url = cfg_council.get("base_url", "").rstrip("/")
        api_key = cfg_council.get("api_key", "")
        if base_url and api_key:
            items = [f"{cid}: {fil} asserts {a!r} but live is {l!r}"
                     for cid, fil, _, a, l, _ in stale]
            payload = json.dumps({
                "type": "alert", "channel": "alerts", "agents": ["ops-watchdog"],
                "content": {
                    "title": "gaius live-claim drift (memory vs cluster)",
                    "stale_count": len(stale), "items": items,
                    "source": "gaius drift --live --post-council (nightly)",
                },
            }).encode()
            req = urllib.request.Request(
                f"{base_url}/council/log", data=payload,
                headers={"Content-Type": "application/json", "X-API-Key": api_key},
                method="POST")
            try:
                urllib.request.urlopen(req, timeout=10)
                print(f"[drift --live] Posted {len(stale)} stale claim(s) to council alerts.")
            except urllib.error.HTTPError as e:
                print(f"[drift --live] WARNING: council POST failed: {e.code}", file=sys.stderr)
        else:
            print("[drift --live] --post-council: council.base_url/api_key not set in ~/.gaius/config.yaml",
                  file=sys.stderr)

    sys.exit(1 if stale else 0)


def cmd_drift(args):
    """Check canonical cluster facts for cross-agent drift.

    Reads drift-facts.yaml (or --registry path), extracts the expected value
    from each fact's canonical source file, then greps each check_in location
    for the same value. Reports mismatches with file + line context.

    Exits 0 if clean, 1 if any drift detected (enables git pre-commit use).

    Usage:
      gaius drift [--registry PATH] [--post-council] [--json]

    Options:
      --registry PATH   Path to drift-facts.yaml (default: alongside gaius source)
      --post-council    POST any detected drift to council alerts channel
      --json            Emit JSON report instead of human-readable text
      --quiet           Suppress clean-fact lines, only show drift/warnings
    """
    import argparse as _ap
    import re
    import urllib.request
    import urllib.error

    parser = _ap.ArgumentParser(prog="gaius drift")
    parser.add_argument("--registry", default=None,
                        help="Path to drift-facts.yaml (default: gaius source dir)")
    parser.add_argument("--post-council", action="store_true",
                        help="POST drift findings to council alerts channel")
    parser.add_argument("--json", dest="json_out", action="store_true",
                        help="Emit JSON report")
    parser.add_argument("--quiet", action="store_true",
                        help="Only show drift/warnings, suppress clean lines")
    parser.add_argument("--live", action="store_true",
                        help="Validate memory claims against LIVE cluster state (live-claims.yaml)")
    parsed = parser.parse_args(args)

    if parsed.live:
        return _drift_live(parsed)

    # --- Locate registry ---
    if parsed.registry:
        registry_path = Path(parsed.registry).expanduser()
    else:
        # Default: alongside the gaius package source
        registry_path = Path(__file__).parent.parent / "drift-facts.yaml"
        if not registry_path.exists():
            registry_path = Path.home() / "Projects" / "agent-memory" / "gaius" / "drift-facts.yaml"

    if not registry_path.exists():
        print(f"[drift] ERROR: registry not found at {registry_path}", file=sys.stderr)
        print("[drift] Create drift-facts.yaml or pass --registry PATH", file=sys.stderr)
        sys.exit(1)

    try:
        with open(registry_path) as _f:
            registry = yaml.safe_load(_f) or {}
    except Exception as e:
        print(f"[drift] ERROR: cannot load registry: {e}", file=sys.stderr)
        sys.exit(1)

    facts = registry.get("facts", [])
    if not facts:
        print("[drift] No facts defined in registry.")
        return

    # --- Helper: extract first non-empty capture group from a file ---
    def _extract_value(filepath: str, pattern: str) -> tuple[str | None, int | None, str | None]:
        """Return (value, line_number, matched_line) or (None, None, None) if not found."""
        p = Path(filepath).expanduser()
        if not p.exists():
            return None, None, None
        try:
            text = p.read_text(errors="replace")
        except Exception:
            return None, None, None
        compiled = re.compile(pattern, re.IGNORECASE)
        for lineno, line in enumerate(text.splitlines(), 1):
            m = compiled.search(line)
            if m:
                # Return first non-empty capture group
                for grp in m.groups():
                    if grp is not None:
                        return grp.strip(), lineno, line.strip()
        return None, None, None

    # --- Process each fact ---
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    RED = "\033[0;31m"
    RESET = "\033[0m"

    results = []
    drift_count = 0
    warn_count = 0

    for fact in facts:
        key = fact.get("key", "?")
        desc = fact.get("description", "")
        canonical = fact.get("canonical", {})
        check_ins = fact.get("check_in", [])

        # 1. Get expected value from canonical source
        expected = None
        if "literal" in canonical:
            expected = str(canonical["literal"])
        elif "file" in canonical and "pattern" in canonical:
            expected, _, _ = _extract_value(canonical["file"], canonical["pattern"])

        if expected is None:
            results.append({
                "key": key, "status": "warn",
                "message": f"canonical value not found ({canonical.get('file', '?')})",
                "checks": [],
            })
            warn_count += 1
            continue

        # 2. Check each location
        fact_results = {"key": key, "description": desc, "expected": expected,
                        "status": "clean", "checks": []}
        clean_count = 0

        for loc in check_ins:
            loc_file = loc.get("file", "")
            loc_pattern = loc.get("pattern", "")
            found, lineno, matched_line = _extract_value(loc_file, loc_pattern)

            if found is None:
                fact_results["checks"].append({
                    "file": loc_file, "status": "not_found",
                    "expected": expected, "found": None, "line": None,
                })
                warn_count += 1
                if fact_results["status"] == "clean":
                    fact_results["status"] = "warn"
            elif found != expected:
                fact_results["checks"].append({
                    "file": loc_file, "status": "drift",
                    "expected": expected, "found": found,
                    "lineno": lineno, "matched_line": matched_line,
                })
                drift_count += 1
                fact_results["status"] = "drift"
            else:
                fact_results["checks"].append({
                    "file": loc_file, "status": "clean",
                    "expected": expected, "found": found, "lineno": lineno,
                })
                clean_count += 1

        results.append(fact_results)

    # --- Emit report ---
    if parsed.json_out:
        print(json.dumps({
            "drift_count": drift_count,
            "warn_count": warn_count,
            "facts": results,
        }, indent=2))
    else:
        total_locs = sum(len(r.get("checks", [])) for r in results)
        print(f"\nChecking {len(facts)} canonical facts across {total_locs} locations...\n")
        for r in results:
            key = r["key"]
            exp = r.get("expected", "?")
            status = r.get("status", "clean")
            checks = r.get("checks", [])
            clean = sum(1 for c in checks if c["status"] == "clean")
            total = len(checks)

            if status == "clean":
                if not parsed.quiet:
                    print(f"  {GREEN}✓{RESET} {key}: {exp} ({clean}/{total} locations match)")
            elif status == "warn":
                msg = r.get("message", "")
                if msg:
                    print(f"  {YELLOW}!{RESET} {key}: {YELLOW}{msg}{RESET}")
                for c in checks:
                    if c["status"] == "not_found":
                        print(f"    {YELLOW}!{RESET} NOT FOUND in {c['file']} (pattern matched 0 lines)")
            else:  # drift
                print(f"  {RED}✗{RESET} {key}: {RED}DRIFT DETECTED{RESET} (expected: {exp})")
                for c in checks:
                    if c["status"] == "drift":
                        print(f"    {RED}✗{RESET} {c['file']}")
                        print(f"        expected: {c['expected']}")
                        print(f"        found:    {c['found']}  (line {c.get('lineno', '?')}: {c.get('matched_line', '')[:80]})")
                    elif c["status"] == "not_found" and not parsed.quiet:
                        print(f"    {YELLOW}!{RESET} NOT FOUND in {c['file']}")
                    elif c["status"] == "clean" and not parsed.quiet:
                        print(f"    {GREEN}✓{RESET} {c['file']}: {c['found']}")
                print()

        summary_parts = []
        if drift_count:
            summary_parts.append(f"{RED}{drift_count} drift(s) detected{RESET}")
        if warn_count:
            summary_parts.append(f"{YELLOW}{warn_count} warning(s){RESET}")
        if not summary_parts:
            print(f"{GREEN}✓ All facts consistent across agents.{RESET}\n")
        else:
            print(f"\n{' | '.join(summary_parts)}\n")

    # --- Post to council if requested and drift found ---
    if parsed.post_council and drift_count > 0:
        cfg_council = _gaius_cfg.get("council", {})
        base_url = cfg_council.get("base_url", "").rstrip("/")
        api_key = cfg_council.get("api_key", "")
        if base_url and api_key:
            drift_items = [
                f"{r['key']}: expected {r.get('expected')} — "
                + "; ".join(
                    f"{c['file'].split('/')[-1]} has {c.get('found')}"
                    for c in r.get("checks", []) if c["status"] == "drift"
                )
                for r in results if r.get("status") == "drift"
            ]
            payload = json.dumps({
                "type": "alert",
                "channel": "alerts",
                "agents": ["ops-watchdog"],
                "content": {
                    "title": "gaius drift detected",
                    "drift_count": drift_count,
                    "items": drift_items,
                    "source": "gaius drift --post-council (nightly)",
                },
            }).encode()
            req = urllib.request.Request(
                f"{base_url}/council/log",
                data=payload,
                headers={"Content-Type": "application/json", "X-API-Key": api_key},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=10)
                print(f"[drift] Posted {drift_count} drift(s) to council alerts.")
            except urllib.error.HTTPError as e:
                print(f"[drift] WARNING: council POST failed: {e.code}", file=sys.stderr)
        else:
            print("[drift] --post-council: council.base_url/api_key not set in ~/.gaius/config.yaml",
                  file=sys.stderr)

    sys.exit(1 if drift_count > 0 else 0)


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
# ── Session-format adapters (extracted to gaius/parsers.py) ──────────────────
from gaius.parsers import (  # noqa: E402
    detect_format,
    parse_claude_events,
    parse_gemini_events,
    parse_pentagi_flow,
    parse_pentagi_flow_from_jsonl,
    parse_ollama_events,
    _content_blocks_to_text,
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
    cmd_maturity, cmd_readiness, cmd_snapshot, cmd_governor, cmd_route,
)


from gaius.outcomes import (  # noqa: E402,F401  re-export (outcomes split 2026-07-01)
    _ensure_outcomes_table, ingest_outcomes, outcome_winrates, cmd_ingest_outcomes,
)

from gaius.corpus_audit import (  # noqa: E402,F401  re-export (corpus_audit split 2026-07-01)
    REPETITION_THRESHOLD, repetition_candidates, corpus_audit_stats,
    route_suggest, cmd_corpus_audit, cmd_route_suggest,
)

from gaius.reconcile import (  # noqa: E402,F401  re-export (reconcile split 2026-07-01)
    load_source_registry, source_divergence, reconcile_source,
    _reconcile_excluded, _dir_fingerprint, remote_divergence, cmd_reconcile,
)

from gaius.landscape import (  # noqa: E402,F401  re-export (landscape split 2026-07-01)
    cmd_inject, cmd_landscape,
)

from gaius.concord import (  # noqa: E402,F401  re-export (cross-session coordination, 2026-07-17 — OSS-included)
    cmd_concord, init_concord,
)

COMMANDS = {
    "init":       cmd_init,
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
    "embed":           cmd_embed,
    "kg":              cmd_kg,
    "decay":           cmd_decay,
    "sync-memory":     cmd_sync_memory,
    "suggest":         cmd_suggest,
    "drift":           cmd_drift,
    "record":          cmd_record,
    "rescore":         cmd_rescore,
    "ingest-outcomes": cmd_ingest_outcomes,
    "corpus-audit":    cmd_corpus_audit,
    "route-suggest":   cmd_route_suggest,
    "reconcile":       cmd_reconcile,
    "concord":         cmd_concord,
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

    # Ensure facts.db is initialized on every run
    init_db()

    COMMANDS[command](cmd_argv)


if __name__ == "__main__":
    main()
