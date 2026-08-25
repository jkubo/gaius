"""mnemosyne test suite — memory file health monitor."""
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from importlib.machinery import SourceFileLoader
from importlib.util import spec_from_loader, module_from_spec

import pytest

_REPO = Path(__file__).parent.parent


def _load_script(name, path):
    loader = SourceFileLoader(name, str(path))
    spec = spec_from_loader(name, loader)
    mod = module_from_spec(spec)
    loader.exec_module(mod)
    return mod


mn = _load_script("mnemosyne", _REPO / "mnemosyne")


@pytest.fixture(autouse=True)
def _isolate_gaius_staging(tmp_path, monkeypatch):
    """Gap 63 scans GAIUS_STAGING_DIR / ~/.gaius/staged. Isolate so the live
    queue cannot trip 'All files within threshold' in unrelated tests."""
    d = tmp_path / "_empty_staged"
    d.mkdir()
    monkeypatch.setenv("GAIUS_STAGING_DIR", str(d))


# ─────────────────────────────────────────────────────────────────────────────
# color_status
# ─────────────────────────────────────────────────────────────────────────────

class TestColorStatus:
    def test_green_below_warn(self):
        assert "GREEN" in mn.color_status(50, 180, 200)

    def test_yellow_at_warn_boundary(self):
        assert "YELLOW" in mn.color_status(180, 180, 200)

    def test_yellow_between_warn_and_error(self):
        assert "YELLOW" in mn.color_status(195, 180, 200)

    def test_red_at_error_boundary(self):
        assert "RED" in mn.color_status(200, 180, 200)

    def test_red_above_error(self):
        assert "RED" in mn.color_status(999, 180, 200)

    def test_green_zero_lines(self):
        assert "GREEN" in mn.color_status(0, 50, 100)


# ─────────────────────────────────────────────────────────────────────────────
# count_lines
# ─────────────────────────────────────────────────────────────────────────────

class TestCountLines:
    def test_correct_line_count(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("line1\nline2\nline3\n")
        assert mn.count_lines(f) == 3

    def test_single_line_no_newline(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("one line")
        assert mn.count_lines(f) == 1

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.md"
        f.write_text("")
        assert mn.count_lines(f) == 0

    def test_nonexistent_returns_minus_one(self, tmp_path):
        assert mn.count_lines(tmp_path / "ghost.md") == -1


# ─────────────────────────────────────────────────────────────────────────────
# audit keyword detection
# ─────────────────────────────────────────────────────────────────────────────

class TestAudit:
    @pytest.fixture(autouse=True)
    def _default_keywords(self, monkeypatch):
        # Isolate from the operator's ~/.gaius/config.yaml audit_keywords (which
        # drift over time). _load_domain_keywords() honors GAIUS_CONFIG=/dev/null
        # → built-in defaults; DOMAIN_KEYWORDS is bound at import, so reload it.
        monkeypatch.setenv("GAIUS_CONFIG", "/dev/null")
        monkeypatch.setattr(mn, "DOMAIN_KEYWORDS", mn._load_domain_keywords())

    def _run_audit(self, memory_dir):
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_audit(memory_dir, [])
        return buf.getvalue()

    def test_flags_storage_keyword(self, tmp_path):
        (tmp_path / "common.md").write_text("- drbd replication needs LINSTOR config\n")
        out = self._run_audit(tmp_path)
        assert "storage" in out

    def test_flags_networking_keyword(self, tmp_path):
        (tmp_path / "common.md").write_text("- flannel VXLAN requires MTU tuning\n")
        out = self._run_audit(tmp_path)
        assert "networking" in out

    def test_ignores_universal_hard_rules(self, tmp_path):
        """Lines containing 'never'/'always' are treated as global rules — not flagged."""
        (tmp_path / "common.md").write_text("- Never run drbd without backup\n")
        out = self._run_audit(tmp_path)
        assert "✓" in out

    def test_clean_file_passes(self, tmp_path):
        (tmp_path / "common.md").write_text(
            "- Always check logs before escalating\n"
            "- Request reviews for all PRs\n"
        )
        out = self._run_audit(tmp_path)
        assert "✓" in out

    def test_multiple_domains_flagged(self, tmp_path):
        (tmp_path / "common.md").write_text(
            "- drbd volume needs format\n"
            "- grafana dashboard shows metrics\n"
        )
        out = self._run_audit(tmp_path)
        assert "storage" in out
        assert "observability" in out

    def test_missing_common_md_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            mn.cmd_audit(tmp_path, [])


class TestContentDefects:
    """scan_content_defects catches structural corruption line/byte checks miss."""

    def test_detects_joined_bullet(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("- **A**: text ending (2026-06-08).- **B**: merged on one line\n")
        kinds = [k for _, k, _ in mn.scan_content_defects(p)]
        assert "joined-line" in kinds

    def test_clean_file_no_defects(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("- **A**: a fact.\n- **B**: another fact.\n")
        assert mn.scan_content_defects(p) == []

    def test_legit_inline_dash_not_flagged(self, tmp_path):
        # space before the dash => legitimate inline emphasis, not a merged bullet
        p = tmp_path / "x.md"
        p.write_text("- **A**: uses X - **bold** mid sentence.\n")
        assert all(k != "joined-line" for _, k, _ in mn.scan_content_defects(p))

    def test_internal_hyphen_date_not_flagged(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("- **Window**: 2026-06-29/30 genesis-config window pending.\n")
        assert mn.scan_content_defects(p) == []

    def test_detects_runaway_line(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("- **Header**: " + ("accretion " * 230) + "\n")  # >2000 chars, no merge
        kinds = [k for _, k, _ in mn.scan_content_defects(p)]
        assert "long-line" in kinds and "joined-line" not in kinds


class TestMemoryByteBudget:
    """MEMORY.md injection-budget check. Sizes are derived from
    MEMORY_BYTE_WARN / MEMORY_BYTE_ERR so a recalibration (16/20 → 20/24
    on 2026-08-13) cannot leave the suite asserting the superseded pair.
    Bodies use <180 short lines to isolate bytes from the line-count and
    runaway-line checks."""

    def _write(self, d, total_bytes, line_len=100):
        line = "z" * (line_len - 1) + "\n"
        n = total_bytes // line_len
        (d / "MEMORY.md").write_text(line * n)

    def test_just_over_warn_is_yellow_advisory(self, tmp_path, capsys):
        self._write(tmp_path, mn.MEMORY_BYTE_WARN + 512)  # GREEN on lines
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "YELLOW" in out and "injection-budget" in out
        assert "within threshold" not in out

    def test_at_err_emits_blocking_red_marker(self, tmp_path, capsys):
        # 150 B/line keeps the line-count GREEN (<180) while crossing 24 KB.
        self._write(tmp_path, mn.MEMORY_BYTE_ERR + 150, line_len=150)
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "\033[31m\033[1mRED\033[0m" in out     # pre-commit hook greps this -> blocks

    def test_under_warn_clean(self, tmp_path, capsys):
        self._write(tmp_path, 5100, line_len=51)     # 100 lines, all GREEN
        mn.cmd_health(tmp_path, [])
        assert "within threshold" in capsys.readouterr().out


class TestRankStatus:
    """rank_status — shared severity rank so a line verdict and a byte verdict are
    comparable and max() can pick the worse one."""

    def test_ranks_are_ordered(self):
        assert mn.GREEN_RANK < mn.YELLOW_RANK < mn.RED_RANK

    def test_green_below_warn(self):
        assert mn.rank_status(50, 180, 200) == mn.GREEN_RANK

    def test_yellow_at_warn_boundary(self):
        assert mn.rank_status(180, 180, 200) == mn.YELLOW_RANK

    def test_red_at_error_boundary(self):
        assert mn.rank_status(200, 180, 200) == mn.RED_RANK

    def test_red_label_is_the_precommit_token(self):
        assert mn.RANK_LABEL[mn.RED_RANK] == "\033[31m\033[1mRED\033[0m"


class TestDomainByteBudget:
    """domain/*.md are gated on BYTES as well as lines, and report the WORSE of the
    two verdicts.

    The line cap alone is satisfiable by REFLOW — join two bullets and the count
    drops while the injection cost does not. domain/services.md held exactly 149
    lines (GREEN, one under the warn) from 2026-07-14 to 2026-07-31 while growing
    18,484 → 31,814 B, and four domain files were pinned at 149. Five exceeded
    MEMORY.md's own 20KB ERROR ceiling and every one of them read GREEN.

    Bodies use short lines so bytes are isolated from the line-count and
    runaway-line (>2000 char) checks."""

    RED_TOKEN = "\033[31m\033[1mRED\033[0m"   # pre-commit greps this → blocks the commit

    def _domain(self, d, name, total_bytes, line_len=100):
        dom = d / "domain"
        dom.mkdir(exist_ok=True)
        line = "z" * (line_len - 1) + "\n"
        (dom / name).write_text(line * (total_bytes // line_len))

    def test_reflowed_file_green_on_lines_is_red_on_bytes(self, tmp_path, capsys):
        # 106 lines (GREEN on lines) / 31.8KB — the exact services.md shape.
        self._domain(tmp_path, "services.md", 31800, line_len=300)
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert self.RED_TOKEN in out
        assert "over limit" in out and "(bytes)" in out

    def test_at_byte_warn_is_yellow(self, tmp_path, capsys):
        # 103 lines (GREEN on lines) / 20.1KB — just past the 20KB warn.
        self._domain(tmp_path, "storage.md", 20 * 1024 + 200, line_len=200)
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "YELLOW" in out and "approaching limit" in out and "(bytes)" in out
        assert self.RED_TOKEN not in out

    def test_just_under_byte_warn_is_green(self, tmp_path, capsys):
        # 102 lines / 19.9KB — near-miss on bytes must pass, or the gate is noise.
        self._domain(tmp_path, "recurring-alerts.md", 20 * 1024 - 100, line_len=200)
        mn.cmd_health(tmp_path, [])
        assert "within threshold" in capsys.readouterr().out

    def test_line_red_survives_a_green_byte_count(self, tmp_path, capsys):
        # 220 lines (RED on lines) / 4.4KB (GREEN on bytes) — lines are kept as a
        # readability proxy; adding bytes must not weaken the existing gate.
        self._domain(tmp_path, "cctv.md", 220 * 20, line_len=20)
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert self.RED_TOKEN in out
        assert "over limit" in out and "(lines)" in out

    def test_over_on_both_reports_both(self, tmp_path, capsys):
        # 290 lines / 29KB — over on each axis independently.
        self._domain(tmp_path, "security.md", 29000, line_len=100)
        mn.cmd_health(tmp_path, [])
        assert "(lines+bytes)" in capsys.readouterr().out

    def test_non_domain_categories_are_not_byte_gated(self, tmp_path, capsys):
        # gotchas.md is 80KB+ by design (deep reference, read on demand, never
        # injected whole). Byte gating is domain-only; a fat root file stays GREEN.
        (tmp_path / "gotchas.md").write_text(("z" * 299 + "\n") * 300)   # 90KB, 300 lines
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert self.RED_TOKEN not in out and "within threshold" in out

    def test_domain_byte_threshold_is_configured_and_bounded(self):
        # Calibration comes from the injection budget, not the current distribution.
        # Locking presence + ordering + a ceiling leaves room to retune but not to
        # relax the gate until it re-blesses the drift it exists to catch.
        warn, err = mn.BYTE_THRESHOLDS["domain"]
        assert warn < err
        assert warn >= 20 * 1024                      # MEMORY.md's own error ceiling
        assert err <= 32 * 1024                       # below today's fattest domain file
        assert "skills" not in mn.BYTE_THRESHOLDS and "other" not in mn.BYTE_THRESHOLDS


class TestIndexGlossAccretion:
    """scan_index_gloss — Gap-32 structural cure. Flags MEMORY.md '## Project
    Files' index lines carrying accreted prose, measured link-count-agnostically
    by stripping [label](path) tokens. A vertical with MANY terse links must pass;
    one whose links carry paragraph glosses must flag. (This is the failure the
    2000-char runaway check + total-byte budget both miss.)"""

    TERSE = "- **Widgets**: " + " | ".join(f"[f{i}](project/p{i}.md)" for i in range(22))
    ACCRETED = ("- **Widgets**: [master](project/p.md) — "
                + "verbose resolved status detail from a session note " * 12)

    def _doc(self, *index_lines, recent=None):
        body = "# MEMORY\n\n## Project Files — grouped by vertical\n\n"
        body += "\n".join(index_lines) + "\n"
        if recent:
            body += "\n## Recent State\n\n" + recent + "\n"
        return body

    def test_terse_link_dense_line_passes(self, tmp_path):
        # 22 links, ~492 chars total, but tiny gloss — must NOT flag.
        p = tmp_path / "MEMORY.md"
        p.write_text(self._doc(self.TERSE))
        assert mn.scan_index_gloss(p) == []

    def test_accreted_line_flagged(self, tmp_path):
        p = tmp_path / "MEMORY.md"
        p.write_text(self._doc(self.ACCRETED))
        hits = mn.scan_index_gloss(p)
        assert len(hits) == 1
        assert hits[0][1] > mn.INDEX_GLOSS_WARN     # gloss bytes over threshold

    def test_only_scans_project_files_section(self, tmp_path):
        # an accreted-looking line in Recent State must NOT be flagged
        p = tmp_path / "MEMORY.md"
        p.write_text(self._doc(self.TERSE, recent=self.ACCRETED))
        assert mn.scan_index_gloss(p) == []

    def test_heaviest_first(self, tmp_path):
        p = tmp_path / "MEMORY.md"
        small = "- **A**: [x](p.md) — " + "gloss " * 70
        big   = "- **B**: [y](p.md) — " + "gloss " * 140
        p.write_text(self._doc(small, big))
        hits = mn.scan_index_gloss(p)
        assert len(hits) == 2
        assert hits[0][1] > hits[1][1]              # heaviest-first

    def test_cmd_health_surfaces_accretion(self, tmp_path, capsys):
        (tmp_path / "MEMORY.md").write_text(self._doc(self.ACCRETED))
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "Gap-32" in out
        assert "within threshold" not in out

    def test_cmd_health_clean_index_no_accretion(self, tmp_path, capsys):
        (tmp_path / "MEMORY.md").write_text(self._doc(self.TERSE))
        mn.cmd_health(tmp_path, [])
        assert "Gap-32" not in capsys.readouterr().out

    def test_accretion_advisory_is_not_a_blocking_red_token(self, tmp_path, capsys):
        # the YELLOW accretion advisory must never emit the exact ANSI token the
        # pre-commit hook greps to block commits.
        (tmp_path / "MEMORY.md").write_text(self._doc(self.ACCRETED))
        mn.cmd_health(tmp_path, [])
        assert "\033[31m\033[1mRED\033[0m" not in capsys.readouterr().out


class TestRecentStateAdvisory:
    """scan_recent_state_bullets — flags a fact that outgrew the ## Recent State
    changelog. Separate from scan_index_gloss (which is '## Project Files'-scoped
    and must stay so). Advisory only: YELLOW, never the blocking RED token."""

    FAT = "- **X**: " + "verbose resolved status detail from a session note " * 16
    TERSE = "- **Y**: [home](project/p.md) — shipped 07-19."

    def _doc(self, *recent_lines, project=None):
        body = "# MEMORY\n\n"
        if project:
            body += "## Project Files\n\n" + project + "\n\n"
        body += "## Recent State (2026-07-20)\n\n" + "\n".join(recent_lines) + "\n"
        return body

    def test_fat_recent_bullet_flagged(self, tmp_path):
        p = tmp_path / "MEMORY.md"
        p.write_text(self._doc(self.FAT))
        hits = mn.scan_recent_state_bullets(p)
        assert len(hits) == 1
        assert hits[0][1] > mn.RECENT_STATE_BULLET_WARN

    def test_terse_recent_bullet_passes(self, tmp_path):
        p = tmp_path / "MEMORY.md"
        p.write_text(self._doc(self.TERSE))
        assert mn.scan_recent_state_bullets(p) == []

    def test_index_gloss_does_not_scan_recent_state(self, tmp_path):
        # the FAT bullet lives ONLY in Recent State — scan_index_gloss must ignore it
        p = tmp_path / "MEMORY.md"
        p.write_text(self._doc(self.FAT, project="- **P**: [x](project/p.md) terse"))
        assert mn.scan_index_gloss(p) == []

    def test_cmd_health_surfaces_recent_state_accretion(self, tmp_path, capsys):
        (tmp_path / "MEMORY.md").write_text(self._doc(self.FAT))
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "Recent State bullet" in out
        assert "within threshold" not in out

    def test_recent_state_advisory_is_not_a_blocking_red_token(self, tmp_path, capsys):
        (tmp_path / "MEMORY.md").write_text(self._doc(self.FAT))
        mn.cmd_health(tmp_path, [])
        assert "\033[31m\033[1mRED\033[0m" not in capsys.readouterr().out


class TestPointerResolution:
    """scan_pointer_resolution — Gap-42. A pointer is a CLAIM about another file
    and rots independently of the bullet it sits on. Catches (b) dangling,
    (c) ambiguous bare basename, and the mechanical subset of (a): a §anchor
    naming a heading the target does not have. Advisory only: YELLOW, never the
    blocking RED token — stranding the nightly memory auto-commit over a broken
    link is a worse failure than the broken link."""

    @pytest.fixture(autouse=True)
    def _no_env_override(self, monkeypatch):
        monkeypatch.delenv("MNEMOSYNE_POINTER_FILES", raising=False)

    def _tree(self, tmp_path):
        """A memory root mirroring the real shape: networking.md collides across
        domain/ and troubleshooting/; etcd.md is unique to troubleshooting/."""
        for d in ("domain", "troubleshooting", "project", "skills", "sop"):
            (tmp_path / d).mkdir()
        (tmp_path / "domain" / "networking.md").write_text("# Networking\n")
        (tmp_path / "troubleshooting" / "networking.md").write_text("# Networking\n")
        (tmp_path / "troubleshooting" / "etcd.md").write_text("# etcd\n")
        (tmp_path / "gotchas.md").write_text("# Gotchas\n")
        # no `Registry` HEADING — the section is a bold label, not a heading
        (tmp_path / "domain" / "services.md").write_text(
            "# Services\n\n**Registry**:\n- pull-through cache serves stale tags\n")
        (tmp_path / "skills" / "audit.md").write_text("## RBAC Review Pattern\n")
        (tmp_path / "project" / "p.md").write_text("## Deploy Model & Infra Gotchas\n")
        return tmp_path

    def _doc(self, tmp_path, *body):
        (tmp_path / "MEMORY.md").write_text("# MEMORY\n\n" + "\n".join(body) + "\n")
        return tmp_path

    def _kinds(self, hits):
        return sorted((h[2], h[3]) for h in hits)

    # ── resolves clean ───────────────────────────────────────────────────────
    def test_qualified_paths_are_silent(self, tmp_path):
        root = self._doc(self._tree(tmp_path),
                         "- fact → `domain/networking.md`",
                         "- fact → [p](project/p.md)",
                         "- fact → `gotchas.md`",
                         "- fact → `domain/`")
        assert mn.scan_pointer_resolution(root) == []

    # ── class (b): dangling ──────────────────────────────────────────────────
    def test_dangling_slashed_path(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- fact → `project/project_nope.md`")
        assert self._kinds(mn.scan_pointer_resolution(root)) == \
            [("dangling", "project/project_nope.md")]

    def test_slashed_path_gets_no_fallback_rescue(self, tmp_path):
        # domain/etcd.md does not exist; troubleshooting/etcd.md does. A literal
        # path must NOT be silently rescued by searching the other roots.
        root = self._doc(self._tree(tmp_path), "- fact → `domain/etcd.md`")
        assert self._kinds(mn.scan_pointer_resolution(root)) == \
            [("dangling", "domain/etcd.md")]

    def test_bare_basename_at_the_root_resolves(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- fact → `gotchas.md`")
        assert mn.scan_pointer_resolution(root) == []

    def test_unique_bare_basename_in_a_subdir_is_unqualified(self, tmp_path):
        # Gap 42 class (c) is "unresolvable path": it does not exist AT the memory
        # root, so a follower has to guess the directory. A unique hit is not a
        # pass — the documented repair is to path-qualify it.
        root = self._doc(self._tree(tmp_path), "- fact → `etcd.md`")
        hits = mn.scan_pointer_resolution(root)
        assert self._kinds(hits) == [("unqualified", "etcd.md")]
        assert "troubleshooting/etcd.md" in hits[0][4]

    def test_unqualified_still_checks_its_anchor(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- fact → `services.md` §Nope")
        # services.md collides, so it is ambiguous, not unqualified — use a unique one
        (tmp_path / "domain" / "solo.md").write_text("# Solo\n\n**Real**:\n")
        self._doc(root, "- fact → `solo.md` §Ghost")
        kinds = [h[2] for h in mn.scan_pointer_resolution(root)]
        assert "unqualified" in kinds and "anchor" in kinds

    # ── class (c): ambiguous ─────────────────────────────────────────────────
    def test_ambiguous_bare_basename(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- fact → `networking.md`")
        hits = mn.scan_pointer_resolution(root)
        assert self._kinds(hits) == [("ambiguous", "networking.md")]
        assert "domain/networking.md" in hits[0][4]
        assert "troubleshooting/networking.md" in hits[0][4]

    def test_ambiguity_is_never_resolved_by_root_priority(self, tmp_path):
        # picking a winner would turn a visible failure into a confidently-wrong read
        root = self._doc(self._tree(tmp_path), "- fact → `networking.md`")
        assert mn.scan_pointer_resolution(root)[0][2] == "ambiguous"

    def test_qualifying_the_path_is_the_repair(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- fact → `troubleshooting/networking.md`")
        assert mn.scan_pointer_resolution(root) == []

    def test_multiple_targets_on_one_line(self, tmp_path):
        # finditer, not search: one line carrying 3 chained targets
        root = self._doc(self._tree(tmp_path),
                         "- fact → `networking.md`/`etcd.md`/`gotchas.md`")
        # all three adjudicated independently: collides / subdir-only / at the root
        assert self._kinds(mn.scan_pointer_resolution(root)) == \
            [("ambiguous", "networking.md"), ("unqualified", "etcd.md")]

    # ── mechanical subset of class (a): anchors ──────────────────────────────
    def test_anchor_matches_bold_label_not_just_heading(self, tmp_path):
        # domain/services.md has no `Registry` heading — only `**Registry**:`.
        # A heading-only matcher false-positives here.
        root = self._doc(self._tree(tmp_path), "- fact → `domain/services.md` §Registry")
        assert mn.scan_pointer_resolution(root) == []

    def test_anchor_absent_is_flagged(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- fact → `domain/services.md` §Scanner")
        hits = mn.scan_pointer_resolution(root)
        assert self._kinds(hits) == [("anchor", "domain/services.md")]
        assert "§Scanner" in hits[0][4]

    def test_anchor_trailing_qualifier_is_stripped(self, tmp_path):
        # `§RBAC step 5` points at `## RBAC Review Pattern`, item 5. The
        # step/digit tail is a locator, not part of the section name.
        root = self._doc(self._tree(tmp_path), "- fact → `skills/audit.md` §RBAC step 5")
        assert mn.scan_pointer_resolution(root) == []

    def test_anchor_binds_to_nearest_preceding_pointer(self, tmp_path):
        root = self._doc(self._tree(tmp_path),
                         "- fact → `gotchas.md` and → `domain/services.md` §Registry")
        assert mn.scan_pointer_resolution(root) == []

    def test_anchor_on_directory_target_is_skipped(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- fact → `domain/` §Whatever")
        assert mn.scan_pointer_resolution(root) == []

    # ── false-positive defenses ──────────────────────────────────────────────
    @pytest.mark.parametrize("token", [
        "https://git.example.com/acme/infra.git",   # F1 URL
        "/admin/reports/summary",                   # F2 HTTP route
        "git ls-tree origin/main --name-only",      # F3 shell command
        "pods/exec:create",                         # F4 k8s verb
        "modules/widget*",                          # F4 glob
        "ci.yml",                                   # F5 not .md
        "./deploy.sh",                              # F5 not .md
        "tool.py",                                  # F5 not .md
        "networking",                               # F5 bare identifier
        "order_entries",                            # F5 code identifier
        "services/",                                # F6 another repo's dir
        ".cache/",                                  # F6 another repo's dir
        "origin/main",                              # F7 git ref
        "acme/widgets",                             # F7 repo slug
        "postmortem/2026-04-06-network-outage.md",  # another repo's .md
    ])
    def test_non_pointers_are_silent(self, tmp_path, token):
        root = self._doc(self._tree(tmp_path), f"- fact `{token}` in prose")
        assert mn.scan_pointer_resolution(root) == []

    def test_absolute_path_outside_the_memory_root_is_silent(self, tmp_path):
        root = self._doc(self._tree(tmp_path), "- vault at `~/infra/vault.yml`")
        assert mn.scan_pointer_resolution(root) == []

    def test_absolute_path_inside_the_memory_root_resolves(self, tmp_path):
        # the same tree written two ways must resolve to one identity
        root = self._tree(tmp_path)
        self._doc(root, f"- fact → `{root}/domain/networking.md`")
        assert mn.scan_pointer_resolution(root) == []

    def test_trailing_slash_survives_path_resolution(self, tmp_path):
        # Path.resolve() strips a trailing slash; reading the dir intent after
        # normalizing would silently demote this to a non-.md skip
        root = self._tree(tmp_path)
        self._doc(root, f"- SOPs live in `{root}/sop/`")
        assert mn.scan_pointer_resolution(root) == []

    # ── plumbing ─────────────────────────────────────────────────────────────
    def test_missing_memory_file_returns_empty(self, tmp_path):
        assert mn.scan_pointer_resolution(tmp_path) == []

    def test_kill_switch(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".gaius").mkdir(parents=True)
        (home / ".gaius" / "pointer-check-disabled").touch()
        root = self._doc(self._tree(tmp_path), "- fact → `networking.md`")
        assert mn.scan_pointer_resolution(root)          # fires without the switch
        monkeypatch.setenv("HOME", str(home))
        assert mn.scan_pointer_resolution(root) == []

    def test_cmd_health_surfaces_pointer_rot(self, tmp_path, capsys):
        self._doc(self._tree(tmp_path), "- fact → `networking.md`")
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "pointer(s) do not resolve" in out
        assert "within threshold" not in out

    def test_pointer_rot_is_not_a_blocking_red_token(self, tmp_path, capsys):
        self._doc(self._tree(tmp_path),
                  "- a → `networking.md`",
                  "- b → `project/project_nope.md`",
                  "- c → `domain/services.md` §Scanner")
        mn.cmd_health(tmp_path, [])
        assert "\033[31m\033[1mRED\033[0m" not in capsys.readouterr().out


class TestPointerContent:
    """Gap-42 class (a): pointer RESOLVES but the target lacks the fact.
    Bar is zero false POSITIVES — false negatives (fact homed under other
    wording) are accepted by design."""

    def _tree(self, tmp_path, body):
        (tmp_path / "project").mkdir(parents=True, exist_ok=True)
        (tmp_path / "project" / "t.md").write_text(body)
        return tmp_path

    def _doc(self, root, *lines):
        (root / "MEMORY.md").write_text("\n".join(lines) + "\n")
        return root

    def test_flags_when_no_ident_landed(self, tmp_path):
        root = self._doc(self._tree(tmp_path, "# T\nunrelated prose\n"),
                         "- `headlamp-admin-token` and `credential-sync` → [x](project/t.md)")
        found = mn.scan_pointer_content(root)
        assert len(found) == 1
        assert found[0][1] == 1

    def test_silent_when_one_ident_landed(self, tmp_path):
        root = self._doc(self._tree(tmp_path, "# T\nthe `drift-scan.sh` script\n"),
                         "- `drift-scan.sh:23` and `nowhere.yaml` → [x](project/t.md)")
        assert mn.scan_pointer_content(root) == []

    def test_whitespace_insensitive_match(self, tmp_path):
        """MEMORY.md compresses `pods:create`; the target writes `pods: create`.
        Same fact — this was the check's only real-world false positive."""
        root = self._doc(self._tree(tmp_path, "# T\nunscoped `pods: create` escalates\n"),
                         "- `pods:create` and `absent-thing.sh` → [x](project/t.md)")
        assert mn.scan_pointer_content(root) == []

    def test_single_ident_is_too_fragile_to_judge(self, tmp_path):
        root = self._doc(self._tree(tmp_path, "# T\nnothing\n"),
                         "- `lonely-token.sh` → [x](project/t.md)")
        assert mn.scan_pointer_content(root) == []

    def test_dangling_pointer_left_to_sibling_scan(self, tmp_path):
        root = self._doc(self._tree(tmp_path, "# T\nnothing\n"),
                         "- `aaa-bbb.sh` and `ccc-ddd.sh` → [x](project/nope.md)")
        assert mn.scan_pointer_content(root) == []

    # --- Unread destinations (mnemos #168). Idents are gathered line-wide but
    # targets are not, so one readable pointer was being held answerable for a
    # whole multi-topic bullet. Each case below is a REPRODUCED false positive. ---

    def test_dir_pointer_on_the_line_silences(self, tmp_path):
        """`troubleshooting/` names a directory — the fact may be in any file
        under it and the scanner opens none of them. This is the shape of the
        real MEMORY.md roll-off line that fired."""
        root = self._doc(self._tree(tmp_path, "# T\nunrelated\n"),
                         "- traps in `troubleshooting/`: `super_read_only`, "
                         "`prometheus.io/scrape` → [x](project/t.md)")
        (root / "troubleshooting").mkdir(exist_ok=True)
        assert mn.scan_pointer_content(root) == []

    def test_out_of_root_md_pointer_silences(self, tmp_path):
        """`archive/x.md` is a real destination the resolver skips (archive/ is
        not in POINTER_ROOTS), so project/t.md is not answerable for the line."""
        root = self._doc(self._tree(tmp_path, "# T\nunrelated\n"),
                         "- `aaa-bbb.sh` and `ccc-ddd.sh` rolled to "
                         "[archive](archive/x.md) → [x](project/t.md)")
        (root / "archive").mkdir(exist_ok=True)
        (root / "archive" / "x.md").write_text("`aaa-bbb.sh` lives here\n")
        assert mn.scan_pointer_content(root) == []

    def test_dangling_beside_a_good_target_silences(self, tmp_path):
        """One dangling pointer was survivable only when it was the ONLY one;
        beside a resolvable target the line still fired."""
        root = self._doc(self._tree(tmp_path, "# T\nunrelated\n"),
                         "- `aaa-bbb.sh` and `ccc-ddd.sh` → [x](project/t.md) "
                         "· [y](project/nope.md)")
        assert mn.scan_pointer_content(root) == []

    def test_still_fires_when_every_destination_was_read(self, tmp_path):
        """Near-miss: the abstain rule must not silence the true positive it was
        narrowed around. Two pointers, both read, neither holding an ident."""
        root = self._tree(tmp_path, "# T\nunrelated\n")
        (root / "project" / "u.md").write_text("# U\nalso unrelated\n")
        self._doc(root, "- `aaa-bbb.sh` and `ccc-ddd.sh` → [x](project/t.md) "
                        "· [y](project/u.md)")
        assert len(mn.scan_pointer_content(root)) == 1

    def test_prose_md_mention_does_not_silence(self, tmp_path):
        """Near-miss: `the notes.md` is prose, not a destination. Abstaining on
        any backticked string containing '.md' would mute the scanner wholesale."""
        root = self._doc(self._tree(tmp_path, "# T\nunrelated\n"),
                         "- `aaa-bbb.sh` and `ccc-ddd.sh`, see `the notes.md` "
                         "→ [x](project/t.md)")
        assert len(mn.scan_pointer_content(root)) == 1

    def test_kill_switch(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".gaius").mkdir(parents=True)
        (home / ".gaius" / "pointer-check-disabled").touch()
        root = self._doc(self._tree(tmp_path, "# T\nunrelated\n"),
                         "- `aaa-bbb.sh` and `ccc-ddd.sh` → [x](project/t.md)")
        assert mn.scan_pointer_content(root)             # fires without the switch
        monkeypatch.setenv("HOME", str(home))
        assert mn.scan_pointer_content(root) == []

    def test_not_a_blocking_red_token(self, tmp_path, capsys):
        self._doc(self._tree(tmp_path, "# T\nunrelated\n"),
                  "- `aaa-bbb.sh` and `ccc-ddd.sh` → [x](project/t.md)")
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "does not contain the fact" in out
        assert "\033[31m\033[1mRED\033[0m" not in out


class TestHeaviestLines:
    def test_returns_longest_first(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("short\n" + "m" * 200 + "\n" + "l" * 500 + "\n")
        rows = mn.heaviest_lines(p, n=2)
        assert len(rows) == 2
        assert rows[0][1] == 500 and rows[1][1] == 200

    def test_weight_is_utf8_bytes_not_characters(self, tmp_path):
        """The canary the ASCII fixture above could never be: it passes identically whether
        the weight is `len(line)` or `len(line.encode())`, so it certified the mislabel for
        as long as it existed. Real Recent-State bullets are emoji-dense, and that is the
        population the heaviest-lines queue ranks."""
        p = tmp_path / "x.md"
        p.write_text("📌" * 100 + "\n")                  # 100 chars, 400 UTF-8 bytes
        assert mn.heaviest_lines(p, n=1)[0][1] == 400

    def test_ranks_by_bytes_when_bytes_and_chars_disagree(self, tmp_path):
        """Ordering, not just the printed figure — an emoji line SHORTER in characters is
        HEAVIER in the budget that actually binds, so it must rank first. Under `len()` this
        inverts, which is how the mislabel sent surgeons at the wrong line."""
        p = tmp_path / "x.md"
        p.write_text("📌" * 100 + "\n" + "a" * 300 + "\n")  # 400 B vs 300 B; 100 ch vs 300 ch
        rows = mn.heaviest_lines(p, n=2)
        assert rows[0][1] == 400 and rows[1][1] == 300


class TestScanRecentStateDuplicates:
    """scan_recent_state_duplicates — Gap-43. Two sessions writing the same event
    as two bullets was the dominant MEMORY.md byte-growth vector, and nothing
    detected it. Detection is by SHARED RARE IDENTIFIERS (issue refs, SHAs,
    backticked symbols), never prose similarity: two bullets on one SUBSYSTEM
    share none of those, two bullets on one EVENT share several.

    Zero false positives is the bar — the scanner is only worth reading if a hit
    means something. Every negative case below defends that bar."""

    def _mem(self, tmp_path, body):
        p = tmp_path / "MEMORY.md"
        p.write_text(body)
        return p

    def test_same_two_issue_refs_flags(self, tmp_path):
        p = self._mem(tmp_path, (
            "## Recent State\n"
            "- shipped the alerting sweep (#285, #288) — pager never fired\n"
            "- the alert work landed in #285 and #288, evaluator was dead\n"
        ))
        hits = mn.scan_recent_state_duplicates(p)
        assert len(hits) == 1
        assert hits[0][2] == "same-ids"

    def test_same_subsystem_different_ids_is_clean(self, tmp_path):
        """The precision case: same words, no shared identifiers, no hit."""
        p = self._mem(tmp_path, (
            "## Recent State\n"
            "- alerting sweep for the site pager (#285, #288)\n"
            "- alerting sweep for the storage pager (#411, #412)\n"
        ))
        assert mn.scan_recent_state_duplicates(p) == []

    def test_single_shared_id_is_not_enough(self, tmp_path):
        """DUP_MIN_STRONG is 2 — one co-cited issue is ordinary cross-reference."""
        p = self._mem(tmp_path, (
            "## Recent State\n"
            "- the readiness gate fix landed in #290\n"
            "- unrelated work that also happens to mention #290 in passing\n"
        ))
        assert mn.scan_recent_state_duplicates(p) == []

    def test_shared_sha_counts_as_strong(self, tmp_path):
        p = self._mem(tmp_path, (
            "## Recent State\n"
            "- reverted in a1b2c3d and re-landed as e4f5a6b\n"
            "- the a1b2c3d revert plus e4f5a6b, same incident\n"
        ))
        hits = mn.scan_recent_state_duplicates(p)
        assert len(hits) == 1

    def test_common_vocabulary_is_dropped(self, tmp_path):
        """A token in more than DUP_DF_CAP bullets is this file's vocabulary,
        not an event fingerprint — otherwise every bullet pairs with every other."""
        rows = "".join(
            f"- bullet {i} about `flannel-mtu` and `tailscale0-pmtud` "
            f"and `drbd-quorum` and `lost-quorum-taint`\n"
            for i in range(6)
        )
        assert mn.scan_recent_state_duplicates(self._mem(tmp_path, "## Recent State\n" + rows)) == []

    def test_earlier_section_is_scanned_too(self, tmp_path):
        """A bullet re-added after being rolled to '## Earlier' is the same defect."""
        p = self._mem(tmp_path, (
            "## Recent State\n"
            "- the pager sweep (#285, #288)\n"
            "\n## Standing Gates\n"
            "- something else entirely\n"
            "\n## Earlier\n"
            "- rolled bullet covering #285 and #288\n"
        ))
        assert len(mn.scan_recent_state_duplicates(p)) == 1

    def test_bullets_outside_the_scanned_sections_are_ignored(self, tmp_path):
        p = self._mem(tmp_path, (
            "## Domain Files\n"
            "- the pager sweep (#285, #288)\n"
            "- duplicate of the pager sweep (#285, #288)\n"
        ))
        assert mn.scan_recent_state_duplicates(p) == []

    def test_issue_range_interiors_are_NOT_expanded(self, tmp_path):
        """Endpoints-only is deliberate — see the _DUP_ISSUE comment. Expanding
        `#285-#288` to its interior was built and reverted 2026-07-26: across 40
        committed MEMORY.md versions it gained zero true duplicates and produced a
        false positive, because hand-written ranges are approximations. A bullet
        citing only an INTERIOR id must not pair with the range."""
        p = self._mem(tmp_path, (
            "## Recent State\n"
            "- the sweep covering #285-#288 and also #301-#304\n"
            "- separate work on #286 and #302, different incident\n"
        ))
        assert mn.scan_recent_state_duplicates(p) == []

    def test_missing_file_returns_empty(self, tmp_path):
        assert mn.scan_recent_state_duplicates(tmp_path / "ghost.md") == []


class TestScanUngatedTrees:
    """scan_ungated_trees — Gap 58. cmd_health measured MEMORY.md/common.md, root
    *.md, domain/ and skills/ and nothing else, so project/ (123 files, 1.76MB) and
    troubleshooting/ (43, 577KB) — 81% of the corpus by bytes — could not go YELLOW
    or RED however large they grew. Gap 51's byte gate was built for one tree and
    never extended.

    The defining constraint is that this reports and does NOT rank: RED is not
    advisory here, .git/hooks/pre-commit greps the RED token and exit 1s, so
    ranking these trees on the principled 28KB/40KB pair would have blocked every
    commit in two repos on day one. The tests below pin BOTH halves — that the
    weight becomes visible, and that it can never reach the verdict path."""

    def _tree(self, tmp_path, tree, name, size):
        d = tmp_path / tree
        d.mkdir(exist_ok=True)
        (d / name).write_text("x" * size)

    def test_file_over_ceiling_is_reported(self, tmp_path):
        self._tree(tmp_path, "project", "fat.md", 40 * 1024)
        over, _ = mn.scan_ungated_trees(tmp_path)
        assert [r for r, _ in over] == ["project/fat.md"]

    def test_file_under_ceiling_is_silent(self, tmp_path):
        """Near-miss: one byte under must not fire, or the ceiling means nothing."""
        self._tree(tmp_path, "project", "lean.md", mn.UNGATED_ADVISORY_BYTES - 1)
        over, _ = mn.scan_ungated_trees(tmp_path)
        assert over == []

    def test_exactly_at_ceiling_fires(self, tmp_path):
        self._tree(tmp_path, "project", "edge.md", mn.UNGATED_ADVISORY_BYTES)
        over, _ = mn.scan_ungated_trees(tmp_path)
        assert len(over) == 1

    def test_troubleshooting_is_scanned_too(self, tmp_path):
        """The gap WAS 'fixed for one tree and never extended' — so a scanner that
        covers project/ alone reproduces the defect it exists to close."""
        self._tree(tmp_path, "troubleshooting", "fat.md", 40 * 1024)
        over, _ = mn.scan_ungated_trees(tmp_path)
        assert [r for r, _ in over] == ["troubleshooting/fat.md"]

    def test_gated_trees_are_NOT_double_reported(self, tmp_path):
        """domain/ and skills/ already have verdict rows. Reporting them again here
        would add noise to a banner whose whole value is naming the UNmeasured."""
        self._tree(tmp_path, "domain", "big.md", 40 * 1024)
        self._tree(tmp_path, "skills", "big.md", 40 * 1024)
        over, totals = mn.scan_ungated_trees(tmp_path)
        assert over == []
        assert totals == []

    def test_totals_count_every_file_not_just_the_heavy_ones(self, tmp_path):
        """The headline number is tree WEIGHT — the thing nobody could see."""
        self._tree(tmp_path, "project", "a.md", 100)
        self._tree(tmp_path, "project", "b.md", 40 * 1024)
        _, totals = mn.scan_ungated_trees(tmp_path)
        assert totals == [("project", 2, 100 + 40 * 1024)]

    def test_heaviest_first(self, tmp_path):
        self._tree(tmp_path, "project", "mid.md", 40 * 1024)
        self._tree(tmp_path, "project", "huge.md", 90 * 1024)
        self._tree(tmp_path, "troubleshooting", "small.md", 30 * 1024)
        over, _ = mn.scan_ungated_trees(tmp_path)
        assert [r for r, _ in over] == [
            "project/huge.md", "project/mid.md", "troubleshooting/small.md"]

    def test_absent_trees_do_not_crash(self, tmp_path):
        assert mn.scan_ungated_trees(tmp_path) == ([], [])

    def test_health_reports_but_never_ranks(self, tmp_path):
        """The commit-blocker guard, and the reason this ships reporting only:
        a 90KB project file must show up in the banner and must NOT produce a RED
        verdict — pre-commit greps that exact token and exit 1s."""
        (tmp_path / "MEMORY.md").write_text("# index\n")
        self._tree(tmp_path, "project", "huge.md", 90 * 1024)
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_health(tmp_path, [])
        out = buf.getvalue()
        assert "ungated file(s) over the 28KB advisory ceiling" in out
        assert "project/huge.md" in out
        assert "RED" not in out
        assert "All files within threshold" not in out

    def test_health_stays_clean_when_ungated_trees_are_lean(self, tmp_path):
        """The all-clean guard must remain reachable — a banner that can never go
        silent is one the next session learns to skim (the Gap 57 decay)."""
        (tmp_path / "MEMORY.md").write_text("# index\n")
        self._tree(tmp_path, "project", "lean.md", 500)
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_health(tmp_path, [])
        assert "All files within threshold" in buf.getvalue()


class TestScanSummaryQueue:
    """scan_summary_queue — Gap 63. The retire→review SUMMARY queue compounded
    invisibly because cmd_health never reported it, and surgeons skipped it by
    applying the facts-pending 'not a drain target' rule to the wrong queue.

    Same constraint as Gap 58: report, do NOT rank. RED is a commit blocker.
    Alert only when unreviewed-with-signal > 0 (the `gaius batch` filter);
    leftover no-signal `[-]` summaries must not make all-clean unreachable."""

    def _write(self, staging, name, *, reviewed=False, timestamp="2026-08-13T00:00:00Z",
               sections=None):
        staging.mkdir(exist_ok=True)
        body = {
            "uuid": name.replace(".json", ""),
            "reviewed": reviewed,
            "timestamp": timestamp,
            "sections": sections if sections is not None else {
                "key_concepts": "a real finding",
            },
        }
        (staging / name).write_text(json.dumps(body))

    def test_signal_unreviewed_is_counted(self, tmp_path):
        d = tmp_path / "staged"
        self._write(d, "a.json", timestamp="2026-08-10T12:00:00Z")
        got = mn.scan_summary_queue(d)
        assert got == {"unreviewed": 1, "with_signal": 1, "oldest": "2026-08-10"}

    def test_reviewed_with_signal_is_silent(self, tmp_path):
        """Near-miss: batch does not list reviewed summaries. Counting them
        would make the banner a lie about the queue a surgeon can still act on."""
        d = tmp_path / "staged"
        self._write(d, "done.json", reviewed=True)
        assert mn.scan_summary_queue(d) == {"unreviewed": 0, "with_signal": 0, "oldest": ""}

    def test_unreviewed_without_signal_is_not_an_alert(self, tmp_path):
        """The leftover no-signal `[-]` artifacts. They increment unreviewed
        but must not fire with_signal — otherwise all-clean is unreachable."""
        d = tmp_path / "staged"
        self._write(d, "noise.json", sections={"primary_request": "hi"})
        got = mn.scan_summary_queue(d)
        assert got["unreviewed"] == 1
        assert got["with_signal"] == 0
        assert got["oldest"] == "2026-08-13"

    def test_oldest_is_the_earliest_unreviewed(self, tmp_path):
        d = tmp_path / "staged"
        self._write(d, "new.json", timestamp="2026-08-14T00:00:00Z")
        self._write(d, "old.json", timestamp="2026-07-26T00:00:00Z")
        self._write(d, "done.json", reviewed=True, timestamp="2026-01-01T00:00:00Z")
        assert mn.scan_summary_queue(d)["oldest"] == "2026-07-26"

    def test_missing_dir_is_zeros_not_an_error(self, tmp_path):
        assert mn.scan_summary_queue(tmp_path / "ghost") == {
            "unreviewed": 0, "with_signal": 0, "oldest": ""}

    def test_unreadable_json_is_skipped(self, tmp_path):
        d = tmp_path / "staged"
        d.mkdir()
        (d / "broken.json").write_text("{not json")
        self._write(d, "ok.json")
        got = mn.scan_summary_queue(d)
        assert got["with_signal"] == 1
        assert got["unreviewed"] == 1

    def test_same_uuid_last_file_wins(self, tmp_path):
        """Rescans leave extra files. Counting files (not uuids) was the live
        597-vs-12 lie. Sorted glob + last write wins, matching load_staged."""
        d = tmp_path / "staged"
        self._write(d, "2026-08-01_deadbeef.json", timestamp="2026-08-01T00:00:00Z")
        body = json.loads((d / "2026-08-01_deadbeef.json").read_text())
        body["reviewed"] = True
        body["timestamp"] = "2026-08-14T00:00:00Z"
        (d / "2026-08-14_deadbeef.json").write_text(json.dumps(body))
        # force same uuid as the helper's name-derived uuid would differ
        for p in d.glob("*.json"):
            data = json.loads(p.read_text())
            data["uuid"] = "deadbeef"
            p.write_text(json.dumps(data))
        assert mn.scan_summary_queue(d) == {
            "unreviewed": 0, "with_signal": 0, "oldest": ""}

    def test_health_reports_but_never_ranks(self, tmp_path, monkeypatch):
        (tmp_path / "MEMORY.md").write_text("# index\n")
        d = tmp_path / "live-staged"
        self._write(d, "a.json", timestamp="2026-07-26T00:00:00Z")
        monkeypatch.setenv("GAIUS_STAGING_DIR", str(d))
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_health(tmp_path, [])
        out = buf.getvalue()
        assert "summary queue:" in out
        assert "1 unreviewed-with-signal" in out
        assert "oldest 2026-07-26" in out
        assert "RED" not in out
        assert "All files within threshold" not in out

    def test_health_stays_clean_when_queue_is_empty(self, tmp_path):
        """all-clean must remain reachable — a banner that can never go silent
        is one the next session learns to skim (Gap 57)."""
        (tmp_path / "MEMORY.md").write_text("# index\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_health(tmp_path, [])
        assert "All files within threshold" in buf.getvalue()
        assert "summary queue:" not in buf.getvalue()

    def test_signal_sections_match_gaius_extract(self):
        """Local copy must stay equal to gaius.extract.SIGNAL_SECTIONS.
        Importing extract from mnemosyne itself is forbidden (standalone)."""
        from gaius.extract import SIGNAL_SECTIONS
        assert set(mn.SUMMARY_SIGNAL_SECTIONS) == set(SIGNAL_SECTIONS)


class TestScanIndexCompleteness:
    """scan_index_completeness — Gap 55/59. Built for feedback/ in 2026-07-03 after
    64 rules (incl HARD gates) went invisible in the always-injected awareness layer,
    then never extended: `project/` drifted to 29 unlisted files before a session
    counted by hand, and `troubleshooting/` was never measured at all. Same shape as
    Gap 58 — 'fixed for one tree and never extended'.

    The bar is zero false POSITIVES, so the two index STYLES get two match modes:
    feedback/INDEX.md lists bare keys in prose, project/ and troubleshooting/ list
    relative markdown links. Each mode has a canary that must fire and a near-miss
    that must stay silent; applying either mode to the other tree's style is itself
    a test, because that mistake reports the whole tree missing on day one."""

    def _tree(self, tmp_path, tree, index_body, files, archived=()):
        d = tmp_path / tree
        d.mkdir(exist_ok=True)
        (d / "INDEX.md").write_text(index_body)
        for name in files:
            (d / name).write_text("# x\n")
        if archived:
            a = d / ".archive"
            a.mkdir(exist_ok=True)
            for name in archived:
                (a / name).write_text("# x\n")

    # ── link mode (project/, troubleshooting/) ───────────────────────────────
    def test_link_mode_unlisted_file_fires(self, tmp_path):
        self._tree(tmp_path, "project",
                   "- [a](project_a.md)\n", ["project_a.md", "project_b.md"])
        assert mn.scan_index_completeness(tmp_path) == [
            ("project", "project_b.md", "missing")]

    def test_link_mode_listed_file_is_silent(self, tmp_path):
        """Near-miss: the whole tree listed must produce nothing, or the scanner is
        an alarm that is always on — the Gap 57 decay by another route."""
        self._tree(tmp_path, "project",
                   "- [a](project_a.md) | [b](project_b.md)\n",
                   ["project_a.md", "project_b.md"])
        assert mn.scan_index_completeness(tmp_path) == []

    def test_link_mode_file_without_the_tree_prefix_is_still_checked(self, tmp_path):
        """Two live project files carry no `project_` prefix; the old
        `<prefix>_*.md` glob could not see them at all."""
        self._tree(tmp_path, "project", "- [a](project_a.md)\n",
                   ["project_a.md", "unprefixed.md"])
        assert mn.scan_index_completeness(tmp_path) == [
            ("project", "unprefixed.md", "missing")]

    def test_link_mode_suffix_substring_does_not_count_as_listed(self, tmp_path):
        """`foo.md` must not be scored listed because `xfoo.md` is — a bare
        substring test silently under-reports, which is the failure this scanner
        exists to catch."""
        self._tree(tmp_path, "project", "- [x](xfoo.md)\n", ["xfoo.md", "foo.md"])
        assert mn.scan_index_completeness(tmp_path) == [
            ("project", "foo.md", "missing")]

    def test_link_mode_emphasised_entry_is_listed(self, tmp_path):
        """`_name_` is markdown emphasis, not a longer identifier. A `\\b` left
        boundary treats the `_` as a word char and reports a listed file missing —
        a false positive, the one direction this scanner may never fail in."""
        self._tree(tmp_path, "project", "- _project_a.md_ — gloss\n", ["project_a.md"])
        assert mn.scan_index_completeness(tmp_path) == []

    def test_link_mode_anchored_link_still_counts_as_listed(self, tmp_path):
        self._tree(tmp_path, "project",
                   "- [a](project_a.md#section)\n", ["project_a.md"])
        assert mn.scan_index_completeness(tmp_path) == []

    def test_link_mode_dangling_target_is_a_ghost(self, tmp_path):
        """An archived or deleted file leaves a link that resolves to nothing —
        the reader follows it and gets silence, which is worse than absence."""
        self._tree(tmp_path, "project",
                   "- [a](project_a.md) | [gone](project_gone.md)\n", ["project_a.md"])
        assert mn.scan_index_completeness(tmp_path) == [
            ("project", "project_gone.md", "ghost")]

    def test_link_mode_dangling_ANCHORED_target_is_also_a_ghost(self, tmp_path):
        """The two halves must agree on what a link is. The missing-check counts
        `](x.md#sec)` as a listing (test above), so the ghost-check has to be able
        to call that same entry dangling once the target goes — otherwise an
        anchored link is the one form that can rot invisibly."""
        self._tree(tmp_path, "project",
                   "- [a](project_a.md) | [gone](project_gone.md#why)\n",
                   ["project_a.md"])
        assert mn.scan_index_completeness(tmp_path) == [
            ("project", "project_gone.md", "ghost")]

    def test_link_mode_ghost_ignores_code_fences_and_comments(self, tmp_path):
        """Link-shaped text quoted as an EXAMPLE names no real file. Both live
        indexes already put filenames in inline code, so scraping raw text reports
        a dangling link where nothing is linked at all."""
        body = ("- [a](project_a.md)\n\n"
                "```\n- [demo](project_example.md)\n```\n"
                "`](project_inline.md)`\n"
                "<!-- - [old](project_commented.md) -->\n")
        self._tree(tmp_path, "project", body, ["project_a.md"])
        assert mn.scan_index_completeness(tmp_path) == []

    def test_link_mode_duplicate_dangling_link_reported_once(self, tmp_path):
        """A hub link plus an inline mention is one dead file, not two — a doubled
        count inflates the banner and the weekly report."""
        self._tree(tmp_path, "project",
                   "- [a](project_a.md)\n- [gone](project_gone.md) see [gone](project_gone.md)\n",
                   ["project_a.md"])
        assert mn.scan_index_completeness(tmp_path) == [
            ("project", "project_gone.md", "ghost")]

    def test_link_mode_index_self_link_is_not_a_ghost(self, tmp_path):
        """`active` excludes INDEX.md, so an index that links to itself would
        otherwise be reported as its own dangling target."""
        self._tree(tmp_path, "project",
                   "- [top](INDEX.md) | [a](project_a.md)\n", ["project_a.md"])
        assert mn.scan_index_completeness(tmp_path) == []

    def test_link_mode_ignores_cross_tree_links(self, tmp_path):
        """`../domain/x.md` and `troubleshooting/y.md` are not this tree's business;
        flagging them would be a false positive on the first live run."""
        self._tree(tmp_path, "project",
                   "- [a](project_a.md) [d](../domain/x.md) [t](troubleshooting/y.md)\n",
                   ["project_a.md"])
        assert mn.scan_index_completeness(tmp_path) == []

    def test_index_never_reports_itself(self, tmp_path):
        self._tree(tmp_path, "project", "- [a](project_a.md)\n", ["project_a.md"])
        assert mn.scan_index_completeness(tmp_path) == []

    # ── key mode (feedback/) ─────────────────────────────────────────────────
    def test_key_mode_unlisted_rule_fires(self, tmp_path):
        self._tree(tmp_path, "feedback", "**HARD**: alpha\n",
                   ["feedback_alpha.md", "feedback_beta.md"])
        assert mn.scan_index_completeness(tmp_path) == [
            ("feedback", "beta", "missing")]

    def test_key_mode_prose_listing_is_silent(self, tmp_path):
        """feedback/INDEX.md lists bare keys, never links. Applying link mode here
        would report every rule missing on the first run."""
        self._tree(tmp_path, "feedback", "**HARD**: alpha, beta\n",
                   ["feedback_alpha.md", "feedback_beta.md"])
        assert mn.scan_index_completeness(tmp_path) == []

    def test_key_mode_unprefixed_rule_is_still_checked(self, tmp_path):
        """4 live feedback rules lack the `feedback_` prefix and the old glob
        skipped them — an awareness gate cannot have a blind spot."""
        self._tree(tmp_path, "feedback", "**HARD**: alpha\n",
                   ["feedback_alpha.md", "unprefixed-rule.md"])
        assert mn.scan_index_completeness(tmp_path) == [
            ("feedback", "unprefixed-rule", "missing")]

    def test_key_mode_rule_listed_by_full_filename_is_listed(self, tmp_path):
        """🔴 The regression that made `_LISTED_LEFT` necessary. `\\b` counts `_` as a
        word char, so the prefix the scanner just stripped becomes the character
        before the key and the match fails — a rule listed by its FULL filename (the
        more informative form, and the house style of the other two trees) was
        reported missing. At live scale that was 165 of 171 rules against an index
        naming every one of them."""
        self._tree(tmp_path, "feedback",
                   "- [no git stash](feedback_no_git_stash.md)\n",
                   ["feedback_no_git_stash.md"])
        assert mn.scan_index_completeness(tmp_path) == []

    def test_key_mode_longer_key_does_not_list_the_shorter_one(self, tmp_path):
        """The near-miss for the loosened boundary: `alert_fatigue_proactive` must
        not be read as listing `alert_fatigue`. Live feedback/ has such pairs."""
        self._tree(tmp_path, "feedback", "**HARD**: alert_fatigue_proactive\n",
                   ["feedback_alert_fatigue.md", "feedback_alert_fatigue_proactive.md"])
        assert mn.scan_index_completeness(tmp_path) == [
            ("feedback", "alert_fatigue", "missing")]

    def test_key_mode_retired_rule_still_listed_is_a_ghost(self, tmp_path):
        self._tree(tmp_path, "feedback", "**HARD**: alpha, retired\n",
                   ["feedback_alpha.md"], archived=["feedback_retired.md"])
        assert mn.scan_index_completeness(tmp_path) == [
            ("feedback", "retired", "ghost")]

    def test_key_mode_retired_rule_NOT_listed_is_silent(self, tmp_path):
        """The other half of the ghost conjunct: an archived rule the index
        correctly never mentions is a clean retirement, not drift."""
        self._tree(tmp_path, "feedback", "**HARD**: alpha\n",
                   ["feedback_alpha.md"], archived=["feedback_retired.md"])
        assert mn.scan_index_completeness(tmp_path) == []

    def test_key_mode_readopted_rule_is_not_a_ghost(self, tmp_path):
        """A rule present in BOTH the tree and .archive/ is live, not retired."""
        self._tree(tmp_path, "feedback", "**HARD**: alpha\n",
                   ["feedback_alpha.md"], archived=["feedback_alpha.md"])
        assert mn.scan_index_completeness(tmp_path) == []

    def test_key_mode_archived_index_is_not_a_ghost(self, tmp_path):
        """The archive glob widened to `*.md` too, so a stale INDEX.md sitting in
        `.archive/` yields key 'INDEX' — which word-matches an index's own prose
        essentially always, and would fire on every run forever."""
        self._tree(tmp_path, "feedback", "# INDEX\n\n**HARD**: alpha\n",
                   ["feedback_alpha.md"], archived=["INDEX.md"])
        assert mn.scan_index_completeness(tmp_path) == []

    # ── shape / wiring ───────────────────────────────────────────────────────
    def test_absent_trees_do_not_crash(self, tmp_path):
        assert mn.scan_index_completeness(tmp_path) == []

    def test_tree_without_an_index_is_skipped(self, tmp_path):
        """No INDEX.md means the tree opted out — inventing one file-per-line is
        not this scanner's call."""
        (tmp_path / "project").mkdir()
        (tmp_path / "project" / "project_a.md").write_text("# x\n")
        assert mn.scan_index_completeness(tmp_path) == []

    def test_unreadable_index_does_not_take_down_the_whole_health_run(self, tmp_path):
        """🔴 FAIL TOWARD SILENCE, NEVER SILENT-CLEAN. This scanner runs BEFORE every
        banner prints, so an exception here blanks the entire health output — and all
        three consumers read a blank run as healthy (pre-commit greps for RED and
        finds none; mnemos-audit runs `|| true` and greps an allowlist). One
        unreadable index must cost one scanner, not every gate in the process."""
        (tmp_path / "MEMORY.md").write_text("# index\n")
        d = tmp_path / "project"
        d.mkdir()
        (d / "project_a.md").write_text("# x\n")
        (d / "INDEX.md").mkdir()          # IsADirectoryError on read_text
        assert mn.scan_index_completeness(tmp_path) == []
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_health(tmp_path, [])
        assert "All files within threshold" in buf.getvalue()

    def test_every_indexed_tree_is_covered(self, tmp_path):
        """The Gap 58 lesson verbatim: a scanner that covers one tree reproduces the
        defect it exists to close. Pin the tree set so dropping one reds a test."""
        assert {t for t, _, _, _ in mn.INDEX_TREES} == {
            "feedback", "project", "troubleshooting"}

    def test_health_reports_each_tree_separately(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("# index\n")
        self._tree(tmp_path, "project", "- [a](project_a.md)\n",
                   ["project_a.md", "project_b.md"])
        self._tree(tmp_path, "troubleshooting", "- [a](t_a.md)\n",
                   ["t_a.md", "t_b.md"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_health(tmp_path, [])
        out = buf.getvalue()
        assert "INDEX drift in 2 tree(s): 2 file(s) unlisted" in out
        assert "project/ missing: project_b.md" in out
        assert "troubleshooting/ missing: t_b.md" in out
        assert "All files within threshold" not in out
        # 🔴 CONTRACT CHANGED 2026-08-23 (mnemos #253, J-authorised): bijection drift is
        # BLOCKING, not advisory. The old assertion here was `"RED" not in out`.
        assert "RED" in out              # blocking — the pre-commit hook must see it
        assert "BLOCKING, reconcile before commit" in out

    @staticmethod
    def _red_paths(out):
        """Reproduce the pre-commit hook's parse EXACTLY (.git/hooks/pre-commit:39-51):
        ANSI-strip, then take field 1 of any line whose LAST field is exactly `RED`."""
        import re as _re
        plain = _re.sub(r"\x1b\[[0-9;]*m", "", out)
        return sorted({ln.split()[0] for ln in plain.splitlines()
                       if ln.split() and ln.split()[-1] == "RED"})

    def test_drift_red_rows_are_parseable_paths_that_exist(self, tmp_path):
        """The two failure modes that make this gate worse than useless.

        (1) FAIL-CLOSED DETONATION: the hook blocks EVERY commit on a RED entry whose
            field 1 is not `*.md`, so a bare key or a `tree/` token wedges the repo.
        (2) SILENT NO-OP: a path that IS *.md but does not exist on disk (the
            `project_orphan.md.md` double-extension bug) matches no staged file and
            therefore never blocks -- a control that looks armed and is not.
        Both are invisible to a `"RED" in out` assertion, which is why this exists."""
        (tmp_path / "MEMORY.md").write_text("# index\n")
        # key mode (stem minus prefix) and link mode (full filename) carry DIFFERENT
        # keys -- cover both, plus a ghost, or the mode bug reappears unnoticed.
        self._tree(tmp_path, "feedback", "- [k](feedback_k.md)\n",
                   ["feedback_k.md", "feedback_unlisted.md"])
        self._tree(tmp_path, "project", "- [a](project_a.md)\n",
                   ["project_a.md", "project_b.md"])
        self._tree(tmp_path, "troubleshooting", "- [gone](t_ghost.md)\n", ["t_a.md"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_health(tmp_path, [])
        reds = self._red_paths(buf.getvalue())
        assert reds, "bijection drift emitted no blocking RED row"
        for rel in reds:
            assert rel.endswith(".md"), f"fail-closed detonation: {rel!r} is not *.md"
            assert "…" in rel or (tmp_path / rel).is_file(), \
                f"silent no-op: {rel!r} matches no file on disk, so it can never block"
        assert "feedback/feedback_unlisted.md" in reds   # key mode
        assert "project/project_b.md" in reds            # link mode
        assert "troubleshooting/INDEX.md" in reds        # ghost -> the index itself

    def test_health_stays_clean_when_indexes_are_bijections(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("# index\n")
        self._tree(tmp_path, "project", "- [a](project_a.md)\n", ["project_a.md"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_health(tmp_path, [])
        assert "All files within threshold" in buf.getvalue()

    def test_drift_pat_still_matches_the_banner(self, tmp_path):
        """mnemos-audit's DRIFT_PAT is an ALLOWLIST that fails SILENT-CLEAN. The
        banner text changed from 'feedback/INDEX drift' to a per-tree form; the
        'INDEX drift' fragment must survive or the weekly cron goes blind (the exact
        12-day outage Gap 57 was opened for). hooks/test-install-drift.sh enumerates
        this generally — this pins it at the unit level too, since the two travel
        in different suites."""
        (tmp_path / "MEMORY.md").write_text("# index\n")
        self._tree(tmp_path, "project", "- [a](project_a.md)\n",
                   ["project_a.md", "project_b.md"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_health(tmp_path, [])
        out = buf.getvalue()
        assert "INDEX drift" in out
        assert "missing:" in out         # mnemos-audit greps this to build the report

    def test_ghost_label_survives_for_the_report_grep_too(self, tmp_path):
        """`ghost:` is the other literal mnemos-audit greps. It sits in a nested
        print, so hooks/test-install-drift.sh's AST enumeration (top-level ⚡/⚠
        prints only) cannot see it — rename it and every gate stays green while the
        weekly report silently drops dangling-link detail."""
        (tmp_path / "MEMORY.md").write_text("# index\n")
        self._tree(tmp_path, "project",
                   "- [a](project_a.md) | [gone](project_gone.md)\n", ["project_a.md"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_health(tmp_path, [])
        out = buf.getvalue()
        assert "ghost:" in out
        assert "project_gone.md" in out


class TestScanOverviewHeader:
    """Gap 64 (2026-08-23, mnemos #253): `specs/` is fed to NO content-defect detector,
    which let specs/system-overview.md's `**Last updated**:` field chain to 76 KB inside
    a 307 KB file before a human noticed. A blanket specs/ add was measured WRONG (0
    blocking + 91 advisory findings: gap-table rows are 2.2-13.6 KB by design and the
    changelog legitimately holds a 75,055-char line), so this scans ONE file, ONE field."""

    @staticmethod
    def _ov(root, header_body):
        (root / "specs").mkdir(exist_ok=True)
        (root / "specs" / "system-overview.md").write_text(
            "# Map\n\n**Last updated**: " + header_body + "\n\nbody\n")

    def test_fires_over_threshold(self, tmp_path):
        self._ov(tmp_path, "x" * 25000)
        hits = mn.scan_overview_header(tmp_path)
        assert hits and hits[0][0] == 3 and hits[0][1] > 25000

    def test_near_miss_stays_silent(self, tmp_path):
        """Just under the bar must pass, or the gate is a nag rather than a trigger."""
        self._ov(tmp_path, "x" * (mn.OVERVIEW_HEADER_WARN - 100))
        assert mn.scan_overview_header(tmp_path) == []

    def test_weighs_bytes_not_characters(self, tmp_path):
        """Gap 62's lesson, re-armed: the header is emoji/em-dash dense, so a char-count
        implementation understates it. 7000 x U+1F4CC is 7018 chars but 28018 bytes --
        this test passes byte-for-byte and FAILS under `len(line)`."""
        self._ov(tmp_path, "\U0001F4CC" * 7000)
        hits = mn.scan_overview_header(tmp_path)
        assert hits and hits[0][1] > 20 * 1024

    def test_missing_file_fails_toward_silence(self, tmp_path):
        assert mn.scan_overview_header(tmp_path) == []

    def test_absent_header_field_fails_toward_silence(self, tmp_path):
        (tmp_path / "specs").mkdir()
        (tmp_path / "specs" / "system-overview.md").write_text("# Map\n\nno field\n")
        assert mn.scan_overview_header(tmp_path) == []

    def test_never_emits_the_red_token(self, tmp_path):
        """ADVISORY by construction. The header is rolled by hand and self-clears, so a
        hard block would wedge the nightly -- unlike bijection drift, which is caused by
        the very commit that can fix it."""
        (tmp_path / "MEMORY.md").write_text("# index\n")
        self._ov(tmp_path, "x" * 25000)
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_health(tmp_path, [])
        out = buf.getvalue()
        assert "Last updated" in out and "Gap 64" not in out.split("Last updated")[0]
        import re as _re
        plain = _re.sub(r"\x1b\[[0-9;]*m", "", out)
        assert not [ln for ln in plain.splitlines()
                    if ln.split() and ln.split()[-1] == "RED"]

    def test_clean_header_keeps_health_all_clean(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("# index\n")
        self._ov(tmp_path, "2026-08-23 (compact entry)")
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_health(tmp_path, [])
        assert "All files within threshold" in buf.getvalue()


class TestPorcelainContract:
    """`health --porcelain` — Gap 65. The pre-commit hook used to screen-scrape the
    HUMAN table: ANSI-strip, then `awk '$NF=="RED"{print $1}'`, trusting positional
    field 1 as a path. Under that parse every table row that ends in the RED token is
    a path CLAIM, and the drift rows had to be forged table-shaped to be seen at all.

    The machine contract replaces the scrape:
      stdout : zero or more `RED\\t<relpath>\\t<reason>` lines, NOTHING else.
      exit   : 0 clean / 1 blocking / 2 internal error (consumers fail closed on >=2).
    Human mode is untouched and still exits 0 in every case — unknown consumers run it
    under `set -e`, and it has never returned anything but 0.

    🔴 The canary that matters is `test_both_modes_name_the_same_paths`: two renderers
    over one finding set desync silently — the human run stays green while the machine
    gate emits a row matching no staged file, or blocks on a row no human ever saw."""

    @staticmethod
    def _tree(root, tree, index_body, files):
        d = root / tree
        d.mkdir(exist_ok=True)
        (d / "INDEX.md").write_text(index_body)
        for name in files:
            (d / name).write_text("# x\n")

    @staticmethod
    def _red_paths(out):
        """Reproduce the legacy pre-commit parse EXACTLY — the thing porcelain
        replaces. Kept here so the two renderers are compared through the consumer's
        own eyes, not through an assertion written to agree with itself."""
        import re as _re
        plain = _re.sub(r"\x1b\[[0-9;]*m", "", out)
        return sorted({ln.split()[0] for ln in plain.splitlines()
                       if ln.split() and ln.split()[-1] == "RED"})

    def _clean(self, root):
        (root / "MEMORY.md").write_text("# index\n")
        self._tree(root, "project", "- [a](project_a.md)\n", ["project_a.md"])

    def _drifted(self, root):
        """Both blocking classes at once, in every shape they come in: key mode
        (feedback), link mode (project), a ghost, and the MEMORY.md byte-RED.
        MEMORY.md is sized from MEMORY_BYTE_ERR at 150 B/line so it crosses the byte
        ceiling while staying GREEN on lines — isolating the byte finding from the
        line one, which is NOT blocking and must not leak into the comparison."""
        n = (mn.MEMORY_BYTE_ERR // 150) + 2          # ~165 lines, < the 180 warn
        (root / "MEMORY.md").write_text(("z" * 149 + "\n") * n)
        self._tree(root, "feedback", "- [k](feedback_k.md)\n",
                   ["feedback_k.md", "feedback_unlisted.md"])
        self._tree(root, "project", "- [a](project_a.md) | [gone](project_gone.md)\n",
                   ["project_a.md", "project_b.md"])
        self._tree(root, "troubleshooting", "- [gone](t_ghost.md)\n", ["t_a.md"])

    @staticmethod
    def _lines(out):
        return [ln for ln in out.splitlines() if ln]

    def _run_main(self, monkeypatch, root, argv):
        """Exercise the real dispatch — the exit STATUS is a main() property, and
        `cmd_health` returning None is only half the contract."""
        monkeypatch.setattr(sys, "argv",
                            ["mnemosyne", "--memory-dir", str(root)] + argv)
        with pytest.raises(SystemExit) as excinfo:
            mn.main()
        return excinfo.value.code

    # ── the contract ─────────────────────────────────────────────────────────
    def test_clean_tree_is_silent_and_exits_zero(self, tmp_path, capsys):
        self._clean(tmp_path)
        rc = mn.cmd_health(tmp_path, ["--porcelain"])
        assert rc == 0
        assert capsys.readouterr().out == ""

    def test_drift_emits_one_row_per_finding_and_exits_one(self, tmp_path, capsys):
        self._drifted(tmp_path)
        rc = mn.cmd_health(tmp_path, ["--porcelain"])
        out = capsys.readouterr().out
        assert rc == 1
        expected = len(mn.scan_index_completeness(tmp_path)) + 1   # + memory-byte-err
        assert len(self._lines(out)) == expected

    def test_every_row_is_three_tab_separated_fields(self, tmp_path, capsys):
        self._drifted(tmp_path)
        mn.cmd_health(tmp_path, ["--porcelain"])
        out = capsys.readouterr().out
        for ln in self._lines(out):
            fields = ln.split("\t")
            assert len(fields) == 3, f"not 3 tab-separated fields: {ln!r}"
            assert fields[0] == "RED"
            assert fields[2] in mn.BLOCK_REASONS, f"unknown reason slug: {ln!r}"

    def test_every_emitted_path_is_md_and_exists_on_disk(self, tmp_path, capsys):
        """The two ways this gate becomes worse than useless: a non-`.md` field fails
        the consumer CLOSED (wedging every commit in the repo), and an `.md` path that
        exists nowhere matches no staged file (a control that looks armed and is not
        — the `project_orphan.md.md` double-extension bug)."""
        self._drifted(tmp_path)
        mn.cmd_health(tmp_path, ["--porcelain"])
        rows = [ln.split("\t")[1] for ln in self._lines(capsys.readouterr().out)]
        assert rows
        for rel in rows:
            assert rel.endswith(".md"), f"fail-closed detonation: {rel!r} is not *.md"
            assert (tmp_path / rel).is_file(), \
                f"silent no-op: {rel!r} matches no file on disk, so it can never block"

    def test_no_ansi_bytes_anywhere_in_porcelain_stdout(self, tmp_path, capsys):
        """A colour code inside field 1 makes the path un-openable, and the reason
        slug unmatchable. The human table shares these constants, so an ANSI leak is
        one careless f-string away."""
        self._drifted(tmp_path)
        mn.cmd_health(tmp_path, ["--porcelain"])
        assert "\x1b" not in capsys.readouterr().out

    def test_porcelain_prints_no_table_or_banner(self, tmp_path, capsys):
        """`NOTHING else on stdout` — an advisory banner leaking here is parsed as a
        finding by anything doing `wc -l`."""
        self._drifted(tmp_path)
        mn.cmd_health(tmp_path, ["--porcelain"])
        out = capsys.readouterr().out
        for token in ("Threshold", "within threshold", "INDEX drift", "⚡", "⚠"):
            assert token not in out

    def test_memory_byte_red_is_reported_as_its_own_reason(self, tmp_path, capsys):
        """Blocking is TWO classes, not one. A porcelain that only knew about drift
        would silently drop the byte gate the moment the hook stopped scraping."""
        n = (mn.MEMORY_BYTE_ERR // 150) + 2
        (tmp_path / "MEMORY.md").write_text(("z" * 149 + "\n") * n)
        rc = mn.cmd_health(tmp_path, ["--porcelain"])
        assert rc == 1
        assert self._lines(capsys.readouterr().out) == [
            f"RED\tMEMORY.md\t{mn.BLOCK_MEMORY_BYTE_ERR}"]

    # ── human mode is untouched ──────────────────────────────────────────────
    def test_human_mode_still_prints_the_red_table_rows(self, tmp_path, capsys):
        self._drifted(tmp_path)
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "\033[31m\033[1mRED\033[0m" in out
        assert "INDEX drift" in out and "BLOCKING, reconcile before commit" in out
        assert self._red_paths(out)

    def test_human_mode_exits_zero_even_on_blocking_findings(self, tmp_path,
                                                             monkeypatch, capsys):
        """Unchanged on purpose: today's rc is 0 in all cases and unknown consumers
        run `mnemosyne health` under `set -e`. Porcelain is where the status lives."""
        self._drifted(tmp_path)
        assert self._run_main(monkeypatch, tmp_path, ["health"]) == 0
        capsys.readouterr()

    def test_main_exits_one_on_porcelain_drift_and_zero_when_clean(self, tmp_path,
                                                                   monkeypatch, capsys):
        self._drifted(tmp_path)
        assert self._run_main(monkeypatch, tmp_path, ["health", "--porcelain"]) == 1
        capsys.readouterr()
        clean = tmp_path / "clean"
        clean.mkdir()
        self._clean(clean)
        assert self._run_main(monkeypatch, clean, ["health", "--porcelain"]) == 0
        assert capsys.readouterr().out == ""

    # ── the desync canary ────────────────────────────────────────────────────
    def test_both_modes_name_the_same_paths(self, tmp_path, capsys):
        """The reason `blocking_findings` exists. Two renderers over one finding set
        drift apart silently — this compares the machine rows against the consumer's
        own parse of the human table, so a second copy of the row construction reds
        here before it ships."""
        self._drifted(tmp_path)
        mn.cmd_health(tmp_path, ["--porcelain"])
        machine = sorted({ln.split("\t")[1]
                          for ln in self._lines(capsys.readouterr().out)})
        mn.cmd_health(tmp_path, [])
        human = self._red_paths(capsys.readouterr().out)
        assert machine == human
        assert machine, "both modes reported nothing on a deliberately drifted tree"

    # ── failure direction ────────────────────────────────────────────────────
    def test_internal_error_exits_two_never_zero(self, tmp_path, monkeypatch, capsys):
        """🔴 A consumer reads 0 as clean and commits. An unexpected exception must
        land on 2 (fail closed), not on a silent green — the exact failure the
        scrape had (a crashed run prints no RED, so the hook passed it)."""
        def _boom(*a, **kw):
            raise RuntimeError("scanner exploded")
        monkeypatch.setattr(mn, "scan_index_completeness", _boom)
        self._drifted(tmp_path)
        rc = mn.cmd_health(tmp_path, ["--porcelain"])
        captured = capsys.readouterr()
        assert rc == 2
        assert captured.out == ""
        assert "scanner exploded" in captured.err

    def test_unbuildable_row_is_dropped_not_emitted(self, tmp_path):
        """Fail toward silence, never toward detonation — applied in BOTH modes
        because the guard lives in the shared builder."""
        self._clean(tmp_path)
        assert mn.blocking_findings(tmp_path, [
            ("project", "does_not_exist.md", "missing"),   # *.md, but no such file
            ("project", "a … b", "missing"),               # unparseable
        ]) == []

    def test_reason_slugs_are_pinned(self, tmp_path):
        """Slugs are the contract; renaming one silently reclassifies the finding for
        every consumer. Add a slug, never rename one."""
        assert mn.BLOCK_INDEX_BIJECTION == "index-bijection"
        assert mn.BLOCK_MEMORY_BYTE_ERR == "memory-byte-err"
        assert mn.BLOCK_FILE_OVER_LIMIT == "file-over-limit"
        assert set(mn.BLOCK_REASONS) == {
            "index-bijection", "memory-byte-err", "file-over-limit"}

    def test_porcelain_matches_the_scrape_it_replaced(self, tmp_path):
        """🔴 REGRESSION GUARD (mnemos #253). Porcelain shipped covering only
        index-bijection + memory-byte-err, but the hook it replaced screen-scraped EVERY
        table row ending in the RED token -- so an oversized file DID block a commit and
        would silently have stopped blocking. A replacement gate must never be weaker
        than the one it replaces. The fixture MUST contain an over-limit file: the
        original desync canary passed only because its fixture had none."""
        (tmp_path / "MEMORY.md").write_text("# index\n")
        (tmp_path / "common.md").write_text("# common\n")
        (tmp_path / "domain").mkdir()
        (tmp_path / "domain" / "big.md").write_text("line\n" * 900)   # past domain err
        for tree in ("feedback", "project", "troubleshooting"):
            (tmp_path / tree).mkdir()
            (tmp_path / tree / "INDEX.md").write_text("# idx\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            mn.cmd_health(tmp_path, [])
        legacy = self._red_paths_from_human(buf.getvalue())
        porcelain = {r for r, _ in mn.blocking_findings(tmp_path)}
        assert "domain/big.md" in porcelain, "over-limit file stopped blocking"
        assert legacy == porcelain, f"gate weakened: scrape={legacy} porcelain={porcelain}"

    @staticmethod
    def _red_paths_from_human(out):
        """The pre-porcelain hook parse, kept as the parity oracle."""
        import re as _re
        plain = _re.sub(r"\x1b\[[0-9;]*m", "", out)
        return {ln.split()[0] for ln in plain.splitlines()
                if ln.split() and ln.split()[-1] == "RED"}
