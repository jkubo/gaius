"""gaius.raft — Blog-post → RAFT training-sidecar YAML generation.

Self-contained: parses markdown frontmatter/body into incident/architecture RAFT
items. Owns the canonical ``_parse_frontmatter`` and the failure-class / domain
keyword maps, which other regions consume via the facade re-export in
gaius/_core.py.

Facade convention (see ARCHITECTURE.md): shared config is imported from
gaius._core at the top of this module; _core re-imports this module's public
symbols near its bottom (before the COMMANDS dict) so
``from gaius._core import _parse_frontmatter`` (etc.) keeps working unchanged.
"""
import re
import sys
from pathlib import Path

# imports from gaius._core (shared hub) — circular-by-design, see ARCHITECTURE.md
from gaius._core import _gaius_cfg


# Incident indicators in blog categories/tags/content
_INCIDENT_KEYWORDS = frozenset([
    "incident", "debugging", "postmortem", "outage", "failure", "broke",
    "crash", "cascade", "recovery", "fix", "broken",
])

# Architecture indicators
_ARCHITECTURE_KEYWORDS = frozenset([
    "architecture", "design", "deployment", "platform", "stack", "pipeline",
    "build", "deploy", "setup", "integration",
])

# Failure class detection — generic K8s/ops defaults; extend via config failure_class_keywords.
# RULE: _FAILURE_CLASS_MAP_DEFAULT must contain only generic infrastructure terms.
#       Stack-specific names (CNIs, storage backends, service meshes) belong in
#       ~/.gaius/config.yaml [failure_class_keywords]. CI enforces clean defaults.
_FAILURE_CLASS_MAP_DEFAULT = {
    "networking":    ["dns", "mtu", "route", "tunnel", "overlay", "proxy", "cni",
                      "ingress", "loadbalancer", "endpoint"],
    "storage":       ["pvc", "s3", "volume", "disk", "mount", "persistent", "csi"],
    "compute":       ["oom", "cpu", "memory", "gpu", "containerd", "sandbox", "cgroup"],
    "control_plane": ["etcd", "apiserver", "kubelet", "scheduler", "quorum", "kube-proxy"],
    "observability": ["prometheus", "grafana", "loki", "otel", "alert", "metric", "scrape"],
    "security":      ["oauth", "cert", "tls", "rbac", "token"],
}
_FAILURE_CLASS_MAP: dict = {}
for _cls, _kws in _FAILURE_CLASS_MAP_DEFAULT.items():
    _FAILURE_CLASS_MAP[_cls] = list(_kws) + list(
        _gaius_cfg.get("failure_class_keywords", {}).get(_cls, [])
    )

# Domain detection from categories/tags — generic defaults; extend via config domain_tags.
# Same rule: keep defaults generic; project-specific tag→domain mappings go in config.
_DOMAIN_MAP_DEFAULT = {
    "networking":    ["networking", "dns", "cni"],
    "storage":       ["storage"],
    "observability": ["observability", "monitoring", "prometheus", "grafana"],
    "security":      ["security", "authentication", "oauth"],
    "agent":         ["ai", "agent", "llm", "claude", "gemini"],
}
_DOMAIN_MAP: dict = {}
for _dom, _tags in _DOMAIN_MAP_DEFAULT.items():
    _DOMAIN_MAP[_dom] = list(_tags) + list(
        _gaius_cfg.get("domain_tags", {}).get(_dom, [])
    )


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown. Returns (frontmatter_dict, body)."""
    import yaml
    if not text.startswith("---"):
        return {}, text
    end = text.find("---", 3)
    if end == -1:
        return {}, text
    fm_text = text[3:end].strip()
    body = text[end+3:].strip()
    try:
        fm = yaml.safe_load(fm_text)
    except Exception:
        fm = {}
    return fm or {}, body


def _detect_type(fm: dict, body: str) -> str:
    """Detect if post is incident or architecture from frontmatter + content."""
    tags = set()
    for key in ("tags", "categories"):
        val = fm.get(key, [])
        if isinstance(val, list):
            tags.update(t.lower() for t in val)
        elif isinstance(val, str):
            tags.add(val.lower())

    title = fm.get("title", "").lower()
    body_lower = body[:2000].lower()  # check first 2K chars

    incident_score = sum(1 for kw in _INCIDENT_KEYWORDS if kw in tags or kw in title or kw in body_lower)
    arch_score = sum(1 for kw in _ARCHITECTURE_KEYWORDS if kw in tags or kw in title or kw in body_lower)

    return "incident" if incident_score >= arch_score else "architecture"


def _detect_failure_class(body: str) -> str:
    """Detect failure class from body content."""
    body_lower = body.lower()
    scores = {}
    for cls, keywords in _FAILURE_CLASS_MAP.items():
        scores[cls] = sum(1 for kw in keywords if kw in body_lower)
    if not scores or max(scores.values()) == 0:
        return "unknown"
    return max(scores, key=scores.get)


def _detect_domain(fm: dict) -> str:
    """Detect domain from categories/tags."""
    tags = set()
    for key in ("tags", "categories"):
        val = fm.get(key, [])
        if isinstance(val, list):
            tags.update(t.lower() for t in val)
    for domain, keywords in _DOMAIN_MAP.items():
        if any(kw in tags for kw in keywords):
            return domain
    return "infrastructure"


def _detect_complexity(body: str) -> str:
    """Detect complexity from content structure."""
    # Count distinct failure mechanisms / components mentioned
    acts = len(re.findall(r'^#{1,3}\s+Act\s+\d', body, re.MULTILINE))
    sections = len(re.findall(r'^#{1,3}\s+', body, re.MULTILINE))
    if acts >= 4 or sections >= 8:
        return "cascade"
    elif acts >= 2 or sections >= 4:
        return "multi-step"
    return "simple"


def _yaml_quote(s: str) -> str:
    """Always double-quote YAML list items to prevent special-char breakage."""
    escaped = s.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _raft_item(s: str) -> str:
    """Format a RAFT list item — quote content, pass through TODO stubs."""
    return s if s.startswith("# TODO") else _yaml_quote(s)


def _split_sections(body: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, content) pairs.  First section heading is ''."""
    sections: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    for line in body.splitlines():
        m = re.match(r'^#{1,3}\s+(.+)', line)
        if m:
            if buf:
                sections.append((heading, "\n".join(buf)))
            heading = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    if buf:
        sections.append((heading, "\n".join(buf)))
    return sections


def _clean_md(s: str) -> str:
    """Strip markdown formatting from a line."""
    s = re.sub(r'^[*\->]+\s*', '', s)                                    # bullet/quote
    s = re.sub(r'^\d+\.\s+', '', s)                                     # numbered list
    s = re.sub(r'\*\*([^*]+)\*\*', r'\1', s)                            # unbold
    s = re.sub(r'\[([^\]]+)\]\([^)]+\)(?:\{[^}]*\})?', r'\1', s)       # unlink + target
    s = re.sub(r'`([^`]+)`', r'\1', s)                                   # un-backtick
    return s.strip()


def _extract_items(body: str, patterns: list[str], max_items: int = 6,
                   section_hints: list[str] | None = None) -> list[str]:
    """Extract items matching patterns.  Skips code blocks, truncates prose.

    If section_hints provided, searches only sections whose headings contain
    any of those keywords (case-insensitive).
    """
    if section_hints:
        sections = _split_sections(body)
        narrowed = []
        for heading, content in sections:
            if any(h in heading.lower() for h in section_hints):
                narrowed.append(content)
        if narrowed:
            body = "\n".join(narrowed)

    items: list[str] = []
    in_code = False

    for line in body.splitlines():
        stripped = line.strip()

        if stripped.startswith('```'):
            in_code = not in_code
            continue
        if in_code or not stripped or stripped.startswith('|') or stripped.startswith('!['):
            continue
        if re.match(r'^#{1,3}\s+', stripped):
            continue

        line_lower = stripped.lower()
        if not any(re.search(p, line_lower) for p in patterns):
            continue

        clean = _clean_md(stripped)
        if len(clean) < 15:
            continue
        # Truncate prose — take first sentence
        if len(clean) > 180:
            m = re.match(r'([^.!?]+[.!?])', clean)
            clean = m.group(1).strip() if m and len(m.group(1)) > 20 else clean[:150]

        if clean not in items:
            items.append(clean)
        if len(items) >= max_items:
            break

    return items


def _extract_objective(body: str) -> str:
    """Try to extract a one-line objective from opening paragraphs."""
    in_code = False
    paragraphs: list[str] = []
    buf: list[str] = []

    for line in body.splitlines()[:40]:
        if line.strip().startswith('```'):
            in_code = not in_code
            continue
        if in_code or re.match(r'^#{1,3}\s+', line):
            if buf:
                paragraphs.append(" ".join(buf))
                buf = []
            continue
        if not line.strip():
            if buf:
                paragraphs.append(" ".join(buf))
                buf = []
            continue
        buf.append(line.strip())

    if buf:
        paragraphs.append(" ".join(buf))

    for p in paragraphs[:4]:
        p_clean = _clean_md(p)
        if len(p_clean) < 30:
            continue
        m = re.match(r'([^.!?]+[.!?])', p_clean)
        if m and 30 < len(m.group(1)) < 200:
            return m.group(1).strip()

    return "# TODO: one-line summary"


def _extract_bold_items(body: str, section_kws: list[str],
                        max_items: int = 6) -> list[str]:
    """Extract **bold-prefixed** items from sections whose headings match keywords."""
    items: list[str] = []
    sections = _split_sections(body)

    for heading, content in sections:
        if not any(kw in heading.lower() for kw in section_kws):
            continue
        for m in re.finditer(r'\*\*([^*]+)\*\*([^*\n]*)', content):
            bold = m.group(1).strip().rstrip('.')
            rest = m.group(2).strip().lstrip('. ')
            if len(bold) < 5:
                continue
            item = bold
            rest = _clean_md(rest)
            if rest and len(rest) < 130:
                item += f" ({rest})"
            if len(item) > 200:
                item = item[:150]
            items.append(item)
            if len(items) >= max_items:
                return items

    return items


def _extract_incident_yaml(fm: dict, body: str, slug: str) -> str:
    """Generate incident-type RAFT YAML from blog post."""
    title = fm.get("title", slug.replace("-", " ").title())
    date = str(fm.get("date", ""))[:10]
    author = fm.get("author", "unknown")
    failure_class = _detect_failure_class(body)
    complexity = _detect_complexity(body)

    mechanisms = _extract_items(body, [
        r'overwrit|wip|destroy|flush|reset|regenerat|poison|propagat',
        r'without\s+\w+ing|silently|invisible|stale',
        r'race\s+condition|boot\s+race|timing',
    ])

    symptoms = _extract_items(body, [
        r'timeout|offline|crash|fail|error|dead|unreachable|stuck|pending',
        r'oom|restart|flap|partition|degraded',
        r'nothing\s+(?:is\s+)?(?:actually\s+)?work',
    ])

    root_causes = _extract_items(body, [
        r'root\s+cause|the\s+(?:real|actual)\s+(?:cause|problem|issue)',
        r'because|the\s+reason|what\s+(?:actually\s+)?happened',
        r'overwrote|wiped|destroyed|poisoned|corrupted',
        r'installer\s+(?:is|overwrit|wip)',
    ])

    fixes = _extract_items(body, [
        r'fix|solution|resolve|workaround|restore|recover|repair',
        r'the\s+correct\s+sequence|we\s+(?:fixed|resolved|restored)',
        r'fsck\.repair|fsck\.mode|grub',
        r'layer\s+\d|insurance|prevention',
    ])

    anti_patterns = _extract_items(body, [
        r'(?:don.t|never|avoid|do\s+not)\s+\w+',
        r'trap|gotcha|mistake|lie|mirage|propaganda',
        r'assuming|trusting.*(?:ready|running|healthy)',
    ])

    # Prevention — try to extract, fall back to TODO
    prevention = _extract_items(body, [
        r'prevent|ensure|gate|alert|monitor|never\s+again',
        r'added?\s+(?:a\s+)?(?:alert|check|metric|gate|guard)',
    ], section_hints=["prevention", "after", "fix", "lesson", "going forward"])

    lines = [
        f'title: "{title}"',
        f"slug: {slug}",
        f"date: {date}",
        f"type: incident",
        f"author: {author}",
        f"reviewed_by:",
        f"source: /posts/{slug}/",
        f"confidence: observed",
        f"complexity: {complexity}",
        f"failure_class: {failure_class}",
        "",
        "mechanism:",
    ]
    for m in (mechanisms or ["# TODO: extract from post"]):
        lines.append(f"  - {_raft_item(m)}")

    lines.append("")
    lines.append("symptom:")
    for s in (symptoms or ["# TODO: extract from post"]):
        lines.append(f"  - {_raft_item(s)}")

    lines.append("")
    lines.append("root_cause:")
    for r in (root_causes or ["# TODO: extract from post"]):
        lines.append(f"  - {_raft_item(r)}")

    lines.append("")
    lines.append("fix:")
    for f_ in (fixes or ["# TODO: extract from post"]):
        lines.append(f"  - {_raft_item(f_)}")

    lines.append("")
    lines.append("prevention:")
    for pv in (prevention or ["# TODO: extract from post"]):
        lines.append(f"  - {_raft_item(pv)}")

    lines.append("")
    lines.append("anti_patterns:")
    for a in (anti_patterns or ["# TODO: extract from post"]):
        lines.append(f"  - {_raft_item(a)}")

    lines.append("")
    return "\n".join(lines)


def _extract_architecture_yaml(fm: dict, body: str, slug: str) -> str:
    """Generate architecture-type RAFT YAML from blog post."""
    title = fm.get("title", slug.replace("-", " ").title())
    date = str(fm.get("date", ""))[:10]
    author = fm.get("author", "unknown")
    domain = _detect_domain(fm)
    complexity = _detect_complexity(body)

    objective = _extract_objective(body)

    # Components — prefer table bold entries + targeted section patterns
    components = _extract_items(body, [
        r'cronjob|cron\s*job|deployment|daemonset|statefulset',
        r'redis|kafka|postgres|mysql|etcd|s3|seaweedfs',
        r'endpoint|pipeline|sandbox|engine|proxy|gateway',
    ], section_hints=["pipeline", "defense", "built", "architecture", "stack",
                      "system", "component", "infrastructure"])
    # Also grab bold names from tables
    table_bold: list[str] = []
    in_code = False
    for line in body.splitlines():
        if line.strip().startswith('```'):
            in_code = not in_code
            continue
        if in_code:
            continue
        if '|' in line and not line.strip().startswith('|--'):
            for tm in re.finditer(r'\*\*([A-Z][^*]{2,40})\*\*', line):
                name = tm.group(1).strip()
                if name not in table_bold:
                    table_bold.append(name)
    # Filter technique IDs and too-short items from table bold extraction
    table_bold = [t for t in table_bold if len(t) > 5 and not re.match(r'^AML\.', t)]
    all_comp = table_bold + [c for c in components if c not in table_bold]
    components = all_comp[:8] or ["# TODO: extract from post"]

    # Decisions — fix/design sections
    decisions = _extract_items(body, [
        r'decided|chose|exempt|added|implemented|switched|replaced',
        r'permanent\s+fix|the\s+fix|solution|now\s+\w+s\s+',
    ], section_hints=["fix", "decision", "design", "after", "solution", "permanent"])
    if not decisions:
        decisions = _extract_items(body, [
            r'decided|chose|design|instead\s+of|the\s+reason|exempt',
        ])
    decisions = decisions or ["# TODO: extract from post"]

    # Tradeoffs — tension/irony language
    tradeoffs = _extract_items(body, [
        r'tradeoff|trade-off|tension|but\s+(?:the|it|this)',
        r'(?:too|so)\s+(?:effective|sensitive|aggressive|broad)',
        r'neither\s+\w+\s+alone|double.edged|at\s+the\s+cost\s+of',
    ], section_hints=["irony", "tradeoff", "tension", "collision", "cost"])
    tradeoffs = tradeoffs or ["# TODO: extract from post"]

    # Outcomes — results/metrics/after sections
    outcomes = _extract_items(body, [
        r'result|outcome|achieved|operational|live|running|deployed',
        r'before.*after|\d+\s*->|increased|decreased|improved',
        r'total|count|rate|metric|percent',
    ], section_hints=["after", "result", "outcome", "metric", "impact"])
    outcomes = outcomes or ["# TODO: extract from post"]

    # Anti-patterns — bold items in failure/lesson sections first, then keyword fallback
    anti_patterns = _extract_bold_items(body, [
        "failure", "bug", "lesson", "mistake", "problem", "wrong",
        "pattern", "irony", "under one",
    ])
    if not anti_patterns:
        anti_patterns = _extract_items(body, [
            r'(?:don.t|never|avoid|do\s+not)\s+\w+',
            r'anti.pattern|mistake|silent(?:ly)?|invisible|indistinguish',
        ], section_hints=["failure", "bug", "lesson", "pattern", "irony"])
    anti_patterns = anti_patterns or ["# TODO: extract from post"]

    lines = [
        f'title: "{title}"',
        f"slug: {slug}",
        f"date: {date}",
        f"type: architecture",
        f"author: {author}",
        f"reviewed_by:",
        f"source: /posts/{slug}/",
        f"confidence: observed",
        f"complexity: {complexity}",
        f"domain: {domain}",
        "",
        f"objective: {_raft_item(objective)}",
        "",
        "components:",
    ]
    for c in components:
        lines.append(f"  - {_raft_item(c)}")

    lines.append("")
    lines.append("decisions:")
    for d in decisions:
        lines.append(f"  - {_raft_item(d)}")

    lines.append("")
    lines.append("tradeoffs:")
    for t in tradeoffs:
        lines.append(f"  - {_raft_item(t)}")

    lines.append("")
    lines.append("outcomes:")
    for o in outcomes:
        lines.append(f"  - {_raft_item(o)}")

    lines.append("")
    lines.append("anti_patterns:")
    for a in anti_patterns:
        lines.append(f"  - {_raft_item(a)}")

    lines.append("")
    return "\n".join(lines)


def cmd_raft(args):
    """Generate a draft RAFT sidecar YAML from a blog post markdown file."""
    import argparse
    parser = argparse.ArgumentParser(prog="gaius raft",
                                     description="Extract RAFT sidecar YAML from blog post")
    parser.add_argument("post_file", help="Path to blog post markdown file")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Output file (default: _data/raft/<slug>.yaml)")
    parser.add_argument("--type", choices=["incident", "architecture"], default=None,
                        help="Force type (auto-detected if omitted)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print YAML to stdout instead of writing file")
    parser.add_argument("--no-clobber", "-n", action="store_true",
                        help="Skip if output already exists and has been reviewed")
    parsed = parser.parse_args(args)

    post_path = Path(parsed.post_file)
    if not post_path.exists():
        print(f"ERROR: {post_path} not found", file=sys.stderr)
        sys.exit(1)

    text = post_path.read_text()
    fm, body = _parse_frontmatter(text)

    # Derive slug from filename: 2026-03-25-the-great-api-mirage.md → the-great-api-mirage
    stem = post_path.stem
    slug = re.sub(r'^\d{4}-\d{2}-\d{2}-', '', stem)

    # Determine output path early for --no-clobber check
    if parsed.output:
        out_path = Path(parsed.output)
    else:
        raft_dir = post_path.parent.parent / "_data" / "raft"
        if not raft_dir.exists():
            raft_dir.mkdir(parents=True, exist_ok=True)
        out_path = raft_dir / f"{slug}.yaml"

    if parsed.no_clobber and out_path.exists():
        existing = out_path.read_text()
        reviewed = re.search(r'reviewed_by:\s*(\S+)', existing)
        if reviewed:
            print(f"SKIP: {out_path} already reviewed by {reviewed.group(1)}")
            return
        if "# TODO" not in existing:
            print(f"SKIP: {out_path} already filled (no TODOs)")
            return

    post_type = parsed.type or _detect_type(fm, body)

    if post_type == "incident":
        yaml_content = _extract_incident_yaml(fm, body, slug)
    else:
        yaml_content = _extract_architecture_yaml(fm, body, slug)

    # Validate generated YAML
    import yaml as _yaml
    try:
        _yaml.safe_load(yaml_content)
    except _yaml.YAMLError as e:
        print(f"WARNING: Generated YAML has syntax errors: {e}", file=sys.stderr)
        print("Likely unquoted special characters — check output.", file=sys.stderr)

    if parsed.dry_run:
        print(yaml_content)
        return

    out_path.write_text(yaml_content)
    todo_count = yaml_content.count("# TODO")
    print(f"RAFT sidecar written to {out_path}")
    print(f"  Type:       {post_type}")
    print(f"  Slug:       {slug}")
    print(f"  Title:      {fm.get('title', '?')}")
    print(f"  # TODOs:    {todo_count}")
    if todo_count == 0:
        print(f"  All fields auto-filled. Review before committing.")
    print(f"\nReview and fill in # TODO items before committing.")
