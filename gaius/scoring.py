"""gaius.scoring — ranking primitives: TF-IDF, BM25, decay, boosts, domain stats.

Pure scoring math plus the per-domain session-stats file (domain_stats.json in
the corpus dir). Consumed by the retire/index pipeline and by landscape's
cmd_inject ranking via the _core facade.

Facade convention (see ARCHITECTURE.md): shared hub symbols are imported from
gaius._core at the top; _core re-imports this module's public symbols in the
Phase-A zone of the FACADE RE-EXPORTS block.
"""
import json
import math
import re
from collections import Counter

# imports from gaius._core (shared hub) — circular-by-design, see ARCHITECTURE.md
from gaius._core import CORPUS_DIR

from gaius.extract import (
    SECTION_HEADERS, DECISION_KEYWORDS, DOMAIN_KEYWORDS, tag_domains_from_specs,
)

# TF-IDF scoring configuration
DECAY_HALF_LIFE = 90.0          # days — confidence halves without reconfirmation
CROSS_AGENT_MULTIPLIER = 1.5    # bonus when both claude and gemini confirm
BOOTSTRAP_THRESHOLD = 20        # sessions per domain before scoring applies
DOMAIN_STATS_FILE = "domain_stats.json"


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
