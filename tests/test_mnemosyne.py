"""mnemosyne test suite — memory file health monitor."""
import io
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
    """MEMORY.md injection-budget check (16KB warn / 20KB error). Regressed once
    when an installed-only copy was overwritten by source — now tested so it can't
    silently vanish again. Bodies use <180 short lines to isolate bytes from the
    line-count and runaway-line checks."""

    def _write(self, d, total_bytes, line_len=100):
        line = "z" * (line_len - 1) + "\n"
        n = total_bytes // line_len
        (d / "MEMORY.md").write_text(line * n)

    def test_over_16kb_is_yellow_advisory(self, tmp_path, capsys):
        self._write(tmp_path, 17000)                 # 170 lines, GREEN on lines
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "YELLOW" in out and "injection-budget" in out
        assert "within threshold" not in out

    def test_over_20kb_emits_blocking_red_marker(self, tmp_path, capsys):
        self._write(tmp_path, 22100, line_len=130)   # 170 lines, RED on bytes only
        mn.cmd_health(tmp_path, [])
        out = capsys.readouterr().out
        assert "\033[31m\033[1mRED\033[0m" in out     # pre-commit hook greps this -> blocks

    def test_under_16kb_clean(self, tmp_path, capsys):
        self._write(tmp_path, 5100, line_len=51)     # 100 lines, all GREEN
        mn.cmd_health(tmp_path, [])
        assert "within threshold" in capsys.readouterr().out


class TestIndexGlossAccretion:
    """scan_index_gloss — Gap-32 structural cure. Flags MEMORY.md '## Project
    Files' index lines carrying accreted prose, measured link-count-agnostically
    by stripping [label](path) tokens. A vertical with MANY terse links must pass;
    one whose links carry paragraph glosses must flag. (This is the failure the
    2000-char runaway check + total-byte budget both miss.)"""

    TERSE = "- **JDT**: " + " | ".join(f"[f{i}](project/p{i}.md)" for i in range(22))
    ACCRETED = ("- **JDT**: [master](project/p.md) — "
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


class TestHeaviestLines:
    def test_returns_longest_first(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("short\n" + "m" * 200 + "\n" + "l" * 500 + "\n")
        rows = mn.heaviest_lines(p, n=2)
        assert len(rows) == 2
        assert rows[0][1] == 500 and rows[1][1] == 200
