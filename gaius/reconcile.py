"""gaius.reconcile — source-of-truth reconciler (the promotion-gap fix).

Curated source registry, dev<->mirror tree fingerprint divergence, remote-branch
HEAD divergence, curated-fact promotion, and the ``reconcile`` command.

Facade convention (see ARCHITECTURE.md): shared helpers imported from gaius._core
at top; _core re-imports this module's public symbols before the COMMANDS dict.
NOTE: ``_remote_head`` now lives here — tests must monkeypatch
``gaius.reconcile._remote_head``, not ``gaius._core._remote_head``.
"""
import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

# imports from gaius._core (shared hub) — circular-by-design, see ARCHITECTURE.md
from gaius._core import init_db, upsert_fact, DB_PATH


# Closes the "known-but-not-injected-at-depth" gap: load-bearing facts that live only in repo docs
# (READMEs, PLANs, Makefiles) never reach facts.db, so injection can't surface them — the 2026-06-24
# gaius-OSS divergence (gaius was open-source for days; no session knew the publish flow existed).
# CONSERVATIVE by design: a CURATED registry (no LLM guessing) of canonical sources, each carrying
# human-curated topology facts + an optional dev<->mirror pair. The reconciler (a) PROMOTES the
# curated facts into facts.db ONCE, flagged-unverified (source='reconcile', outcome=None) so the
# corpus-audit/outcome QC governs them (never trusted by fiat); insert-once — the nightly does NOT
# re-corroborate, so it never inflates confirmation_count (the repetition-poison it exists to avoid).
# And (b) runs a DIVERGENCE SENTINEL diffing dev vs mirror, reporting drift (the check that would
# have screamed "jkubo/gaius is behind agent-memory"). Mechanical, no LLM, dry-run by default.

DEFAULT_SOURCES = [
    {
        "name": "gaius",
        "dev_path": str(Path.home() / "Projects/agent-memory/gaius"),
        "mirror_path": str(Path.home() / "Projects/gaius"),
        "publish_cmd": "make publish  (then push ~/Projects/gaius to github via gh-token HTTPS)",
        "facts": [
            "gaius dev repo = kub0-ai/agent-memory (Forgejo, private); the OSS mirror = jkubo/gaius "
            "(GitHub, PUBLIC, Apache-2.0). The gaius/ subdir is the published package.",
            "Sync gaius dev -> OSS: from the gaius/ dir run `make publish` (rsync to the ~/Projects/gaius "
            "mirror clone + leak scan), review the diff, then push the mirror to github via gh-token "
            "HTTPS (the ~/.ssh/keys/gh_claudeus_ai key is passphrase-locked, so SSH push is blocked).",
            "After ANY change to gaius source, publish to the OSS mirror in the same session — it drifts "
            "silently otherwise (the 2026-06-24 divergence: the closed-loop commands sat unpublished for days).",
        ],
    },
]

# Mirrors the Makefile `publish` rsync excludes so the divergence check compares the publishable set.
_RECONCILE_EXCLUDES = (".git", "__pycache__", ".venv", ".pytest_cache", "benchmarks",
                       "OPEN-SOURCE-PLAN.md", "drift-facts.yaml", "Makefile")
_RECONCILE_EXCLUDE_SUFFIXES = (".pyc", ".egg-info", ".archive")


def load_source_registry():
    """DEFAULT_SOURCES merged with ~/.gaius/sources.yaml (optional, user-curated; same shape)."""
    sources = {s["name"]: dict(s) for s in DEFAULT_SOURCES}
    cfg = Path.home() / ".gaius" / "sources.yaml"
    if cfg.exists():
        try:
            import yaml
            extra = yaml.safe_load(cfg.read_text()) or []
            if isinstance(extra, dict):
                extra = extra.get("sources", [])
            for s in extra:
                sources[s["name"]] = {**sources.get(s["name"], {}), **s}
        except Exception as e:
            print(f"  (sources.yaml ignored: {e})", file=sys.stderr)
    return list(sources.values())


def _reconcile_excluded(rel):
    parts = Path(rel).parts
    for p in parts:
        if p in _RECONCILE_EXCLUDES or p.endswith(_RECONCILE_EXCLUDE_SUFFIXES):
            return True
    return False


def _dir_fingerprint(root):
    """{relpath: sha256} for non-excluded files under root (empty dict if root is absent)."""
    import hashlib
    root = Path(root)
    fp = {}
    if not root.exists():
        return fp
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if _reconcile_excluded(rel):
            continue
        try:
            fp[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            pass
    return fp


def source_divergence(dev_path, mirror_path):
    """Mechanical dev<->mirror drift: files missing-from-mirror / differing / stale-in-mirror."""
    if not Path(mirror_path).exists():
        return {"in_sync": False, "mirror_exists": False,
                "missing_from_mirror": [], "differ": [], "only_in_mirror": [], "dev_files": 0}
    dev, mir = _dir_fingerprint(dev_path), _dir_fingerprint(mirror_path)
    missing = sorted(set(dev) - set(mir))
    differ = sorted(f for f in (set(dev) & set(mir)) if dev[f] != mir[f])
    stale = sorted(set(mir) - set(dev))
    return {"in_sync": not (missing or differ or stale), "mirror_exists": True,
            "missing_from_mirror": missing, "differ": differ, "only_in_mirror": stale,
            "dev_files": len(dev)}


def _remote_head(url, branch="main", timeout=15):
    """SHA of <branch> at a remote via `git ls-remote` over HTTPS (uses the configured git credential
    helper, so it avoids a passphrase-locked SSH key). Returns None on failure/timeout."""
    import subprocess
    try:
        out = subprocess.run(["git", "ls-remote", "--heads", url, branch],
                             capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return out.stdout.split()[0]


def remote_divergence(remotes, branch="main", head_fn=None):
    """Compare a source's `main` HEAD across remotes (e.g. a Forgejo dev repo vs its GitHub mirror).
    remotes = {label: https-url}. Mechanical — `git ls-remote` only, no fetch. Catches the drift the
    tree-vs-tree check can't see (two remotes on one working tree). An unreachable remote yields
    in_sync=False (fail-safe — don't silently claim sync when a side is unknown). head_fn defaults to
    the module-level _remote_head looked up dynamically (so it stays patchable)."""
    fn = head_fn or _remote_head
    heads = {label: fn(url, branch) for label, url in remotes.items()}
    present = [h for h in heads.values() if h]
    reachable = bool(present) and len(present) == len(heads)
    in_sync = reachable and len(set(present)) == 1
    return {"in_sync": in_sync, "reachable": reachable, "branch": branch,
            "heads": {k: (v[:9] if v else None) for k, v in heads.items()}}


def reconcile_source(conn, src, promote, head_fn=None):
    """Promote a source's curated facts (insert-once, flagged-unverified) + check divergence.
    head_fn is injectable for tests (defaults to the real git ls-remote probe)."""
    name = src["name"]
    results = []
    for i, text in enumerate(src.get("facts", [])):
        fk = f"reconcile:{name}:{i}"
        exists = conn.execute(
            "SELECT 1 FROM facts WHERE fact_key=? AND tombstoned_at IS NULL", (fk,)).fetchone()
        if exists:
            results.append("exists")
        elif promote:
            upsert_fact(conn, domain="general", fact_key=fk, fact_text=text, agent="reconcile",
                        session_uuid="reconcile", provenance=f"source-registry:{name}",
                        score=0.5, outcome=None, source="reconcile", fact_type="operational")
            results.append("inserted")
        else:
            results.append("would-insert")
    div = source_divergence(src["dev_path"], src["mirror_path"]) if src.get("mirror_path") else None
    rdiv = remote_divergence(src["remotes"], head_fn=head_fn) if src.get("remotes") else None
    return {"name": name, "facts": results, "divergence": div, "remote_divergence": rdiv,
            "publish_cmd": src.get("publish_cmd")}


def cmd_reconcile(args):
    """Source-of-truth reconciler: promote curated repo facts into the corpus (flagged-unverified,
    insert-once) + a dev<->mirror divergence sentinel. Closes the promotion gap (a fact known only
    in a repo doc never reaches injectable memory). Mechanical, no LLM. Default = dry-run.

    Usage: gaius reconcile [--promote] [--json] [--source NAME]
    """
    p = argparse.ArgumentParser(prog="gaius reconcile")
    p.add_argument("--promote", action="store_true",
                   help="write curated facts to facts.db (additive, flagged-unverified, insert-once)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--source", default="", help="reconcile only this source name")
    ns = p.parse_args(args)

    sources = load_source_registry()
    if ns.source:
        sources = [s for s in sources if s["name"] == ns.source]
        if not sources:
            print(f"no source named {ns.source!r}", file=sys.stderr)
            return

    conn = init_db() if ns.promote else sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    results = [reconcile_source(conn, s, ns.promote) for s in sources]
    if ns.promote:
        conn.commit()
    conn.close()

    if ns.json:
        print(json.dumps({"promote": ns.promote, "sources": results}, indent=2))
        return

    print(f"\nSOURCE RECONCILE ({'PROMOTE' if ns.promote else 'dry-run — no writes'})")
    for r in results:
        c = {k: r["facts"].count(k) for k in ("inserted", "would-insert", "exists")}
        bits = [f"{c['inserted']} promoted"] if c["inserted"] else []
        if c["would-insert"]:
            bits.append(f"{c['would-insert']} would-promote")
        if c["exists"]:
            bits.append(f"{c['exists']} already-present")
        print(f"\n  [{r['name']}] facts: {', '.join(bits) or '0'}")
        d = r["divergence"]
        if d is None:
            print("    divergence: (no mirror configured)")
        elif not d["mirror_exists"]:
            print("    ⚠ divergence: mirror path does not exist")
        elif d["in_sync"]:
            print(f"    ✓ divergence: IN SYNC ({d['dev_files']} files match)")
        else:
            print(f"    ⚠ DRIFT: {len(d['missing_from_mirror'])} missing-from-mirror, "
                  f"{len(d['differ'])} differ, {len(d['only_in_mirror'])} stale-in-mirror")
            for f in (d["missing_from_mirror"] + d["differ"])[:8]:
                print(f"        - {f}")
            if r.get("publish_cmd"):
                print(f"      → sync: {r['publish_cmd']}")
        rd = r.get("remote_divergence")
        if rd is not None:
            if not rd["reachable"]:
                print(f"    ⚠ remote: UNREACHABLE (can't verify): {rd['heads']}")
            elif rd["in_sync"]:
                print(f"    ✓ remote: IN SYNC (main {next(iter(rd['heads'].values()))})")
            else:
                print(f"    ⚠ remote DRIFT — main differs across remotes: {rd['heads']}")
    print()
