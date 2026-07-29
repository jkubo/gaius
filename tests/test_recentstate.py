"""recent-roll test suite — the ## Recent State auto-roll safety gate.

Pure-function / tmp_path only; no facts.db, no live MEMORY_DIR.

The gate was REDESIGNED 2026-07-28 (mnemos #123). It is now a single safety
property — PROVABLE REDUNDANCY — expressed in four fail-closed steps:

  (1) an explicit ``📌`` / ``<!--pin-->`` pin → KEEP (⚠️ is NO LONGER a veto),
  (2) no trailing pointer (``→ <file>`` / ``[label](path)``) → KEEP,
  (3) ``home_text_provider is None`` → KEEP — "cannot verify ⇒ keep", the
      INVERSE of the old contract where omitting it skipped the check,
  (4) otherwise evict iff the pointer target genuinely contains the bullet's
      signature (``MIN_HOME_HITS`` = 2 tokens, ``HOME_MATCH_FRACTION`` = 0.5).

Age, the done-marker and the per-bullet date NO LONGER GATE ANYTHING;
``section_date`` / ``max_age_days`` are accepted for call-site compatibility and
deliberately not consulted. The retired behaviours are asserted RETIRED below
(``test_*_no_longer_blocks_when_homed``) so a silent revert fails the suite.

The helper predicates (``_has_trailing_pointer``, ``_has_done_marker``,
``_bullet_date``, ``_section_date``) still exist and are still tested —
``_section_date`` in particular still drives the archive filename.
"""
import datetime as dt
from pathlib import Path

import pytest

from gaius.recentstate import (
    MIN_HOME_HITS,
    should_evict,
    roll_recent_state,
    _has_trailing_pointer,
    _has_done_marker,
    _has_veto,
    _has_pin,
    _bullet_date,
    _section_date,
)

SEC = dt.date(2026, 7, 20)   # section header date; passed through, never consulted

# The canonical roll-off bullet: a trailing pointer plus two unmistakable
# code-span/compound signature tokens, so the 2-hit homing floor is comfortably met.
EVICTABLE = "- **A**: `drbd-socket-guard` armed on `flannel-mtu`. → project/a.md"
EVICTABLE_SIG = ("drbd-socket-guard", "flannel-mtu")


# ── fixture helpers ──────────────────────────────────────────────────────────

def _homed(*tokens):
    """A ``home_text_provider`` returning a (lowercased) home whose text contains
    ``tokens`` — i.e. the pointer target really absorbed the fact."""
    body = ("# home\n\n" + " ".join(tokens) + "\n").lower()
    return lambda _text: body


def _unrelated(_text):
    """A home_text_provider whose target EXISTS but shares no signature."""
    return "# home\n\nunrelated notes about pvc provisioning and volumes.\n"


def _write_home(root, rel, *tokens):
    """Write the pointer-target file under ``root`` (== MEMORY.md's parent) with
    content carrying the bullet's signature tokens, so the real roll can resolve
    and verify it. The relative path itself is included because it is a signature
    token too (``project/a.md`` is a compound identifier)."""
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# home\n\n" + " ".join(tokens) + "\n" + rel + "\n", encoding="utf-8")
    return p


def _write_unrelated_home(root, rel):
    """Write the pointer target with content that does NOT contain the signature."""
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# home\n\nUnrelated notes about storage volumes and PVC provisioning.\n",
                 encoding="utf-8")
    return p


# ─────────────────────────────────────────────────────────────────────────────
# The gate, step by step
# ─────────────────────────────────────────────────────────────────────────────

class TestShouldEvictGate:
    def test_homed_pointered_bullet_evicts(self):
        assert should_evict(EVICTABLE, SEC, 7,
                            home_text_provider=_homed(*EVICTABLE_SIG)) is True

    def test_no_pointer_keeps(self):
        # homed, but NO trailing pointer (no durable home named) → KEEP
        b = "- **E**: `drbd-socket-guard` and `flannel-mtu` rewritten, no home left."
        assert should_evict(b, SEC, 7,
                            home_text_provider=_homed(*EVICTABLE_SIG)) is False

    def test_no_home_text_provider_keeps(self):
        """⚠️ THE INVERTED CONTRACT. ``home_text_provider=None`` used to mean
        "skip the homing check" (evict on the proxies alone); it now means
        "cannot verify ⇒ KEEP". A caller that omits it must evict NOTHING."""
        assert should_evict(EVICTABLE, SEC, 7) is False
        assert should_evict(EVICTABLE, SEC, 7, home_text_provider=None) is False

    def test_pointer_target_without_signature_keeps(self):
        # the home file exists and is readable, but does not contain the fact → KEEP
        assert should_evict(EVICTABLE, SEC, 7, home_text_provider=_unrelated) is False

    def test_empty_home_text_keeps(self):
        # target resolved to nothing (missing/unreadable file) → KEEP
        assert should_evict(EVICTABLE, SEC, 7, home_text_provider=lambda _t: "") is False

    def test_pin_mark_keeps_even_when_homed(self):
        b = "- **F**: 📌 `drbd-socket-guard` pins `flannel-mtu`. → project/f.md"
        assert should_evict(b, SEC, 7, home_text_provider=_homed(*EVICTABLE_SIG)) is False
        # sanity: drop the pin and the very same bullet evicts (pin is the only blocker)
        assert should_evict(b.replace("📌 ", ""), SEC, 7,
                            home_text_provider=_homed(*EVICTABLE_SIG)) is True

    def test_html_comment_pin_keeps_even_when_homed(self):
        b = "- **F**: <!--pin--> `drbd-socket-guard` pins `flannel-mtu`. → project/f.md"
        assert should_evict(b, SEC, 7, home_text_provider=_homed(*EVICTABLE_SIG)) is False

    def test_markdown_link_pointer_evicts(self):
        b = "- **H**: `dirty-clone` covers `kernel-lsm` now [see](project/h.md)"
        assert should_evict(b, SEC, 7,
                            home_text_provider=_homed("dirty-clone", "kernel-lsm",
                                                      "project/h.md")) is True

    def test_inline_transformation_arrow_is_not_a_pointer(self):
        # "A→B" mid-bullet names no home file → KEEP even though fully homed
        b = "- **G**: migrated `linstor-drbd`→tailscale, `flannel-mtu` retuned."
        assert should_evict(b, SEC, 7,
                            home_text_provider=_homed("linstor-drbd", "flannel-mtu")) is False


# ─────────────────────────────────────────────────────────────────────────────
# Retired semantics — these MUST NOT come back. Each of these bullets was KEPT
# by the old gate and is deliberately EVICTED by the new one.
# ─────────────────────────────────────────────────────────────────────────────

class TestRetiredGateConditions:
    def test_veto_mark_no_longer_blocks_when_homed(self):
        """⚠️ was the old veto. It is ambient on nearly every Recent-State bullet,
        which is precisely why the roll evicted nothing for weeks. Only 📌 pins now."""
        b = "- **F**: ⚠️ `drbd-socket-guard` armed on `flannel-mtu`. → project/f.md"
        assert _has_veto(b)                                    # the glyph IS still there
        assert should_evict(b, SEC, 7,
                            home_text_provider=_homed(*EVICTABLE_SIG)) is True

    def test_missing_done_marker_no_longer_blocks_when_homed(self):
        """No ✅ / LIVE / RESOLVED / MERGED anywhere, and no date-stamp either —
        the old gate KEPT this; homing is the only ground truth now."""
        b = "- **D**: `drbd-socket-guard` bound to `flannel-mtu`. → project/d.md"
        assert not _has_done_marker(b)
        assert _bullet_date(b, SEC) is None
        assert should_evict(b, SEC, 7,
                            home_text_provider=_homed(*EVICTABLE_SIG)) is True

    def test_recent_bullet_no_longer_blocked_by_age(self):
        # dated the same day as the header (age 0) — the old gate needed age > 7
        b = "- **B** (2026-07-20): `drbd-socket-guard` on `flannel-mtu`. → project/b.md"
        assert should_evict(b, SEC, 7,
                            home_text_provider=_homed(*EVICTABLE_SIG, "2026-07-20")) is True

    def test_missing_section_date_no_longer_blocks(self):
        # section_date=None used to be an automatic KEEP; it is no longer consulted
        assert should_evict(EVICTABLE, None, 7,
                            home_text_provider=_homed(*EVICTABLE_SIG)) is True

    def test_max_age_days_is_not_consulted(self):
        # any max_age_days gives the same verdict — the kwarg is call-site compat only
        prov = _homed(*EVICTABLE_SIG)
        assert should_evict(EVICTABLE, SEC, 0, home_text_provider=prov) is True
        assert should_evict(EVICTABLE, SEC, 99999, home_text_provider=prov) is True


# ─────────────────────────────────────────────────────────────────────────────
# MIN_HOME_HITS = 2 — one shared token is too weak to authorise a delete
# ─────────────────────────────────────────────────────────────────────────────

class TestHomeHitFloor:
    def test_floor_is_two(self):
        assert MIN_HOME_HITS == 2

    def test_single_signature_hit_keeps(self):
        # sig = {alpha-widget, project/x.md}; home carries only ONE of them.
        # fraction = 1/2 = 0.5 PASSES HOME_MATCH_FRACTION, so this isolates the
        # raised hit floor: 1 hit < MIN_HOME_HITS → KEEP.
        b = "- **X**: `alpha-widget` rolled out. → project/x.md"
        assert should_evict(b, SEC, 7, home_text_provider=_homed("alpha-widget")) is False

    def test_second_signature_hit_flips_to_evict(self):
        b = "- **X**: `alpha-widget` rolled out. → project/x.md"
        assert should_evict(b, SEC, 7,
                            home_text_provider=_homed("alpha-widget",
                                                      "project/x.md")) is True


# ─────────────────────────────────────────────────────────────────────────────
# helper predicates (still exported, still used — _section_date names the archive)
# ─────────────────────────────────────────────────────────────────────────────

class TestPredicates:
    def test_trailing_arrow_pointer(self):
        assert _has_trailing_pointer("text → project/x.md")
        assert _has_trailing_pointer("text → `gotchas.md`")
        assert _has_trailing_pointer("text → archive + project/x.md")
        assert _has_trailing_pointer("text → CLAUDE.md.")

    def test_trailing_markdown_link(self):
        assert _has_trailing_pointer("text [cd](project/cd.md)")
        assert _has_trailing_pointer("text → [seo](project/seo.md), [rev](project/rev.md)")

    def test_inline_arrow_is_not_trailing_pointer(self):
        assert not _has_trailing_pointer("migrated LINSTOR→tailscale, done.")
        assert not _has_trailing_pointer("OLLAMA_URL→no svc; all escalate GChat.")

    def test_no_pointer_at_all(self):
        assert not _has_trailing_pointer("just a plain sentence with no home.")

    def test_done_markers(self):
        assert _has_done_marker("shipped ✅ today")
        for tok in ("LIVE", "FIXED", "RESOLVED", "MERGED", "DONE", "SHIPPED"):
            assert _has_done_marker(f"status {tok} now")
        assert not _has_done_marker("live state of the system")   # lowercase prose

    def test_veto_detects_warning_glyph(self):
        # the predicate still exists (and still detects); it just no longer GATES.
        assert _has_veto("something ⚠️ careful")
        assert _has_veto("something ⚠ careful")   # bare glyph, no VS16
        assert not _has_veto("no warning here")

    def test_pin_detects_only_explicit_markers(self):
        assert _has_pin("hold this 📌")
        assert _has_pin("hold this <!--pin-->")
        assert not _has_pin("something ⚠️ careful")   # ambient warning is NOT a pin
        assert not _has_pin("nothing special here")

    def test_section_date_parse(self):
        assert _section_date(["## Recent State (2026-07-20)"]) == dt.date(2026, 7, 20)
        assert _section_date(["no header here"]) is None

    def test_full_date_year_used_directly(self):
        assert _bullet_date("thing (2026-05-01) old", SEC) == dt.date(2026, 5, 1)

    def test_short_date_infers_section_year(self):
        assert _bullet_date("thing (07-01) old", SEC) == dt.date(2026, 7, 1)

    def test_newest_stamp_governs(self):
        # a bullet with an old ref but touched recently is governed by the newest
        assert _bullet_date("from (07-01) updated (07-19)", SEC) == dt.date(2026, 7, 19)

    def test_dec_jan_boundary_infers_prior_year(self):
        jan = dt.date(2026, 1, 5)
        # bullet month (12) > header month (1) → prior year
        assert _bullet_date("thing (12-20) shipped", jan) == dt.date(2025, 12, 20)


# ─────────────────────────────────────────────────────────────────────────────
# end-to-end roll: verbatim archive landing + MEMORY.md mutation
# ─────────────────────────────────────────────────────────────────────────────

def _doc(*recent_bullets):
    return (
        "# MEMORY\n\n"
        "## Project Files\n\n- **P**: [x](project/p.md) terse index line\n\n"
        "## Recent State (2026-07-20)\n\n" + "\n".join(recent_bullets) + "\n"
    )


class TestRollEndToEnd:
    # one evictable bullet + one KEEP per surviving fail-closed step
    KEEP_NO_POINTER = "- **D**: `etcd-quota` raised to 2GiB, no home left."
    KEEP_PINNED = "- **E**: 📌 `drbd-socket-guard` and `flannel-mtu` pinned. → project/e.md"
    KEEP_UNHOMED = "- **C**: `loki-compactor` stalled on `seaweedfs-s3`. → project/c.md"
    KEEP_MISSING_HOME = "- **B**: `mullvad-exit` flapped on `apex-vm`. → project/missing.md"

    def _bullets(self):
        return [EVICTABLE, self.KEEP_MISSING_HOME, self.KEEP_UNHOMED,
                self.KEEP_NO_POINTER, self.KEEP_PINNED]

    def _seed(self, tmp_path, *bullets):
        """Write MEMORY.md plus the home files the bullets point at."""
        mem = tmp_path / "MEMORY.md"
        mem.write_text(_doc(*(bullets or self._bullets())))
        _write_home(tmp_path, "project/a.md", *EVICTABLE_SIG)
        _write_home(tmp_path, "project/e.md", *EVICTABLE_SIG)   # homed, but PINNED
        _write_unrelated_home(tmp_path, "project/c.md")          # exists, no signature
        return mem

    def test_only_evictable_bullet_removed(self, tmp_path):
        mem = self._seed(tmp_path)
        res = roll_recent_state(mem, tmp_path / "archive", max_age_days=7)
        assert len(res["evicted"]) == 1

        after = mem.read_text()
        assert "**A**" not in after                 # evicted bullet gone
        for keep in ("**B**", "**C**", "**D**", "**E**", "## Project Files"):
            assert keep in after                    # everything else survived

    def test_evicted_line_lands_verbatim(self, tmp_path):
        mem = self._seed(tmp_path)
        res = roll_recent_state(mem, tmp_path / "archive", max_age_days=7)
        arch_text = res["archive_path"].read_text()
        assert res["archive_path"].name == "recent-state-2026-07.md"
        # the evicted line is present byte-for-byte (verbatim, incl. trailing \n)
        assert res["evicted"][0] in arch_text
        assert res["evicted"][0].rstrip("\n") == EVICTABLE

    def test_archive_appends_across_runs(self, tmp_path):
        arch = tmp_path / "archive"
        _write_home(tmp_path, "project/a.md", *EVICTABLE_SIG)
        _write_home(tmp_path, "project/z.md", "zeta-rollout", "kube-proxy")
        m1 = tmp_path / "M1.md"
        m1.write_text(_doc(EVICTABLE))
        roll_recent_state(m1, arch, max_age_days=7)
        m2 = tmp_path / "M2.md"
        other = "- **Z**: `zeta-rollout` swapped `kube-proxy`. → project/z.md"
        m2.write_text(_doc(other))
        res2 = roll_recent_state(m2, arch, max_age_days=7)
        arch_text = res2["archive_path"].read_text()
        assert EVICTABLE in arch_text and other in arch_text   # both runs accreted

    def test_archive_bucket_named_by_section_header_month(self, tmp_path):
        # _section_date still drives the archive filename (independent of any date
        # inside the bullet, which the gate no longer reads at all).
        mem = tmp_path / "MEMORY.md"
        mem.write_text("# MEMORY\n\n## Recent State (2026-01-05)\n\n" + EVICTABLE + "\n")
        _write_home(tmp_path, "project/a.md", *EVICTABLE_SIG)
        res = roll_recent_state(mem, tmp_path / "archive", max_age_days=7)
        assert len(res["evicted"]) == 1
        assert res["archive_path"].name == "recent-state-2026-01.md"

    def test_nothing_evictable_leaves_files_untouched(self, tmp_path):
        mem = self._seed(tmp_path, self.KEEP_MISSING_HOME, self.KEEP_UNHOMED,
                         self.KEEP_NO_POINTER, self.KEEP_PINNED)
        content = mem.read_text()
        res = roll_recent_state(mem, tmp_path / "archive", max_age_days=7)
        assert res["evicted"] == []
        assert mem.read_text() == content               # byte-identical, no rewrite
        assert not res["archive_path"].exists()         # archive not created

    def test_verify_homing_false_evicts_nothing(self, tmp_path):
        """verify_homing=False passes home_text_provider=None → fail-closed KEEP.
        The default is now True (was False), so the plain call above DOES roll."""
        mem = self._seed(tmp_path, EVICTABLE)
        content = mem.read_text()
        res = roll_recent_state(mem, tmp_path / "archive", max_age_days=7,
                                verify_homing=False)
        assert res["evicted"] == []
        assert mem.read_text() == content


class TestDryRun:
    def test_dry_run_mutates_nothing(self, tmp_path):
        mem = tmp_path / "MEMORY.md"
        content = _doc(EVICTABLE, "- **B**: `mullvad-exit` flapped. → project/missing.md")
        mem.write_text(content)
        _write_home(tmp_path, "project/a.md", *EVICTABLE_SIG)
        arch = tmp_path / "archive"
        res = roll_recent_state(mem, arch, max_age_days=7, dry_run=True)
        assert len(res["evicted"]) == 1                 # reports what WOULD go
        assert mem.read_text() == content               # but MEMORY.md untouched
        assert not arch.exists()                        # and no archive written


class TestAtomicReread:
    def test_reads_file_fresh_each_call(self, tmp_path):
        # a peer append between two rolls must be seen by the second run
        mem = tmp_path / "MEMORY.md"
        mem.write_text(_doc("- **B**: `mullvad-exit` flapped. → project/missing.md"))
        _write_home(tmp_path, "project/a.md", *EVICTABLE_SIG)
        r1 = roll_recent_state(mem, tmp_path / "archive", max_age_days=7)
        assert r1["evicted"] == []
        # a live peer appends an evictable bullet to the section
        text = mem.read_text().rstrip("\n") + "\n" + EVICTABLE + "\n"
        mem.write_text(text)
        r2 = roll_recent_state(mem, tmp_path / "archive", max_age_days=7)
        assert len(r2["evicted"]) == 1                  # re-read caught the append


class TestConcurrencyGuard:
    """The optimistic-concurrency guard: if MEMORY.md changes between the
    start-of-run snapshot and the write, the roll must BAIL — write nothing,
    clobber no peer append. Simulated deterministically via the ``_probe`` seam."""

    def _seed(self, tmp_path):
        mem = tmp_path / "MEMORY.md"
        mem.write_text(_doc(EVICTABLE,
                            "- **B**: `mullvad-exit` flapped. → project/missing.md"))
        _write_home(tmp_path, "project/a.md", *EVICTABLE_SIG)
        return mem

    def test_concurrent_append_bails_and_preserves(self, tmp_path):
        mem = self._seed(tmp_path)
        arch = tmp_path / "archive"
        peer_line = "- **PEER**: landed mid-roll. → project/peer.md\n"

        def peer_append(p):
            # a live peer appends to MEMORY.md after our snapshot, before our write
            p.write_text(p.read_text().rstrip("\n") + "\n" + peer_line)

        res = roll_recent_state(mem, arch, max_age_days=7, _probe=peer_append)

        assert res["skipped_concurrent"] is True          # guard tripped
        assert peer_line.strip() in mem.read_text()        # peer's fact SURVIVES
        assert EVICTABLE in mem.read_text()                # nothing was evicted
        assert not arch.exists()                           # archive NOT written (no dup)

    def test_probe_noop_writes_normally(self, tmp_path):
        # guard passes when the file is unchanged under it → normal eviction
        mem = self._seed(tmp_path)
        arch = tmp_path / "archive"
        res = roll_recent_state(mem, arch, max_age_days=7, _probe=lambda p: None)
        assert res["skipped_concurrent"] is False
        assert len(res["evicted"]) == 1
        assert EVICTABLE not in mem.read_text()            # evicted out of MEMORY.md
        assert EVICTABLE in res["archive_path"].read_text()

    def test_default_has_skipped_flag_false(self, tmp_path):
        # the normal no-probe path reports the flag and never trips it
        mem = tmp_path / "MEMORY.md"
        mem.write_text(_doc(EVICTABLE))
        _write_home(tmp_path, "project/a.md", *EVICTABLE_SIG)
        res = roll_recent_state(mem, tmp_path / "archive", max_age_days=7)
        assert res["skipped_concurrent"] is False
        assert len(res["evicted"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# homing-verification guard, exercised through the real file-resolution path
# (_pointer_target_paths → _resolve_home_text). Conservative: unverifiable → KEEP.
# ─────────────────────────────────────────────────────────────────────────────

class TestHomingGuard:
    CID = ("- **CI/deploy** (07-01): deploy-target=`github` RESOLVED. "
           "→ project/foo.md")

    def test_pointer_target_missing_signature_keeps(self, tmp_path):
        # home file exists but does NOT contain the bullet's signature → KEEP
        mem = tmp_path / "MEMORY.md"
        mem.write_text(_doc(self.CID))
        _write_unrelated_home(tmp_path, "project/foo.md")
        res = roll_recent_state(mem, tmp_path / "archive", max_age_days=7, verify_homing=True)
        assert res["evicted"] == []
        assert "**CI/deploy**" in mem.read_text()          # bullet stays put
        assert not (tmp_path / "archive").exists()          # nothing archived

    def test_pointer_target_has_signature_evicts(self, tmp_path):
        # same bullet, but the home file DOES contain the signature → EVICT
        mem = tmp_path / "MEMORY.md"
        mem.write_text(_doc(self.CID))
        (tmp_path / "project").mkdir(exist_ok=True)
        (tmp_path / "project" / "foo.md").write_text(
            "# Foo\n\nThe ci/deploy pipeline: deploy-target is `github` now.\n")
        res = roll_recent_state(mem, tmp_path / "archive", max_age_days=7, verify_homing=True)
        assert len(res["evicted"]) == 1
        assert "**CI/deploy**" not in mem.read_text()       # evicted out

    def test_pointer_target_absent_keeps(self, tmp_path):
        # home file does not exist at all → cannot verify → KEEP (conservative)
        bullet = ("- **CI/deploy** (07-01): deploy-target=`github` RESOLVED. "
                  "→ project/missing.md")
        mem = tmp_path / "MEMORY.md"
        mem.write_text(_doc(bullet))
        res = roll_recent_state(mem, tmp_path / "archive", max_age_days=7, verify_homing=True)
        assert res["evicted"] == []
        assert "**CI/deploy**" in mem.read_text()
