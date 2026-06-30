#!/usr/bin/env python3
"""gaius retrieval benchmark — measures how well inject finds relevant facts.

Uses hand-crafted queries with known ground-truth terms from facts.db.
Measures Recall@5, Recall@10, MRR, and NDCG@k.
Metrics adapted from the standard LongMemEval protocol.

Usage:
    python bench_retrieval.py                    # compare all modes (default)
    python bench_retrieval.py --mode keyword     # keyword-only
    python bench_retrieval.py --mode semantic    # semantic-only
    python bench_retrieval.py --mode hybrid      # RRF fusion
    python bench_retrieval.py --export eval.json # export eval set for external harnesses
"""

import json
import math
import os
import re
import sqlite3
import struct
import sys
from pathlib import Path

EMBED_DIM = 384

# ── Metrics (adapted from the standard LongMemEval protocol) ──────────────────

def dcg(relevances: list[float], k: int) -> float:
    """Discounted Cumulative Gain."""
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances[:k]))

def ndcg_at_k(ranked_facts: list[dict], query: dict, k: int) -> float:
    """NDCG@k — normalized DCG comparing actual ranking to ideal."""
    relevances = [1.0 if is_relevant(r["fact"], query) else 0.0 for r in ranked_facts[:k]]
    ideal = sorted(relevances, reverse=True)
    idcg = dcg(ideal, k)
    return dcg(relevances, k) / idcg if idcg > 0 else 0.0

# ── Benchmark Queries ────────────────────────────────────────────────────────
BENCHMARK_QUERIES = [
    {
        "query": "DRBD split brain recovery after node reboot",
        "domain": "storage",
        "relevant_terms": ["drbd", "quorum", "split-brain", "tiebreaker", "reboot"],
        "description": "Storage: DRBD failure mode"
    },
    {
        "query": "Tetragon eBPF trace capture from sandbox pods",
        "domain": "security",
        "relevant_terms": ["tetragon", "trace", "sandbox", "ebpf", "detonat"],
        "description": "Security: Tetragon trace pipeline"
    },
    {
        "query": "flannel VXLAN MTU cross-site networking timeout",
        "domain": "networking",
        "relevant_terms": ["flannel", "mtu", "vxlan", "cross-site", "timeout"],
        "description": "Networking: flannel cross-site issue"
    },
    {
        "query": "OAuth2 proxy ForwardAuth infinite redirect loop",
        "domain": "services",
        "relevant_terms": ["oauth2", "forwardauth", "redirect", "loop", "companion"],
        "description": "Services: OAuth2 ingress pattern"
    },
    {
        "query": "etcd quorum loss Headscale IP certificate SAN mismatch",
        "domain": "general",
        "relevant_terms": ["etcd", "quorum", "headscale", "san", "tls", "certificate"],
        "description": "General: etcd TLS SAN issue"
    },
    {
        "query": "SeaweedFS volume server capacity exhaustion write blocked",
        "domain": "storage",
        "relevant_terms": ["seaweedfs", "volume", "capacity", "max", "exhaustion"],
        "description": "Storage: SeaweedFS capacity"
    },
    {
        "query": "ingress controller node pinning cross-site latency broken",
        "domain": "networking",
        "relevant_terms": ["ingress", "pinning", "affinity", "node", "cross-site"],
        "description": "Networking: ingress placement"
    },
    {
        "query": "LINSTOR satellite CrashLoopBackOff missing kernel headers",
        "domain": "storage",
        "relevant_terms": ["linstor", "satellite", "crashloop", "kernel", "headers"],
        "description": "Storage: LINSTOR satellite fix"
    },
    {
        "query": "OTel collector memory limit dropped metrics no data Grafana",
        "domain": "observability",
        "relevant_terms": ["otel", "collector", "metric", "grafana", "dropped"],
        "description": "Observability: collector memory limit"
    },
    {
        "query": "BPF LSM etcd deadlock gRPC TLS handshake control plane",
        "domain": "general",
        "relevant_terms": ["bpf", "lsm", "etcd", "deadlock", "tls", "handshake"],
        "description": "General: BPF_LSM etcd issue"
    },
    {
        "query": "Multus DaemonSet restart cascade cluster outage",
        "domain": "networking",
        "relevant_terms": ["multus", "restart", "cascade", "daemonset", "outage"],
        "description": "Networking: Multus cascade"
    },
    {
        "query": "SeaweedFS filer Raft consensus volume archive NAS",
        "domain": "storage",
        "relevant_terms": ["seaweedfs", "filer", "raft", "archive", "nas"],
        "description": "Storage: SeaweedFS Raft"
    },
    {
        "query": "Nebula overlay mesh MTU fragmentation Tailscale",
        "domain": "networking",
        "relevant_terms": ["nebula", "mtu", "tailscale", "fragment", "overlay"],
        "description": "Networking: Nebula/Tailscale MTU"
    },
    {
        "query": "image hash dedup FNV luminance archival pipeline",
        "domain": "storage",
        "relevant_terms": ["hash", "dedup", "fnv", "luminance", "archival", "image"],
        "description": "Storage: image dedup archival"
    },
    {
        "query": "security model malware analysis training corpus",
        "domain": "security",
        "relevant_terms": ["malware", "training", "security", "corpus", "model"],
        "description": "Security: malware model training"
    },
]


def load_facts_and_conn():
    """Load facts + connection with sqlite-vec.

    Honors GAIUS_DB_PATH so the benchmark can run against the generic demo
    corpus (build it with bench_inject.py --keep-db) instead of a private DB.
    """
    db_path = os.environ.get("GAIUS_DB_PATH", str(Path.home() / ".gaius" / "facts.db"))
    try:
        import sqlite_vec
        has_vec = True
    except ImportError:
        has_vec = False

    conn = sqlite3.connect(db_path)
    if has_vec:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    conn.row_factory = sqlite3.Row

    facts = conn.execute(
        "SELECT id, domain, fact_key, fact_text, score FROM facts "
        "WHERE tombstoned_at IS NULL AND (outcome IS NULL OR outcome != 'rejected')"
    ).fetchall()
    return [dict(f) for f in facts], conn, has_vec


def is_relevant(fact: dict, query: dict) -> bool:
    """Check if a fact is relevant based on term overlap (≥2 terms)."""
    text = (fact.get("fact_text", "") or "").lower()
    matches = sum(1 for t in query["relevant_terms"] if t in text)
    return matches >= 2


def keyword_rank(facts: list[dict], query_text: str, k: int = 20) -> list[dict]:
    """Rank facts by keyword overlap, return top-k."""
    terms = re.sub(r'[^\w\s]', ' ', query_text.lower()).split()
    scored = []
    for fact in facts:
        text = (fact.get("fact_text", "") or "").lower()
        matches = sum(1 for t in terms if t in text)
        if matches > 0:
            scored.append((matches / len(terms), fact))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"fact": s[1], "score": s[0]} for s in scored[:k]]


def semantic_rank(conn, query_text: str, k: int = 20) -> list[dict]:
    """Use sqlite-vec KNN to find top-k facts by cosine similarity. One query, fast."""
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2")
    vec = model.encode(query_text, normalize_embeddings=True).tolist()
    vec_blob = struct.pack(f'{EMBED_DIM}f', *vec)

    rows = conn.execute("""
        SELECT fe.fact_id, fe.distance, f.id, f.domain, f.fact_key, f.fact_text, f.score as db_score
        FROM fact_embeddings fe
        JOIN facts f ON f.id = fe.fact_id
        WHERE fe.embedding MATCH ?
          AND k = ?
          AND f.tombstoned_at IS NULL
    """, (vec_blob, k)).fetchall()

    results = []
    for row in rows:
        l2_dist = row[1]  # sqlite-vec returns L2 distance
        cosine_sim = 1.0 - (l2_dist ** 2 / 2.0)  # for normalized vectors
        results.append({
            "fact": {"id": row[2], "domain": row[3], "fact_key": row[4], "fact_text": row[5], "score": row[6]},
            "score": max(0, cosine_sim)
        })
    return results


def hybrid_rank(facts: list[dict], conn, query_text: str, k: int = 20,
                kw_weight: float = 0.4, sem_weight: float = 0.6) -> list[dict]:
    """Hybrid: merge keyword + semantic rankings with RRF (Reciprocal Rank Fusion)."""
    kw_results = keyword_rank(facts, query_text, k=50)
    sem_results = semantic_rank(conn, query_text, k=50)

    # RRF: score = 1/(rank + 60) for each ranking, summed
    rrf_scores = {}
    for rank, r in enumerate(kw_results):
        fid = r["fact"]["id"]
        rrf_scores[fid] = rrf_scores.get(fid, 0) + kw_weight / (rank + 60)
        if fid not in rrf_scores:
            rrf_scores[fid] = 0

    for rank, r in enumerate(sem_results):
        fid = r["fact"]["id"]
        rrf_scores[fid] = rrf_scores.get(fid, 0) + sem_weight / (rank + 60)

    # Build merged results
    fact_by_id = {}
    for r in kw_results + sem_results:
        fact_by_id[r["fact"]["id"]] = r["fact"]

    merged = [{"fact": fact_by_id[fid], "score": score}
              for fid, score in sorted(rrf_scores.items(), key=lambda x: -x[1])]
    return merged[:k]


def evaluate(queries, facts, conn, has_vec, mode="keyword", k_values=[5, 10]):
    """Run benchmark, return metrics."""
    reciprocal_ranks = []
    recall_at_k = {k: [] for k in k_values}
    ndcg_at = {k: [] for k in k_values}
    query_results = []

    # Pre-load embedding model once for semantic modes
    if mode in ("semantic", "hybrid") and has_vec:
        from sentence_transformers import SentenceTransformer
        _ = SentenceTransformer("all-MiniLM-L6-v2")  # warm cache

    for query in queries:
        if mode == "keyword":
            ranked = keyword_rank(facts, query["query"], k=max(k_values))
        elif mode == "semantic" and has_vec:
            ranked = semantic_rank(conn, query["query"], k=max(k_values))
        elif mode == "hybrid" and has_vec:
            ranked = hybrid_rank(facts, conn, query["query"], k=max(k_values))
        else:
            ranked = keyword_rank(facts, query["query"], k=max(k_values))

        # Find relevant facts in ranking
        relevant_positions = []
        for i, r in enumerate(ranked):
            if is_relevant(r["fact"], query):
                relevant_positions.append(i + 1)

        # MRR
        if relevant_positions:
            reciprocal_ranks.append(1.0 / relevant_positions[0])
        else:
            reciprocal_ranks.append(0.0)

        # Recall@k and NDCG@k
        for k in k_values:
            found = any(pos <= k for pos in relevant_positions)
            recall_at_k[k].append(1.0 if found else 0.0)
            ndcg_at[k].append(ndcg_at_k(ranked, query, k))

        query_results.append({
            "description": query["description"],
            "found_at": relevant_positions[:3] if relevant_positions else [],
            "top_text": ranked[0]["fact"]["fact_text"][:80] if ranked else "",
        })

    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0
    recall = {k: sum(v) / len(v) if v else 0 for k, v in recall_at_k.items()}
    ndcg = {k: sum(v) / len(v) if v else 0 for k, v in ndcg_at.items()}
    return {"mrr": mrr, "recall": recall, "ndcg": ndcg, "queries": query_results}


def print_results(results, mode, n_queries):
    print(f"\n{'='*60}")
    print(f"  Mode: {mode.upper()}")
    print(f"{'='*60}")
    print(f"  MRR:       {results['mrr']:.3f}")
    for k in sorted(results["recall"].keys()):
        r_pct = results["recall"][k] * 100
        n_pct = results["ndcg"][k] * 100
        print(f"  Recall@{k}:  {r_pct:.1f}%  NDCG@{k}: {n_pct:.1f}%")
    print()
    for q in results["queries"]:
        status = "✓" if q["found_at"] else "✗"
        pos = f"@{q['found_at'][0]}" if q["found_at"] else "miss"
        print(f"  {status} {q['description']:<40} {pos}")
    print()


def export_eval_set(facts: list[dict], queries: list[dict], output_path: str):
    """Export eval set in a format compatible with external benchmark harnesses.
    Format: JSONL with question, corpus_id, relevant_ids."""
    with open(output_path, "w") as f:
        for query in queries:
            relevant_ids = []
            for fact in facts:
                if is_relevant(fact, query):
                    relevant_ids.append(fact["id"])
            entry = {
                "question": query["query"],
                "domain": query.get("domain", ""),
                "description": query["description"],
                "relevant_fact_ids": relevant_ids,
                "relevant_terms": query["relevant_terms"],
                "n_relevant": len(relevant_ids),
            }
            f.write(json.dumps(entry) + "\n")
    print(f"Exported {len(queries)} queries ({sum(1 for q in queries for f in facts if is_relevant(f, q))} relevant pairs) to {output_path}")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["keyword", "semantic", "hybrid", "all"], default="all")
    parser.add_argument("--export", type=str, default=None, help="Export eval set to JSONL file")
    args = parser.parse_args()

    facts, conn, has_vec = load_facts_and_conn()
    print(f"Loaded {len(facts)} facts, sqlite-vec: {has_vec}")

    if args.export:
        export_eval_set(facts, BENCHMARK_QUERIES, args.export)
        return

    modes = ["keyword", "semantic", "hybrid"] if args.mode == "all" else [args.mode]
    for mode in modes:
        if mode in ("semantic", "hybrid") and not has_vec:
            print(f"\nSkipping {mode} — sqlite-vec not available")
            continue
        results = evaluate(BENCHMARK_QUERIES, facts, conn, has_vec, mode=mode)
        print_results(results, mode, len(BENCHMARK_QUERIES))


if __name__ == "__main__":
    main()
