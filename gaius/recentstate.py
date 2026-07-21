"""gaius recent-roll — evict aged, done, pointered ``## Recent State`` bullets from
the always-injected MEMORY.md into a non-injected archive changelog.

CONSERVATIVE by design. A bullet is evicted ONLY when ALL THREE hold:

  (1) its newest date-stamp is older than ``--max-age-days`` (measured relative
      to the ``## Recent State (YYYY-MM-DD)`` section-header date, with the
      Dec→Jan year boundary handled),
  (2) it carries a done-marker (``✅`` or a whole-word
      ``LIVE|FIXED|RESOLVED|MERGED|DONE|SHIPPED``), and
  (3) it already ends in a pointer (``→ <file>`` or a ``[label](path)`` link) —
      i.e. the content has a durable home elsewhere.

A bullet containing ``⚠️`` (U+26A0) is NEVER evicted (veto), even if 1-3 pass.
A bullet with no trailing pointer is NEVER evicted (no home = losing the fact).
Evicted lines land VERBATIM (appended) in ``archive/recent-state-YYYY-MM.md``.

Safety of the archive location: the gaius-inject memory-file scan (landscape.py)
globs ONLY the whitelist ``feedback/domain/project/user/reference`` subdirs, and
Claude Code native memory injects ONLY ``MEMORY.md``. A new ``archive/`` subdir is
in neither path, so archived facts never re-enter session context. (Verified
2026-07-21 against gaius/landscape.py `_MEMORY_DIRS`.)
"""

import argparse
import datetime as _dt
import os
import re
import sys
import tempfile
from pathlib import Path

try:  # facade — MEMORY_DIR is resolved once in _core
    from gaius._core import MEMORY_DIR
except Exception:  # pragma: no cover - standalone / test import
    MEMORY_DIR = None


# ── predicates ───────────────────────────────────────────────────────────────

VETO_MARK = "⚠"  # ⚠ (matches ⚠️ with or without the VS16 variation selector)

# case-SENSITIVE uppercase tokens — prose "live state" must not count as a marker
_DONE_RE = re.compile(r"✅|\b(?:LIVE|FIXED|RESOLVED|MERGED|DONE|SHIPPED)\b")

_SECTION_HEADER_RE = re.compile(r"^##\s+Recent State\s*\((\d{4})-(\d{2})-(\d{2})\)")
_FULL_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_SHORT_DATE_RE = re.compile(r"\b(\d{2})-(\d{2})\b")

_MD_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\)")
# A pointer TARGET is a markdown link, a `backtick-path`, or a bareword that
# CONTAINS a "." or "/" (a real filename/path). A plain word like "svc" is NOT a
# target — this guards against inline "A→B" transformation arrows mid-bullet.
_PTR_TARGET = r"(?:\[[^\]]+\]\([^)]+\)|`[^`]+`|[\w#@-]*[./][\w./#@-]*)"
# A trailing pointer: the last "→" (or "->") whose target phrase reaches EOL.
_TRAILING_PTR_RE = re.compile(
    r"(?:→|->)\s*"                                      # arrow
    r"(?:archive\s*\+\s*)?"                                  # optional "archive + "
    + _PTR_TARGET
    + r"(?:\s*(?:[,+]|and)\s*" + _PTR_TARGET + r")*"         # more targets, sep , + and
    r"\s*[.)]*\s*$"                                          # to EOL (allow trailing . or ))
)
_ENDS_WITH_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\)[.)]*\s*$")


def _has_veto(text: str) -> bool:
    return VETO_MARK in text


def _has_done_marker(text: str) -> bool:
    return bool(_DONE_RE.search(text))


def _has_trailing_pointer(text: str) -> bool:
    """True iff the bullet ENDS in a durable pointer (→ <path> or a [..](..) link).

    Strict / conservative: an inline transformation arrow ("LINSTOR→tailscale")
    mid-bullet is NOT a pointer; only an arrow (or link) whose target phrase runs
    to end-of-line counts. False negatives are safe (they KEEP the bullet)."""
    t = text.rstrip()
    if _ENDS_WITH_LINK_RE.search(t):
        return True
    return bool(_TRAILING_PTR_RE.search(t))


def _section_date(header_lines) -> "_dt.date | None":
    for ln in header_lines:
        m = _SECTION_HEADER_RE.match(ln)
        if m:
            y, mo, d = map(int, m.groups())
            try:
                return _dt.date(y, mo, d)
            except ValueError:
                return None
    return None


def _bullet_date(text: str, section_date) -> "_dt.date | None":
    """The NEWEST date-stamp in the bullet. Full (YYYY-MM-DD) stamps use their own
    year; bare MM-DD stamps infer the year from ``section_date`` (a month LATER
    than the header month = the prior year → Dec→Jan boundary). Returning the max
    means a spurious MM-DD can only ever make a bullet look YOUNGER (keep), never
    older — so date false-positives never cause a wrongful eviction."""
    if section_date is None:
        return None
    cands = []
    for m in _FULL_DATE_RE.finditer(text):
        y, mo, d = map(int, m.groups())
        try:
            cands.append(_dt.date(y, mo, d))
        except ValueError:
            pass
    # strip full YYYY-MM-DD spans so their MM-DD tail isn't re-counted as a short stamp
    stripped = _FULL_DATE_RE.sub(" ", text)
    for m in _SHORT_DATE_RE.finditer(stripped):
        mo, d = int(m.group(1)), int(m.group(2))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        year = section_date.year
        if mo > section_date.month:
            year -= 1  # header is Jan, bullet is Dec → previous year
        try:
            cands.append(_dt.date(year, mo, d))
        except ValueError:
            pass
    return max(cands) if cands else None


def should_evict(text: str, section_date, max_age_days: int) -> bool:
    """The whole safety gate: ALL of (age>threshold, done-marker, trailing pointer)
    AND NOT veto. Any single failure → KEEP (the fact stays in MEMORY.md)."""
    if _has_veto(text):
        return False
    if not _has_trailing_pointer(text):
        return False
    if not _has_done_marker(text):
        return False
    if section_date is None:
        return False
    bd = _bullet_date(text, section_date)
    if bd is None:
        return False
    return (section_date - bd).days > max_age_days


# ── atomic MEMORY.md rewrite ────────────────────────────────────────────────

def _atomic_write(path: Path, content: str) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".recentroll-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)  # atomic on the same filesystem
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── core roll ────────────────────────────────────────────────────────────────

def roll_recent_state(mem_path, archive_dir, max_age_days: int = 7, dry_run: bool = False,
                      _probe=None):
    """Evict eligible ``## Recent State`` bullets from ``mem_path`` into
    ``archive_dir/recent-state-YYYY-MM.md`` (YYYY-MM = section-header month).

    Concurrency: MEMORY.md is the always-injected file, written by MANY actors that
    do NOT share a lock (Claude Code's Edit tool, PostToolUse hooks, peer sessions).
    ``os.replace`` is atomic but last-writer-wins, so a peer append landing between
    our start-of-run read and the replace would be silently clobbered. Guard: we
    snapshot the file at start, then re-read it IMMEDIATELY BEFORE any write and BAIL
    (write nothing — neither archive nor MEMORY.md) if it changed since the snapshot.
    A skipped roll is retried on the next run; a clobber loses a peer's fact. This
    shrinks — does not fully eliminate — the race: a residual window remains between
    the guard re-read and the archive-write+replace, bounded by that append + a full
    temp-file rewrite (so it scales with file size, not a fixed sub-ms), which the
    nightly (once/day, low contention) tolerates. Archive is written AFTER the guard passes
    and BEFORE the replace, so a crash mid-run still only leaves a harmless duplicate.

    ``_probe`` is a test seam: if given, it is called with ``mem_path`` right before
    the guard re-read, letting a test simulate a concurrent append deterministically.

    Returns a dict with ``evicted`` (verbatim lines), ``archive_path``,
    ``section_date`` and ``skipped_concurrent`` (True iff the guard bailed)."""
    mem_path = Path(mem_path)
    archive_dir = Path(archive_dir)
    text = mem_path.read_text(encoding="utf-8")  # re-read at start
    lines = text.splitlines(keepends=True)
    section_date = _section_date([l.rstrip("\n") for l in lines])

    start = None
    for i, l in enumerate(lines):
        if _SECTION_HEADER_RE.match(l.rstrip("\n")):
            start = i
            break

    evicted, kept = [], list(lines)
    if start is not None and section_date is not None:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("## "):
                end = j
                break
        out = lines[: start + 1]
        for j in range(start + 1, end):
            raw = lines[j]
            body = raw.rstrip("\n")
            if body.lstrip().startswith(("-", "*")) and should_evict(body, section_date, max_age_days):
                evicted.append(raw if raw.endswith("\n") else raw + "\n")
            else:
                out.append(raw)
        out.extend(lines[end:])
        kept = out

    if section_date is not None:
        archive_path = archive_dir / f"recent-state-{section_date:%Y-%m}.md"
    else:
        archive_path = archive_dir / "recent-state.md"

    skipped_concurrent = False
    if evicted and not dry_run:
        if _probe is not None:
            _probe(mem_path)  # test seam: simulate a concurrent append here
        # optimistic-concurrency guard: bail if a peer wrote MEMORY.md since our snapshot
        if mem_path.read_text(encoding="utf-8") != text:
            skipped_concurrent = True
        else:
            archive_dir.mkdir(parents=True, exist_ok=True)
            new_file = not archive_path.exists() or archive_path.stat().st_size == 0
            with archive_path.open("a", encoding="utf-8") as af:
                if new_file:
                    stamp = f"{section_date:%Y-%m}" if section_date else "undated"
                    af.write(f"# Recent State archive — {stamp}\n\n")
                af.writelines(evicted)          # VERBATIM
            _atomic_write(mem_path, "".join(kept))

    return {"evicted": evicted, "archive_path": archive_path,
            "section_date": section_date, "skipped_concurrent": skipped_concurrent}


# ── CLI ──────────────────────────────────────────────────────────────────────

def cmd_recent_roll(args):
    parser = argparse.ArgumentParser(prog="gaius recent-roll")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be evicted; write nothing")
    parser.add_argument("--max-age-days", type=int, default=7,
                        help="Evict eligible bullets whose newest date-stamp is older than "
                             "this, relative to the section-header date (default: 7)")
    parser.add_argument("--memory-file", default=None,
                        help="Path to MEMORY.md (default: <MEMORY_DIR>/MEMORY.md)")
    parser.add_argument("--archive-dir", default=None,
                        help="Archive directory (default: <MEMORY_DIR>/archive)")
    parsed = parser.parse_args(args)

    if parsed.memory_file:
        mem_path = Path(parsed.memory_file).expanduser()
    elif MEMORY_DIR is not None:
        mem_path = Path(MEMORY_DIR) / "MEMORY.md"
    else:
        mem_path = None
    if mem_path is None or not mem_path.is_file():
        print(f"[recent-roll] ERROR: MEMORY.md not found ({mem_path}); "
              "set --memory-file or GAIUS_MEMORY_DIR", file=sys.stderr)
        return 1

    if parsed.archive_dir:
        archive_dir = Path(parsed.archive_dir).expanduser()
    else:
        archive_dir = mem_path.parent / "archive"

    result = roll_recent_state(mem_path, archive_dir,
                               max_age_days=parsed.max_age_days,
                               dry_run=parsed.dry_run)
    if result.get("skipped_concurrent"):
        print("[recent-roll] SKIPPED: MEMORY.md changed under us (concurrent write) "
              "— wrote nothing; the next run retries.")
        return 0
    evicted = result["evicted"]
    if not evicted:
        print(f"[recent-roll] nothing to evict (0 bullets aged >{parsed.max_age_days}d "
              "+ done-marker + trailing-pointer + non-veto).")
        return 0
    verb = "would evict" if parsed.dry_run else "evicted"
    print(f"[recent-roll] {verb} {len(evicted)} bullet(s) -> {result['archive_path']}")
    for ln in evicted:
        print(f"   - {ln.lstrip()[:80].rstrip()}")
    return 0
