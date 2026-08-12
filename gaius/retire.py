"""gaius.retire — session mining and the retire/index/harvest command family.

Owns uncompacted-session mining (MINE_* thresholds, _mine_session), cmd_retire
+ extra-sessions scan, cmd_s3_retire, the index pipeline (cmd_index,
process_session, write_*_deltas, archive_session — every write path here MUST
call _core._guard_write_path before opening, see the module invariant in
_core), cmd_harvest (Gemini), and the event-based peer retires
(pentagi/ollama/grok/codex).

Facade convention (see ARCHITECTURE.md): runtime-rebindable / test-patched hub
state (PROJECT_DIR, STAGING_DIR, EXTRA_SESSIONS_DIR, DB_PATH, CORPUS_DIR,
MEMORY_DIR, _gaius_cfg) is read at call time as `_core.NAME` — main() rebinds
the path trio from CLI flags and tests monkeypatch them on gaius._core.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import gaius._core as _core
# imports from gaius._core (shared hub) — circular-by-design, see ARCHITECTURE.md
from gaius._core import (
    GREEN, YELLOW, RED, BOLD, RESET,
    GEMINI_DIR, SIGNAL_THRESHOLD, MODEL_INFO, FORMAT_BY_AGENT,
    agent_to_principal, get_session_threshold, content_hash, _guard_write_path,
    GEMINI_COLD_THRESHOLD_HOURS, _DEFAULT_PRINCIPAL,
)
from gaius.parsers import (
    detect_format, parse_claude_events, parse_gemini_events,
    parse_ollama_events, parse_pentagi_flow_from_jsonl,
    parse_grok_events, parse_codex_events,
    _discover_grok_sessions, _discover_codex_sessions,
)
from gaius.embed import _embed_text
from gaius.extract import (
    SECTION_HEADERS, SIGNAL_SECTIONS, DOMAIN_KEYWORDS,
    extract_section, has_signal, classify_entry, boost_score, classify_finding,
    classify_procedure, extract_procedure, sample_entry, tag_domains,
    tag_domains_from_specs, extract_delta_lines, load_domain_specs,
    is_gemini_cold, strip_bloat, GEMINI_NOISE_SUBJECTS, GEMINI_CREDENTIAL_PATTERNS,
    CREDENTIAL_PATTERNS, _is_noise, _seeded_score, _extract_clarified_intent,
    DECISION_KEYWORDS, _ERROR_KEYWORDS,
)
from gaius.facts import (
    init_db, register_session, upsert_fact, _upsert_distillations,
)
from gaius.review import load_staged, save_staged, display_uid
from gaius.scoring import (
    compute_entry_tfidf_score, build_doc_freq, update_domain_stats,
    load_domain_stats,
)

# ── Uncompacted session mining ────────────────────────────────────────────────

# Minimum session file size to attempt mining (skip abandoned/empty sessions)
MINE_MIN_BYTES = 10_000  # 10 KB

# Re-mine threshold: re-process if file grew >3x since last mine
MINE_REGROW_FACTOR = 3

# Minimum text length for an assistant block to be worth keeping
MINE_MIN_TEXT_LEN = 150

# Score threshold — only keep blocks above this after classification
MINE_SCORE_THRESHOLD = 0.50

# Max blocks to keep per section to avoid staged entries becoming walls of text
MINE_MAX_BLOCKS_PER_SECTION = 12



def _mine_session(path: Path) -> dict | None:
    """Extract signal from a non-compacted session JSONL.

    Parses assistant text blocks, user messages, and error tool results.
    Classifies each using the existing scoring pipeline and synthesizes
    high-signal blocks into the standard sections format.

    Returns a sections dict compatible with staged entries, or None if
    the session has insufficient signal.
    """
    entries = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:
        return None

    if not entries:
        return None

    # Collect classified blocks
    concepts = []     # high-signal assistant reasoning
    errors = []       # error tool results + fix reasoning
    user_context = [] # user messages for intent

    for entry in entries:
        etype = entry.get("type", "")
        entry_type, base_score = classify_entry(entry)

        if etype == "assistant":
            msg = entry.get("message", {})
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = block.get("text", "").strip()
                if len(text) < MINE_MIN_TEXT_LEN:
                    continue

                # Noise filter — skip boilerplate before scoring
                if _is_noise(text):
                    continue

                # Score the block
                score = boost_score(text, base_score)
                _, score = classify_finding(text, entry_type, score)
                _, score = classify_procedure(text, entry_type, score)

                if score >= MINE_SCORE_THRESHOLD:
                    # Classify into concepts vs errors
                    text_lower = text.lower()
                    if any(kw in text_lower for kw in _ERROR_KEYWORDS):
                        errors.append(text)
                    else:
                        concepts.append(text)

        elif etype == "tool_result":
            content = str(entry.get("content", ""))
            if len(content) < 100:
                continue
            content_lower = content.lower()
            if any(kw in content_lower for kw in _ERROR_KEYWORDS):
                # Truncate long error outputs
                errors.append(content[:800])

        elif etype == "user":
            msg = entry.get("message", {})
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if len(text) > 30:
                            user_context.append(text)
            elif isinstance(content, str) and len(content) > 30:
                user_context.append(content.strip())

    # Need minimum signal to stage
    if not concepts and not errors:
        return None

    # Trim to max blocks
    concepts = concepts[:MINE_MAX_BLOCKS_PER_SECTION]
    errors = errors[:MINE_MAX_BLOCKS_PER_SECTION]
    user_context = user_context[:8]

    # Build sections matching SECTION_HEADERS format
    # Derive primary request from first few user messages
    primary_request = ""
    if user_context:
        first_msgs = user_context[:3]
        primary_request = "\n".join(f"- {msg[:200]}" for msg in first_msgs)

    sections = {
        "primary_request": primary_request,
        "key_concepts": "\n".join(f"- {c[:300]}" for c in concepts) if concepts else "",
        "files_changed": "",  # not reliably extractable without compaction
        "errors_fixes": "\n".join(f"- {e[:300]}" for e in errors) if errors else "",
        "pending_tasks": "",  # not reliably extractable without compaction
        "current_work": "",
    }

    return sections


def _mine_uncompacted_sessions(conn: sqlite3.Connection, staged: dict,
                                compacted_stems: set[str],
                                all_jsonl: list[Path]) -> int:
    """Mine signal from sessions that were never compacted.

    Scans session JSONLs that have no isCompactSummary entry, extracts
    high-signal assistant reasoning and error blocks, stages them as
    mined summaries, AND auto-promotes high-signal content to facts.db.

    Re-mines sessions that have grown significantly (>MINE_REGROW_FACTOR)
    since last processing.

    Returns count of newly staged/updated mined entries.
    """
    mined_count = 0

    for path in all_jsonl:
        # Skip if already compacted
        if path.stem in compacted_stems:
            continue

        # Skip tiny files (abandoned/empty sessions)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < MINE_MIN_BYTES:
            continue

        # Use a synthetic UUID for mined entries: "mined-" + session stem
        mined_uuid = f"mined-{path.stem}"

        if mined_uuid in staged:
            # Already mined — check if session has grown significantly
            prev_size = staged[mined_uuid].get("_mined_size", 0)
            if prev_size > 0 and size < prev_size * MINE_REGROW_FACTOR:
                continue  # not grown enough to re-mine
            # Session grew significantly — re-mine it

        sections = _mine_session(path)
        if sections is None:
            # No signal — register as processed (size tracked for regrow check)
            register_session(conn, path.stem, "local", _DEFAULT_PRINCIPAL,
                             _core.PROJECT_DIR.name, size, compaction_present=False)
            continue

        # Check if sections have real content
        if not has_signal({"sections": sections}):
            register_session(conn, path.stem, "local", _DEFAULT_PRINCIPAL,
                             _core.PROJECT_DIR.name, size, compaction_present=False)
            continue

        # Stage the mined entry (or update if re-mining)
        is_update = mined_uuid in staged
        record = {
            "uuid": mined_uuid,
            "session_id": path.stem,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reviewed": False,
            "sections": sections,
            "content_hash": content_hash(json.dumps(sections, sort_keys=True)),
            "agent_source": "claude",
            "source": "mined",
            "_mined_size": size,  # track for regrow detection
        }
        save_staged(record)
        staged[mined_uuid] = record
        mined_count += 1

        register_session(conn, path.stem, "local", _DEFAULT_PRINCIPAL,
                         _core.PROJECT_DIR.name, size, compaction_present=False)

        # Auto-promote: insert high-signal blocks directly into facts.db
        _promote_mined_to_facts(conn, path.stem, sections)

    return mined_count


def _promote_mined_to_facts(conn: sqlite3.Connection, session_stem: str,
                             sections: dict) -> int:
    """Insert high-signal mined content as facts in facts.db.

    Takes the concepts and errors from a mined session and inserts them
    as individual facts with seeded scores. Returns count of new facts.
    """
    count = 0
    now = datetime.now(timezone.utc).isoformat()

    # Collect text blocks from key_concepts and errors_fixes
    blocks = []
    for section_key in ("key_concepts", "errors_fixes"):
        text = sections.get(section_key, "")
        if not text:
            continue
        # Split on bullet points
        for line in text.split("\n"):
            line = line.strip().lstrip("- ").strip()
            if len(line) < 80:
                continue
            # Skip noise one more time at promotion boundary
            if _is_noise(line):
                continue
            blocks.append(line)

    for block in blocks:
        fact_key = hashlib.sha256(f"{session_stem}:{block[:200]}".encode()).hexdigest()[:16]

        # Assign domain from keywords
        domain = "general"
        block_lower = block.lower()
        for d, kws in DOMAIN_KEYWORDS.items():
            if any(kw in block_lower for kw in kws):
                domain = d
                break

        # Use seeded score based on content type
        score = _seeded_score(block)

        # fact_type is 'operational' (the schema default), NOT 'structural'.
        # An auto-mined block is arbitrary session prose — it may be design, but it is just
        # as often a point-in-time state snapshot. 'structural' is the no-decay class
        # (volatility_recency() returns exactly 1.0 for it), so tagging every mined block
        # structural made stale state claims immortal: 89% of the corpus sat decay-proof.
        # Only rate a fact 'structural' where something actually knows it is design-level.
        # ⚠️ GO-FORWARD ONLY. This fixes new writes; it migrates nothing. Measured
        # 2026-07-26, AFTER the fix: 17,631 of 19,818 live facts (88.9%) are still
        # 'structural', and no shipped command can reclassify them — `_corroborate`'s
        # UPDATE does not touch fact_type, and cmd_rescore rewrites provenance/score
        # only. Do not read this comment as "the decay-proof corpus problem is closed."
        upsert_fact(conn, domain, fact_key, block[:500],
                    _DEFAULT_PRINCIPAL, session_stem, "auto-mined",
                    score=score, source="autonomous", fact_type="operational",
                    injection_weight=0.7)
        count += 1

    return count


def cmd_retire(args):


    """Scan JSONL files and stage new compact summaries.

    Supports --format to dispatch to format-specific parsers:
      claude (default): scan for isCompactSummary in project JSONL
      gemini: delegate to harvest
      ollama: parse ~/.ollama/sessions/ JSONL
      pentagi: parse ~/.pentagi/sessions/ JSONL
      grok: parse ~/.grok/sessions/<cwd>/<uuid>/chat_history.jsonl
      codex: parse ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

    Plain `retire` (no --format) also auto-sweeps local Grok + Codex sessions
    when those CLIs are installed — they are peers of Claude Code.
    """
    # --all: run all retire paths in sequence
    clean_args = list(args)

    if "--claude-shim" in clean_args:
        clean_args.remove("--claude-shim")
        print("Running Claude session retirement shim (#89)...")
        staged = load_staged()
        all_files = list(_core.PROJECT_DIR.iterdir()) if _core.PROJECT_DIR.exists() else []
        jsonl_files = sorted((f for f in all_files if f.suffix == '.jsonl'), 
                            key=lambda x: x.stat().st_mtime, reverse=True)
        
        batch = jsonl_files[:100]  # last 100 sessions
        print(f"Processing last {len(batch)} sessions...")
        
        conn = init_db()
        new_facts = 0
        for path in batch:
            events = parse_claude_events(path)
            for ev in events:
                # Map to facts.db fields
                # Use signal as fact_text, source as provenance
                fact_text = ev["signal"]
                provenance = ev["source"]
                outcome = ev["outcome"]
                
                # Derive a deterministic key from the signal
                fact_key = hashlib.sha256(f"{path.stem}:{fact_text}".encode()).hexdigest()[:16]
                
                # Assign domain using config-driven DOMAIN_KEYWORDS
                domain = "general"
                _ft_lower = fact_text.lower()
                for _d, _kws in DOMAIN_KEYWORDS.items():
                    if any(_kw in _ft_lower for _kw in _kws):
                        domain = _d
                        break
                
                # 'operational', not 'structural' — same reasoning as _promote_mined_to_facts.
                upsert_fact(conn, domain, fact_key, fact_text,
                            _DEFAULT_PRINCIPAL, path.stem, provenance,
                            outcome=outcome, source="autonomous", fact_type="operational",
                            injection_weight=0.7, score=_seeded_score(fact_text))
                new_facts += 1
        print(f"Imported {new_facts} facts from Claude sessions.")
        return

    if "--all" in clean_args:
        clean_args.remove("--all")
        print("=" * 68)
        print("gaius retire --all")
        print("=" * 68)

        print("\n── Claude ──")
        cmd_retire(clean_args)

        print("\n── Gemini ──")
        try:
            cmd_harvest(clean_args)
        except SystemExit:
            pass

        print("\n── Ollama ──")
        try:
            cmd_ollama_retire(clean_args)
        except SystemExit:
            pass

        print("\n── PentAGI ──")
        try:
            cmd_pentagi_retire(["--parse-only"] + clean_args)
        except SystemExit:
            pass

        print(f"\n{'=' * 68}")
        print("All formats processed. Run `gaius next` to review staged facts.")
        return

    # Format dispatch — non-claude formats use event-based parsers
    fmt_flag = None
    if "--format" in clean_args:
        idx = clean_args.index("--format")
        if idx + 1 < len(clean_args):
            fmt_flag = clean_args[idx + 1]
            clean_args = clean_args[:idx] + clean_args[idx + 2:]

    if fmt_flag and fmt_flag != "claude":
        if fmt_flag == "gemini":
            cmd_harvest(clean_args)
            return
        elif fmt_flag in ("ollama", "vllm"):
            cmd_ollama_retire(clean_args)
            return
        elif fmt_flag == "pentagi":
            cmd_pentagi_retire(clean_args)
            return
        elif fmt_flag == "grok":
            cmd_grok_retire(clean_args)
            return
        elif fmt_flag == "codex":
            cmd_codex_retire(clean_args)
            return
        else:
            print(f"Unknown format: {fmt_flag}. Supported: {', '.join(sorted(_core.SUPPORTED_FORMATS))}", file=sys.stderr)
            sys.exit(1)

    staged = load_staged()
    new_count = skip_count = updated_count = dedup_skip = 0

    # Explicit .jsonl filter — non-JSONL files (tool cache .json, .txt, .jpg) silently skipped
    all_files = list(_core.PROJECT_DIR.iterdir()) if _core.PROJECT_DIR.exists() else []
    jsonl_files = sorted(f for f in all_files if f.suffix == '.jsonl')
    non_jsonl = len(all_files) - len(jsonl_files)
    print(f"Scanning {len(jsonl_files)} JSONL session files in {_core.PROJECT_DIR}...")
    if non_jsonl:
        print(f"  (skipping {non_jsonl} non-JSONL files)")

    conn = init_db()
    # UUID dedup: same session may appear at live path AND archive path (same stem).
    # Build a seen set; only process the first occurrence of each stem.
    seen_stems: set[str] = set()
    dedup_filtered: list[Path] = []
    for f in jsonl_files:
        if f.stem in seen_stems:
            dedup_skip += 1
        else:
            seen_stems.add(f.stem)
            dedup_filtered.append(f)

    compacted_stems: set[str] = set()  # track which sessions have compaction

    for path in dedup_filtered:
        try:
            has_compaction = False
            with open(path) as f:
                for line in f:
                    if "isCompactSummary" not in line:
                        continue
                    entry = json.loads(line)
                    if not entry.get("isCompactSummary"):
                        continue
                    has_compaction = True
                    uuid = entry.get("uuid", "")
                    if not uuid:
                        continue

                    content = entry.get("message", {}).get("content", "")
                    if not content:
                        continue

                    chash = content_hash(content)

                    # Already staged — check if content has changed
                    if uuid in staged:
                        if staged[uuid].get("content_hash") == chash:
                            skip_count += 1
                            continue
                        # Content changed — update the staged entry
                        sections = {
                            key: extract_section(content, header)
                            for key, header in SECTION_HEADERS
                        }
                        staged[uuid]["sections"] = sections
                        staged[uuid]["content_hash"] = chash
                        staged[uuid]["updated_at"] = datetime.now(timezone.utc).isoformat()
                        staged[uuid]["reviewed"] = False  # re-queue for review
                        save_staged(staged[uuid])
                        updated_count += 1
                        # Re-promote updated content to facts.db
                        _promote_mined_to_facts(conn, path.stem, sections)
                        continue

                    sections = {
                        key: extract_section(content, header)
                        for key, header in SECTION_HEADERS
                    }

                    record = {
                        "uuid": uuid,
                        "session_id": path.stem,
                        "timestamp": entry.get("timestamp", ""),
                        "reviewed": False,
                        "sections": sections,
                        "content_hash": chash,
                        "agent_source": "claude",
                        "last_confirmed": entry.get("timestamp", ""),
                    }
                    save_staged(record)
                    staged[uuid] = record
                    new_count += 1
                    # Register in DB for dedup tracking
                    register_session(conn, path.stem, "local", _DEFAULT_PRINCIPAL,
                                     _core.PROJECT_DIR.name, path.stat().st_size,
                                     compaction_present=True)
                    # Auto-promote compacted content to facts.db
                    _promote_mined_to_facts(conn, path.stem, sections)

            if has_compaction:
                compacted_stems.add(path.stem)

        except Exception as e:
            print(f"  warning: {path.name}: {e}", file=sys.stderr)

    # Mine uncompacted sessions for signal
    mined_count = _mine_uncompacted_sessions(conn, staged, compacted_stems, dedup_filtered)

    # Compute TF-IDF scores across all staged entries
    all_entries = list(staged.values())
    if all_entries:
        doc_freq = build_doc_freq(all_entries)
        total_docs = len(all_entries)
        scored_count = 0
        for entry in all_entries:
            score = compute_entry_tfidf_score(entry, doc_freq, total_docs)
            if score != entry.get("score", 0):
                entry["score"] = round(score, 4)
                save_staged(entry)
                scored_count += 1
        update_domain_stats(all_entries)
        print(f"Scored:    {scored_count} entries (TF-IDF)")

    total = len(staged)
    unreviewed = sum(1 for e in staged.values() if not e.get("reviewed"))
    print(f"New:       {new_count}")
    print(f"Mined:     {mined_count} (from uncompacted sessions)")
    print(f"Updated:   {updated_count} (content changed)")
    print(f"Skipped:   {skip_count} (unchanged)")
    print(f"Deduped:   {dedup_skip} (duplicate UUID paths skipped)")
    print(f"Total:     {total}  ({unreviewed} unreviewed)")
    print(f"Staging:   {_core.STAGING_DIR}")

    # Extra session scan — optional second project dir (advisor, relay agent, etc.)
    if _core.EXTRA_SESSIONS_DIR and _core.EXTRA_SESSIONS_DIR.exists():
        extra_staged, extra_distillations, extra_mined = _scan_extra_sessions(conn, staged)
        if extra_staged > 0 or extra_distillations > 0 or extra_mined > 0:
            parts = []
            if extra_staged:
                parts.append(f"{extra_staged} summaries staged")
            if extra_distillations:
                parts.append(f"{extra_distillations} distillation facts upserted")
            if extra_mined:
                parts.append(f"{extra_mined} sessions mined")
            print(f"\nExtra:     {', '.join(parts)} from {_core.EXTRA_SESSIONS_DIR}")

    # Peer coding-agent sessions (Grok, Codex) — first-class local sessions,
    # swept on every retire like Claude. Deduped by session UUID, so re-scans
    # are cheap. Silently skipped for users without these CLIs installed.
    for _pname, _pdir, _pparser, _pdiscover, _psub in (
        ("Grok",  Path.home() / ".grok"  / "sessions", parse_grok_events,  _discover_grok_sessions,  "grok-facts"),
        ("Codex", Path.home() / ".codex" / "sessions", parse_codex_events, _discover_codex_sessions, "codex-facts"),
    ):
        if _pdir.exists():
            _pcount = _retire_event_sessions(_pdir, _pparser, _psub, _pname.lower(),
                                             conn, discover_fn=_pdiscover)
            if _pcount:
                print(f"{_pname + ':':<10} {_pcount} events staged from {_pdir}")


def _scan_extra_sessions(conn: sqlite3.Connection, staged: dict) -> tuple[int, int, int]:
    """Scan an extra session directory for compact summaries, distillations, and mined signal.

    Three scan paths:
    1. Compact summary entries (isCompactSummary=True) — staged for gaius review UI.
    2. Assistant messages — scanned for clarified_intent JSON, upserted directly into
       facts.db as provenance='distillation'. Useful for short sessions that never
       reach context compaction (no compact summaries).
    3. Mining — sessions with no compaction and no distillations get the same
       _mine_session treatment as uncompacted primary sessions.

    Records sessions as agent='extra' in the DB for separate tracking.
    Returns (newly_staged_count, distillation_facts_upserted_count, mined_count).
    """
    if not _core.EXTRA_SESSIONS_DIR or not _core.EXTRA_SESSIONS_DIR.exists():
        return (0, 0, 0)

    jsonl_files = sorted(f for f in _core.EXTRA_SESSIONS_DIR.iterdir() if f.suffix == '.jsonl')
    if not jsonl_files:
        return (0, 0, 0)

    # Check which sessions are already in the sessions table (assistant-message scan dedup)
    already_registered: set[str] = set(
        row[0] for row in conn.execute(
            "SELECT uuid FROM sessions WHERE agent = 'extra'"
        ).fetchall()
    )

    new_count = 0
    distillation_count = 0
    mined_count = 0
    seen_stems: set[str] = set()
    compacted_stems: set[str] = set()
    dedup_filtered: list[Path] = []

    for f in jsonl_files:
        if f.stem in seen_stems:
            continue
        seen_stems.add(f.stem)
        dedup_filtered.append(f)

    for path in dedup_filtered:
        try:
            entries = []
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

            # --- Path 1: compact summary staging ---
            has_compaction = False
            for entry in entries:
                if not entry.get("isCompactSummary"):
                    continue
                has_compaction = True
                uuid = entry.get("uuid", "")
                if not uuid:
                    continue
                content = entry.get("message", {}).get("content", "")
                if not content:
                    continue

                distillations = _extract_clarified_intent(content)
                if distillations:
                    _upsert_distillations(conn, distillations, path.stem)

                if uuid in staged:
                    continue

                chash = content_hash(content)
                sections = {
                    key: extract_section(content, header)
                    for key, header in SECTION_HEADERS
                }
                if distillations:
                    sections["distillations"] = distillations

                record = {
                    "uuid": uuid,
                    "session_id": path.stem,
                    "timestamp": entry.get("timestamp", ""),
                    "reviewed": False,
                    "sections": sections,
                    "content_hash": chash,
                    "agent": "extra",
                }
                save_staged(record)
                staged[uuid] = record
                new_count += 1
                register_session(conn, path.stem, "local", "extra",
                                 _core.EXTRA_SESSIONS_DIR.name, path.stat().st_size,
                                 compaction_present=True)
                already_registered.add(path.stem)

            if has_compaction:
                compacted_stems.add(path.stem)

            # --- Path 2: assistant message distillation scan ---
            # Run for sessions not yet registered (avoids re-scanning on every retire).
            if path.stem not in already_registered:
                session_distillations: list[dict] = []
                for entry in entries:
                    if entry.get("type") != "assistant":
                        continue
                    msg_content = entry.get("message", {}).get("content", [])
                    if isinstance(msg_content, list):
                        for block in msg_content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "")
                                if text:
                                    session_distillations.extend(
                                        _extract_clarified_intent(text)
                                    )
                    elif isinstance(msg_content, str) and msg_content:
                        session_distillations.extend(
                            _extract_clarified_intent(msg_content)
                        )

                if session_distillations:
                    upserted = _upsert_distillations(conn, session_distillations, path.stem)
                    distillation_count += upserted
                    register_session(conn, path.stem, "local", "extra",
                                     _core.EXTRA_SESSIONS_DIR.name, path.stat().st_size,
                                     compaction_present=False)
                    already_registered.add(path.stem)
                else:
                    # No distillations found; register anyway so we skip on next run.
                    register_session(conn, path.stem, "local", "extra",
                                     _core.EXTRA_SESSIONS_DIR.name, path.stat().st_size,
                                     compaction_present=False)
                    already_registered.add(path.stem)

        except Exception as e:
            print(f"  warning (extra-sessions): {path.name}: {e}", file=sys.stderr)

    # --- Path 3: mine uncompacted extra sessions ---
    # Reuse the same _mine_session function used for primary sessions.
    # Only process sessions that had no compaction summary AND are large enough.
    for path in dedup_filtered:
        if path.stem in compacted_stems:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < MINE_MIN_BYTES:
            continue

        mined_uuid = f"mined-extra-{path.stem}"
        if mined_uuid in staged:
            continue

        sections = _mine_session(path)
        if sections is None or not has_signal({"sections": sections}):
            continue

        record = {
            "uuid": mined_uuid,
            "session_id": path.stem,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reviewed": False,
            "sections": sections,
            "content_hash": content_hash(json.dumps(sections, sort_keys=True)),
            "agent": "extra",
            "source": "mined",
        }
        save_staged(record)
        staged[mined_uuid] = record
        mined_count += 1

    return (new_count, distillation_count, mined_count)






def cmd_s3_retire(args):
    """Scan session JSONLs from S3/rclone remote and stage new compact summaries.

    Requires s3.remote (and optionally s3.prefix) in ~/.gaius/config.yaml.
    """
    parser = argparse.ArgumentParser(prog="gaius s3-retire")
    parser.add_argument("agent_name", help="Agent name (e.g. gemini-agent)")
    parser.add_argument("--format", type=str, default=None,
                        help="Session format: claude, gemini, ollama (auto-detected from agent name)")
    parser.add_argument("--s3-path", type=str, default=None,
                        help="Override full rclone path (e.g. my-remote:bucket/path/)")
    parsed = parser.parse_args(args)

    agent_name = parsed.agent_name
    fmt = parsed.format or FORMAT_BY_AGENT.get(agent_name, "claude")

    # Verify rclone is available
    try:
        subprocess.run(["rclone", "version"], capture_output=True, check=True)
    except FileNotFoundError:
        print("Error: rclone is not installed. Install it first.", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"Error: rclone failed: {e}", file=sys.stderr)
        sys.exit(1)

    if parsed.s3_path:
        s3_path = parsed.s3_path.rstrip("/") + "/"
    else:
        s3_cfg = _core._gaius_cfg.get("s3", {})
        remote = s3_cfg.get("remote", "")
        prefix = s3_cfg.get("prefix", "sessions").strip("/")
        if not remote:
            print("Error: s3.remote not set in ~/.gaius/config.yaml\n"
                  "  Add: s3:\n    remote: my-rclone-remote\n    prefix: sessions",
                  file=sys.stderr)
            sys.exit(1)
        # Agent dir root, not <agent>/sessions/ — uploads exist under both
        # sessions/ and projects/ subtrees depending on uploader generation.
        s3_path = f"{remote}:{prefix}/{agent_name}/"
    threshold_bytes = get_session_threshold("cluster", agent_name)
    threshold_mb = threshold_bytes / (1024 * 1024)

    # One bulk rclone copy into a persistent local mirror, then process
    # locally. A per-file size+copyto loop spawns two rclone processes per
    # session — minutes of pure round-trip overhead on high-latency links.
    # The mirror also makes re-runs incremental.
    local_dir = Path.home() / ".gaius" / "s3-sessions" / agent_name
    local_dir.mkdir(parents=True, exist_ok=True)
    print(f"[s3] syncing {s3_path} -> {local_dir} (format: {fmt}, threshold: {threshold_mb:.0f}MB)...", flush=True)
    copy_cmd = ["rclone", "copy", s3_path, str(local_dir),
                "--include", "*.jsonl", "--transfers", "8",
                "--retries", "3", "--low-level-retries", "3",
                "--timeout", "90s", "--contimeout", "30s",
                "--log-level", "ERROR"]
    if threshold_bytes > 0:
        copy_cmd += ["--min-size", f"{threshold_bytes}B"]
    copy_result = subprocess.run(copy_cmd, capture_output=True, text=True)
    if copy_result.returncode != 0:
        # Degraded objects (e.g. ghost chunks after a volume loss) must not
        # abort the sweep — mine whatever synced. Hard-fail only when
        # nothing is available locally at all.
        stderr = copy_result.stderr.strip() if copy_result.stderr else ""
        print(f"warning: rclone copy from {s3_path} incomplete — mining what synced: {stderr}",
              file=sys.stderr)

    local_files = sorted(local_dir.rglob("*.jsonl"))
    if not local_files:
        if copy_result.returncode != 0:
            print(f"Error: rclone copy from {s3_path} failed and no local mirror exists",
                  file=sys.stderr)
            sys.exit(1)
        print(f"[s3] no .jsonl files found in {s3_path}")
        return

    print(f"[s3] found {len(local_files)} session file(s)")

    staged = load_staged()
    # Build set of existing content hashes for dedup
    existing_hashes = {e.get("content_hash") for e in staged.values() if e.get("content_hash")}

    new_count = 0
    skip_count = 0

    for local_file in local_files:
        try:
            # Format-aware dispatch: event-based formats use their parsers
            if fmt in ("gemini", "ollama", "pentagi"):
                parser_map = {
                    "gemini":  parse_gemini_events,
                    "ollama":  parse_ollama_events,
                    "pentagi": parse_pentagi_flow_from_jsonl,
                }
                agent_map = {
                    "gemini":  "gemini",
                    "ollama":  "ollama",
                    "pentagi": "pentagi",
                }
                staging_map = {
                    "gemini":  "gemini-facts",
                    "ollama":  "ollama-facts",
                    "pentagi": "pentagi-facts",
                }
                session_id = local_file.stem
                conn_s3 = init_db()
                existing_s3 = conn_s3.execute(
                    "SELECT uuid FROM sessions WHERE uuid = ?", (session_id,)
                ).fetchone()
                if existing_s3:
                    skip_count += 1
                    continue

                events = parser_map[fmt](local_file)
                if events:
                    for ev in events:
                        text = " ".join(filter(None, [
                            ev.get("subject", ""), ev.get("description", ""),
                            ev.get("output", ""), str(ev.get("tool", "")),
                        ]))
                        domains = tag_domains_from_specs(text, load_domain_specs())
                        ev["domain"] = domains[0] if domains else "general"

                    staging_dir = _core.STAGING_DIR / staging_map[fmt]
                    staging_dir.mkdir(parents=True, exist_ok=True)
                    out_path = staging_dir / f"{session_id}.jsonl"
                    with open(out_path, "w") as outf:
                        for ev in events:
                            outf.write(json.dumps(ev) + "\n")

                    register_session(conn_s3, session_id, f"s3:{agent_name}",
                                     agent_map[fmt], "cluster",
                                     local_file.stat().st_size)
                    new_count += len(events)
                    print(f"  {session_id}: {len(events)} events staged")
                continue

            # Claude format: scan for isCompactSummary
            with open(local_file) as f:
                for line in f:
                    if "isCompactSummary" not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not entry.get("isCompactSummary"):
                        continue

                    uuid = entry.get("uuid", "")
                    if not uuid:
                        continue

                    content = entry.get("message", {}).get("content", "")
                    if not content:
                        continue

                    chash = content_hash(content)

                    # Skip if already staged with same content
                    if uuid in staged and staged[uuid].get("content_hash") == chash:
                        skip_count += 1
                        continue
                    if chash in existing_hashes:
                        skip_count += 1
                        continue

                    sections = {
                        key: extract_section(content, header)
                        for key, header in SECTION_HEADERS
                    }

                    record = {
                        "uuid": uuid,
                        "session_id": local_file.stem,
                        "timestamp": entry.get("timestamp", ""),
                        "reviewed": False,
                        "sections": sections,
                        "content_hash": chash,
                        "source": f"s3:{agent_name}",
                        "format": fmt,
                    }
                    save_staged(record)
                    staged[uuid] = record
                    existing_hashes.add(chash)
                    new_count += 1

        except Exception as e:
            print(f"  warning: failed to process {local_file.name}: {e}", file=sys.stderr)

    print(f"[s3] processed {new_count} new sessions for {agent_name} ({skip_count} skipped)")


def cmd_index(args):
    """Parse JSONL, build domain index, write deltas and corpus."""
    parser = argparse.ArgumentParser(prog="gaius index")
    parser.add_argument("session_id", nargs="?", help="Session ID prefix to index")
    parser.add_argument("--threshold-mb", type=int, default=0, help="Min size in MB to index (default: 0)")
    parser.add_argument("--sample-rate", type=float, default=0.25, help="Sample rate for low-signal entries (default: 0.25)")
    parser.add_argument("--no-archive", action="store_true", help="Skip S3 archival (faster, local-only)")
    parsed_args = parser.parse_args(args)

    # 1. Load index of already indexed sessions
    _core.CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    index_path = _core.CORPUS_DIR / "index.jsonl"
    indexed_sessions = set()
    if index_path.exists():
        with open(index_path, "r") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if "session_id" in d:
                        indexed_sessions.add(d["session_id"])
                except Exception:
                    pass

    # 2. Identify sessions to process
    jsonl_files = sorted(_core.PROJECT_DIR.glob("*.jsonl"))
    targets = []
    if parsed_args.session_id:
        targets = [f for f in jsonl_files if f.stem.startswith(parsed_args.session_id)]
        if not targets:
            print(f"No session matching {parsed_args.session_id} found in {_core.PROJECT_DIR}", file=sys.stderr)
            sys.exit(1)
    else:
        for f in jsonl_files:
            if f.stem in indexed_sessions:
                continue
            if parsed_args.threshold_mb > 0:
                size_mb = f.stat().st_size / (1024 * 1024)
                if size_mb < parsed_args.threshold_mb:
                    continue
            targets.append(f)

    if not targets:
        print("No new sessions to index.")
        return

    print(f"Indexing {len(targets)} sessions...")

    for path in targets:
        process_session(path, parsed_args.sample_rate, index_path,
                        archive=not parsed_args.no_archive)


def process_session(path, sample_rate, index_path, archive=True):
    session_id = path.stem
    print(f"🚀 Processing {session_id} ({path.stat().st_size / (1024*1024):.1f} MB)...")

    total_entries = 0
    corpus_entries = 0
    domain_counts = {}
    domain_deltas = {}  # {domain: [lines]}
    procedures = []     # extracted procedure dicts

    corpus_subdir = _core.CORPUS_DIR / datetime.now().strftime("%Y-%m")
    corpus_subdir.mkdir(parents=True, exist_ok=True)
    corpus_path = corpus_subdir / f"{session_id}.jsonl"

    with open(path, "r") as f, open(_guard_write_path(corpus_path), "w") as out_f:
        for line in f:
            total_entries += 1
            try:
                entry = json.loads(line)
            except Exception:
                continue

            uuid = entry.get("uuid", "")
            timestamp = entry.get("timestamp", "")

            # Classification and Scoring
            etype, base_score = classify_entry(entry)

            # Extract text for scoring and tagging
            text = ""
            if etype == "compaction_summary":
                text = entry.get("message", {}).get("content", "")
            elif etype == "assistant_reasoning" or etype == "user_instruction":
                content_list = entry.get("message", {}).get("content", [])
                text = " ".join(c.get("text", "") for c in content_list if c.get("type") == "text")
            elif etype.startswith("tool_result"):
                text = str(entry.get("content", ""))

            score = boost_score(text, base_score)
            etype, score = classify_finding(text, etype, score)
            etype, score = classify_procedure(text, etype, score)
            domains = tag_domains(text)

            # Procedure Extraction
            if etype == "procedure":
                proc = extract_procedure(text)
                if proc:
                    procedures.append(proc)

            # Domain Delta Extraction
            if etype == "compaction_summary":
                # Handle summary sections
                for key, header in SECTION_HEADERS:
                    section_text = extract_section(text, header)
                    if not section_text:
                        continue
                    lines_by_domain = extract_delta_lines(section_text, domains)
                    for dom, dlines in lines_by_domain.items():
                        if dom not in domain_deltas:
                            domain_deltas[dom] = []
                        for dl in dlines:
                            domain_deltas[dom].append(f"- **[{key}]** {dl}")
            elif score >= SIGNAL_THRESHOLD and etype == "assistant_reasoning":
                # Extract lines with decision keywords
                lines = text.splitlines()
                matching_lines = [l.strip() for l in lines if any(kw in l.lower() for kw in DECISION_KEYWORDS)]
                if matching_lines:
                    date_str = timestamp[:10] if timestamp else datetime.now().strftime("%Y-%m-%d")
                    for dom in domains:
                        if dom not in domain_deltas:
                            domain_deltas[dom] = []
                        for ml in matching_lines:
                            domain_deltas[dom].append(f"- [{date_str}] {ml} (session: {uuid[:8]})")

            # Update stats
            for dom in domains:
                domain_counts[dom] = domain_counts.get(dom, 0) + 1

            # Corpus Sampling
            included = (score >= SIGNAL_THRESHOLD) or sample_entry(uuid, sample_rate)
            if included:
                corpus_entries += 1
                record = {
                    "session_id": session_id,
                    "uuid": uuid,
                    "timestamp": timestamp,
                    "entry_type": etype,
                    "signal_score": round(score, 3),
                    "domains": domains,
                    "included": True,
                    "content": strip_bloat(entry)
                }
                out_f.write(json.dumps(record) + "\n")

    # Write Domain Deltas
    write_domain_deltas(session_id, domain_deltas)

    # Write Procedure Deltas
    write_procedure_deltas(session_id, procedures)

    # S3 Archival
    archived_to = archive_session(path) if archive else None

    # Update Index
    summary = {
        "session_id": session_id,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "total_entries": total_entries,
        "corpus_entries": corpus_entries,
        "domain_counts": domain_counts,
        "archived_to": archived_to
    }
    with open(_guard_write_path(index_path), "a") as f:
        f.write(json.dumps(summary) + "\n")

    print(f"  ✅ Indexed: {corpus_entries}/{total_entries} entries in corpus. Deltas written to {len(domain_deltas)} domains.")


def write_domain_deltas(session_id, deltas):
    """Write domain deltas to corpus/deltas/ — NOT to human-maintained domain/*.md files.

    Domain files are hand-curated gotchas/incidents. Raw index extracts belong in
    corpus/deltas/{domain}/{session_id[:8]}.md for gaius inject consumption only.
    """
    if not deltas:
        return
    date_str = datetime.now().strftime("%Y-%m-%d")
    delta_root = _core.CORPUS_DIR / "deltas"
    delta_root.mkdir(parents=True, exist_ok=True)

    for domain, lines in deltas.items():
        if not lines:
            continue
        domain_delta_dir = delta_root / domain
        domain_delta_dir.mkdir(parents=True, exist_ok=True)
        delta_file = domain_delta_dir / f"{session_id[:8]}.md"
        with open(_guard_write_path(delta_file), "w") as f:
            f.write(f"# Delta: {domain} / {session_id[:8]} ({date_str})\n\n")
            for line in lines:
                f.write(f"{line}\n")
        print(f"  📝 Delta → corpus/deltas/{domain}/{session_id[:8]}.md")


def write_procedure_deltas(session_id, procedures):
    """Write extracted procedures to corpus/deltas/troubleshooting/ — NOT troubleshooting.md.

    troubleshooting.md is hand-curated. Raw extracted procedures go to corpus/deltas/
    for gaius inject consumption only.
    """
    if not procedures:
        return
    delta_dir = _core.CORPUS_DIR / "deltas" / "troubleshooting"
    delta_dir.mkdir(parents=True, exist_ok=True)
    delta_file = delta_dir / f"{session_id[:8]}.md"
    date_str = datetime.now().strftime("%Y-%m-%d")

    with open(_guard_write_path(delta_file), "w") as f:
        for proc in procedures:
            f.write(f"\n## {proc['trigger']}\n\n")
            f.write(f"**Symptom**: {proc['trigger']}\n\n")
            for i, step in enumerate(proc["steps"], 1):
                f.write(f"{i}. {step}\n")
            f.write(f"\n**Resolution**: {proc.get('resolution', 'See final step above')}\n")
            if not proc.get("complete"):
                f.write(f"**Status**: Incomplete — no clear resolution identified\n")
            f.write(f"\n*Extracted from session {session_id[:8]} on {date_str}*\n")
    print(f"  📋 Procedures → corpus/deltas/troubleshooting/{session_id[:8]}.md")


def archive_session(path, strip_before_archive=True):
    """Archive a session JSONL to S3/rclone remote. Requires s3.remote in config."""
    s3_cfg = _core._gaius_cfg.get("s3", {})
    remote = s3_cfg.get("remote", "")
    prefix = s3_cfg.get("prefix", "sessions").strip("/")
    if not remote:
        print("  ⚠️  s3.remote not set in config — skipping archive", file=sys.stderr)
        return None

    project = _core.PROJECT_DIR.name
    month = datetime.now().strftime("%Y-%m")
    target = f"{remote}:{prefix}/local/{project}/archive/{month}/{path.name}"

    upload_path = str(path)
    tmp_path = None

    if strip_before_archive:
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
                tmp_path = tmp.name
                with open(path) as src:
                    for line in src:
                        try:
                            entry = json.loads(line)
                            stripped = strip_bloat(entry)
                            tmp.write(json.dumps(stripped) + "\n")
                        except json.JSONDecodeError:
                            tmp.write(line)
            upload_path = tmp_path
        except Exception as e:
            print(f"  ⚠️  Bloat strip failed, archiving raw: {e}", file=sys.stderr)
            upload_path = str(path)
            tmp_path = None

    try:
        subprocess.run([
            "rclone", "copyto", upload_path,
            target,
            "--s3-upload-cutoff", "200M", "--s3-disable-checksum",
            "--retries", "3", "--log-level", "INFO"
        ], check=True, capture_output=True)
        return target
    except Exception as e:
        print(f"  ⚠️  S3 archive failed: {e}", file=sys.stderr)
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def cmd_harvest(args):
    """Scan cold Gemini CLI sessions, extract events, stage for review.

    Gemini sessions are .json files (single object, not line-delimited).
    A session is 'cold' if lastModified > GEMINI_COLD_THRESHOLD_HOURS ago.
    Events are grouped by domain keyword and written to staging/gemini-facts/.
    """
    parser = argparse.ArgumentParser(prog="gaius harvest")
    parser.add_argument("--gemini-dir", type=str, default=None,
                        help=f"Gemini sessions directory (default: {GEMINI_DIR})")
    parser.add_argument("--threshold-hours", type=float, default=GEMINI_COLD_THRESHOLD_HOURS,
                        help=f"Cold threshold in hours (default: {GEMINI_COLD_THRESHOLD_HOURS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be harvested without writing anything")
    parsed_args = parser.parse_args(args)

    gemini_dir = Path(parsed_args.gemini_dir) if parsed_args.gemini_dir else GEMINI_DIR
    if not gemini_dir.exists():
        print(f"Gemini sessions directory not found: {gemini_dir}", file=sys.stderr)
        sys.exit(1)

    conn = init_db()
    domain_specs = load_domain_specs()

    # Find all .json files recursively (Gemini CLI session files)
    all_json = list(gemini_dir.rglob("*.json"))
    # Filter to cold sessions only
    cold = [p for p in all_json if is_gemini_cold(p, parsed_args.threshold_hours)]
    # Get already-processed session UUIDs from DB
    processed_uuids = {
        row[0] for row in conn.execute("SELECT uuid FROM sessions").fetchall()
    }

    print(f"Gemini dir:   {gemini_dir}")
    print(f"Total .json:  {len(all_json)}")
    print(f"Cold (>{parsed_args.threshold_hours}h): {len(cold)}")

    new_count = skip_count = event_count = 0
    gemini_staging = _core.STAGING_DIR / "gemini-facts"
    if not parsed_args.dry_run:
        gemini_staging.mkdir(parents=True, exist_ok=True)

    for path in sorted(cold):
        # Peek at sessionId for dedup check
        try:
            with open(path) as f:
                first_chunk = f.read(256)
            # Quick extract sessionId without full parse
            import re as _re
            sid_match = _re.search(r'"sessionId"\s*:\s*"([^"]+)"', first_chunk)
            session_uuid = sid_match.group(1) if sid_match else path.stem
        except Exception:
            session_uuid = path.stem

        if session_uuid in processed_uuids:
            skip_count += 1
            continue

        events = parse_gemini_events(path)
        if not events:
            skip_count += 1
            continue

        # Tag events with domain keywords
        domain_groups: dict[str, list[dict]] = {}
        for ev in events:
            ev_text = " ".join([
                ev.get("subject", ""),
                ev.get("description", ""),
                ev.get("tool", ""),
                ev.get("output", "") or "",
            ])
            domains = tag_domains_from_specs(ev_text, domain_specs)
            if not domains:
                domains = ["general"]
            for dom in domains:
                domain_groups.setdefault(dom, []).append(ev)

        if parsed_args.dry_run:
            print(f"\n  [dry-run] {path.name} ({session_uuid[:8]})")
            for dom, evs in sorted(domain_groups.items()):
                print(f"    {dom}: {len(evs)} events")
            event_count += len(events)
            new_count += 1
            continue

        # Write staged facts file: one per session
        staged_file = gemini_staging / f"{session_uuid[:8]}_{path.stem[-8:]}.jsonl"
        with open(staged_file, "w") as f:
            for dom, evs in sorted(domain_groups.items()):
                for ev in evs:
                    record = {"session_uuid": session_uuid, "domain": dom, **ev}
                    f.write(json.dumps(record) + "\n")

        # Register session in DB (dedup key)
        register_session(conn, session_uuid, "cluster", "gemini",
                         path.parent.name, path.stat().st_size)
        processed_uuids.add(session_uuid)
        event_count += len(events)
        new_count += 1
        print(f"  harvested {path.name}: {len(events)} events → {staged_file.name}")

    print(f"\nNew:          {new_count}")
    print(f"Skipped:      {skip_count} (already processed or empty)")
    print(f"Events:       {event_count}")
    if not parsed_args.dry_run and new_count:
        print(f"Staged:       {gemini_staging}")
        print(f"\nNext:  gaius next   (topic-grouped review → promote staged facts to facts.db)")


# ── Event-based session retire (shared by pentagi/ollama) ─────────────────────

def _retire_event_sessions(sessions_dir: Path, parser_fn, staging_subdir: str,
                           agent: str, conn: sqlite3.Connection,
                           dry_run: bool = False, discover_fn=None) -> int:
    """Scan a directory of session files, parse events, domain-tag, stage.

    By default sessions are flat ``*.jsonl`` files. Pass ``discover_fn`` to
    enumerate a non-flat layout (e.g. Grok session dirs, Codex date-nested
    rollouts) — it receives ``sessions_dir`` and yields Path objects (files or
    directories) understood by ``parser_fn``.

    Returns count of new events staged.
    """
    if not sessions_dir.exists():
        print(f"  No sessions directory: {sessions_dir}")
        return 0

    session_paths = (
        sorted(discover_fn(sessions_dir)) if discover_fn
        else sorted(sessions_dir.glob("*.jsonl"))
    )
    if not session_paths:
        print(f"  No sessions found in {sessions_dir}")
        return 0

    staged_dir = _core.STAGING_DIR / staging_subdir
    staged_dir.mkdir(parents=True, exist_ok=True)
    total_events = 0

    for path in session_paths:
        session_id = path.stem
        # Check if already processed
        existing = conn.execute(
            "SELECT uuid FROM sessions WHERE uuid = ?", (session_id,)
        ).fetchone()
        if existing:
            continue

        events = parser_fn(path)
        if not events:
            continue

        # Domain-tag
        for ev in events:
            text = " ".join(filter(None, [
                ev.get("subject", ""), ev.get("description", ""),
                ev.get("output", ""), str(ev.get("tool", "")),
            ]))
            domains = tag_domains_from_specs(text, load_domain_specs())
            ev["domain"] = domains[0] if domains else "general"

        if dry_run:
            print(f"  [dry-run] {path.name}: {len(events)} events")
            total_events += len(events)
            continue

        # Write staged JSONL
        out_path = staged_dir / f"{session_id}.jsonl"
        with open(out_path, "w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        # Promote to the corpus: peer parity with the Claude retire path,
        # which auto-promotes via _promote_mined_to_facts. Without this, peer
        # (Grok/Codex) events stage but never reach facts.db (search/injection).
        # Run BEFORE register_session: upsert_fact is idempotent on fact_key, so
        # a crash mid-loop just re-promotes next run; the session is marked
        # processed only once promotion has run.
        promoted = 0
        for ev in events:
            try:
                upsert_fact(
                    conn, domain=ev.get("domain", "general"),
                    fact_key=ev["fact_key"], fact_text=ev.get("description", ""),
                    agent=agent, session_uuid=ev.get("session_uuid", session_id),
                    provenance=ev.get("provenance", "inference"),
                    model_family=ev.get("model_family", agent),
                    model_version=ev.get("model_version", ""),
                    outcome=ev.get("outcome"), source=agent,
                )
                promoted += 1
            except Exception as e:
                print(f"  warn: promote {ev.get('fact_key', '?')}: {e}", file=sys.stderr)

        register_session(conn, session_id, "local", agent,
                         "cluster", path.stat().st_size)
        total_events += len(events)
        print(f"  Staged {len(events)} events ({promoted} promoted) from {path.name}")

    return total_events


def cmd_pentagi_retire(args):
    """Fetch PentAGI flows via GraphQL, save to local JSONL, then parse and stage."""
    import argparse
    import getpass
    parser = argparse.ArgumentParser(prog="gaius pentagi-retire")
    parser.add_argument("--host", default="localhost:8443")
    parser.add_argument("--mail", default="", help="PentAGI login email (required)")
    parser.add_argument("--password", default=None, help="PentAGI password (prompted if missing)")
    parser.add_argument("--flow-id", type=int, default=None, help="Specific flow (default: all finished)")
    parser.add_argument("--sessions-dir", default=str(Path.home() / ".pentagi" / "sessions"))
    parser.add_argument("--fetch-only", action="store_true", help="Fetch from API, don't parse")
    parser.add_argument("--parse-only", action="store_true", help="Parse existing local files, don't fetch")
    parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(args)

    sessions_dir = Path(parsed.sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    conn = init_db()

    # Phase A — Fetch from GraphQL
    if not parsed.parse_only:
        password = parsed.password or getpass.getpass("PentAGI password: ")
        base_url = f"http://{parsed.host}"

        # Authenticate
        import urllib.request
        import urllib.error
        auth_data = json.dumps({"mail": parsed.mail, "password": password}).encode()
        auth_req = urllib.request.Request(
            f"{base_url}/api/v1/auth/login",
            data=auth_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            auth_resp = urllib.request.urlopen(auth_req)
        except urllib.error.HTTPError as e:
            print(f"Auth failed: {e.code} {e.read().decode()}", file=sys.stderr)
            sys.exit(1)

        # Extract cookie
        cookie = None
        for header in auth_resp.headers.get_all("Set-Cookie") or []:
            if header.startswith("auth="):
                cookie = header.split(";")[0]
                break
        if not cookie:
            print("Auth failed: no auth cookie returned", file=sys.stderr)
            sys.exit(1)

        print(f"[pentagi] Authenticated to {parsed.host}")

        def graphql_query(query: str) -> dict:
            data = json.dumps({"query": query}).encode()
            req = urllib.request.Request(
                f"{base_url}/api/v1/graphql",
                data=data,
                headers={"Content-Type": "application/json", "Cookie": cookie},
                method="POST",
            )
            resp = urllib.request.urlopen(req)
            return json.loads(resp.read().decode())

        # Query flows
        if parsed.flow_id:
            flow_query = f"""{{ flows {{ id status title createdAt updatedAt }} }}"""
        else:
            flow_query = """{ flows { id status title createdAt updatedAt } }"""

        result = graphql_query(flow_query)
        flows = result.get("data", {}).get("flows", [])

        if parsed.flow_id:
            flows = [f for f in flows if int(f.get("id", 0)) == parsed.flow_id]
        else:
            flows = [f for f in flows if f.get("status") == "finished"]

        if not flows:
            print("[pentagi] No matching flows found")
            if not parsed.fetch_only:
                # Fall through to parse phase
                pass
            else:
                return

        print(f"[pentagi] Found {len(flows)} flow(s)")

        for flow in flows:
            fid = flow["id"]
            print(f"  Flow {fid}: {flow.get('title', '?')} ({flow.get('status', '?')})")

            # Fetch logs for this flow
            logs_query = f"""{{
                agentLogs(flowId: {fid}) {{ id initiator executor task result }}
                terminalLogs(flowId: {fid}) {{ id type text }}
                searchLogs(flowId: {fid}) {{ id engine query result }}
                messageLogs(flowId: {fid}) {{ id type message }}
            }}"""

            logs_result = graphql_query(logs_query)
            logs_data = logs_result.get("data", {})

            agent_count = len(logs_data.get("agentLogs", []))
            terminal_count = len(logs_data.get("terminalLogs", []))
            search_count = len(logs_data.get("searchLogs", []))
            message_count = len(logs_data.get("messageLogs", []))
            print(f"    Logs: {agent_count} agent, {terminal_count} terminal, "
                  f"{search_count} search, {message_count} message")

            # Write to local JSONL
            out_path = sessions_dir / f"flow-{fid}.jsonl"
            with open(out_path, "w") as f:
                # Meta header
                f.write(json.dumps({"_meta": flow}) + "\n")
                for log_type in ("agentLogs", "terminalLogs", "searchLogs", "messageLogs"):
                    for entry in logs_data.get(log_type, []):
                        entry["_log_type"] = log_type
                        f.write(json.dumps(entry) + "\n")

            print(f"    Saved to {out_path}")

    # Phase B — Parse local JSONL files
    if not parsed.fetch_only:
        print(f"\n[pentagi] Parsing sessions in {sessions_dir}...")
        count = _retire_event_sessions(
            sessions_dir, parse_pentagi_flow_from_jsonl,
            "pentagi-facts", "pentagi", conn, dry_run=parsed.dry_run,
        )
        print(f"[pentagi] Staged {count} events total")


def cmd_ollama_retire(args):
    """Parse Ollama inference session logs and stage for review."""
    import argparse
    parser = argparse.ArgumentParser(prog="gaius ollama-retire")
    parser.add_argument("--sessions-dir", default=str(Path.home() / ".ollama" / "sessions"))
    parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(args)

    sessions_dir = Path(parsed.sessions_dir)
    conn = init_db()

    print(f"[ollama] Parsing sessions in {sessions_dir}...")
    count = _retire_event_sessions(
        sessions_dir, parse_ollama_events,
        "ollama-facts", "ollama", conn, dry_run=parsed.dry_run,
    )
    print(f"[ollama] Staged {count} events total")


def cmd_grok_retire(args):
    """Parse Grok CLI session directories and stage decision events for review."""
    import argparse
    parser = argparse.ArgumentParser(prog="gaius grok-retire")
    parser.add_argument("--sessions-dir", default=str(Path.home() / ".grok" / "sessions"))
    parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(args)

    sessions_dir = Path(parsed.sessions_dir)
    conn = init_db()

    print(f"[grok] Parsing sessions in {sessions_dir}...")
    count = _retire_event_sessions(
        sessions_dir, parse_grok_events, "grok-facts", "grok", conn,
        dry_run=parsed.dry_run, discover_fn=_discover_grok_sessions,
    )
    print(f"[grok] Staged {count} events total")


def cmd_codex_retire(args):
    """Parse Codex CLI rollout sessions and stage decision events for review."""
    import argparse
    parser = argparse.ArgumentParser(prog="gaius codex-retire")
    parser.add_argument("--sessions-dir", default=str(Path.home() / ".codex" / "sessions"))
    parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(args)

    sessions_dir = Path(parsed.sessions_dir)
    conn = init_db()

    print(f"[codex] Parsing sessions in {sessions_dir}...")
    count = _retire_event_sessions(
        sessions_dir, parse_codex_events, "codex-facts", "codex", conn,
        dry_run=parsed.dry_run, discover_fn=_discover_codex_sessions,
    )
    print(f"[codex] Staged {count} events total")
