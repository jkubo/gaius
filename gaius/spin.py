"""gaius spin — HITL context-spin. Reuse or replace the newest same-skill handoff.

The hole this closes: a saturated session whose handoff is already on disk has no
safe next verb. `gaius-session-handoff --new` still writes a NEW file, then
prune_old_handoffs deletes existing[3:]. Bare `gaius-session-handoff` now
replaces (2026-08-18). `gaius baton` is a tombstone. `spawn_subagent` / `Agent`
is a child of the dying parent, not a successor. Grok has no `claude --bg`
baton spawn.

`gaius spin` is the missing middle between baton (one-shot, always writes new) and
marathon (continuous, no HITL):

  empty stdin + existing handoff  → reuse; print the successor card; write nothing
  body on stdin + existing        → replace that file in place; no prune
  body on stdin + none            → create the first handoff (prune is a no-op)
  empty stdin + none              → refuse (do not persist an empty stub)
  --transcript (PreCompact)       → author a body from the transcript tail, then
                                    replace or create. Never reuse-without-write
                                    when authoring succeeded. Never prune. Never hot-stamp.

Never releases concord claims (that is `gaius concord handoff`).
Never hot-stamps (Grok SessionStart does not consume `hot: true`; skill-keyed
48h inject on `/{skill}` is the dual-harness path).
Never spawns unless `--spawn`, and Grok `--spawn` only stages a paste-ready
interactive launch — it does not detach a headless lap.

Spec: specs/context-spin.md (in the memory repo, not shipped)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_HANDOFFS_PER_SKILL = 3
HANDOFF_TTL_H = 48
_HEADING_RE = re.compile(r"^#\s+Session Handoff:.*\n+", re.I)


def handoff_dir() -> Path:
    env = (os.environ.get("GAIUS_HANDOFF_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / "Projects" / "agent-memory" / "handoffs"


def list_skill_handoffs(skill: str) -> list[Path]:
    """Newest-first, same reverse-lex sort prune_old_handoffs uses on the filename."""
    return sorted(handoff_dir().glob(f"*-{skill}.md"), reverse=True)


def newest_handoff(skill: str) -> Path | None:
    existing = list_skill_handoffs(skill)
    return existing[0] if existing else None


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        fm[key.strip()] = val.strip()
    return fm, parts[2].lstrip("\n")


def _strip_duplicate_heading(body: str) -> str:
    return _HEADING_RE.sub("", body.lstrip(), count=1)


def render_handoff(skill: str, body: str, fm: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M UTC")
    body = _strip_duplicate_heading(body or "").rstrip()
    lines = [
        "---",
        f"skill: {skill}",
        f"date: {date_str}",
        f"time: {time_str}",
    ]
    severity = fm.get("severity") or "normal"
    lines.append(f"severity: {severity}")
    for key in ("hot", "session", "cwd", "destructive_pending"):
        if fm.get(key):
            lines.append(f"{key}: {fm[key]}")
    try:
        spin_n = int(fm.get("spin") or 0) + 1
    except (TypeError, ValueError):
        spin_n = 1
    lines.append(f"spin: {spin_n}")
    lines.append(f"spun_at: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("---")
    lines.append("")
    lines.append(f"# Session Handoff: {skill} ({date_str})")
    lines.append("")
    if body:
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


def write_in_place(path: Path, skill: str, body: str, fm: dict) -> Path:
    path.write_text(render_handoff(skill, body, fm))
    return path


def create_new(skill: str, body: str, severity: str = "normal", extra_fm: dict | None = None) -> Path:
    d = handoff_dir()
    d.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%H%M%S")
    date_str = now.strftime("%Y-%m-%d")
    path = d / f"{date_str}-{stamp}-{skill}.md"
    dup = 2
    while path.exists():
        path = d / f"{date_str}-{stamp}-{dup}-{skill}.md"
        dup += 1
    fm = {"severity": severity}
    if extra_fm:
        fm.update(extra_fm)
    path.write_text(render_handoff(skill, body, fm, now=now))
    return path


def detect_harness() -> str:
    if os.environ.get("GROK_SESSION_ID"):
        return "grok"
    if os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID"):
        return "claude"
    return "unknown"


def resolve_skill(explicit: str) -> str:
    return (explicit or os.environ.get("GAIUS_ACTIVE_SKILL") or "").strip()


def _read_stdin_body() -> str:
    try:
        if sys.stdin.isatty():
            return ""
    except Exception:
        pass
    try:
        return sys.stdin.read().strip()
    except Exception:
        return ""


def successor_card(skill: str, path: Path | None, action: str, slots: list[Path]) -> str:
    n = len(slots)
    evict = slots[MAX_HANDOFFS_PER_SKILL - 1].name if n >= MAX_HANDOFFS_PER_SKILL else ""
    name = path.name if path else "(none)"
    lines = [
        f"⚑ spin ready  (skill {skill}, {action} {name})",
        f"  slots: {n}/{MAX_HANDOFFS_PER_SKILL}",
    ]
    if action == "reused":
        lines.append("  action: reused existing handoff — no write, no prune")
    elif action == "replaced":
        lines.append("  action: replaced newest same-skill handoff in place — no prune")
    elif action == "created":
        lines.append("  action: created first handoff for this skill")
    elif action.startswith("would-"):
        lines.append(f"  action: dry-run ({action}) — nothing written")
    if evict and action in ("created", "would-create"):
        lines.append(f"  ⚠ a NEW write would evict {evict}")
    elif n >= MAX_HANDOFFS_PER_SKILL:
        lines.append(f"  ⚠ slots full — gaius-session-handoff --new would evict {evict}")

    lines += [
        "",
        "  This session should STOP. Context spin keeps the human in the loop —",
        "  nothing is authorized to continue from this working set.",
        "",
        "  Successor (human starts a FRESH interactive session in this cwd):",
        f"    Grok:   grok     then  /{skill}",
        f"    Claude: claude   then  /{skill}",
        "  The <48h skill-keyed handoff injects automatically. Do not write another handoff.",
        "",
        "  Do NOT: gaius concord handoff · gaius baton pass · gaius-session-handoff --new",
        "  Do NOT: spawn_subagent / Agent as a successor (child of a dying parent, not a new session)",
        "  gaius-session-handoff --new evicts a peer (MAX_HANDOFFS_PER_SKILL = 3). Bare write replaces.",
        "  Handoffs are context, not authorization.",
    ]
    return "\n".join(lines)


def _stage_grok_launch(skill: str, sid: str, cwd: str) -> str:
    try:
        d = Path.home() / ".gaius" / "baton"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sid or 'session'}.launch"
        p.write_text(
            "# Paste-ready Grok context-spin launch. There is no grok --bg.\n"
            "# Open a NEW interactive session (not grok -p, not spawn_subagent).\n"
            f"cd {cwd or '.'} && grok\n"
            f"# then type: /{skill}\n"
        )
        return str(p)
    except Exception:
        return ""


def cmd_spin(args):
    parser = argparse.ArgumentParser(
        prog="gaius spin",
        description="HITL context-spin: reuse or replace the newest same-skill handoff "
                    "without evicting peers. Prints a successor card. Never spawns "
                    "unless --spawn (Grok only stages a paste-ready interactive launch).",
    )
    parser.add_argument("--skill", default="",
                        help="Skill the successor will load (or $GAIUS_ACTIVE_SKILL)")
    parser.add_argument("--severity", default="normal",
                        choices=["normal", "urgent", "critical"])
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the card / would-action; write nothing")
    parser.add_argument("--list", action="store_true",
                        help="List same-skill handoff slots and exit")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable result on stdout (card on stderr)")
    parser.add_argument("--spawn", action="store_true",
                        help="TTY-gated: Claude uses concord's inert plan-mode "
                             "spawner; Grok stages a paste-ready `grok` + /skill card "
                             "(never detaches a headless lap)")
    parser.add_argument("--transcript", default="",
                        help="Author a body from this transcript tail when stdin is "
                             "empty (PreCompact / headless). Then replace or create "
                             "— no prune, no hot-stamp. Replaces `gaius baton`.")
    parser.add_argument("--session", "--session-id", dest="session", default="",
                        help="Predecessor session id (recorded in frontmatter)")
    parser.add_argument("--model", default="",
                        help="Summarizer model when --transcript authors a body")
    parser.add_argument("--timeout", type=int, default=60,
                        help="Hard timeout (s) for the --transcript summarizer")
    parser.add_argument("--tail-bytes", type=int, default=48000,
                        help="Bytes of transcript tail fed to the summarizer")
    p = parser.parse_args(args)

    skill = resolve_skill(p.skill)
    if not skill:
        parser.error("the following arguments are required: --skill "
                     "(or set GAIUS_ACTIVE_SKILL)")

    slots = list_skill_handoffs(skill)
    newest = slots[0] if slots else None

    if p.list:
        if p.json:
            print(json.dumps({
                "skill": skill,
                "slots": [s.name for s in slots],
                "newest": newest.name if newest else "",
                "would_evict": slots[-1].name if len(slots) >= MAX_HANDOFFS_PER_SKILL else "",
            }, indent=2))
        else:
            print(f"skill {skill}: {len(slots)}/{MAX_HANDOFFS_PER_SKILL} slots")
            for i, s in enumerate(slots):
                mark = " newest" if i == 0 else ""
                evict = " (evicted by a NEW write)" if i >= MAX_HANDOFFS_PER_SKILL - 1 and len(slots) >= MAX_HANDOFFS_PER_SKILL and i == len(slots) - 1 else ""
                print(f"  {s.name}{mark}{evict}")
            if not slots:
                print("  (none)")
        return 0

    body = _read_stdin_body()
    harness = detect_harness()
    authored_from_transcript = False

    if not body and p.transcript:
        # PreCompact / headless persist: author, then replace-or-create.
        # Do not fall through to reuse — compaction is about to drop state.
        from gaius.baton import (  # local import: baton is the author library, not the verb
            _DEFAULT_MODEL, _author_body, _is_destructive, _strip_leading_frontmatter,
        )
        authored = _author_body(
            skill, p.transcript, p.model or _DEFAULT_MODEL, p.timeout, p.tail_bytes,
        )
        authored = _strip_leading_frontmatter(authored)
        if authored:
            body = authored
            authored_from_transcript = True
        elif newest is None:
            body = (
                "## HANDOFF STATE\n"
                "Task: PreCompact persist (summarizer unavailable)\n"
                f"Status: transcript at {p.transcript}\n"
                "Next action: read the transcript tail; this stub is not authorization\n"
                "Pending destructive ops: none\n"
                "Irreversible ops in flight: none\n"
            )
            authored_from_transcript = True
        # else: author failed and a same-skill file exists — reuse it (do not
        # wipe a better handoff with a "summarizer unavailable" stub).

    if not body and newest is None:
        msg = (f"gaius spin: no existing {skill} handoff and no stdin body — "
               "nothing to reuse. Pipe a body, or pass --transcript.")
        if p.json:
            print(json.dumps({"error": "empty", "skill": skill}))
        print(msg, file=sys.stderr)
        return 1

    extra_fm: dict[str, str] = {}
    if p.session:
        extra_fm["session"] = p.session
    if authored_from_transcript and body:
        from gaius.baton import _is_destructive
        if _is_destructive(body):
            extra_fm["destructive_pending"] = "true"

    if not body and newest is not None:
        action = "would-reuse" if p.dry_run else "reused"
        path = newest
        wrote = False
    elif newest is not None:
        fm, _old = parse_frontmatter(newest.read_text())
        if p.severity and p.severity != "normal":
            fm["severity"] = p.severity
        fm.update(extra_fm)
        action = "would-replace" if p.dry_run else "replaced"
        path = newest
        wrote = False
        if not p.dry_run:
            write_in_place(newest, skill, body, fm)
            wrote = True
    else:
        action = "would-create" if p.dry_run else "created"
        path = None
        wrote = False
        if not p.dry_run:
            path = create_new(skill, body, p.severity, extra_fm=extra_fm)
            wrote = True
            slots = list_skill_handoffs(skill)

    card = successor_card(skill, path, action, slots)
    result = {
        "skill": skill,
        "action": action,
        "path": str(path) if path else "",
        "wrote": wrote,
        "slots": len(slots),
        "would_evict": slots[-1].name if len(slots) >= MAX_HANDOFFS_PER_SKILL else "",
        "harness": harness,
        "spawned": False,
    }

    if p.spawn and not p.dry_run and path is not None:
        sid = (os.environ.get("GROK_SESSION_ID")
               or os.environ.get("CLAUDE_CODE_SESSION_ID")
               or os.environ.get("CLAUDE_SESSION_ID")
               or "")
        if harness == "claude":
            from gaius.concord import _print_spawn, _spawn_successor
            r = _spawn_successor(skill, sid, str(path), os.getcwd())
            _print_spawn(r)
            result["spawn"] = r
            result["spawned"] = bool(r.get("spawned"))
        else:
            launch = _stage_grok_launch(skill, sid, os.getcwd())
            result["launch"] = launch
            result["spawned"] = False
            print("\n  ⚑ Grok has no --bg successor. Staged a paste-ready "
                  "interactive launch — not a detached lap.", file=sys.stderr)
            if launch:
                print(f"      {launch}", file=sys.stderr)

    if p.json:
        print(json.dumps(result, indent=2))
        print(card, file=sys.stderr)
    else:
        print(card)
    return 0
