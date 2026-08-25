"""gaius baton — TOMBSTONE 2026-08-18. The HITL verb is `gaius spin`.

`cmd_baton` prints a redirect and returns 2. Do not alias it to spin: baton
wrote a NEW handoff then pruned existing[3:], and `baton pass` hot-stamped
(Claude SessionStart only). Spin reuses or replaces the newest same-skill file.

This module still exports the authoring helpers (`_author_body`, destructive
token scan, frontmatter patchers) that `gaius spin --transcript` and the
landscape hot-pickup tests import. The user-facing verb is dead.

Spec: specs/context-spin.md (in the memory repo, not shipped)
"""
import os
import re
import shutil
import subprocess
import sys

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Tokens that mark a genuinely irreversible / outward-facing op. If the authored body mentions
# any of these, the handoff MUST carry destructive_pending: true regardless of what the
# summarizer wrote — the CLAUDE.md handoff protocol keys on that flag to force an operator ask.
_IRREVERSIBLE_TOKENS = (
    "git push", "npm publish", "twine upload", "pypi upload", "kubectl apply",
    "kubectl delete", "terraform apply", "helm upgrade", "helm install",
    "rollout restart", "kubectl drain", "scale --replicas=0", "flux reconcile",
)

# System prompt for the summarizer child: the CLAUDE.md HANDOFF STATE format, with the
# destructive-ops guardrail made explicit.
_BATON_TEMPLATE = """You are writing a session HANDOFF for a Claude Code session that is about \
to hit its context limit, so a fresh successor can continue the work. Output ONLY the handoff \
markdown — no preamble, no code fences, and NO `---` YAML frontmatter block (a wrapper adds \
that). Start directly with the `## HANDOFF STATE` heading. Use EXACTLY this structure:

## HANDOFF STATE
destructive_pending: <true|false>
Task: <the exact task in progress — one sentence>
Status: <what is done / verified / not done yet>
Tried and ruled out: <approaches attempted and why they failed>
Current blockers: <what is preventing completion right now>
Live state at cut:
  - <relevant pods/nodes/files/PRs; any DRY_RUN or feature-flag state in flight>
Next action: <the exact first NON-destructive step for the successor>
Pending destructive ops: <or "none">
Irreversible ops in flight: <or "none">

HARD RULE: every `git push`, PyPI/npm publish, `kubectl apply`, drain, delete, scale-down or \
otherwise irreversible action goes ONLY in "Pending destructive ops" / "Irreversible ops in \
flight" — NEVER as a step to run under "Next action". Set destructive_pending: true if any \
such op is pending. A handoff is context, not authorization."""


def _read_tail(path, nbytes):
    """Return the last nbytes of a (possibly large) transcript, decoded lossily. '' on error."""
    try:
        path = os.path.expanduser(path)
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _author_body(skill, transcript, model, timeout, tail_bytes):
    """Author a handoff body via a cheap safe-mode claude child. Returns '' on any failure.

    --safe-mode is MANDATORY: a default child hangs ~2min on SessionStart hooks + MCP load;
    safe-mode returns in a few seconds and keeps OAuth. No tools, print-and-exit — read-only.
    """
    claude = shutil.which("claude")
    if not claude:
        return ""
    tail = _read_tail(transcript, tail_bytes) if transcript else ""
    if not tail:
        return ""
    prompt = (
        "Below is the tail of a dying Claude Code session's transcript (JSONL). Write its "
        "HANDOFF STATE per the format in your system prompt so a successor can continue.\n\n"
        + tail
    )
    cmd = [claude, "-p", "--safe-mode", "--model", model,
           "--output-format", "text", "--append-system-prompt", _BATON_TEMPLATE, prompt]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _strip_leading_frontmatter(body):
    """Drop a leading '---\\n...\\n---' YAML block if the summarizer emitted one anyway.
    gaius-session-handoff supplies the authoritative frontmatter; a second block would nest and
    duplicate keys (skill:/severity:) into a malformed handoff."""
    if not body:
        return body
    b = body.lstrip()
    if not b.startswith("---"):
        return body
    m = re.match(r'^---\s*\n.*?\n---\s*\n?', b, re.DOTALL)
    return b[m.end():].lstrip() if m else body


def _is_destructive(body):
    """True if the body mentions a genuinely irreversible / outward-facing op. Deterministic
    backstop for a safe-mode summarizer that under-reports (observed: it filed `git push` under
    Next action with destructive_pending false). Does NOT move the op — the real enforcement is
    the plan-mode successor + manual operator adoption; this only fixes the frontmatter flag."""
    low = (body or "").lower()
    return any(tok in low for tok in _IRREVERSIBLE_TOKENS)


def _patch_destructive_frontmatter(path):
    """Insert `destructive_pending: true` into the written handoff's top frontmatter block, so
    the flag lands where the CLAUDE.md handoff protocol reads it. Best-effort, idempotent."""
    try:
        with open(path) as f:
            txt = f.read()
    except Exception:
        return
    m = re.match(r'^(---\s*\n.*?\n)(---\s*\n)', txt, re.DOTALL)  # first frontmatter block
    if not m:
        return
    if re.search(r'(?im)^destructive_pending:\s*true\b', m.group(1)):
        return  # already in the frontmatter block itself (not just the body)
    new = txt[:m.end(1)] + "destructive_pending: true\n" + txt[m.end(1):]
    try:
        with open(path, "w") as f:
            f.write(new)
    except Exception:
        pass


def _patch_hot_frontmatter(path):
    """Insert `hot: true` into the written handoff's top frontmatter block — the marker the
    SessionStart hot-baton pickup consumes (landscape.py cmd_inject --handoff-hot). Lives in
    frontmatter, NEVER the filename: prune_old_handoffs globs `*-{skill}.md`, so a renamed
    file would silently escape pruning. Best-effort, idempotent."""
    try:
        with open(path) as f:
            txt = f.read()
    except Exception:
        return
    m = re.match(r'^(---\s*\n.*?\n)(---\s*\n)', txt, re.DOTALL)  # first frontmatter block
    if not m:
        return
    if re.search(r'(?im)^hot:\s*true\b', m.group(1)):
        return
    new = txt[:m.end(1)] + "hot: true\n" + txt[m.end(1):]
    try:
        with open(path, "w") as f:
            f.write(new)
    except Exception:
        pass


def _patch_scope_frontmatter(path, sid, cwd):
    """Record `session:` (predecessor id) and `cwd:` (physical dir) in the handoff frontmatter so
    hot-baton pickup can SCOPE delivery: a successor consumes a baton only from its OWN cwd and
    only once the predecessor session is no longer live (landscape._hot_handoff_take). Without
    this every concurrent same-scope session hot-stamps skill=session and the NEWEST silently
    clobbers the rest — the exact misdelivery observed 2026-08-12 (a sibling's git-state stub
    shadowed the real baton). Best-effort, idempotent; skips a field that is empty or present."""
    try:
        with open(path) as f:
            txt = f.read()
    except Exception:
        return
    m = re.match(r'^(---\s*\n.*?\n)(---\s*\n)', txt, re.DOTALL)  # first frontmatter block
    if not m:
        return
    ins = ""
    if sid and not re.search(r'(?im)^session:\s', m.group(1)):
        ins += f"session: {sid}\n"
    if cwd and not re.search(r'(?im)^cwd:\s', m.group(1)):
        ins += f"cwd: {cwd}\n"
    if not ins:
        return
    new = txt[:m.end(1)] + ins + txt[m.end(1):]
    try:
        with open(path, "w") as f:
            f.write(new)
    except Exception:
        pass


def _derive_transcript(sid):
    """Locate the .jsonl transcript for a session id under ~/.claude/projects. '' if absent."""
    import glob
    if not sid:
        return ""
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl"))
    return hits[0] if hits else ""


def _newest_transcript(cwd):
    """Newest .jsonl in the Claude Code project dir for cwd (CC munges [/.] → '-'), so a bare
    `gaius baton pass` from a shell finds the most recently active session here. '' if none."""
    proj = os.path.expanduser("~/.claude/projects/") + re.sub(r"[/.]", "-", cwd)
    try:
        files = [os.path.join(proj, f) for f in os.listdir(proj) if f.endswith(".jsonl")]
        return max(files, key=os.path.getmtime) if files else ""
    except Exception:
        return ""


def _resolve_skill(transcript, tail_bytes=48000):
    """Best-effort skill from the transcript tail: last attributionSkill, else the last
    /<command-name>. Mirrors gaius-baton-watch:_resolve_context (the canonical copy). ''
    when unresolvable — the caller falls back to 'session'."""
    tail = _read_tail(transcript, tail_bytes) if transcript else ""
    if not tail:
        return ""
    m = re.findall(r'"attributionSkill"\s*:\s*"([\w:-]+)"', tail)
    if m:
        return m[-1].split(":")[-1]
    m = re.findall(r'<command-name>/?([\w-]+)</command-name>', tail)
    return m[-1] if m else ""


def _handoff_exe():
    return (shutil.which("gaius-session-handoff")
            or os.path.expanduser("~/.local/bin/gaius-session-handoff"))


def _persist(skill, sid, severity, body, next_steps=""):
    """Pipe the body to gaius-session-handoff; return the written path or ''.

    Mirrors concord._write_handoff: argv list, body on stdin, best-effort, recover the path
    from the writer's `Handoff written: <path>` stdout line.
    """
    exe = _handoff_exe()
    if not os.path.exists(exe):
        return ""
    cmd = [exe, "--skill", skill or "session", "--session-id", sid or "",
           "--severity", severity or "urgent", "--replace"]
    if next_steps and not body:
        cmd += ["--next", next_steps]
    try:
        r = subprocess.run(cmd, input=body or "", capture_output=True, text=True, timeout=15)
        for line in r.stdout.splitlines():
            if line.startswith("Handoff written:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""


_TOMBSTONE = """\
gaius baton is retired (2026-08-18). It is not an alias for spin.

Use:  gaius spin --skill <name>
      cat <<'EOF' | gaius spin --skill <name>
      ...updated body...
      EOF
      gaius spin --skill <name> --transcript <jsonl>   # PreCompact / headless persist
Spec: specs/context-spin.md (in the memory repo, not shipped)

Why not aliased: `gaius baton` / `baton pass` writes a NEW handoff then
prunes existing[3:], and `pass` hot-stamps (Claude SessionStart only).
Spin reuses or replaces the newest same-skill file — no prune, no hot-stamp.

gaius-session-handoff remains the Gap-47/attest writer. Do not call it
as the HITL verb. gaius concord handoff stays for claim→pool transfer.
"""


def cmd_baton(args):
    # args ignored — every subcommand (pass/…) is the same dead end.
    # Authoring helpers above stay importable (spin --transcript, tests).
    del args
    sys.stderr.write(_TOMBSTONE)
    return 2

