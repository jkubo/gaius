"""gaius.extract — session-content classification, stripping, and domain tagging.

Owns the extraction-side vocabulary (decision/finding/procedure patterns, section
headers, DOMAIN_KEYWORDS incl. the config merge), entry classification and score
boosting, the strip_bloat family, the noise/narration/reviewer-verdict filters
(_is_noise — imported by gaius.parsers via the _core facade), seeded scoring, and
clarified-intent distillation parsing.

Facade convention (see ARCHITECTURE.md): shared hub symbols are imported from
gaius._core at the top; _core re-imports this module's public symbols in the
Phase-A zone of the FACADE RE-EXPORTS block (which runs BEFORE parsers/kg/... so
their own `from gaius._core import` lines keep resolving).
"""
import copy
import hashlib
import json
import re
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

# imports from gaius._core (shared hub) — circular-by-design, see ARCHITECTURE.md
from gaius._core import (
    HAS_YAML, SPECS_DIR, GEMINI_COLD_THRESHOLD_HOURS, _gaius_cfg,
)

# Secret exclusion regex (follow sentinel.go pattern)
SECRET_KEYS_RE = re.compile(r'(password|secret|token|key|vault|crypt|private)', re.IGNORECASE)


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
}
# Domains named after a deployment's own products are not shipped defaults —
# they are exactly what `domain_keywords` in ~/.gaius/config.yaml is for, and
# baking them in both mis-classifies everyone else's sessions and publishes an
# internal product roster.
DOMAIN_KEYWORDS: dict = {
    **_DOMAIN_KEYWORDS_DEFAULT,
    **_gaius_cfg.get("domain_keywords", {}),
}


def extract_section(text: str, header: str) -> str:
    """Pull content of a numbered section from a compaction summary."""
    pattern = rf'\d+\.\s+{re.escape(header)}[^\n]*\n(.*?)(?=\n\d+\.\s+[A-Z]|\Z)'
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else ""


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

# ── Anchored mid-reasoning narration ───────────────────────────────────────
# A session's own planning prose ("Let me find out why…", "Now let me verify…")
# gets mined as a pending "fact", so one session's reasoning becomes the next
# session's review queue — measured ~7 new/day steady-state, which makes hand
# draining a treadmill.
#
# The clause must BEGIN within the first _NARRATION_ANCHOR_CHARS characters.
# That offset bound is the entire safety property: measured against the live
# pending set, the anchored form is ~100% precision over a 28-sample read, while
# the loose anywhere-in-text form matches ~24% of the queue and swallows real
# operational facts embedded mid-paragraph. Do NOT relax the bound.
_NARRATION_ANCHOR_CHARS = 40

_NARRATION_CLAUSE = re.compile(
    r"\b(?:"
    r"let me"
    r"|let['’]s(?!\s+encrypt\b)"   # "Let's Encrypt" is an issuer, not narration
    r"|i['’]ll"
    r"|i will"
    r"|i need to"
    r"|i['’]m going to"
    r"|i am going to"
    r")\s+\S",
    re.IGNORECASE,
)

# Reviewer-of-record verdict prose from a prior mnemos session — meta, not ops.
# Covers the QUOTED form ("… is my own prior verdict: \"Fact 3941 = … Keep\" …"),
# where the fact-ref sits mid-sentence and the verdict word follows closely.
_REVIEWER_VERDICT = re.compile(
    r"\bfact\s+\d+\s*=.{0,120}?\b(?:reject|keep via agent-review|defer)\b",
    re.IGNORECASE | re.DOTALL,
)

# The AUTHORED form — the verdict a mnemos session writes about a numbered fact.
# Measured 2026-07-26 over the 19,817 live facts: the pattern above caught 2 of
# the 13 live instances (~15% recall), because the assessment clause between the
# "=" and the verdict routinely runs 150-200 chars — past the 120-char window and
# past the 200-char head slice. Split into two conditions instead of widening it:
#
#   OPENER — the fact-ref must OPEN the text (markdown emphasis tolerated). This
#            is the precision anchor, same discipline as _NARRATION_ANCHOR_CHARS.
#            All 12 live texts that open this way are reviewer verdicts.
#   TERM   — a gaius review VERB (`agent-review`, `gaius reject`) anywhere, or a
#            verdict token that TERMINATES the text. Terminal-only is what keeps
#            a mid-sentence "…the operator may reject the claim." out of scope.
#
# Both must hold. Yield: 11 of 12 (the 12th carries no verdict term at all —
# under-catching is the intended failure mode).
_REVIEWER_VERDICT_OPENER = re.compile(r"^[\s*_#>\-]{0,8}fact\s+\d+\s*=", re.IGNORECASE)

_REVIEWER_VERDICT_TERM = re.compile(
    r"\bagent-review\b"
    r"|\bgaius\s+(?:reject|defer)\b"
    r"|\b(?:reject|keep|defer)\b[^.!?]{0,20}[.!]\s*$"
    r"|\bdefer(?:red)?\b[^.]{0,24}\bdays?\b",
    re.IGNORECASE,
)


def _is_noise(text: str) -> bool:
    """Return True if text matches known boilerplate/navigation patterns."""
    head = text[:200]  # only check first 200 chars
    for pat in NOISE_PATTERNS:
        if pat.search(head):
            return True
    if _REVIEWER_VERDICT.search(head):
        return True
    # Authored reviewer verdict: anchored opener AND a review term. Checked over
    # the FULL text — the verdict is the last clause, not the first 200 chars.
    if _REVIEWER_VERDICT_OPENER.search(text) and _REVIEWER_VERDICT_TERM.search(text):
        return True
    # Anchored only — a planning clause that STARTS past the bound is prose
    # wrapped around a real fact, and dropping it destroys signal.
    m = _NARRATION_CLAUSE.search(text[:_NARRATION_ANCHOR_CHARS + 20])
    if m and m.start() < _NARRATION_ANCHOR_CHARS:
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
