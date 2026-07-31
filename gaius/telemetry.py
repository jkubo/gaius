"""gaius telemetry — lightweight event logging for injection quality monitoring.

Event types:
  - prompt_events: every UserPromptSubmit hook invocation
  - enforcement_events: every PreToolUse hard-enforce check
  - session_summaries: aggregated at session stop
  - tool_events: every tool call (gaius-observe PreToolUse hook + MCP tool
    decorator) — args stored only as sha256 hash + redacted compact summary

DB lives at ~/.gaius/telemetry.db (separate from facts.db — no schema coupling).
GAIUS_TELEMETRY_DB env var overrides the path (tests / sandboxes).
"""

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

_DB_PATH = Path(os.environ.get("GAIUS_TELEMETRY_DB") or (Path.home() / ".gaius" / "telemetry.db"))
_conn = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), timeout=5)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS prompt_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,                     -- unix timestamp
            session_id TEXT,
            prompt_hash TEXT,                     -- sha256[:12] of raw prompt
            prompt_len INTEGER,                   -- raw character count
            terms_raw INTEGER,                    -- word count before stop filter
            terms_filtered INTEGER,               -- word count after stop filter
            skip_reason TEXT,                     -- null=injected, 'short','dedup','slash','no_match','timeout','error'
            entries_injected INTEGER DEFAULT 0,
            memory_files_injected INTEGER DEFAULT 0,
            memory_types TEXT,                    -- JSON: {"Feedback":1,"Domain":1}
            tokens_used INTEGER DEFAULT 0,
            budget INTEGER DEFAULT 0,
            top_cosine REAL,                      -- highest cosine in injected set
            active_skill TEXT                     -- skill active at prompt time
        );

        CREATE TABLE IF NOT EXISTS enforcement_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            session_id TEXT,
            command_hash TEXT,                    -- sha256[:16] of command
            check_name TEXT,                     -- which rule matched
            result TEXT,                         -- 'allow','block','bypass','soft_deny'
            skill TEXT,                          -- active skill (for bypass audit)
            message TEXT                         -- block message shown to agent
        );

        CREATE TABLE IF NOT EXISTS injection_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            session_id TEXT,
            prompt_hash TEXT,
            fact_key TEXT,                        -- links to facts.fact_key
            score REAL,
            priority REAL,
            cosine REAL,
            source TEXT                          -- 'corpus','memory_feedback','memory_domain','memory_project'
        );

        CREATE TABLE IF NOT EXISTS skill_injections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,                  -- unix timestamp
            session_id TEXT,
            skill TEXT,                           -- skill name
            gate TEXT,                            -- skill gate (always/mandate/hard/reference)
            score REAL                            -- per-token score at injection time (nullable)
        );

        CREATE TABLE IF NOT EXISTS tool_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,                     -- unix timestamp
            session_id TEXT,
            tool_name TEXT,                       -- Bash, Edit, gaius_search, ...
            args_sha256 TEXT,                     -- sha256 of full canonical-JSON tool_input (pre-redaction)
            args_redacted TEXT,                   -- compact redacted summary of tool_input
            source TEXT DEFAULT 'hook',           -- 'hook' (gaius-observe) | 'mcp' (mcp_server decorator)
            event TEXT DEFAULT 'pre',             -- 'pre','post'
            project TEXT                          -- project hash (sha256(git remote)[:12], from gaius-observe)
        );

        CREATE TABLE IF NOT EXISTS coaching_tips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tip_key TEXT UNIQUE NOT NULL,         -- e.g. 'short_prompt', 'no_domain_terms'
            title TEXT NOT NULL,
            body TEXT NOT NULL,                   -- markdown guidance
            category TEXT NOT NULL,              -- 'prompt','injection','enforcement'
            severity TEXT DEFAULT 'info'          -- 'info','warning','action'
        );

        CREATE INDEX IF NOT EXISTS idx_prompt_ts ON prompt_events(ts);
        CREATE INDEX IF NOT EXISTS idx_prompt_session ON prompt_events(session_id);
        CREATE INDEX IF NOT EXISTS idx_enforce_ts ON enforcement_events(ts);
        CREATE INDEX IF NOT EXISTS idx_injfact_key ON injection_facts(fact_key);
        CREATE INDEX IF NOT EXISTS idx_injfact_prompt ON injection_facts(prompt_hash);
        CREATE INDEX IF NOT EXISTS idx_skillinj_ts ON skill_injections(ts);
        CREATE INDEX IF NOT EXISTS idx_skillinj_skill ON skill_injections(skill);
        CREATE INDEX IF NOT EXISTS idx_toolev_ts ON tool_events(ts);
        CREATE INDEX IF NOT EXISTS idx_toolev_session ON tool_events(session_id);
    """)
    # Seed coaching tips if empty
    count = conn.execute("SELECT COUNT(*) FROM coaching_tips").fetchone()[0]
    if count == 0:
        _seed_coaching_tips(conn)
    conn.commit()


def _seed_coaching_tips(conn: sqlite3.Connection):
    tips = [
        # Prompt quality
        ("short_prompt", "Prompts under 30 characters rarely trigger useful injection",
         "Gaius needs semantic signal to find relevant context. Short prompts like "
         "\"fix it\" or \"what's next\" produce zero BM25 matches.\n\n"
         "**Try instead:** Include the subsystem, symptom, or goal — "
         "\"fix storage split-brain on node-01\" gives gaius 4 useful search terms.",
         "prompt", "info"),

        ("no_terms_after_filter", "All meaningful words were filtered as stop words",
         "After removing common English words, your prompt had zero search terms. "
         "This means gaius couldn't find relevant context to inject.\n\n"
         "**Tip:** Use specific technical terms — service names, error messages, "
         "component names. \"Deploy oauth2 proxy on prod\" has 4 useful terms; "
         "\"set up the new thing\" has zero.",
         "prompt", "warning"),

        ("low_diversity", "You've been asking similar prompts — injection is repetitive",
         "When the same terms appear across multiple prompts, gaius injects the same "
         "facts repeatedly. This wastes context budget on stale information.\n\n"
         "**Tip:** If you're deep in a debugging session, try rephrasing with the "
         "specific error or symptom instead of repeating the general topic.",
         "prompt", "info"),

        ("vague_intent", "Prompts without clear intent produce scattered injection",
         "Questions like \"anything else?\" or \"what should I do?\" don't give gaius "
         "enough signal to find relevant context.\n\n"
         "**Distillation technique:** Before prompting, ask yourself: *What specific "
         "system/component am I working on? What's the symptom or goal?* Then include "
         "those terms. This trains you to frame problems precisely — a skill that "
         "improves both AI collaboration and human communication.",
         "prompt", "info"),

        # Injection quality
        ("budget_underutilized", "Less than 30% of injection budget was used",
         "The injection pipeline found very few relevant facts for your prompt. "
         "This often means the corpus lacks coverage for your current topic.\n\n"
         "**Action:** After resolving this task, consider whether key learnings "
         "should be distilled into a domain file or feedback entry. The memory "
         "system improves when sessions close the loop.",
         "injection", "info"),

        ("no_domain_file", "No domain file matched — deep context unavailable",
         "Domain files contain the richest context (architecture, gotchas, patterns) "
         "but none matched your prompt above the cosine similarity threshold.\n\n"
         "**Tip:** If you're working in a specific subsystem (storage, networking, "
         "security), mention it by name. \"DRBD\" triggers storage.md; \"oauth2\" "
         "triggers services.md. The model name matters more than the description.",
         "injection", "warning"),

        ("stale_facts_dominant", "Most injected facts are >14 days old",
         "The injected context is mostly from older sessions. Recent work may have "
         "changed the landscape without updating the corpus.\n\n"
         "**Distillation technique:** At the end of significant sessions, spend 30 "
         "seconds on what surprised you. If you discovered a gotcha, workaround, or "
         "architectural decision — that's a memory candidate. Say \"remember: X\" "
         "and the system will capture it.",
         "injection", "info"),

        ("memory_budget_exhausted", "Memory files consumed >80% of injection budget",
         "Feedback and domain files used most of the token budget, leaving little "
         "room for corpus facts. This happens when many HARD GATE rules match.\n\n"
         "**This is usually correct behavior** — safety rails should take priority. "
         "If the rules feel excessive, review them at /gaius and consider whether "
         "any are stale or overly broad.",
         "injection", "info"),

        # Enforcement
        ("hard_gate_fired", "A HARD GATE rule was injected for this session",
         "A safety rule was surfaced because your prompt matched its trigger pattern. "
         "These rules exist because previous sessions caused real incidents.\n\n"
         "**The rule is context, not restriction.** Read it to understand *why* the "
         "gate exists. If the rule no longer applies, tell the system to forget it.",
         "enforcement", "info"),

        ("violation_detected", "The model acted contrary to an injected HARD GATE",
         "A safety rule was injected but the model's action violated it. This is "
         "logged for review.\n\n"
         "**Action:** Review the violation at /gaius under Enforcement. Determine "
         "if the rule needs strengthening (add to gaius-hard-enforce hook for exit-2 "
         "blocking) or if the model's action was actually correct (update the rule).",
         "enforcement", "action"),

        ("enforcement_bypass", "A hard-enforce block was bypassed",
         "The gaius-hard-enforce hook detected a blocked pattern but the operation "
         "proceeded anyway. This can happen via indirect execution (Write + bash) "
         "or skill-based bypass.\n\n"
         "**Action:** Review whether the bypass was legitimate (skill-authorized) "
         "or represents a gap in enforcement coverage.",
         "enforcement", "action"),

        ("rule_never_fires", "A HARD GATE rule hasn't matched any prompt in 30+ days",
         "Rules that never fire may be stale (the condition no longer applies) or "
         "too narrowly scoped (the trigger pattern doesn't match real prompts).\n\n"
         "**Action:** Review the rule. If the underlying risk still exists, widen "
         "the trigger. If the risk is resolved, retire the rule.",
         "enforcement", "warning"),
    ]
    conn.executemany(
        "INSERT INTO coaching_tips (tip_key, title, body, category, severity) VALUES (?, ?, ?, ?, ?)",
        tips
    )


# ── Public API ────────────────────────────────────────────────────────────────

def log_prompt_event(
    session_id: str,
    prompt_hash: str,
    prompt_len: int,
    terms_raw: int,
    terms_filtered: int,
    skip_reason: str | None = None,
    entries_injected: int = 0,
    memory_files_injected: int = 0,
    memory_types: dict | None = None,
    tokens_used: int = 0,
    budget: int = 0,
    top_cosine: float | None = None,
    active_skill: str = "",
):
    conn = _get_conn()
    conn.execute(
        """INSERT INTO prompt_events
           (ts, session_id, prompt_hash, prompt_len, terms_raw, terms_filtered,
            skip_reason, entries_injected, memory_files_injected, memory_types,
            tokens_used, budget, top_cosine, active_skill)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), session_id, prompt_hash, prompt_len, terms_raw, terms_filtered,
         skip_reason, entries_injected, memory_files_injected,
         json.dumps(memory_types) if memory_types else None,
         tokens_used, budget, top_cosine, active_skill)
    )
    conn.commit()


def log_enforcement_event(
    session_id: str,
    command_hash: str,
    check_name: str,
    result: str,
    skill: str = "",
    message: str = "",
):
    conn = _get_conn()
    conn.execute(
        """INSERT INTO enforcement_events
           (ts, session_id, command_hash, check_name, result, skill, message)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), session_id, command_hash, check_name, result, skill, message)
    )
    conn.commit()


def log_injection_fact(
    session_id: str,
    prompt_hash: str,
    fact_key: str,
    score: float,
    priority: float,
    cosine: float | None = None,
    source: str = "corpus",
):
    conn = _get_conn()
    conn.execute(
        """INSERT INTO injection_facts
           (ts, session_id, prompt_hash, fact_key, score, priority, cosine, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), session_id, prompt_hash, fact_key, score, priority, cosine, source)
    )
    conn.commit()


def log_skill_injection(session_id: str, skills: list):
    """Best-effort: one row per injected skill.

    `skills` is a list of skill dicts (from load_skills / cmd_inject) — each has
    at least 'name' and 'gate'; per-token score may be stashed under '_score'.
    Non-finite scores (e.g. the gate:always float('inf') sentinel) are stored as
    NULL. Mirrors the try/except pattern of log_injection_fact — a telemetry
    failure must never break injection.
    """
    import math
    try:
        conn = _get_conn()
        ts = int(time.time())
        rows = []
        for s in skills or []:
            score = s.get("_score")
            if score is not None:
                try:
                    if not math.isfinite(float(score)):
                        score = None
                    else:
                        score = float(score)
                except (TypeError, ValueError):
                    score = None
            rows.append((ts, session_id, s.get("name", ""), s.get("gate", ""), score))
        if rows:
            conn.executemany(
                "INSERT INTO skill_injections (ts, session_id, skill, gate, score) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
    except Exception:
        pass  # telemetry must never break injection


# ── Tool-event capture (args hashing + redaction) ────────────────────────────
# Raw tool_input is NEVER stored: only a sha256 of the canonical JSON (for
# dedup/correlation) plus a compact redacted summary. Redaction runs known
# token/credential patterns AND sensitive-key-name masking BEFORE storage —
# when in doubt, redact.

_REDACT_MAX = 600          # chars of redacted summary kept
_REDACTED = "[REDACTED]"

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(token|secret|passwd|password|credential|api[_-]?key|apikey|"
    r"private[_-]?key|access[_-]?key|session[_-]?key|client[_-]?secret|"
    r"authorization|vault[_-]?pass|cookie)"
)

_REDACT_PATTERNS = [re.compile(p) for p in (
    # private key blocks (redact even if unterminated)
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END[A-Z ]*PRIVATE KEY-----|\Z)",
    r"\bgh[pousr]_[A-Za-z0-9]{8,}\b",                # GitHub classic tokens (ghp_/gho_/ghu_/ghs_/ghr_)
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",             # GitHub fine-grained PAT
    r"\bsk-ant-[A-Za-z0-9_-]{16,}\b",                # Anthropic API keys
    r"\bsk-[A-Za-z0-9_-]{20,}\b",                    # OpenAI-style keys
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",             # Slack tokens
    r"\bAKIA[0-9A-Z]{16}\b",                         # AWS access key id
    r"\btskey-[A-Za-z0-9-]{12,}\b",                  # Tailscale keys
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b",  # JWT
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}",       # Authorization: Bearer …
    # key=value / key: value assignments — keep the key name, drop the value
    # (lookahead skips values already masked by the key-name pass)
    r"(?i)\b(api[_-]?key|apikey|token|secret|passwd|password|client[_-]?secret|auth)\b"
    r"['\"]?\s*[:=]\s*['\"]?(?!\[REDACTED\])[^\s'\",;&]{6,}",
)]


def _canonical_json(obj) -> str:
    """Deterministic compact JSON of a tool_input payload (sorted keys)."""
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


def hash_args(tool_input) -> str:
    """sha256 hex digest of the full canonical-JSON tool_input (pre-redaction)."""
    return hashlib.sha256(
        _canonical_json(tool_input).encode("utf-8", "replace")
    ).hexdigest()


def _redact_obj(obj):
    """Recursively mask values whose key name looks credential-bearing."""
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k)
                else _redact_obj(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return obj


def redact_args(tool_input) -> str:
    """Compact redacted summary of tool_input — safe to store/display."""
    text = _canonical_json(_redact_obj(tool_input))
    for pat in _REDACT_PATTERNS:
        text = pat.sub(
            lambda m: (m.group(1) + "=" + _REDACTED) if (m.lastindex or 0) >= 1
            else _REDACTED,
            text,
        )
    if len(text) > _REDACT_MAX:
        text = text[:_REDACT_MAX] + f"…[+{len(text) - _REDACT_MAX} chars]"
    return text


def log_tool_event(
    session_id: str,
    tool_name: str,
    tool_input=None,
    source: str = "hook",
    event: str = "pre",
    project: str = "",
):
    """Best-effort: one row per tool call (hook or MCP path).

    Stores hash + redacted summary only — never the raw args. Mirrors the
    try/except pattern of log_skill_injection: a telemetry failure must never
    block or fail the tool call.
    """
    try:
        args_sha256 = hash_args(tool_input)
        args_redacted = redact_args(tool_input)
        conn = _get_conn()
        conn.execute(
            """INSERT INTO tool_events
               (ts, session_id, tool_name, args_sha256, args_redacted, source, event, project)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), session_id, tool_name, args_sha256, args_redacted,
             source, event, project),
        )
        conn.commit()
    except Exception:
        pass  # telemetry must never break a tool call


def _cli_tool_event(argv: list) -> int:
    """`python3 -m gaius.telemetry tool-event <event> [project]` — reads the
    PreToolUse hook JSON (Claude snake_case or Grok camelCase envelope) on
    stdin and logs one tool_events row. Always exits 0, prints nothing —
    called from gaius-observe, which must stay fail-open."""
    import sys
    try:
        event = argv[0] if argv else "pre"
        project = argv[1] if len(argv) > 1 else ""
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        session_id = (payload.get("session_id") or payload.get("sessionId")
                      or os.environ.get("CLAUDE_SESSION_ID")
                      or os.environ.get("GROK_SESSION_ID") or "unknown")
        tool_name = payload.get("tool_name") or payload.get("toolName") or "unknown"
        tool_input = payload.get("tool_input") if "tool_input" in payload \
            else payload.get("toolInput")
        log_tool_event(session_id, tool_name, tool_input,
                       source="hook", event=event, project=project)
    except Exception:
        pass  # never fail the hook
    return 0


def get_summary(hours: int = 24) -> dict:
    """Aggregate telemetry for dashboard display."""
    conn = _get_conn()
    cutoff = time.time() - hours * 3600

    # Prompt quality
    total = conn.execute("SELECT COUNT(*) FROM prompt_events WHERE ts > ?", (cutoff,)).fetchone()[0]
    injected = conn.execute(
        "SELECT COUNT(*) FROM prompt_events WHERE ts > ? AND skip_reason IS NULL", (cutoff,)
    ).fetchone()[0]
    skipped = conn.execute(
        "SELECT skip_reason, COUNT(*) FROM prompt_events WHERE ts > ? AND skip_reason IS NOT NULL GROUP BY skip_reason",
        (cutoff,)
    ).fetchall()
    avg_terms = conn.execute(
        "SELECT AVG(terms_filtered) FROM prompt_events WHERE ts > ? AND skip_reason IS NULL", (cutoff,)
    ).fetchone()[0]
    avg_entries = conn.execute(
        "SELECT AVG(entries_injected) FROM prompt_events WHERE ts > ? AND skip_reason IS NULL", (cutoff,)
    ).fetchone()[0]
    avg_tokens = conn.execute(
        "SELECT AVG(tokens_used) FROM prompt_events WHERE ts > ? AND skip_reason IS NULL", (cutoff,)
    ).fetchone()[0]
    avg_budget = conn.execute(
        "SELECT AVG(budget) FROM prompt_events WHERE ts > ? AND skip_reason IS NULL", (cutoff,)
    ).fetchone()[0]

    # Enforcement
    blocks = conn.execute(
        "SELECT COUNT(*) FROM enforcement_events WHERE ts > ? AND result = 'block'", (cutoff,)
    ).fetchone()[0]
    bypasses = conn.execute(
        "SELECT COUNT(*) FROM enforcement_events WHERE ts > ? AND result = 'bypass'", (cutoff,)
    ).fetchone()[0]
    checks_total = conn.execute(
        "SELECT COUNT(*) FROM enforcement_events WHERE ts > ?", (cutoff,)
    ).fetchone()[0]

    # Top injected facts
    top_facts = conn.execute(
        """SELECT fact_key, COUNT(*) as cnt, AVG(score) as avg_score
           FROM injection_facts WHERE ts > ?
           GROUP BY fact_key ORDER BY cnt DESC LIMIT 10""",
        (cutoff,)
    ).fetchall()

    # Coaching triggers
    triggers = []
    if total > 0:
        skip_rate = (total - injected) / total
        if skip_rate > 0.5:
            triggers.append("short_prompt")
        if avg_terms and avg_terms < 2:
            triggers.append("no_terms_after_filter")
        if avg_tokens and avg_budget and avg_tokens / avg_budget < 0.3:
            triggers.append("budget_underutilized")
    if blocks > 0:
        triggers.append("hard_gate_fired")
    if bypasses > 0:
        triggers.append("enforcement_bypass")

    # Fetch matching coaching tips
    tips = []
    if triggers:
        placeholders = ",".join("?" * len(triggers))
        tips = [dict(r) for r in conn.execute(
            f"SELECT tip_key, title, body, category, severity FROM coaching_tips WHERE tip_key IN ({placeholders})",
            triggers
        ).fetchall()]

    return {
        "period_hours": hours,
        "prompts": {
            "total": total,
            "injected": injected,
            "skip_breakdown": {r[0]: r[1] for r in skipped},
            "avg_terms_filtered": round(avg_terms or 0, 1),
            "avg_entries_injected": round(avg_entries or 0, 1),
            "avg_tokens_used": int(avg_tokens or 0),
            "avg_budget": int(avg_budget or 0),
            "utilization_pct": round((avg_tokens or 0) / (avg_budget or 1) * 100, 1),
        },
        "enforcement": {
            "total_checks": checks_total,
            "blocks": blocks,
            "bypasses": bypasses,
        },
        "top_facts": [{"key": r[0], "count": r[1], "avg_score": round(r[2], 2)} for r in top_facts],
        "coaching": tips,
    }


def get_violations(limit: int = 50) -> list[dict]:
    """Get recent enforcement events for review."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM enforcement_events WHERE result IN ('block','bypass') ORDER BY ts DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import sys
    _args = sys.argv[1:]
    if _args and _args[0] == "tool-event":
        sys.exit(_cli_tool_event(_args[1:]))
    sys.exit(0)
