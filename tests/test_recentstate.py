"""recent-roll test suite — the ## Recent State auto-roll safety gate.

Pure-function / tmp_path only; no facts.db, no live MEMORY_DIR. Every branch of
the 3-condition eviction gate (+ the ⚠️ veto and the no-pointer floor) is asserted
independently, plus the verbatim-archive landing and the dry-run no-mutation."""
import datetime as dt
from pathlib import Path

import pytest

from gaius.recentstate import (
    should_evict,
    roll_recent_state,
    _has_trailing_pointer,
    _has_done_marker,
    _has_veto,
    _bullet_date,
    _section_date,
)

SEC = dt.date(2026, 7, 20)   # section header date; max_age_days=7 → 07-13 boundary

# All-three-pass, no veto → the one bullet that SHOULD roll off.
EVICTABLE = "- **A** (07-01): pipeline RESOLVED and shipped. → project/a.md"


# ─────────────────────────────────────────────────────────────────────────────
# The gate, condition by condition
# ─────────────────────────────────────────────────────────────────────────────

class TestShouldEvictGate:
    def test_all_three_pass_evicts(self):
        assert should_evict(EVICTABLE, SEC, 7) is True

    def test_age_within_threshold_keeps(self):
        # done + pointer + no veto, but dated 07-19 (age 1 ≤ 7) → KEEP
        b = "- **B** (07-19): thing LIVE now. → project/b.md"
        assert should_evict(b, SEC, 7) is False

    def test_age_exactly_at_threshold_keeps(self):
        # 07-13 is exactly 7 days before 07-20; gate is strict `> max_age_days`
        b = "- **C** (07-13): thing FIXED. → project/c.md"
        assert should_evict(b, SEC, 7) is False

    def test_no_done_marker_keeps(self):
        # old + pointer + no veto, but no done-marker → KEEP
        b = "- **D** (07-01): investigation ongoing, pending. → project/d.md"
        assert should_evict(b, SEC, 7) is False

    def test_no_pointer_keeps(self):
        # old + done + no veto, but NO trailing pointer (no durable home) → KEEP
        b = "- **E** (07-01): rewrote the thing, RESOLVED, no home left."
        assert should_evict(b, SEC, 7) is False

    def test_veto_keeps_even_if_all_three_pass(self):
        # old + done + pointer AND ⚠️ → KEEP (veto beats the gate)
        b = "- **F** (07-01): RESOLVED but ⚠️ do NOT re-run. → project/f.md"
        # sanity: strip the veto and it WOULD evict (proves veto is the only blocker)
        assert should_evict(b.replace("⚠️ ", ""), SEC, 7) is True
        assert should_evict(b, SEC, 7) is False

    def test_inline_transformation_arrow_is_not_a_pointer(self):
        # "A→B" mid-bullet is not a trailing pointer → no home → KEEP
        b = "- **G** (07-01): migrated LINSTOR→tailscale, RESOLVED."
        assert should_evict(b, SEC, 7) is False

    def test_markdown_link_pointer_evicts(self):
        b = "- **H** (07-01): DONE and verified [see](project/h.md)"
        assert should_evict(b, SEC, 7) is True

    def test_no_section_date_keeps(self):
        assert should_evict(EVICTABLE, None, 7) is False

    def test_no_bullet_date_keeps(self):
        b = "- **I**: RESOLVED long ago. → project/i.md"   # no date-stamp at all
        assert should_evict(b, SEC, 7) is False


# ─────────────────────────────────────────────────────────────────────────────
# helper predicates
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
        assert _has_veto("something ⚠️ careful")
        assert _has_veto("something ⚠ careful")   # bare glyph, no VS16
        assert not _has_veto("no warning here")

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


class TestDecJanBoundaryEviction:
    def test_december_bullet_rolls_off_january_header(self, tmp_path):
        doc = (
            "# MEMORY\n\n## Recent State (2026-01-05)\n\n"
            "- **Old** (12-20): pipeline RESOLVED. → project/o.md\n"
            "- **New** (01-04): thing LIVE. → project/n.md\n"
        )
        mem = tmp_path / "MEMORY.md"
        mem.write_text(doc)
        res = roll_recent_state(mem, tmp_path / "archive", max_age_days=7)
        assert len(res["evicted"]) == 1
        assert "**Old**" in res["evicted"][0]
        # archive bucket named by the section-header month
        assert res["archive_path"].name == "recent-state-2026-01.md"


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
    def _bullets(self):
        return [
            EVICTABLE,                                                   # evict
            "- **B** (07-19): thing LIVE. → project/b.md",              # keep (recent)
            "- **C** (07-01): pending, no marker. → project/c.md",      # keep (no marker)
            "- **D** (07-01): RESOLVED but no home left.",              # keep (no pointer)
            "- **E** (07-01): RESOLVED ⚠️ do NOT rerun. → project/e.md", # keep (veto)
        ]

    def test_only_evictable_bullet_removed(self, tmp_path):
        mem = tmp_path / "MEMORY.md"
        mem.write_text(_doc(*self._bullets()))
        res = roll_recent_state(mem, tmp_path / "archive", max_age_days=7)
        assert len(res["evicted"]) == 1

        after = mem.read_text()
        assert "**A**" not in after                 # evicted bullet gone
        for keep in ("**B**", "**C**", "**D**", "**E**", "## Project Files"):
            assert keep in after                    # everything else survived

    def test_evicted_line_lands_verbatim(self, tmp_path):
        mem = tmp_path / "MEMORY.md"
        mem.write_text(_doc(*self._bullets()))
        res = roll_recent_state(mem, tmp_path / "archive", max_age_days=7)
        arch_text = res["archive_path"].read_text()
        assert res["archive_path"].name == "recent-state-2026-07.md"
        # the evicted line is present byte-for-byte (verbatim, incl. trailing \n)
        assert res["evicted"][0] in arch_text
        assert res["evicted"][0].rstrip("\n") == EVICTABLE

    def test_archive_appends_across_runs(self, tmp_path):
        arch = tmp_path / "archive"
        m1 = tmp_path / "M1.md"
        m1.write_text(_doc(EVICTABLE))
        roll_recent_state(m1, arch, max_age_days=7)
        m2 = tmp_path / "M2.md"
        other = "- **Z** (07-02): rollout MERGED. → project/z.md"
        m2.write_text(_doc(other))
        res2 = roll_recent_state(m2, arch, max_age_days=7)
        arch_text = res2["archive_path"].read_text()
        assert EVICTABLE in arch_text and other in arch_text   # both runs accreted

    def test_nothing_evictable_leaves_files_untouched(self, tmp_path):
        mem = tmp_path / "MEMORY.md"
        content = _doc(
            "- **B** (07-19): thing LIVE. → project/b.md",
            "- **E** (07-01): RESOLVED ⚠️ veto. → project/e.md",
        )
        mem.write_text(content)
        res = roll_recent_state(mem, tmp_path / "archive", max_age_days=7)
        assert res["evicted"] == []
        assert mem.read_text() == content               # byte-identical, no rewrite
        assert not res["archive_path"].exists()         # archive not created


class TestDryRun:
    def test_dry_run_mutates_nothing(self, tmp_path):
        mem = tmp_path / "MEMORY.md"
        content = _doc(EVICTABLE, "- **B** (07-19): LIVE. → project/b.md")
        mem.write_text(content)
        arch = tmp_path / "archive"
        res = roll_recent_state(mem, arch, max_age_days=7, dry_run=True)
        assert len(res["evicted"]) == 1                 # reports what WOULD go
        assert mem.read_text() == content               # but MEMORY.md untouched
        assert not arch.exists()                        # and no archive written


class TestAtomicReread:
    def test_reads_file_fresh_each_call(self, tmp_path):
        # a peer append between two rolls must be seen by the second run
        mem = tmp_path / "MEMORY.md"
        mem.write_text(_doc("- **B** (07-19): LIVE. → project/b.md"))
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

    def test_concurrent_append_bails_and_preserves(self, tmp_path):
        mem = tmp_path / "MEMORY.md"
        mem.write_text(_doc(EVICTABLE, "- **B** (07-19): LIVE. → project/b.md"))
        arch = tmp_path / "archive"
        peer_line = "- **PEER** (07-20): landed mid-roll LIVE. → project/peer.md\n"

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
        mem = tmp_path / "MEMORY.md"
        mem.write_text(_doc(EVICTABLE, "- **B** (07-19): LIVE. → project/b.md"))
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
        res = roll_recent_state(mem, tmp_path / "archive", max_age_days=7)
        assert res["skipped_concurrent"] is False
        assert len(res["evicted"]) == 1
