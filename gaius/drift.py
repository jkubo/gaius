"""gaius.drift — cross-agent canonical-fact drift + live-claims verification.

cmd_drift checks the drift-facts registry across agents and (with --live) probes
live-claims.yaml assertions against cluster state; --post-council reports to the
council log endpoint configured in ~/.gaius/config.yaml.

Facade convention (see ARCHITECTURE.md): patched hub state (MEMORY_DIR,
_gaius_cfg, …) is read at call time as `_core.NAME`. The registry default path
is __file__-relative and unchanged by the move (same package directory).
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

import gaius._core as _core
# imports from gaius._core (shared hub) — circular-by-design, see ARCHITECTURE.md
from gaius._core import GREEN, YELLOW, RED, BOLD, RESET, HAS_YAML

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
    mem_base = _core.MEMORY_DIR or (Path.home() / ".gaius" / "memory")

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
        cfg_council = _core._gaius_cfg.get("council", {})
        base_url = cfg_council.get("base_url", "").rstrip("/")
        api_key = cfg_council.get("api_key", "")
        # Poster identity is deployment-specific: config it, never bake one in.
        # A hardcoded default both mis-attributes every other deployment's posts
        # and ships the author's internal agent roster in the source.
        poster = cfg_council.get("agent", "gaius")
        if base_url and api_key:
            items = [f"{cid}: {fil} asserts {a!r} but live is {l!r}"
                     for cid, fil, _, a, l, _ in stale]
            payload = json.dumps({
                "type": "alert", "channel": "alerts", "agents": [poster],
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
        cfg_council = _core._gaius_cfg.get("council", {})
        base_url = cfg_council.get("base_url", "").rstrip("/")
        api_key = cfg_council.get("api_key", "")
        poster = cfg_council.get("agent", "gaius")  # see the note on the other post site
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
                "agents": [poster],
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
