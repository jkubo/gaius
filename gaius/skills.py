"""gaius.skills — skill loading, scoring, stubs, scaffolding, and suggestion.

Owns load_skills/compute_skill_score (the injection-side skill ranking),
`gaius skills`, the Claude Code stub wiring (`gaius commands` — modern
skills/<name>/SKILL.md + legacy commands/<name>.md), `gaius scaffold-skill`,
and the suggest pipeline (cmd_suggest + _generate_skill_draft).

Facade convention (see ARCHITECTURE.md): test-patched hub paths (SKILLS_DIR,
CLAUDE_SKILLS_DIR, CLAUDE_COMMANDS_DIR, MEMORY_DIR, DB_PATH, _gaius_cfg …) are
read at call time as `_core.NAME`; the constants themselves stay in _core.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import gaius._core as _core
# imports from gaius._core (shared hub) — circular-by-design, see ARCHITECTURE.md
from gaius._core import (
    GREEN, YELLOW, RED, BOLD, RESET, SOP_DIR,
)
from gaius.facts import init_db
from gaius.raft import _parse_frontmatter
from gaius.scoring import estimate_tokens

SKILL_STALE_DAYS = 90  # flag skills not touched in git for this many days
SKILL_PATH_WEIGHT = 10.0  # per-glob-match weight in compute_skill_score — a path match is
                          # ground truth (the file IS this skill's domain), weighted well
                          # above keyword overlap (frontmatter=3.0, body=0.5)


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
    """Load skill files from _core.SKILLS_DIR.

    Returns list of dicts:
      {name, fm, body, full_text, tokens, domain, gate, also_load, git_date, is_stale}
    body is stored separately for scoring. also_load lists dependency skill names.
    git_date is the last commit date; is_stale flags files unchanged for SKILL_STALE_DAYS.
    """
    if not _core.SKILLS_DIR.is_dir():
        return []

    now_ts = datetime.now(timezone.utc)
    skills = []
    for p in sorted(_core.SKILLS_DIR.glob("*.md")):
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

        paths_raw = fm.get("paths", []) or []
        if isinstance(paths_raw, str):
            paths_raw = [s.strip() for s in paths_raw.split(",") if s.strip()]

        skills.append({
            "name":      p.stem,
            "fm":        fm,
            "body":      body,
            "full_text": text,
            "tokens":    estimate_tokens(text),
            "domain":    fm.get("domain", ""),
            "gate":      fm.get("gate", "reference"),
            "hidden":    bool(fm.get("hidden", False)),
            "also_load": also_load_raw,
            "paths":     paths_raw,
            "git_date":  git_date or "unknown",
            "is_stale":  is_stale,
            "path":      p,
        })

    return skills


def _skill_path_match(path: str, pattern: str) -> bool:
    """Match a repo-relative path against a gitignore-style glob.

    Version-agnostic (works pre-3.13, unlike PurePath.full_match). Supports
    '*' (matches within one path segment, not across '/'), '**' (matches any
    number of segments, including zero), and '?' (single non-'/' char). Anchored.
    """
    i, n = 0, len(pattern)
    out = ["(?s:"]
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i:i+2] == "**":
                i += 2
                if pattern[i:i+1] == "/":
                    i += 1
                    out.append("(?:.*/)?")   # '**/' → zero or more segments
                else:
                    out.append(".*")          # trailing '**' → anything
                continue
            out.append("[^/]*")               # single '*' → within one segment
        elif c == "?":
            out.append("[^/]")
        elif c == "/":
            out.append("/")
        else:
            out.append(re.escape(c))
        i += 1
    out.append(r")\Z")
    try:
        return re.match("".join(out), path) is not None
    except re.error:
        return False


def _glob_specificity(pattern: str) -> float:
    """Higher = more specific glob. Literal path segments score full; '**' is penalized so
    a catch-all ('manifests/**/*.yaml') can't outrank a precise one ('manifests/tetragon/*.yaml').
    Floored at 0.5 so any match still counts for something.
    """
    segs = [s for s in pattern.split("/") if s]
    literal = sum(1 for s in segs if "*" not in s and "?" not in s)
    return max(0.5, literal - 0.5 * pattern.count("**"))


def compute_skill_score(skill: dict, context_terms: set, files: list | None = None,
                        explain: bool = False):
    """Score a skill against context terms and active file paths. Returns score-per-token.

    Scoring (highest precedence first):
    - gate:always → float('inf') sentinel; injected outside budget unconditionally
    - Path-glob signal: active `files` matched against the skill's `paths:` frontmatter
      globs via PurePath.full_match (real ** support). Ground truth — the file IS this
      skill's domain — so weighted SKILL_PATH_WEIGHT per match, well above keyword overlap.
    - gate:mandate and gate:hard → floor score + 1.5x multiplier (always beats reference)
    - gate:reference → score 0 if NO keyword terms AND NO path match (excluded when no signal)
    - Frontmatter signal (trigger + description + domain) weighted 3x body keywords
    - Returns score-per-token so dense high-signal skills beat long diffuse ones

    Backward-compatible: callers passing no `files` get identical pre-path behavior.

    When explain=True, returns (score, reason) where reason is a dict
    {primary_signal, detail, matched_terms} naming the single highest-precedence
    signal that actually contributed (always > path-glob > gate-floor(sole) >
    keyword > body-keyword). Default (explain=False) returns the bare float,
    unchanged — many positional callers depend on this.
    """
    def _ret(score, reason):
        return (score, reason) if explain else score

    gate = skill["gate"]
    if gate == "always":
        return _ret(float("inf"),
                    {"primary_signal": "always", "detail": "gate: always", "matched_terms": []})

    is_hard = gate in ("hard", "mandate")
    tokens = skill["tokens"] if skill["tokens"] > 0 else 1

    # Path-glob signal (strongest). For each active file, take its MOST specific matching
    # glob so a precise skill outranks a catch-all. Repo-relative paths expected.
    path_score = 0.0
    best_pat = None
    best_spec = 0.0
    skill_paths = skill.get("paths") or []
    if files and skill_paths:
        for f in files:
            best = 0.0
            best_f_pat = None
            for pat in skill_paths:
                if _skill_path_match(f, pat):
                    spec = _glob_specificity(pat)
                    if spec > best:
                        best = spec
                        best_f_pat = pat
            path_score += best
            if best > best_spec:
                best_spec = best
                best_pat = best_f_pat

    if not context_terms and not path_score:
        # No keyword and no path signal — only inject hard gates, everything else excluded
        raw = 0.5 * 1.5 if is_hard else 0.0
        if is_hard:
            reason = {"primary_signal": "gate-floor", "detail": f"gate: {gate}", "matched_terms": []}
        else:
            reason = {"primary_signal": "none", "detail": "no signal", "matched_terms": []}
        return _ret(raw / tokens, reason)

    # Build term sets from frontmatter (high signal) and body (low signal)
    def _terms(text: str) -> set:
        return set(re.sub(r'[^\w\s]', ' ', text.lower()).split())

    fm_signal = (
        _terms(skill["fm"].get("trigger", ""))
        | _terms(skill["fm"].get("description", ""))
        | _terms(skill["domain"].replace("-", " "))
    )
    body_signal = _terms(skill["body"])

    matched_fm   = sorted(context_terms & fm_signal)
    matched_body = sorted(context_terms & body_signal)
    overlap_fm   = len(matched_fm)
    overlap_body = len(matched_body)

    score = (path_score * SKILL_PATH_WEIGHT) + (overlap_fm * 3.0) + (overlap_body * 0.5)

    # Hard gate floor — injected even with weak context match
    if is_hard:
        score = max(score, 0.5)
        score *= 1.5

    # Choose the single primary signal by precedence (mirrors the score ladder).
    if path_score > 0:
        reason = {"primary_signal": "path-glob",
                  "detail": f"path-glob match on {best_pat}", "matched_terms": []}
    elif is_hard and overlap_fm == 0 and overlap_body == 0:
        # gate-floor is the only thing keeping it in
        reason = {"primary_signal": "gate-floor", "detail": f"gate: {gate}", "matched_terms": []}
    elif overlap_fm > 0:
        reason = {"primary_signal": "keyword",
                  "detail": f"frontmatter keyword match: {', '.join(matched_fm[:4])}",
                  "matched_terms": matched_fm}
    elif overlap_body > 0:
        reason = {"primary_signal": "body-keyword",
                  "detail": f"body keyword match: {', '.join(matched_body[:4])}",
                  "matched_terms": matched_body}
    else:
        reason = {"primary_signal": "gate-floor" if is_hard else "none",
                  "detail": f"gate: {gate}" if is_hard else "no signal", "matched_terms": []}

    return _ret(score / tokens, reason)


def cmd_skills(args):
    """List all skills with domain/trigger/gate/line-count/staleness. Analogous to gaius stats."""
    parser = argparse.ArgumentParser(prog="gaius skills")
    parser.add_argument("--domain", type=str, default=None, help="Filter by domain")
    parser.add_argument("--stale", action="store_true", help="Show only stale skills")
    parser.add_argument("--score", type=str, default=None,
                        help="Score skills against this context string and show ranked output")
    parser.add_argument("--files", type=str, default=None,
                        help="Comma-separated file paths to match against skill `paths:` globs")
    parsed = parser.parse_args(args)

    score_files = [f.strip() for f in parsed.files.split(",") if f.strip()] if parsed.files else None

    skills = load_skills()

    if parsed.domain:
        skills = [s for s in skills if s["domain"] == parsed.domain]
    if parsed.stale:
        skills = [s for s in skills if s["is_stale"]]

    if not skills:
        print("No skills found.")
        return

    # If --score/--files provided, rank by score descending
    score_active = bool(parsed.score or parsed.files)
    if score_active:
        score_ctx = set(re.sub(r'[^\w\s]', ' ', (parsed.score or "").lower()).split())
        skills = sorted(skills, key=lambda s: compute_skill_score(s, score_ctx, files=score_files), reverse=True)

    col_name   = max(len(s["name"])   for s in skills) + 2
    col_domain = max((len(s["domain"]) for s in skills), default=6) + 2
    col_gate   = 12

    stale_marker = f"  {YELLOW}STALE{RESET}"

    header = f"\n{'Name':<{col_name}} {'Domain':<{col_domain}} {'Gate':<{col_gate}} {'Modified':<12} {'Lines':>5}"
    if score_active:
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
        if score_active:
            score_ctx = set(re.sub(r'[^\w\s]', ' ', (parsed.score or "").lower()).split())
            sc = compute_skill_score(s, score_ctx, files=score_files)
            row += f"   {sc:.4f}"
        print(row + stale)

    summary = f"\n{len(skills)} skill(s)"
    if stale_count:
        summary += f" | {YELLOW}{stale_count} STALE (>{SKILL_STALE_DAYS}d){RESET}"
    print(summary + "\n")


STUB_STRIP_FM_KEYS = ("paths",)


def _strip_frontmatter_key(text: str, key: str) -> str:
    """Drop a top-level frontmatter key and its indented continuation lines.

    Everything outside the frontmatter block is left byte-identical. Returns text
    unchanged if there is no frontmatter.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return text

    out, i = [lines[0]], 1
    while i < end:
        if re.match(rf"^{re.escape(key)}\s*:", lines[i]):
            i += 1
            while i < end and lines[i][:1] in (" ", "\t"):
                i += 1          # swallow the key's indented block/list
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out + lines[end:])


def _stub_content(skill: dict) -> str:
    """Content for the wired copy at ~/.claude/skills/<name>/SKILL.md.

    Keeps the frontmatter — Claude Code shows `description` in the skill picker,
    and dropping it degrades the picker to the first `#` heading — minus the keys
    in STUB_STRIP_FM_KEYS.
    """
    text = skill["full_text"]
    for key in STUB_STRIP_FM_KEYS:
        text = _strip_frontmatter_key(text, key)
    return text.strip() + "\n"


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

    # Soft-hide: skills with `hidden: true` frontmatter are never synced as
    # slash-command stubs (they still rank/inject via compute_skill_score).
    skills = [s for s in skills if not s.get("hidden")]

    _core.CLAUDE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    wrote = 0
    skipped = 0
    unchanged = 0

    for s in skills:
        skill_dir = _core.CLAUDE_SKILLS_DIR / s["name"]
        stub_path = skill_dir / "SKILL.md"

        # Inline the whole skill file so Claude Code gets the content directly.
        stub_content = _stub_content(s)

        # A symlinked wired copy points back at the source skill file, so both the
        # read below and write_text() would follow it — comparing against (and then
        # CLOBBERING) the source file in the skills source dir. Replace the link instead.
        linked = stub_path.is_symlink()
        existed = stub_path.exists() and not linked
        if existed and stub_path.read_text() == stub_content:
            unchanged += 1
            continue

        if parsed.dry_run:
            action = "relink" if linked else ("update" if existed else "create")
            print(f"  {action}: {s['name']}/SKILL.md")
            wrote += 1
            continue

        skill_dir.mkdir(parents=True, exist_ok=True)
        if linked:
            stub_path.unlink()
        stub_path.write_text(stub_content)
        wrote += 1
        print(f"  {'relinked' if linked else 'updated' if existed else 'created'}: /{s['name']}")

    # Prune stale stubs
    pruned = 0
    if parsed.prune:
        skill_names = {s["name"] for s in load_skills() if not s.get("hidden")}
        # Prune modern format (skip symlinks — belong to other tools)
        if _core.CLAUDE_SKILLS_DIR.is_dir():
            for d in _core.CLAUDE_SKILLS_DIR.iterdir():
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
        if _core.CLAUDE_COMMANDS_DIR.is_dir():
            for stub in _core.CLAUDE_COMMANDS_DIR.glob("*.md"):
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


def cmd_scaffold_skill(args):
    """Emit a generation prompt so the user's OWN coding agent authors + installs a
    memory-maintenance ('surgeon') skill localized to this machine's gaius setup.

    Unlike `gaius init` (which copies a static SKILL.md), this hands the agent a
    self-localizing scaffold: the agent discovers local paths/thresholds/conventions and
    writes a concrete skill for THIS system. Nothing machine-specific is shipped — the
    template is generic; localization happens locally, in the user's own agent.
    """
    parser = argparse.ArgumentParser(prog="gaius scaffold-skill")
    parser.add_argument("name", nargs="?", default="mnemos",
                        help="Skill to scaffold (default: mnemos)")
    parser.add_argument("--write", metavar="PATH",
                        help="Write the prompt to PATH instead of stdout")
    parsed = parser.parse_args(args)

    # skill/<name>-scaffold.md ships inside the package (like presets/ and skill/SKILL.md)
    _script_dir = Path(__file__).parent  # gaius package dir (ships presets/ + skill/)
    tmpl = _script_dir / "skill" / f"{parsed.name}-scaffold.md"
    if not tmpl.exists():
        tmpl = Path(__file__).parent.parent / "skill" / f"{parsed.name}-scaffold.md"
    if not tmpl.exists():
        print(f"ERROR: no scaffold template for '{parsed.name}' "
              f"(looked for {parsed.name}-scaffold.md)")
        skill_dir = _script_dir / "skill"
        avail = sorted(p.name[:-len("-scaffold.md")]
                       for p in skill_dir.glob("*-scaffold.md")) if skill_dir.exists() else []
        if avail:
            print(f"Available: {', '.join(avail)}")
        return

    prompt = tmpl.read_text()

    # Warm-start hint: prepend the detected backend + domain dir so the agent starts grounded.
    backend = _core._gaius_cfg.get("backend", "claude")
    domain_dir = _core._gaius_cfg.get("domain_dir", "~/.gaius/memory/domain")
    header = (f"<!-- gaius scaffold-skill: {parsed.name} | detected backend={backend} "
              f"domain_dir={domain_dir} -->\n\n")
    out = header + prompt

    if parsed.write:
        dest = Path(parsed.write).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(out)
        print(f"✓  Scaffold prompt written to {dest}")
        print(f"   Feed it to your coding agent (backend={backend}) to generate + "
              f"install the '{parsed.name}' skill for this system.")
    else:
        sys.stdout.write(out)


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
origin: gaius
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
