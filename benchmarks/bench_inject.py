#!/usr/bin/env python3
"""bench_inject.py — injection quality benchmark for gaius.

Tests what `gaius inject` actually surfaces for realistic operational queries,
independent of fact IDs (which break after tombstoning/cleanup).

Self-contained and reproducible: ships a small GENERIC demo corpus under
`benchmarks/demo_corpus/*.md`, ingests it into a throwaway facts.db, and runs
gaius inject against THAT database (never your private ~/.gaius corpus). Anyone
can clone the repo and reproduce the recall number with no setup.

Methodology:
  - Build a temp facts.db from the demo corpus (one fact per bullet line)
  - Run `gaius inject --task "$QUERY"` against it (via GAIUS_DB_PATH)
  - Check recall: do expected terms appear in the output?
  - Check precision: are obviously-wrong-domain terms absent?
  - Report per-query PASS/FAIL + summary recall score

Usage:
  python3 bench_inject.py [--budget N] [--verbose] [--corpus DIR] [--keep-db]
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

DEFAULT_CORPUS = Path(__file__).resolve().parent / "demo_corpus"

# Test cases over the generic demo corpus. Each has a query, required keywords
# (recall), and wrong-domain anti-terms (precision). `required`: at least
# `min_hits` of these must appear in the output. `anti`: none should dominate.
TEST_CASES = [
    {
        "query": "DRBD quorum loss storage failure recovery after reboot",
        "required": ["DRBD", "quorum", "reboot"],
        "min_hits": 2,
        "anti": ["OAuth2", "Grafana"],
        "domain": "storage",
    },
    {
        "query": "DRBD split-brain discard outdated replica resync peer",
        "required": ["split-brain", "resync", "replica"],
        "min_hits": 2,
        "anti": ["Prometheus", "ingress"],
        "domain": "storage",
    },
    {
        "query": "flannel VXLAN overlay MTU cross-site network timeout fragmentation",
        "required": ["VXLAN", "MTU", "timeout"],
        "min_hits": 2,
        "anti": ["Vault", "Postgres"],
        "domain": "networking",
    },
    {
        "query": "CNI DaemonSet restart cascade cluster outage pod networking",
        "required": ["CNI", "cascade", "outage"],
        "min_hits": 2,
        "anti": ["Vault", "Grafana"],
        "domain": "networking",
    },
    {
        "query": "eBPF tracing policy capture exec events from sandbox pods",
        "required": ["eBPF", "sandbox"],
        "min_hits": 2,
        "anti": ["DRBD", "Grafana"],
        "domain": "security",
    },
    {
        "query": "OAuth2 proxy ForwardAuth companion ingress redirect loop",
        "required": ["OAuth2", "ingress"],
        "min_hits": 1,
        "anti": ["DRBD", "VXLAN"],
        "domain": "security",
    },
    {
        "query": "CSI satellite CrashLoopBackOff missing kernel headers node",
        "required": ["kernel", "CrashLoopBackOff"],
        "min_hits": 2,
        "anti": ["OAuth2", "Grafana"],
        "domain": "storage",
    },
    {
        "query": "S3 object store volume max capacity exhaustion writes blocked",
        "required": ["volume", "capacity"],
        "min_hits": 2,
        "anti": ["OAuth2", "VXLAN"],
        "domain": "storage",
    },
    {
        "query": "OAuth2 ForwardAuth callback ingress missing route loop host",
        "required": ["OAuth2", "callback"],
        "min_hits": 1,
        "anti": ["DRBD", "VXLAN"],
        "domain": "security",
    },
    {
        "query": "etcd quorum loss control plane snapshot restore member",
        "required": ["etcd", "quorum", "snapshot"],
        "min_hits": 2,
        "anti": ["VXLAN", "Grafana"],
        "domain": "services",
    },
    {
        "query": "OTel collector memory limit metrics dropped Grafana no data",
        "required": ["OTel", "Grafana"],
        "min_hits": 1,
        "anti": ["DRBD", "VXLAN"],
        "domain": "observability",
    },
    {
        "query": "ingress controller node pinning cross-site latency placement",
        "required": ["ingress", "latency"],
        "min_hits": 2,
        "anti": ["etcd", "Vault"],
        "domain": "networking",
    },
    {
        "query": "CI runner PVC workspace pending local-path provisioner",
        "required": ["runner", "PVC"],
        "min_hits": 2,
        "anti": ["DRBD", "Grafana"],
        "domain": "services",
    },
    {
        "query": "Postgres replica failover taint toleration unschedulable node",
        "required": ["Postgres", "replica", "taint"],
        "min_hits": 2,
        "anti": ["OAuth2", "VXLAN"],
        "domain": "storage",
    },
    {
        "query": "BPF LSM etcd gRPC TLS handshake deadlock control plane",
        "required": ["BPF", "etcd"],
        "min_hits": 2,
        "anti": ["Grafana", "ingress"],
        "domain": "security",
    },
    {
        "query": "Cloudflare Tunnel HTTP2 QUIC WireGuard UDP encapsulation",
        "required": ["Cloudflare", "HTTP2"],
        "min_hits": 1,
        "anti": ["DRBD", "etcd"],
        "domain": "networking",
    },
    {
        "query": "WireGuard overlay mesh node join IP subnet exhausted route",
        "required": ["WireGuard", "subnet"],
        "min_hits": 1,
        "anti": ["DRBD", "Grafana"],
        "domain": "networking",
    },
    {
        "query": "Prometheus remote write scrape interval flush sample backlog",
        "required": ["Prometheus", "scrape"],
        "min_hits": 2,
        "anti": ["DRBD", "OAuth2"],
        "domain": "observability",
    },
    {
        "query": "node reboot drain uncordon CI agent reschedule jobs",
        "required": ["reboot", "drain"],
        "min_hits": 1,
        "anti": ["OAuth2", "Grafana"],
        "domain": "services",
    },
    {
        "query": "Vault secret rotation ExternalSecret template pod refresh",
        "required": ["Vault", "secret"],
        "min_hits": 2,
        "anti": ["DRBD", "VXLAN"],
        "domain": "security",
    },
    {
        "query": "Helm chart version bump gitops reconcile cached manifests",
        "required": ["Helm", "gitops"],
        "min_hits": 2,
        "anti": ["DRBD", "Grafana"],
        "domain": "services",
    },
    {
        "query": "TLS certificate SAN mismatch etcd peer join address",
        "required": ["SAN", "etcd"],
        "min_hits": 2,
        "anti": ["Grafana", "OAuth2"],
        "domain": "security",
    },
    {
        "query": "gitops kustomization drift loop ignored field controller mutates",
        "required": ["kustomization", "drift"],
        "min_hits": 2,
        "anti": ["DRBD", "VXLAN"],
        "domain": "services",
    },
    {
        "query": "alert rule never fires label selector metric mismatch",
        "required": ["alert", "label"],
        "min_hits": 2,
        "anti": ["DRBD", "VXLAN"],
        "domain": "observability",
    },
    {
        "query": "PVC expansion online resize pod restart remount filesystem",
        "required": ["PVC", "resize"],
        "min_hits": 2,
        "anti": ["OAuth2", "Grafana"],
        "domain": "storage",
    },
]


def _iter_facts(corpus_dir: Path):
    """Yield (domain, fact_text) for each bullet line in the demo corpus."""
    for md in sorted(corpus_dir.glob("*.md")):
        domain = md.stem
        for line in md.read_text().splitlines():
            line = line.strip()
            if line.startswith("- "):
                yield domain, line[2:].strip()


def build_demo_db(corpus_dir: Path, db_path: Path, empty_memory: Path) -> int:
    """Ingest the demo corpus into a fresh facts.db. Returns fact count.

    Uses gaius's own init_db/upsert_fact so the schema and ranking match the
    real tool exactly — the benchmark exercises the same code path as prod.
    """
    # Force gaius to target the temp DB (DB_PATH is read from env at import)
    # and an empty memory dir, so injection never touches a private corpus.
    os.environ["GAIUS_DB_PATH"] = str(db_path)
    os.environ["GAIUS_MEMORY_DIR"] = str(empty_memory)
    os.environ.setdefault("GAIUS_CONFIG", "/dev/null")
    import gaius._core as core
    core.DB_PATH = db_path  # in case the module was already imported

    if db_path.exists():
        db_path.unlink()
    conn = core.init_db(db_path)
    n = 0
    for domain, text in _iter_facts(corpus_dir):
        fk = hashlib.sha256(text.lower().encode()).hexdigest()[:16]
        core.upsert_fact(
            conn,
            domain=domain,
            fact_key=fk,
            fact_text=text,
            agent="demo",
            session_uuid="demo-corpus",
            provenance="human",
            score=0.6,
            model_family="demo",
            source="human",
        )
        n += 1
    conn.commit()
    # Build embeddings if semantic search is available (improves recall but
    # not required — keyword BM25 alone passes the gate).
    if getattr(core, "HAS_SQLITE_VEC", False):
        try:
            core.cmd_embed([])
        except Exception:
            pass
    conn.close()
    return n


def run_inject(query: str, budget: int, db_path: Path, empty_memory: Path) -> str:
    env = dict(os.environ)
    env["GAIUS_DB_PATH"] = str(db_path)
    env["GAIUS_MEMORY_DIR"] = str(empty_memory)
    env.setdefault("GAIUS_CONFIG", "/dev/null")
    result = subprocess.run(
        [sys.executable, "-m", "gaius", "inject", "--budget", str(budget),
         "--skills-budget", "0", "--no-always-skills", "--format", "plain",
         "--task", query],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(_REPO),
    )
    return result.stdout


def score_case(output: str, case: dict) -> dict:
    out_lower = output.lower()
    hits = [t for t in case["required"] if t.lower() in out_lower]
    anti_hits = [t for t in case["anti"] if t.lower() in out_lower]
    n_entries = output.count("\n<!-- ")
    recall_pass = len(hits) >= case["min_hits"]
    precision_warn = len(anti_hits) > 0

    return {
        "pass": recall_pass,
        "hits": hits,
        "missing": [t for t in case["required"] if t.lower() not in out_lower],
        "anti_hits": anti_hits,
        "n_entries": n_entries,
        "precision_warn": precision_warn,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=int, default=800)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS,
                        help="Demo corpus directory (markdown bullet facts)")
    parser.add_argument("--keep-db", action="store_true",
                        help="Keep the temp facts.db for inspection")
    args = parser.parse_args()

    if not args.corpus.is_dir():
        print(f"Error: corpus dir not found: {args.corpus}", file=sys.stderr)
        sys.exit(2)

    tmpdir = Path(tempfile.mkdtemp(prefix="gaius-bench-"))
    db_path = tmpdir / "facts.db"
    empty_memory = tmpdir / "empty-memory"
    empty_memory.mkdir(parents=True, exist_ok=True)
    n_facts = build_demo_db(args.corpus, db_path, empty_memory)

    passed = 0
    warned = 0
    times = []

    print(f"gaius inject benchmark — {len(TEST_CASES)} queries @ {args.budget} "
          f"token budget over {n_facts} demo facts\n")

    for i, case in enumerate(TEST_CASES, 1):
        t0 = time.time()
        try:
            output = run_inject(case["query"], args.budget, db_path, empty_memory)
        except subprocess.TimeoutExpired:
            print(f"  [{i:2d}] TIMEOUT  [{case['domain']}] {case['query'][:55]}")
            continue
        elapsed = time.time() - t0
        times.append(elapsed)

        result = score_case(output, case)
        status = "PASS" if result["pass"] else "FAIL"
        warn = " [precision-warn]" if result["precision_warn"] else ""
        if result["pass"]:
            passed += 1
        if result["precision_warn"]:
            warned += 1

        hits_str = ",".join(result["hits"]) or "none"
        miss_str = (",".join(result["missing"]) + " MISSING") if result["missing"] else ""
        anti_str = (" anti:" + ",".join(result["anti_hits"])) if result["anti_hits"] else ""
        print(f"  [{i:2d}] {status}{warn:20s} [{case['domain']:13s}] {case['query'][:50]}")
        if args.verbose or not result["pass"]:
            print(f"        hits={hits_str}  {miss_str}{anti_str}  entries={result['n_entries']}  {elapsed:.2f}s")

    avg_t = sum(times) / len(times) if times else 0
    recall = passed / len(TEST_CASES)
    print(f"\n{'-'*60}")
    print(f"Recall:    {passed}/{len(TEST_CASES)} ({recall*100:.0f}%)")
    print(f"Prec-warn: {warned}/{len(TEST_CASES)} queries had anti-domain terms in output")
    print(f"Avg time:  {avg_t:.2f}s/query")
    print(f"Corpus:    {n_facts} demo facts in {db_path}")
    print(f"{'-'*60}")

    if not args.keep_db:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    sys.exit(0 if recall >= 0.7 else 1)


if __name__ == "__main__":
    main()
