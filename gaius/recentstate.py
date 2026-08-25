"""gaius recent-roll — evict aged, done, pointered ``## Recent State`` bullets from
the always-injected MEMORY.md into a non-injected archive changelog.

The gate is REDUNDANCY, not age. A bullet is evicted iff ALL of:

  (1) it is not explicitly pinned with ``📌`` (``PIN_MARK``) — unless
      ``--ignore-pins`` / ``ignore_pins=True`` is set (operator override for
      ambient-glyph drift; homing and INDEX reachability still gate),
  (2) it ends in a trailing pointer (``→ <file>`` or a ``[label](path)`` link),
  (3) that pointer's target PROVABLY contains the bullet's signature tokens, and
  (4) every pointer target that lives in an indexed tree (``INDEX_TREES``) is
      listed in that tree's ``INDEX.md`` — Gap 60. Homing without an index
      path is how ``--ignore-pins`` stranded 14 ``troubleshooting/`` files
      on 2026-08-14: the archive is not injected, so an unlisted home is
      dark once the MEMORY.md bullet is gone.

🔴 **``⚠️`` is NOT a veto and has not been one since 2026-07-28 (mnemos #123).** It was
retired precisely because it is ambient on nearly every Recent-State bullet, which made
the roll inert. The ONLY veto is ``📌``. Do not assume a ⚠️/🔴 bullet is protected.

🔴 **Age and done-markers are NOT consulted.** ``--max-age-days`` and ``section_date``
are retained for call-site compatibility only (see ``should_evict``). A bullet describing
work that is OPEN, unmerged, or operator-gated is fully eligible the day it is written —
being *homed* is the whole test, and "homed" says nothing about "finished". If an open
item must stay in the injected index, pin it with ``📌`` or keep it in ``## Standing
Gates``; do not rely on its wording, its date, or its warning glyph.

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

# Explicit "never roll this bullet" marker. Retired ⚠ as the veto on 2026-07-28
# (mnemos #123): ⚠ is ambient on nearly every Recent-State bullet, so vetoing on it
# is what made the auto-roll evict nothing for weeks. See _has_pin().
PIN_MARK = "📌"

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


# ── homing-verification guard ─────────────────────────────────────────────────
# A bullet may pass age+done+pointer+non-veto yet still be UNSAFE to evict when its
# trailing "→ pointer" names a home file that does NOT actually contain the fact
# (e.g. a live "deploy-target=github" bullet pointing at a file that never absorbed
# it). Evicting such a bullet drops the fact from the always-injected MEMORY.md into
# a non-injected archive — silent loss. This guard re-reads the pointer target and
# refuses eviction unless the home genuinely contains the bullet's distinctive
# content. CONSERVATIVE: anything we cannot positively verify → KEEP.

HOME_MATCH_FRACTION = 0.5
# Raised 1 → 2 on 2026-07-28 (mnemos #123) when homing became the SOLE safety gate.
# A single shared token (one PR number, one filename) is too weak to authorise an
# automatic delete on its own; two independent signature hits is the floor.
MIN_HOME_HITS = 2

_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_BOLD_SPAN_RE = re.compile(r"\*\*([^*]+)\*\*")
_COMPOUND_RE = re.compile(r"\b\w+[-_/][\w./#@-]+\b")
_REF_RES = (
    re.compile(r"#\d+"),
    re.compile(r"\bpr\s*#?\d+", re.IGNORECASE),
    re.compile(r"\b[0-9a-f]{7,40}\b"),
)
_SECTION_SUFFIX_RE = re.compile(r"§.*$")
_MD_LINK_TARGET_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

# Generic done-marker / filler words: a bare "LIVE"/"SHIPPED" in prose is NOT a
# distinctive signature (the done-marker gate already keyed on it), so it must not
# count toward homing — otherwise every done bullet would trivially "home" on any
# file that merely mentions the word.
_SIG_STOPWORDS = frozenset({
    "the", "and", "this", "that", "from", "with", "into",
    "live", "done", "fixed", "merged", "shipped", "resolved", "verified", "fallback",
})


def _norm_sig_token(tok: str) -> str:
    return tok.strip().strip("`*_()[]{}<>\"'.,;:!?§ ").lower()


def _signature_tokens(text: str) -> set:
    """Distinctive lowercased tokens that identify a bullet's fact: `code` spans,
    **bold** words, compound identifiers (deploy-target, project/foo.md, ao#114) and
    refs (#123, PR #45, hex commits). Generic short/stopword tokens are dropped so a
    stray 'LIVE' in prose can never be a signature."""
    raw = []
    for m in _CODE_SPAN_RE.finditer(text):
        raw.append(m.group(1))
    for m in _BOLD_SPAN_RE.finditer(text):
        raw.extend(m.group(1).split())
    for m in _COMPOUND_RE.finditer(text):
        raw.append(m.group(0))
    for rx in _REF_RES:
        for m in rx.finditer(text):
            raw.append(m.group(0))
    sig = set()
    for tok in raw:
        t = _norm_sig_token(tok)
        if len(t) < 4:
            continue
        if t in _SIG_STOPWORDS:
            continue
        sig.add(t)
    return sig


def _pointer_target_paths(text: str) -> list:
    """Candidate relative file paths named in the TRAILING pointer region (after the
    final →/->): markdown-link targets, backtick paths, and bare path-like tokens.
    Section suffixes (``§...``) and surrounding punctuation are stripped."""
    t = text.rstrip()
    pos, alen = -1, 0
    for arrow in ("→", "->"):
        p = t.rfind(arrow)
        if p > pos:
            pos, alen = p, len(arrow)
    if pos == -1:
        return []
    region = t[pos + alen:]

    candidates = []
    for p in _MD_LINK_TARGET_RE.findall(region):
        candidates.append(p)
    for p in _CODE_SPAN_RE.findall(region):
        if "/" in p or p.endswith(".md"):
            candidates.append(p)
    bare = _MD_LINK_TARGET_RE.sub(" ", region)
    bare = _CODE_SPAN_RE.sub(" ", bare)
    for tok in re.split(r"[\s,+]+", bare):
        tok = _SECTION_SUFFIX_RE.sub("", tok).strip("()[]{}<>\"'.,;:!?§ ")
        if tok and ("/" in tok or tok.endswith(".md")):
            candidates.append(tok)

    out, seen = [], set()
    for p in candidates:
        p = _SECTION_SUFFIX_RE.sub("", p).strip().strip("()[]{}<>\"'.,;:!?§ ")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _resolve_home_text(text: str, memory_dir) -> str:
    """Concatenated, LOWERCASED contents of every pointer-target file that resolves
    (relative to ``memory_dir``) and exists. ``''`` if none resolve/exist."""
    memory_dir = Path(memory_dir)
    parts = []
    for rel in _pointer_target_paths(text):
        try:
            fp = memory_dir / rel
            if fp.is_file():
                parts.append(fp.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            pass
    return "\n".join(parts).lower()


def _is_homed(text: str, home_text: str) -> bool:
    """True iff the bullet's distinctive signature is genuinely present in its home
    file(s). No signature (can't verify) or no home text → False → KEEP."""
    sig = _signature_tokens(text)
    if not sig:
        return False
    if not home_text:
        return False
    hits = sum(1 for tok in sig if tok in home_text)  # home_text already lowercased
    return hits >= MIN_HOME_HITS and (hits / len(sig)) >= HOME_MATCH_FRACTION


# ── INDEX reachability (Gap 60) ──────────────────────────────────────────────
# Must match mnemosyne.INDEX_TREES + _LISTED_LEFT. Pinned by
# test_index_trees_match_mnemosyne + test_listing_agrees_with_scan.
# Two copies because mnemosyne is a standalone script (no gaius package import)
# and a deleter must not load a 1k-line health tool to refuse a write.
INDEX_TREES = (
    ("feedback",        "INDEX.md", "key",  "feedback_"),
    ("project",         "INDEX.md", "link", ""),
    ("troubleshooting", "INDEX.md", "link", ""),
)
_LISTED_LEFT = r"(?<![A-Za-z0-9])"


def index_lists(memory_dir, rel_path):
    """Is ``rel_path`` listed in its tree INDEX?

    ``True``  — tree has a readable INDEX and the file is listed.
    ``False`` — tree has a readable INDEX and the file is unlisted, OR the
                INDEX exists but cannot be read (deleter must KEEP).
    ``None``  — check does not apply: not an INDEX_TREE path, or the tree
                has no INDEX.md (opted out — same skip as scan_index_completeness).

    Listing uses the same matcher as ``scan_index_completeness@mnemosyne``
    (link mode = exact filename; key mode = stem minus prefix; ``_LISTED_LEFT``
    not ``\\b``). Membership, not transitive reachability — J 2026-08-07:
    an INDEX is an index.
    """
    rel = str(rel_path).replace("\\", "/").lstrip("./")
    rel = rel.split("#", 1)[0].split("?", 1)[0]
    parts = [p for p in rel.split("/") if p]
    if len(parts) < 2:
        return None
    tree, name = parts[0], parts[-1]
    spec = next((t for t in INDEX_TREES if t[0] == tree), None)
    if spec is None:
        return None
    _, index_name, mode, prefix = spec
    idxp = Path(memory_dir) / tree / index_name
    if not idxp.is_file():
        return None
    try:
        idx = idxp.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if mode == "link":
        return bool(re.search(_LISTED_LEFT + re.escape(name), idx))
    stem = Path(name).stem
    key = stem[len(prefix):] if prefix and stem.startswith(prefix) else stem
    return bool(re.search(_LISTED_LEFT + re.escape(key) + r"\b", idx))


def _targets_index_reachable(text: str, memory_dir) -> bool:
    """True iff no pointer target would be stranded by eviction.

    A target outside INDEX_TREES, or in a tree with no INDEX.md, is N/A.
    ANY indexed-tree target that is unlisted (or whose INDEX is unreadable)
    → False → KEEP. No extracted targets → False (fail-closed; the pointer
    gate should have already stopped us)."""
    paths = _pointer_target_paths(text)
    if not paths:
        return False
    for rel in paths:
        listed = index_lists(memory_dir, rel)
        if listed is False:
            return False
    return True


def _has_veto(text: str) -> bool:
    return VETO_MARK in text


def _has_pin(text: str) -> bool:
    """An EXPLICIT operator pin — this bullet never rolls, regardless of homing.

    Deliberately NOT ``⚠``. The ambient warning mark appears on most Recent-State
    bullets by construction, which is exactly why using it as the veto made the roll
    inert (2026-07-28). A pin has to be a decision, not a side effect of tone, so it
    is a marker nobody types by accident."""
    return PIN_MARK in text or "<!--pin-->" in text


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


def should_evict(text: str, section_date=None, max_age_days: int = 0,
                 home_text_provider=None, ignore_pins: bool = False,
                 index_reach_provider=None) -> bool:
    """Evict iff the bullet is PROVABLY REDUNDANT: trailing pointer, home contains
    the signature, AND (when a provider is passed) every indexed-tree target is
    listed in its INDEX. Nothing else.

    REDESIGNED 2026-07-28 (mnemos #123). The previous gate made four PROXIES mandatory —
    non-veto (any ``⚠`` pinned the bullet), a done-marker, a per-bullet date, and
    age > N days — and left the one real safety property, homing, an OPTIONAL kwarg that
    defaulted to a documented no-op. Recent-State bullets essentially never satisfy the
    proxies by construction, so the roll was inert: measured against the live MEMORY.md
    the proxy gate evicted **0 of 18** bullets while this gate evicts **7 (2,206 B)**.
    🔑 The proxies were proxies FOR homing; homing is the ground truth, so it is now the
    gate. A bullet whose fact is provably present in its pointer target loses nothing by
    leaving the index — that is necessary, not sufficient.

    Gap 60 (2026-08-14): homing does not imply the home is *findable*. ``--ignore-pins``
    evicted 31 bullets whose ``troubleshooting/`` targets were signature-homed but
    unlisted in ``INDEX.md``; the archive is not injected, so those files went dark.
    ``index_reach_provider`` is that check. ``None`` skips it (homing-only unit tests);
    ``roll_recent_state`` always passes a real provider when ``verify_homing`` is True.

    ``section_date`` / ``max_age_days`` are retained for call-site compatibility and are
    deliberately NOT consulted.

    FAIL-CLOSED at every step — pinned (unless ``ignore_pins``), no pointer, no provider,
    unreadable home, a signature we cannot positively match, or an unlisted INDEX-tree
    target → KEEP the bullet.

    ``ignore_pins`` is the operator override for ambient 📌 drift (08-14: 55/55 Recent
    State bullets carried the glyph as convention, which made the veto inert the same
    way ⚠️ did). Default False. Homing and INDEX reachability still gate; this only
    skips ``_has_pin``.

    ⚠️ ``home_text_provider=None`` now means "cannot verify ⇒ KEEP", the INVERSE of the
    old contract where it meant "skip the check". A caller that omits it evicts nothing,
    which is the safe direction for an automated deleter."""
    if not ignore_pins and _has_pin(text):
        return False
    if not _has_trailing_pointer(text):
        return False
    if home_text_provider is None:
        return False
    if not _is_homed(text, home_text_provider(text)):
        return False
    if index_reach_provider is not None and not index_reach_provider(text):
        return False
    return True


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
                      verify_homing: bool = True, ignore_pins: bool = False, _probe=None):
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
        mem_root = mem_path.parent
        provider = (lambda body: _resolve_home_text(body, mem_root)) if verify_homing else None
        # Gap 60: same verify_homing flag — if we cannot check homes we cannot
        # check INDEX either. A missing INDEX.md is N/A (opt-out); an existing
        # INDEX that does not list the target is a KEEP.
        index_prov = (
            (lambda body, root=mem_root: _targets_index_reachable(body, root))
            if verify_homing else None
        )
        for j in range(start + 1, end):
            raw = lines[j]
            body = raw.rstrip("\n")
            if body.lstrip().startswith(("-", "*")) and should_evict(
                    body, section_date, max_age_days, home_text_provider=provider,
                    ignore_pins=ignore_pins, index_reach_provider=index_prov):
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
    parser.add_argument("--ignore-pins", action="store_true",
                        help="Override the 📌 / <!--pin--> veto. Homing and INDEX "
                             "reachability still gate. Use only when the pin glyph "
                             "is ambient convention drift, not a deliberate keep. "
                             "Dry-run first.")
    parser.add_argument("--max-age-days", type=int, default=7,
                        help="Retained for call-site compatibility. NOT consulted — "
                             "homing is the gate. The default (7) is unused.")
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
                               dry_run=parsed.dry_run,
                               verify_homing=True,
                               ignore_pins=parsed.ignore_pins)
    if result.get("skipped_concurrent"):
        print("[recent-roll] SKIPPED: MEMORY.md changed under us (concurrent write) "
              "— wrote nothing; the next run retries.")
        return 0
    evicted = result["evicted"]
    if not evicted:
        pin_note = (" (📌 veto still on; pass --ignore-pins to override)"
                    if not parsed.ignore_pins else "")
        print("[recent-roll] nothing to evict — no homed, pointered bullet "
              f"survived the gate{pin_note}.")
        return 0
    verb = "would evict" if parsed.dry_run else "evicted"
    print(f"[recent-roll] {verb} {len(evicted)} bullet(s) -> {result['archive_path']}")
    for ln in evicted:
        print(f"   - {ln.lstrip()[:80].rstrip()}")
    return 0
