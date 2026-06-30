#!/usr/bin/env python3
"""bench_longmemeval.py -- gaius retrieval on LongMemEval-S (ICLR 2025).

External, third-party evaluation of gaius's retrieval. The queries, the haystack,
and the gold labels are LongMemEval's, not ours -- unlike the bundled demo, this is
not graded on our own corpus.

What it measures: gaius's real retrieval ranking, in a config matrix:
  granularity  = session (whole session as one unit) | turn (each turn a unit)
  retrieval    = semantic (cosine, dot-product of stored embeddings -- exactly what
                 `gaius inject` does at _core.py:3488) |
                 hybrid (gaius inject's full ranking: 0.3*tfidf + 0.7*bm25, then
                 0.4*score + 0.6*cosine with a 0.3 cosine floor -- _core.py:3502/3509-3513,
                 reusing the real bm25_score / compute_entry_tfidf_score)

Per question: index its ~48 haystack sessions (or their turns) into in-memory units,
embed with gaius's embedder, rank, then score recall against gold answer_session_ids
(turn hits map back to their session; sessions deduped in rank order).

Data: longmemeval_s_cleaned.json (xiaowu0162/longmemeval-cleaned on HF). Download:
  mkdir -p ~/.cache/longmemeval && wget -q \
    https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json \
    -O ~/.cache/longmemeval/longmemeval_s.json

Run with a python that has sentence-transformers + sqlite-vec + numpy installed.

Two measurement caveats (verified, not bugs):
  - METRIC: R@k here is per-question fractional recall (|gold n topk| / |gold|), strictly
    stricter than the hit-rate (recall_any@k) that published third-party baselines report.
    Compare hit@k to those baselines, not R@k. 65% of LongMemEval-S questions are multi-gold.
  - TRUNCATION: all-MiniLM-L6-v2 caps at 256 tokens (faithful to gaius's real embedder).
    LongMemEval sessions median ~2180 tok -> session-granularity embeddings are first-256-token
    approximations; turn units mostly fit. So session-vs-turn is not a clean granularity-only
    comparison, and gaius's real short-fact corpus (50-500 chars) matches the turn regime, not
    the clipped-session one. The bm25/tfidf signal in hybrid sees full text and is unaffected.
"""
import argparse
import collections
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DEFAULT_DATA = Path.home() / ".cache" / "longmemeval" / "longmemeval_s.json"
CONFIGS = [("session", "semantic"), ("session", "hybrid"),
           ("turn", "semantic"), ("turn", "hybrid")]


def turn_text(t):
    return f"{t.get('role', '')}: {t.get('content', '')}"


def session_text(session):
    return "\n".join(turn_text(t) for t in session)


def units_for(q, granularity):
    """Return [(unit_key, session_id, text)] at the requested granularity."""
    sids, sessions = q["haystack_session_ids"], q["haystack_sessions"]
    out = []
    if granularity == "session":
        for i, (sid, s) in enumerate(zip(sids, sessions)):
            out.append((f"{i}:{sid}", sid, session_text(s)))
    else:
        for i, (sid, s) in enumerate(zip(sids, sessions)):
            for j, t in enumerate(s):
                out.append((f"{i}:{j}:{sid}", sid, turn_text(t)))
    return out


_MODEL = None


def embed_batch(texts):
    """Batch-encode with all-MiniLM-L6-v2, normalized -- identical vectors to gaius's
    embedder (same model), just vectorized for speed instead of per-call over the daemon."""
    global _MODEL
    if not texts:
        return []
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer("all-MiniLM-L6-v2")
    arr = _MODEL.encode(texts, normalize_embeddings=True, batch_size=128, show_progress_bar=False)
    return [np.asarray(v, dtype=np.float32) for v in arr]


def rank_semantic(units, vecs, qv):
    """Pure cosine (dot of normalized embeddings -- gaius inject's semantic signal)."""
    scores = [float(np.dot(qv, v)) if v is not None else -1e9 for v in vecs]
    order = sorted(range(len(units)), key=lambda i: scores[i], reverse=True)
    return [units[i] for i in order]


def rank_hybrid(core, units, vecs, question, qv):
    """gaius inject's full ranking: 0.3*tfidf + 0.7*bm25, then 0.4*score + 0.6*cosine
    with a 0.3 cosine floor (sub-floor facts get *0.1). Reuses the real gaius functions."""
    entries = [{"sections": {"key_concepts": txt}, "is_fact": True, "uuid": uk}
               for (uk, _sid, txt) in units]
    doc_freq = core.build_doc_freq(entries)
    total_docs = len(entries)
    task_terms = re.sub(r'[^\w\s]', ' ', question.lower()).split()
    bm25_df, bm25_avg_len = core._build_bm25_doc_freq(entries, set(task_terms))
    scored = []
    for idx, entry in enumerate(entries):
        score = core.compute_entry_tfidf_score(entry, doc_freq, total_docs)
        if task_terms:
            bm25 = core.bm25_score(task_terms, entry, bm25_df, total_docs, bm25_avg_len)
            score = 0.3 * score + 0.7 * bm25
        v = vecs[idx]
        if v is not None:
            cos = float(np.dot(qv, v))
            if cos < 0.3:
                score *= 0.1
            else:
                score = 0.4 * score + 0.6 * max(0.0, cos)
        scored.append((score, idx))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [units[i] for _s, i in scored]


def ranked_sessions(ranked_units):
    """Dedupe session ids in rank order."""
    seen, out = set(), []
    for (_uk, sid, _txt) in ranked_units:
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--limit", type=int, default=0, help="0 = all 500 questions")
    ap.add_argument("--k", type=int, nargs="+", default=[1, 3, 5, 10])
    ap.add_argument("--granularity", choices=["session", "turn"], default="session")
    ap.add_argument("--retrieval", choices=["semantic", "hybrid"], default="semantic")
    ap.add_argument("--matrix", action="store_true", help="run all 4 configs in one pass")
    args = ap.parse_args()

    os.environ.setdefault("GAIUS_CONFIG", "/dev/null")
    from gaius import _core

    if not Path(args.data).exists():
        print(f"FATAL: {args.data} not found -- see download cmd in the module docstring",
              file=sys.stderr)
        sys.exit(2)
    data = json.load(open(args.data))
    if args.limit:
        data = data[:args.limit]
    n = len(data)
    maxk = max(args.k)
    configs = CONFIGS if args.matrix else [(args.granularity, args.retrieval)]
    grans = sorted({g for g, _ in configs})

    M = {cfg: {"rec": {k: [] for k in args.k}, "hit": {k: [] for k in args.k}, "rr": [],
               "byt": collections.defaultdict(lambda: {k: [] for k in args.k})}
         for cfg in configs}
    t0 = time.time()

    for qi, q in enumerate(data):
        gold = set(q["answer_session_ids"])
        units = {g: units_for(q, g) for g in grans}
        vecs = {g: embed_batch([u[2] for u in units[g]]) for g in grans}
        qv = embed_batch([q["question"]])[0]

        for cfg in configs:
            g, r = cfg
            if r == "semantic":
                ru = rank_semantic(units[g], vecs[g], qv)
            else:
                ru = rank_hybrid(_core, units[g], vecs[g], q["question"], qv)
            rs = ranked_sessions(ru)
            first = next((i + 1 for i, s in enumerate(rs) if s in gold), 0)
            M[cfg]["rr"].append(1.0 / first if first else 0.0)
            for k in args.k:
                topk = set(rs[:k])
                rec = len(gold & topk) / len(gold) if gold else 0.0
                M[cfg]["rec"][k].append(rec)
                M[cfg]["hit"][k].append(1.0 if (gold & topk) else 0.0)
                M[cfg]["byt"][q["question_type"]][k].append(rec)

        if (qi + 1) % 50 == 0:
            print(f"  {qi + 1}/{n}  ({time.time() - t0:.0f}s)", file=sys.stderr)

    print(f"\nLongMemEval-S -- gaius retrieval (all-MiniLM-L6-v2), {n} questions")
    print("=" * 76)
    hdr = "  config".ljust(24) + "MRR    " + "  ".join(f"R@{k}".rjust(6) for k in args.k) \
        + "   " + "  ".join(f"hit@{k}".rjust(7) for k in args.k)
    print(hdr)
    print("  " + "-" * 74)
    for cfg in configs:
        m = M[cfg]
        name = f"{cfg[0]}-{cfg[1]}"
        row = f"  {name:<22}{sum(m['rr']) / n:.3f} "
        row += "  ".join(f"{sum(m['rec'][k]) / n * 100:5.1f}" for k in args.k)
        row += "   " + "  ".join(f"{sum(m['hit'][k]) / n * 100:6.1f}" for k in args.k)
        print(row)
    # per-type for the best-by-R@maxk config
    best = max(configs, key=lambda c: sum(M[c]["rec"][maxk]) / n)
    print(f"\n  by question_type (R@{maxk}, best config = {best[0]}-{best[1]}):")
    for t, d in sorted(M[best]["byt"].items()):
        v = d[maxk]
        print(f"    {t:28s} {sum(v) / len(v) * 100:5.1f}%  (n={len(v)})")
    print(f"\n  elapsed: {time.time() - t0:.0f}s")
    print("  NOTE: R@k = per-question fractional recall |gold n topk|/|gold| (stricter on the")
    print("        65% multi-gold questions). hit@k = recall_any@k (>=1 gold in top-k).")
    print("  For context, published same-model (all-MiniLM-L6-v2) session-level retrieval reports")
    print("    ~96-99% recall_any@k (compare against our hit@k, NOT R@k; protocols differ, and this")
    print("    is retrieval-only, NOT the official LongMemEval QA-accuracy metric).")
    print("  Compare against our hit@k column, NOT R@k. all-MiniLM-L6-v2 caps at 256 tokens:")
    print("    session units (median ~2180 tok) are clipped to their first ~256; turn units mostly fit.")


if __name__ == "__main__":
    main()
