"""gaius.ingest — infrastructure source ingestion: ansible inventory, aliases.

cmd_ansible mines an Ansible tree (inventory, group_vars, playbook headers) and
cmd_aliases mines shell alias files into facts.db as structural facts.

Facade convention (see ARCHITECTURE.md): patched hub state is read at call time
as `_core.NAME`; stable hub constants (ALIAS_BLOCKLIST) are from-imports.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

import gaius._core as _core
# imports from gaius._core (shared hub) — circular-by-design, see ARCHITECTURE.md
from gaius._core import (
    GREEN, YELLOW, RED, BOLD, RESET, HAS_YAML, ALIAS_BLOCKLIST,
)
from gaius.extract import SECRET_KEYS_RE, load_domain_specs, tag_domains_from_specs
from gaius.facts import init_db, upsert_fact

# Synthetic session identity for the two infrastructure-ingest paths. These mine a
# filesystem tree, not an agent session, so there is no real session UUID to thread
# — same situation as reconcile.py, which passes session_uuid="reconcile".
#
# Deliberately CONSTANT, not per-run: upsert_fact's _corroborate appends any unseen
# session_uuid to the row's `sessions` JSON array, so a timestamped value would grow
# that array by one entry on every nightly run, on every corroborated fact, forever.
# A constant keeps it at exactly one entry and still attributes the fact to its source.
_SESSION_ANSIBLE = "ansible"
_SESSION_ALIASES = "aliases"


def cmd_ansible(args):
    """Scan Ansible inventory and manifests, extract operational facts."""
    parser = argparse.ArgumentParser(prog="gaius ansible")
    parser.add_argument("--path", type=str, default=str(Path.home() / "ansible"),
                        help="Path to ansible repo root (default: ~/ansible)")  # leak-scan:allow -- generic default for an Ansible-scanning command, not one deployment's tree
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be extracted without writing to facts.db")
    parser.add_argument("--max-chars", type=int, default=50000,
                        help="Maximum total chars of extracted facts (default: 50000)")
    parsed_args = parser.parse_args(args)

    if not HAS_YAML:
        print("Error: ansible source requires PyYAML: pip install pyyaml", file=sys.stderr)
        sys.exit(1)

    ansible_root = Path(parsed_args.path).expanduser()
    if not ansible_root.exists():
        print(f"Error: path not found: {ansible_root}", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning Ansible repo: {ansible_root}")
    conn = init_db()
    extracted_chars = 0
    fact_count = 0

    def extract_from_yaml(path: Path, domain: str = "infra"):
        nonlocal extracted_chars, fact_count
        if extracted_chars >= parsed_args.max_chars:
            return

        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except Exception as e:
            print(f"  warning: could not parse {path.relative_to(ansible_root)}: {e}")
            return

        if not isinstance(data, (dict, list)):
            return

        # Simplified extraction logic: convert to string, filter secrets/templates
        def process_node(node, prefix=""):
            nonlocal extracted_chars, fact_count
            if extracted_chars >= parsed_args.max_chars:
                return

            if isinstance(node, dict):
                for k, v in node.items():
                    if SECRET_KEYS_RE.search(str(k)):
                        continue
                    process_node(v, f"{prefix}{k}: ")
            elif isinstance(node, list):
                for item in node:
                    process_node(item, prefix)
            else:
                val = str(node)
                if "{{" in val:  # Skip templates
                    return

                fact_text = f"{prefix}{val}"
                if len(fact_text) > 500:  # Cap individual fact length
                    fact_text = fact_text[:500] + "..."

                if not parsed_args.dry_run:
                    # Auto-tag domain (best-match; fall back to the section domain)
                    _doms = tag_domains_from_specs(fact_text, load_domain_specs())
                    derived_domain = _doms[0] if _doms else domain
                    # Distillation pattern: SHA256 first 16 chars
                    fk = hashlib.sha256(fact_text.lower().encode()).hexdigest()[:16]

                    upsert_fact(
                        conn,
                        domain=derived_domain,
                        fact_key=fk,
                        fact_text=fact_text,
                        agent="gaius-ansible",
                        session_uuid=_SESSION_ANSIBLE,
                        provenance="ansible",
                        score=0.7,
                        model_family="human",
                    )
                else:
                    print(f"  [dry-run] {fact_text}")

                extracted_chars += len(fact_text)
                fact_count += 1

        process_node(data)

    # 1. Inventory: hosts.yml
    hosts_path = ansible_root / "inventory" / "hosts.yml"
    if hosts_path.exists():
        extract_from_yaml(hosts_path, domain="infra")

    # 2. Group Vars
    gv_dir = ansible_root / "inventory" / "group_vars"
    if gv_dir.exists():
        for yml in sorted(gv_dir.glob("*.yml")):
            if yml.name == "vault.yml":
                continue  # Skip encrypted vault
            domain_map = {
                "storage.yml": "storage",
                "k3s_cluster.yml": "infra",
                "all.yml": "infra",
            }
            extract_from_yaml(yml, domain=domain_map.get(yml.name, "infra"))

    # 3. Playbooks (summaries)
    pb_dir = ansible_root / "playbooks"
    if pb_dir.exists():
        for yml in sorted(pb_dir.glob("*.yml")):
            # Just extract the 'name' and purpose
            try:
                with open(yml) as f:
                    content = f.read()
                    # Look for the first 'name:' in the playbook
                    match = re.search(r'^\s*-\s*name:\s*(.*)$', content, re.MULTILINE)
                    if match:
                        purpose = match.group(1).strip()
                        fact_text = f"Playbook {yml.name}: {purpose}"
                        if not parsed_args.dry_run:
                            fk = hashlib.sha256(fact_text.lower().encode()).hexdigest()[:16]
                            upsert_fact(
                                conn,
                                domain="infra",
                                fact_key=fk,
                                fact_text=fact_text,
                                agent="gaius-ansible",
                                session_uuid=_SESSION_ANSIBLE,
                                provenance="ansible",
                                score=0.75,
                                model_family="human",
                            )
                            fact_count += 1
                        else:
                            print(f"  [dry-run] {fact_text}")
            except Exception:
                pass

    if not parsed_args.dry_run:
        conn.commit()
    print(f"Extracted {fact_count} facts ({extracted_chars} chars) from Ansible.")


def cmd_aliases(args):
    """Scan shell alias files, extract operational cluster facts."""
    parser = argparse.ArgumentParser(prog="gaius aliases")
    parser.add_argument("--path", type=str, default=str(Path.home() / ".aliases"),
                        help="Path to aliases file (default: ~/.aliases)")
    parser.add_argument("--dry-run", action="store_true")
    parsed_args = parser.parse_args(args)

    alias_path = Path(parsed_args.path).expanduser()
    if not alias_path.exists():
        # Fallback to .bashrc if .aliases not found
        alias_path = Path.home() / ".bashrc"

    if not alias_path.exists():
        print(f"Error: alias source not found: {alias_path}", file=sys.stderr)
        return

    print(f"Scanning Aliases: {alias_path}")
    conn = init_db()
    fact_count = 0

    def parse_file(path: Path, visited=None):
        nonlocal fact_count
        if visited is None:
            visited = set()
        if path in visited:
            return
        visited.add(path)

        try:
            with open(path) as f:
                lines = f.readlines()
        except Exception:
            return

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Handle source directives
            source_match = re.match(r'^(source|\.)\s+(.*)$', line)
            if source_match:
                sub_path_str = source_match.group(2).replace("$HOME", str(Path.home())).replace("~", str(Path.home()))
                sub_path = Path(sub_path_str).expanduser()
                if not sub_path.is_absolute():
                    sub_path = path.parent / sub_path
                parse_file(sub_path, visited)
                continue

            # Handle alias name='command'
            alias_match = re.match(r'^alias\s+([^=]+)=[\'"]?([^\'"]+)[\'"]?$', line)
            if alias_match:
                name = alias_match.group(1).strip()
                cmd = alias_match.group(2).strip()

                if name in ALIAS_BLOCKLIST:
                    continue

                fact_text = f"Alias '{name}' executes: {cmd}"

                # Special IP extraction from ping aliases or similar
                ip_match = re.search(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', cmd)
                if ip_match:
                    fact_text += f" (IP: {ip_match.group(0)})"

                if not parsed_args.dry_run:
                    fk = hashlib.sha256(fact_text.lower().encode()).hexdigest()[:16]
                    _doms = tag_domains_from_specs(fact_text, load_domain_specs())
                    upsert_fact(
                        conn,
                        domain=_doms[0] if _doms else "general",
                        fact_key=fk,
                        fact_text=fact_text,
                        agent="gaius-aliases",
                        session_uuid=_SESSION_ALIASES,
                        provenance="aliases",
                        score=0.65,
                        model_family="human",
                    )
                    fact_count += 1
                else:
                    print(f"  [dry-run] {fact_text}")
                continue

            # Handle simple functions: name() { ... }
            func_match = re.match(r'^([a-zA-Z0-9_-]+)\s*\(\)\s*\{', line)
            if func_match:
                name = func_match.group(1)
                if name in ALIAS_BLOCKLIST:
                    continue

                fact_text = f"Function '{name}' is defined in {path.name}"
                if not parsed_args.dry_run:
                    fk = hashlib.sha256(fact_text.lower().encode()).hexdigest()[:16]
                    upsert_fact(
                        conn,
                        domain="general",
                        fact_key=fk,
                        fact_text=fact_text,
                        agent="gaius-aliases",
                        session_uuid=_SESSION_ALIASES,
                        provenance="aliases",
                        score=0.6,
                        model_family="human",
                    )
                    fact_count += 1
                else:
                    print(f"  [dry-run] {fact_text}")

    parse_file(alias_path)

    # Also check a gen_corpus.py alongside the alias file (optional, user-specific)
    corpus_gen = alias_path.parent / "gen_corpus.py"
    if corpus_gen.exists():
        try:
            with open(corpus_gen) as f:
                content = f.read()
                # Extract qa("...", "...") pairs in the ALIASES section
                alias_section = re.search(r'# ALIASES.*?(?=# PLAYBOOK|$)', content, re.DOTALL)
                if alias_section:
                    qa_pairs = re.findall(r'qa\("(.*?)",\s*"(.*?)"\)', alias_section.group(0), re.DOTALL)
                    for q, a in qa_pairs:
                        # Clean up strings
                        q = q.replace('\\"', '"').strip()
                        a = a.replace('\\"', '"').strip()
                        fact_text = f"Fact: {q} Answer: {a}"
                        if not parsed_args.dry_run:
                            fk = hashlib.sha256(fact_text.lower().encode()).hexdigest()[:16]
                            upsert_fact(
                                conn,
                                domain="infra",
                                fact_key=fk,
                                fact_text=fact_text,
                                agent="gaius-aliases",
                                session_uuid=_SESSION_ALIASES,
                                provenance="aliases",
                                score=0.8,  # Very high confidence from documented corpus
                                model_family="human",
                            )
                            fact_count += 1
                        else:
                            print(f"  [dry-run] {fact_text}")
        except Exception:
            pass

    if not parsed_args.dry_run:
        conn.commit()
    print(f"Extracted {fact_count} facts from Aliases.")
