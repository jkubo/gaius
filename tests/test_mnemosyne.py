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
